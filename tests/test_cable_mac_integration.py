"""Verify the complete three-MAC route at physical Ethernet byte streams."""

import re
import shutil
import subprocess
import tempfile
import zlib
from pathlib import Path

import pytest
from migen import ClockDomain, ClockDomainsRenamer, Memory, Module, Signal
from migen.fhdl import verilog
from migen.sim import passive, run_simulation
from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description
from liteeth.mac.core import LiteEthMACCore

from sfp_loopback.cable import CableLoop


REQUEST = bytes.fromhex(
    "020000000002 102030405060 88b5 4c584c50 "
    "0102030405060708 00000001"
) + bytes(range(30))
REPLY = bytes.fromhex(
    "102030405060 020000000002 88b5 4c584c50 "
    "0102030405060708 00000001"
) + bytes(range(30))


def wire_frame(body):
    return bytes.fromhex("55555555555555d5") + body + zlib.crc32(body).to_bytes(4, "little")


class PhysicalStream(Module):
    dw = 8
    rx_clk_freq = tx_clk_freq = 125e6

    def __init__(self):
        self.source = stream.Endpoint(eth_phy_description(8))
        self.sink = stream.Endpoint(eth_phy_description(8))
        self.comb += self.sink.ready.eq(1)


class CableMACs(Module):
    def __init__(self):
        self.clock_domains.cd_sys = ClockDomain("sys")
        for name in ("host_rx", "host_tx", "sfp0_rx", "sfp0_tx", "sfp1_rx", "sfp1_tx"):
            setattr(self.clock_domains, "cd_" + name, ClockDomain(name))

        self.submodules.route = CableLoop(transaction_timeout_cycles=100_000)
        self.phys = []
        self.macs = []
        for name in ("host", "sfp0", "sfp1"):
            phy = PhysicalStream()
            mac = ClockDomainsRenamer({"eth_tx": name + "_tx", "eth_rx": name + "_rx"})(
                LiteEthMACCore(phy, dw=8, with_sys_datapath=True,
                               with_store_and_forward=True, tx_cdc_depth=64, rx_cdc_depth=64))
            setattr(self.submodules, name + "_phy", phy)
            setattr(self.submodules, name + "_mac", mac)
            self.phys.append(phy)
            self.macs.append(mac)
            self.comb += [mac.source.connect(getattr(self.route, name + "_sink")),
                          getattr(self.route, name + "_source").connect(mac.sink)]


def _simulate_route_python(body, direction, *, cable_connected=True, corrupt_return=False):
    dut = CableMACs()
    transmitted = [[], [], []]
    counts = {}
    sender, receiver = (1, 2) if direction == 0 else (2, 1)

    def emit(endpoint, frame):
        for index, byte in enumerate(frame):
            yield endpoint.valid.eq(1)
            yield endpoint.data.eq(byte)
            yield endpoint.be.eq(1)
            yield endpoint.first.eq(index == 0)
            yield endpoint.last.eq(index == len(frame) - 1)
            yield endpoint.error.eq(0)
            yield
            assert (yield endpoint.ready), "Physical Ethernet cannot pause mid-frame"
        yield endpoint.valid.eq(0)
        yield endpoint.last.eq(0)

    def host():
        yield dut.route.direction.eq(direction)
        yield dut.route.enable.eq(0)
        for _ in range(10):
            yield
        yield dut.route.enable.eq(1)
        for _ in range(100):
            yield
        yield from emit(dut.phys[0].source, wire_frame(body))
        for _ in range(len(body) * 16 + 1200):
            if transmitted[0]:
                break
            yield
        for _ in range(100):
            yield
        counts["host_tx"] = (yield dut.route.host_tx_frames)
        counts["sfp_tx"] = (yield getattr(dut.route, f"sfp{sender - 1}_tx_frames"))
        counts["return_rx"] = (yield getattr(dut.route, f"sfp{receiver - 1}_return_rx_frames"))
        counts["return_crc_errors"] = (yield dut.macs[receiver].rx_datapath.crc_errors.status)

    @passive
    def cable():
        while not transmitted[sender]:
            yield
        if cable_connected:
            frame = bytearray(transmitted[sender][0])
            if corrupt_return:
                frame[-1] ^= 1
            yield from emit(dut.phys[receiver].source, bytes(frame))
        while True:
            yield

    @passive
    def collect(index):
        packet = bytearray()
        while True:
            endpoint = dut.phys[index].sink
            if (yield endpoint.valid):
                packet.append((yield endpoint.data))
                if (yield endpoint.last):
                    transmitted[index].append(bytes(packet))
                    packet.clear()
            elif packet:
                pytest.fail("MAC interrupted its physical Ethernet frame")
            yield

    fragment = dut.get_fragment()
    for special in fragment.specials:
        if isinstance(special, Memory):
            for port in special.ports:
                if port.dat_r is None:
                    port.dat_r = Signal(special.width)
    clocks = {"sys": 20}
    generators = {"host_rx": host(), f"sfp{receiver - 1}_rx": cable()}
    for index, name in enumerate(("host", "sfp0", "sfp1")):
        clocks[name + "_rx"] = 8
        clocks[name + "_tx"] = 8
        generators[name + "_tx"] = collect(index)
    run_simulation(fragment, generators, clocks=clocks)
    return transmitted, counts


def _compiled_ports(dut, fragment):
    ports = []

    def expose(signal, name, direction):
        signal.name_override = name
        ports.append((signal, name, direction, len(signal)))

    domains = {domain.name: domain for domain in fragment.clock_domains}
    for name in ("sys", "host_rx", "host_tx", "sfp0_rx", "sfp0_tx", "sfp1_rx", "sfp1_tx"):
        domain = domains[name]
        expose(domain.clk, name + "_clk", "input")
        expose(domain.rst, name + "_rst", "input")

    for name, phy in zip(("host", "sfp0", "sfp1"), dut.phys):
        rx = phy.source
        for field in ("valid", "first", "last", "data", "be", "error"):
            expose(getattr(rx, field), f"{name}_rx_{field}", "input")
        expose(rx.ready, name + "_rx_ready", "output")

        tx = phy.sink
        for field in ("valid", "first", "last", "data", "be", "error"):
            expose(getattr(tx, field), f"{name}_tx_{field}", "output")

    expose(dut.route.enable, "route_enable", "input")
    expose(dut.route.direction, "route_direction", "input")
    for name, signal in (
        ("host_tx_frames", dut.route.host_tx_frames),
        ("sfp0_tx_frames", dut.route.sfp0_tx_frames),
        ("sfp1_tx_frames", dut.route.sfp1_tx_frames),
        ("sfp0_return_rx_frames", dut.route.sfp0_return_rx_frames),
        ("sfp1_return_rx_frames", dut.route.sfp1_return_rx_frames),
        ("sfp0_rx_crc_errors", dut.macs[1].rx_datapath.crc_errors.status),
        ("sfp1_rx_crc_errors", dut.macs[2].rx_datapath.crc_errors.status),
    ):
        expose(signal, name, "output")
    return ports


def _compiled_testbench(ports):
    declarations = []
    connections = []
    for _, name, direction, width in ports:
        datatype = "reg" if direction == "input" else "wire"
        packed = "" if width == 1 else f" [{width - 1}:0]"
        declarations.append(f"    {datatype}{packed} {name};")
        connections.append(f"        .{name}({name})")

    return """`timescale 1ns/1ps
module tb;
""" + "\n".join(declarations) + """

    reg [7:0] request_bytes [0:2047];
    reg [7:0] host_tx_buffer [0:2047];
    reg [7:0] sfp0_tx_buffer [0:2047];
    reg [7:0] sfp1_tx_buffer [0:2047];
    integer host_tx_ends [0:7];
    integer sfp0_tx_ends [0:7];
    integer sfp1_tx_ends [0:7];
    integer host_tx_length = 0;
    integer sfp0_tx_length = 0;
    integer sfp1_tx_length = 0;
    integer host_tx_frame_count = 0;
    integer sfp0_tx_frame_count = 0;
    integer sfp1_tx_frame_count = 0;
    integer host_tx_in_frame = 0;
    integer sfp0_tx_in_frame = 0;
    integer sfp1_tx_in_frame = 0;
    integer direction;
    integer connected;
    integer corrupt;
    integer request_length;
    integer i;
    integer j;
    integer sender;
    integer receiver;
    integer sender_length;
    string request_file;

    cable_macs dut (
""" + ",\n".join(connections) + """
    );

    initial begin forever #10 sys_clk = ~sys_clk; end
    initial begin forever #4 host_rx_clk = ~host_rx_clk; end
    initial begin #1; forever #4 host_tx_clk = ~host_tx_clk; end
    initial begin #2; forever #4 sfp0_rx_clk = ~sfp0_rx_clk; end
    initial begin #3; forever #4 sfp0_tx_clk = ~sfp0_tx_clk; end
    initial begin #4; forever #4 sfp1_rx_clk = ~sfp1_rx_clk; end
    initial begin #5; forever #4 sfp1_tx_clk = ~sfp1_tx_clk; end

    always @(posedge host_tx_clk) begin
        if (host_tx_rst) begin
            host_tx_length = 0;
            host_tx_frame_count = 0;
            host_tx_in_frame = 0;
        end else if (host_tx_valid) begin
            if (host_tx_length >= 2048) $fatal(1, "host TX frame exceeded buffer");
            host_tx_buffer[host_tx_length] = host_tx_data;
            host_tx_length = host_tx_length + 1;
            if (host_tx_last) begin
                host_tx_ends[host_tx_frame_count] = host_tx_length;
                host_tx_frame_count = host_tx_frame_count + 1;
                host_tx_in_frame = 0;
            end else begin
                host_tx_in_frame = 1;
            end
        end else if (host_tx_in_frame) begin
            $fatal(1, "host MAC interrupted its physical Ethernet frame");
        end
    end

    always @(posedge sfp0_tx_clk) begin
        if (sfp0_tx_rst) begin
            sfp0_tx_length = 0;
            sfp0_tx_frame_count = 0;
            sfp0_tx_in_frame = 0;
        end else if (sfp0_tx_valid) begin
            if (sfp0_tx_length >= 2048) $fatal(1, "SFP0 TX frame exceeded buffer");
            sfp0_tx_buffer[sfp0_tx_length] = sfp0_tx_data;
            sfp0_tx_length = sfp0_tx_length + 1;
            if (sfp0_tx_last) begin
                sfp0_tx_ends[sfp0_tx_frame_count] = sfp0_tx_length;
                sfp0_tx_frame_count = sfp0_tx_frame_count + 1;
                sfp0_tx_in_frame = 0;
            end else begin
                sfp0_tx_in_frame = 1;
            end
        end else if (sfp0_tx_in_frame) begin
            $fatal(1, "SFP0 MAC interrupted its physical Ethernet frame");
        end
    end

    always @(posedge sfp1_tx_clk) begin
        if (sfp1_tx_rst) begin
            sfp1_tx_length = 0;
            sfp1_tx_frame_count = 0;
            sfp1_tx_in_frame = 0;
        end else if (sfp1_tx_valid) begin
            if (sfp1_tx_length >= 2048) $fatal(1, "SFP1 TX frame exceeded buffer");
            sfp1_tx_buffer[sfp1_tx_length] = sfp1_tx_data;
            sfp1_tx_length = sfp1_tx_length + 1;
            if (sfp1_tx_last) begin
                sfp1_tx_ends[sfp1_tx_frame_count] = sfp1_tx_length;
                sfp1_tx_frame_count = sfp1_tx_frame_count + 1;
                sfp1_tx_in_frame = 0;
            end else begin
                sfp1_tx_in_frame = 1;
            end
        end else if (sfp1_tx_in_frame) begin
            $fatal(1, "SFP1 MAC interrupted its physical Ethernet frame");
        end
    end

    task automatic send_beat(input integer port, input integer index,
                             input [7:0] value, input integer total);
    begin
        case (port)
            0: begin
                @(negedge host_rx_clk);
                host_rx_valid = 1;
                host_rx_data = value;
                host_rx_be = 1;
                host_rx_first = (index == 0);
                host_rx_last = (index == total - 1);
                host_rx_error = 0;
                #1;
                if (host_rx_ready !== 1) $fatal(1, "host PHY cannot pause mid-frame");
                @(posedge host_rx_clk);
            end
            1: begin
                @(negedge sfp0_rx_clk);
                sfp0_rx_valid = 1;
                sfp0_rx_data = value;
                sfp0_rx_be = 1;
                sfp0_rx_first = (index == 0);
                sfp0_rx_last = (index == total - 1);
                sfp0_rx_error = 0;
                #1;
                if (sfp0_rx_ready !== 1) $fatal(1, "SFP0 PHY cannot pause mid-frame");
                @(posedge sfp0_rx_clk);
            end
            2: begin
                @(negedge sfp1_rx_clk);
                sfp1_rx_valid = 1;
                sfp1_rx_data = value;
                sfp1_rx_be = 1;
                sfp1_rx_first = (index == 0);
                sfp1_rx_last = (index == total - 1);
                sfp1_rx_error = 0;
                #1;
                if (sfp1_rx_ready !== 1) $fatal(1, "SFP1 PHY cannot pause mid-frame");
                @(posedge sfp1_rx_clk);
            end
            default: $fatal(1, "invalid physical RX port");
        endcase
    end
    endtask

    task automatic end_frame(input integer port);
    begin
        case (port)
            0: begin @(negedge host_rx_clk); host_rx_valid = 0; host_rx_last = 0; end
            1: begin @(negedge sfp0_rx_clk); sfp0_rx_valid = 0; sfp0_rx_last = 0; end
            2: begin @(negedge sfp1_rx_clk); sfp1_rx_valid = 0; sfp1_rx_last = 0; end
            default: $fatal(1, "invalid physical RX port");
        endcase
    end
    endtask

    function automatic [7:0] cable_byte(input integer port, input integer index);
    begin
        case (port)
            1: cable_byte = sfp0_tx_buffer[index];
            2: cable_byte = sfp1_tx_buffer[index];
            default: cable_byte = 0;
        endcase
    end
    endfunction

    task automatic print_frames(input integer port);
        integer frame;
        integer start;
        integer finish;
        integer k;
    begin
        start = 0;
        for (frame = 0; frame < 8; frame = frame + 1) begin
            if ((port == 0 && frame < host_tx_frame_count) ||
                (port == 1 && frame < sfp0_tx_frame_count) ||
                (port == 2 && frame < sfp1_tx_frame_count)) begin
                if (port == 0) finish = host_tx_ends[frame];
                else if (port == 1) finish = sfp0_tx_ends[frame];
                else finish = sfp1_tx_ends[frame];
                $write("FRAME %0d ", port);
                for (k = start; k < finish; k = k + 1) begin
                    if (port == 0) $write("%02x", host_tx_buffer[k]);
                    else if (port == 1) $write("%02x", sfp0_tx_buffer[k]);
                    else $write("%02x", sfp1_tx_buffer[k]);
                end
                $display("");
                start = finish;
            end
        end
    end
    endtask

    initial begin
        if (!$value$plusargs("DIRECTION=%d", direction)) $fatal(1, "missing DIRECTION");
        if (!$value$plusargs("CONNECTED=%d", connected)) $fatal(1, "missing CONNECTED");
        if (!$value$plusargs("CORRUPT=%d", corrupt)) $fatal(1, "missing CORRUPT");
        if (!$value$plusargs("REQUEST_LENGTH=%d", request_length)) $fatal(1, "missing REQUEST_LENGTH");
        if (!$value$plusargs("REQUEST_FILE=%s", request_file)) $fatal(1, "missing REQUEST_FILE");
        $readmemh(request_file, request_bytes, 0, request_length - 1);

        sys_clk = 0;
        host_rx_clk = 0;
        host_tx_clk = 0;
        sfp0_rx_clk = 0;
        sfp0_tx_clk = 0;
        sfp1_rx_clk = 0;
        sfp1_tx_clk = 0;
        sys_rst = 1;
        host_rx_rst = 1;
        host_tx_rst = 1;
        sfp0_rx_rst = 1;
        sfp0_tx_rst = 1;
        sfp1_rx_rst = 1;
        sfp1_tx_rst = 1;
        route_enable = 0;
        route_direction = direction;
        host_rx_valid = 0;
        host_rx_data = 0;
        host_rx_be = 0;
        host_rx_first = 0;
        host_rx_last = 0;
        host_rx_error = 0;
        sfp0_rx_valid = 0;
        sfp0_rx_data = 0;
        sfp0_rx_be = 0;
        sfp0_rx_first = 0;
        sfp0_rx_last = 0;
        sfp0_rx_error = 0;
        sfp1_rx_valid = 0;
        sfp1_rx_data = 0;
        sfp1_rx_be = 0;
        sfp1_rx_first = 0;
        sfp1_rx_last = 0;
        sfp1_rx_error = 0;

        #103;
        sys_rst = 0;
        host_rx_rst = 0;
        host_tx_rst = 0;
        sfp0_rx_rst = 0;
        sfp0_tx_rst = 0;
        sfp1_rx_rst = 0;
        sfp1_tx_rst = 0;
        repeat (10) @(posedge sys_clk);
        route_enable = 1;
        repeat (100) @(posedge sys_clk);

        for (i = 0; i < request_length; i = i + 1)
            send_beat(0, i, request_bytes[i], request_length);
        end_frame(0);

        if (direction == 0) begin
            sender = 1;
            receiver = 2;
            wait (sfp0_tx_frame_count == 1);
            sender_length = sfp0_tx_length;
        end else begin
            sender = 2;
            receiver = 1;
            wait (sfp1_tx_frame_count == 1);
            sender_length = sfp1_tx_length;
        end
        #1;

        if (connected) begin
            for (i = 0; i < sender_length; i = i + 1) begin
                j = (corrupt && i == sender_length - 1) ? 1 : 0;
                send_beat(receiver, i, cable_byte(sender, i) ^ j, sender_length);
            end
            end_frame(receiver);
        end

        if (connected && !corrupt) begin
            wait (host_tx_frame_count == 1);
        end else begin
            repeat (request_length * 16 + 1200) @(posedge sys_clk);
        end
        repeat (100) @(posedge sys_clk);

        print_frames(0);
        print_frames(1);
        print_frames(2);
        if (direction == 0)
            $display("COUNTS %0d %0d %0d %0d", host_tx_frames, sfp0_tx_frames,
                     sfp1_return_rx_frames, sfp1_rx_crc_errors);
        else
            $display("COUNTS %0d %0d %0d %0d", host_tx_frames, sfp1_tx_frames,
                     sfp0_return_rx_frames, sfp0_rx_crc_errors);
        $finish;
    end

    initial begin
        #1000000;
        $fatal(1, "compiled cable MAC simulation timed out");
    end
endmodule
"""


def _simulate_route_verilator(executable, body, direction, *, cable_connected=True,
                              corrupt_return=False):
    frame = wire_frame(body)
    with tempfile.TemporaryDirectory(prefix="cable-mac-compiled-") as directory:
        request_file = Path(directory) / "request.hex"
        request_file.write_text("\n".join(f"{byte:02x}" for byte in frame) + "\n")
        result = subprocess.run(
            [
                executable,
                f"+DIRECTION={direction}",
                f"+CONNECTED={int(cable_connected)}",
                f"+CORRUPT={int(corrupt_return)}",
                f"+REQUEST_LENGTH={len(frame)}",
                f"+REQUEST_FILE={request_file}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    transmitted = [[], [], []]
    for port, data in re.findall(r"^FRAME ([0-2]) ([0-9a-f]*)$", result.stdout, re.MULTILINE):
        transmitted[int(port)].append(bytes.fromhex(data))
    match = re.search(r"^COUNTS (\d+) (\d+) (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert match is not None, result.stdout
    host_tx, sfp_tx, return_rx, return_crc_errors = map(int, match.groups())
    return transmitted, dict(
        host_tx=host_tx,
        sfp_tx=sfp_tx,
        return_rx=return_rx,
        return_crc_errors=return_crc_errors,
    )


@pytest.fixture(scope="module")
def simulate_route(tmp_path_factory):
    """Run compiled MAC RTL when Verilator is installed, otherwise use Migen."""
    verilator = shutil.which("verilator")
    if verilator is None:
        return _simulate_route_python

    dut = CableMACs()
    fragment = dut.get_fragment()
    ports = _compiled_ports(dut, fragment)
    ios = {signal for signal, _, _, _ in ports}
    module = "`timescale 1ns/1ps\n" + verilog.convert(
        fragment, ios=ios, name="cable_macs"
    ).main_source
    build_dir = tmp_path_factory.mktemp("cable-mac-verilator")
    rtl_file = build_dir / "cable_macs.v"
    bench_file = build_dir / "tb.v"
    object_dir = build_dir / "obj"
    executable = object_dir / "Vtb"
    rtl_file.write_text(module)
    bench_file.write_text(_compiled_testbench(ports))
    subprocess.run(
        [
            verilator, "--binary", "--timing", "--top-module", "tb", "-Wno-fatal",
            "--Mdir", str(object_dir), "-j", "2", str(rtl_file), str(bench_file),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return lambda body, direction, **options: _simulate_route_verilator(
        str(executable), body, direction, **options
    )


@pytest.mark.parametrize("direction", [0, 1])
@pytest.mark.parametrize("size", [60, 1514])
def test_three_mac_path_preserves_cable_frame_and_returns_one_swapped_reply(
    simulate_route, direction, size
):
    extra = bytes(index % 256 for index in range(size - 60))
    transmitted, counts = simulate_route(REQUEST + extra, direction)
    sender, receiver = (1, 2) if direction == 0 else (2, 1)
    assert transmitted[sender] == [wire_frame(REQUEST + extra)]
    assert transmitted[receiver] == []
    assert transmitted[0] == [wire_frame(REPLY + extra)]
    assert counts == dict(host_tx=1, sfp_tx=1, return_rx=1, return_crc_errors=0)


def test_disconnected_cable_cannot_produce_a_host_reply(simulate_route):
    transmitted, counts = simulate_route(REQUEST, 0, cable_connected=False)
    assert transmitted == [[], [wire_frame(REQUEST)], []]
    assert counts == dict(host_tx=0, sfp_tx=1, return_rx=0, return_crc_errors=0)


def test_bad_cable_fcs_cannot_produce_a_host_reply(simulate_route):
    transmitted, counts = simulate_route(REQUEST, 1, corrupt_return=True)
    assert transmitted == [[], [], [wire_frame(REQUEST)]]
    assert counts == dict(host_tx=0, sfp_tx=1, return_rx=0, return_crc_errors=1)
