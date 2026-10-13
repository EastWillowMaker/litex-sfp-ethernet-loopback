"""Verify bridge filtering and forwarding through two physical LiteEth MACs."""

import re
import shutil
import subprocess
import tempfile
import zlib
from pathlib import Path

import pytest
from migen import ClockDomain, ClockDomainsRenamer, Module
from migen.fhdl import verilog
from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description
from liteeth.mac.core import LiteEthMACCore

from sfp_loopback.bridge import EtherTypeBridge


PREAMBLE = bytes.fromhex("55555555555555d5")
FORWARD_COUNTERS = (
    "rx_frames", "tx_frames", "ipv4_frames", "arp_frames", "rejected_frames",
    "error_frames", "overflow_frames", "ipv6_drops", "unsupported_drops",
)


def wire_frame(body):
    return PREAMBLE + body + zlib.crc32(body).to_bytes(4, "little")


def ipv4_frame(size):
    """Build a valid literal Ethernet/IPv4/UDP frame of the requested size."""
    ip_length = size - 14
    udp_length = ip_length - 20
    ip_checksum = {60: "a537", 1514: "9f89"}[size]
    body = bytes.fromhex(
        "001122334455 66778899aabb 0800 "
        f"4500{ip_length:04x} 1234 4000 40 11 {ip_checksum} "
        "c0a80101 c0a80102 "
        f"04d2 162e {udp_length:04x} 0000"
    )
    payload_length = size - len(body)
    return body + bytes(index % 256 for index in range(payload_length))


def arp_frame(size):
    body = bytes.fromhex(
        "001122334455 66778899aabb 0806 "
        "0001080006040001 66778899aabb c0a80101 "
        "000000000000 c0a80102"
    )
    return body + bytes((0xAA,)) * (size - len(body))


def filtered_frame(kind):
    ethernet = bytes.fromhex("001122334455 66778899aabb ")
    if kind == "ipv6":
        body = ethernet + bytes.fromhex(
            "86dd 6000000000063b40 "
            "20010db8000000000000000000000001 "
            "20010db8000000000000000000000002 000102030405"
        )
    elif kind == "vlan":
        body = ethernet + bytes.fromhex("8100 0001 0800") + bytes.fromhex(
            "4500001c1234000040110000c0a80101c0a80102 04d2162e00080000"
        )
    elif kind == "qinq":
        body = ethernet + bytes.fromhex("88a8 0001 8100 0002 0806")
    elif kind == "unsupported":
        body = ethernet + bytes.fromhex("88cc")
    else:
        raise ValueError(f"unknown filtered frame kind: {kind}")
    return body + bytes((0xAA,)) * (60 - len(body))


class PhysicalStream(Module):
    dw = 8
    rx_clk_freq = tx_clk_freq = 125e6

    def __init__(self):
        self.source = stream.Endpoint(eth_phy_description(8))
        self.sink = stream.Endpoint(eth_phy_description(8))
        self.comb += self.sink.ready.eq(1)


class BridgeMACs(Module):
    def __init__(self):
        self.clock_domains.cd_sys = ClockDomain("sys")
        for name in ("sfp0_rx", "sfp0_tx", "sfp1_rx", "sfp1_tx"):
            setattr(self.clock_domains, "cd_" + name, ClockDomain(name))

        self.submodules.bridge = EtherTypeBridge()
        self.phys = []
        self.macs = []
        for name in ("sfp0", "sfp1"):
            phy = PhysicalStream()
            mac = ClockDomainsRenamer({"eth_tx": name + "_tx", "eth_rx": name + "_rx"})(
                LiteEthMACCore(
                    phy, dw=8, with_sys_datapath=True,
                    with_store_and_forward=True, tx_cdc_depth=64, rx_cdc_depth=64,
                )
            )
            setattr(self.submodules, name + "_phy", phy)
            setattr(self.submodules, name + "_mac", mac)
            self.phys.append(phy)
            self.macs.append(mac)
            self.comb += [
                mac.source.connect(getattr(self.bridge, name + "_sink")),
                getattr(self.bridge, name + "_source").connect(mac.sink),
            ]


def _compiled_ports(dut, fragment):
    ports = []

    def expose(signal, name, direction):
        signal.name_override = name
        ports.append((signal, name, direction, len(signal)))

    domains = {domain.name: domain for domain in fragment.clock_domains}
    for name in ("sys", "sfp0_rx", "sfp0_tx", "sfp1_rx", "sfp1_tx"):
        domain = domains[name]
        expose(domain.clk, name + "_clk", "input")
        expose(domain.rst, name + "_rst", "input")

    for index, name in enumerate(("sfp0", "sfp1")):
        rx = dut.phys[index].source
        for field in ("valid", "first", "last", "data", "be", "error"):
            expose(getattr(rx, field), f"{name}_rx_{field}", "input")
        expose(rx.ready, name + "_rx_ready", "output")

        tx = dut.phys[index].sink
        for field in ("valid", "first", "last", "data", "be", "error"):
            expose(getattr(tx, field), f"{name}_tx_{field}", "output")

    for direction in (0, 1):
        forward = getattr(dut.bridge, f"forward{direction}")
        for counter in FORWARD_COUNTERS:
            expose(getattr(forward, counter), f"forward{direction}_{counter}", "output")
    for index, mac in enumerate(dut.macs):
        expose(mac.rx_datapath.crc_errors.status, f"sfp{index}_rx_crc_errors", "output")
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
    reg [7:0] frame0 [0:2047];
    reg [7:0] frame1 [0:2047];
    reg [7:0] sfp0_tx_buffer [0:4095];
    reg [7:0] sfp1_tx_buffer [0:4095];
    integer sfp0_tx_ends [0:7];
    integer sfp1_tx_ends [0:7];
    integer sfp0_tx_length = 0;
    integer sfp1_tx_length = 0;
    integer sfp0_tx_frame_count = 0;
    integer sfp1_tx_frame_count = 0;
    integer sfp0_tx_in_frame = 0;
    integer sfp1_tx_in_frame = 0;
    integer input_port;
    integer frame_count;
    integer expected_frames;
    integer frame0_length;
    integer frame1_length;
    integer i;
    integer j;
    integer total_bytes;
    string frame0_file;
    string frame1_file;

    bridge_macs dut (
""" + ",\n".join(connections) + """
    );

    initial begin forever #10 sys_clk = ~sys_clk; end
    initial begin forever #4 sfp0_rx_clk = ~sfp0_rx_clk; end
    initial begin #1; forever #4 sfp0_tx_clk = ~sfp0_tx_clk; end
    initial begin #2; forever #4 sfp1_rx_clk = ~sfp1_rx_clk; end
    initial begin #3; forever #4 sfp1_tx_clk = ~sfp1_tx_clk; end

    always @(posedge sfp0_tx_clk) begin
        if (sfp0_tx_rst) begin
            sfp0_tx_length = 0;
            sfp0_tx_frame_count = 0;
            sfp0_tx_in_frame = 0;
        end else if (sfp0_tx_valid) begin
            if (sfp0_tx_error) $fatal(1, "SFP0 MAC TX reported an error");
            if (sfp0_tx_length >= 4096) $fatal(1, "SFP0 TX buffer overflow");
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
            if (sfp1_tx_error) $fatal(1, "SFP1 MAC TX reported an error");
            if (sfp1_tx_length >= 4096) $fatal(1, "SFP1 TX buffer overflow");
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
                             input integer length, input [7:0] value);
    begin
        if (port == 0) begin
            @(negedge sfp0_rx_clk);
            sfp0_rx_valid = 1;
            sfp0_rx_data = value;
            sfp0_rx_be = 1;
            sfp0_rx_first = (index == 0);
            sfp0_rx_last = (index == length - 1);
            sfp0_rx_error = 0;
            #1;
            if (sfp0_rx_ready !== 1) $fatal(1, "SFP0 RX cannot pause mid-frame");
            @(posedge sfp0_rx_clk);
        end else begin
            @(negedge sfp1_rx_clk);
            sfp1_rx_valid = 1;
            sfp1_rx_data = value;
            sfp1_rx_be = 1;
            sfp1_rx_first = (index == 0);
            sfp1_rx_last = (index == length - 1);
            sfp1_rx_error = 0;
            #1;
            if (sfp1_rx_ready !== 1) $fatal(1, "SFP1 RX cannot pause mid-frame");
            @(posedge sfp1_rx_clk);
        end
    end
    endtask

    task automatic end_frame(input integer port);
    begin
        if (port == 0) begin
            @(negedge sfp0_rx_clk);
            sfp0_rx_valid = 0;
            sfp0_rx_last = 0;
        end else begin
            @(negedge sfp1_rx_clk);
            sfp1_rx_valid = 0;
            sfp1_rx_last = 0;
        end
    end
    endtask

    function automatic [7:0] frame_byte(input integer frame, input integer index);
    begin
        if (frame == 0) frame_byte = frame0[index];
        else frame_byte = frame1[index];
    end
    endfunction

    task automatic send_frame(input integer port, input integer frame,
                              input integer length);
        integer k;
    begin
        for (k = 0; k < length; k = k + 1)
            send_beat(port, k, length, frame_byte(frame, k));
        end_frame(port);
    end
    endtask

    task automatic idle_port(input integer port, input integer cycles);
        integer k;
    begin
        for (k = 0; k < cycles; k = k + 1) begin
            if (port == 0) @(posedge sfp0_rx_clk);
            else @(posedge sfp1_rx_clk);
        end
    end
    endtask

    task automatic print_frames(input integer port);
        integer frame;
        integer start;
        integer finish;
        integer k;
    begin
        start = 0;
        for (frame = 0; frame < 8; frame = frame + 1) begin
            if ((port == 0 && frame < sfp0_tx_frame_count) ||
                (port == 1 && frame < sfp1_tx_frame_count)) begin
                if (port == 0) finish = sfp0_tx_ends[frame];
                else finish = sfp1_tx_ends[frame];
                $write("FRAME %0d ", port);
                for (k = start; k < finish; k = k + 1) begin
                    if (port == 0) $write("%02x", sfp0_tx_buffer[k]);
                    else $write("%02x", sfp1_tx_buffer[k]);
                end
                $display("");
                start = finish;
            end
        end
    end
    endtask

    initial begin
        if (!$value$plusargs("INPUT_PORT=%d", input_port)) $fatal(1, "missing INPUT_PORT");
        if (!$value$plusargs("FRAME_COUNT=%d", frame_count)) $fatal(1, "missing FRAME_COUNT");
        if (!$value$plusargs("EXPECTED_FRAMES=%d", expected_frames)) $fatal(1, "missing EXPECTED_FRAMES");
        if (!$value$plusargs("FRAME0_LENGTH=%d", frame0_length)) $fatal(1, "missing FRAME0_LENGTH");
        if (!$value$plusargs("FRAME0_FILE=%s", frame0_file)) $fatal(1, "missing FRAME0_FILE");
        $readmemh(frame0_file, frame0, 0, frame0_length - 1);
        frame1_length = 0;
        if (frame_count > 1) begin
            if (!$value$plusargs("FRAME1_LENGTH=%d", frame1_length)) $fatal(1, "missing FRAME1_LENGTH");
            if (!$value$plusargs("FRAME1_FILE=%s", frame1_file)) $fatal(1, "missing FRAME1_FILE");
            $readmemh(frame1_file, frame1, 0, frame1_length - 1);
        end

        sys_clk = 0;
        sfp0_rx_clk = 0;
        sfp0_tx_clk = 0;
        sfp1_rx_clk = 0;
        sfp1_tx_clk = 0;
        sys_rst = 1;
        sfp0_rx_rst = 1;
        sfp0_tx_rst = 1;
        sfp1_rx_rst = 1;
        sfp1_tx_rst = 1;
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
        sfp0_rx_rst = 0;
        sfp0_tx_rst = 0;
        sfp1_rx_rst = 0;
        sfp1_tx_rst = 0;
        repeat (10) @(posedge sys_clk);
        if (frame_count > 0) send_frame(input_port, 0, frame0_length);
        if (frame_count > 1) begin
            idle_port(input_port, 12);
            send_frame(input_port, 1, frame1_length);
        end

        total_bytes = frame0_length + frame1_length;
        if (expected_frames > 0) begin
            if (input_port == 0) wait (sfp1_tx_frame_count == expected_frames);
            else wait (sfp0_tx_frame_count == expected_frames);
        end
        repeat (total_bytes * 16 + 1200) @(posedge sys_clk);
        repeat (100) @(posedge sys_clk);

        print_frames(0);
        print_frames(1);
        $display("COUNTS0 %0d %0d %0d %0d %0d %0d %0d %0d %0d",
                 forward0_rx_frames, forward0_tx_frames, forward0_ipv4_frames,
                 forward0_arp_frames, forward0_rejected_frames, forward0_error_frames,
                 forward0_overflow_frames, forward0_ipv6_drops,
                 forward0_unsupported_drops);
        $display("COUNTS1 %0d %0d %0d %0d %0d %0d %0d %0d %0d",
                 forward1_rx_frames, forward1_tx_frames, forward1_ipv4_frames,
                 forward1_arp_frames, forward1_rejected_frames, forward1_error_frames,
                 forward1_overflow_frames, forward1_ipv6_drops,
                 forward1_unsupported_drops);
        $display("MACCRC %0d %0d", sfp0_rx_crc_errors, sfp1_rx_crc_errors);
        $finish;
    end

    initial begin
        #1000000;
        $fatal(1, "compiled bridge MAC simulation timed out");
    end
endmodule
"""


def _simulate_bridge(executable, frames, input_port, expected_frames):
    with tempfile.TemporaryDirectory(prefix="bridge-mac-") as directory:
        paths = []
        for index, frame in enumerate(frames):
            path = Path(directory) / f"frame{index}.hex"
            path.write_text("\n".join(f"{byte:02x}" for byte in frame) + "\n")
            paths.append(path)

        args = [
            executable,
            f"+INPUT_PORT={input_port}",
            f"+FRAME_COUNT={len(frames)}",
            f"+EXPECTED_FRAMES={expected_frames}",
            f"+FRAME0_LENGTH={len(frames[0])}",
            f"+FRAME0_FILE={paths[0]}",
        ]
        if len(frames) > 1:
            args.extend((f"+FRAME1_LENGTH={len(frames[1])}", f"+FRAME1_FILE={paths[1]}"))
        result = subprocess.run(
            args, check=True, capture_output=True, text=True, timeout=10
        )

    transmitted = [[], []]
    for port, data in re.findall(r"^FRAME ([01]) ([0-9a-f]*)$", result.stdout, re.MULTILINE):
        transmitted[int(port)].append(bytes.fromhex(data))
    counts = {}
    for direction in (0, 1):
        match = re.search(
            rf"^COUNTS{direction} ((?:\d+\s+){{8}}\d+)$", result.stdout, re.MULTILINE
        )
        assert match is not None, result.stdout
        counts[f"forward{direction}"] = dict(
            zip(FORWARD_COUNTERS, map(int, match.group(1).split()))
        )
    match = re.search(r"^MACCRC (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert match is not None, result.stdout
    counts["mac_crc_errors"] = tuple(map(int, match.groups()))
    return transmitted, counts


@pytest.fixture(scope="module")
def simulate_bridge(tmp_path_factory):
    verilator = shutil.which("verilator")
    if verilator is None:
        pytest.skip("Verilator is required for compiled two-MAC bridge integration")

    dut = BridgeMACs()
    fragment = dut.get_fragment()
    ports = _compiled_ports(dut, fragment)
    ios = {signal for signal, _, _, _ in ports}
    module = "`timescale 1ns/1ps\n" + verilog.convert(
        fragment, ios=ios, name="bridge_macs"
    ).main_source
    build_dir = tmp_path_factory.mktemp("bridge-mac-verilator")
    rtl_file = build_dir / "bridge_macs.v"
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
    return lambda frames, input_port, expected_frames: _simulate_bridge(
        str(executable), frames, input_port, expected_frames
    )


def expected_forward_counts(direction, **changes):
    counts = {counter: 0 for counter in FORWARD_COUNTERS}
    counts.update(changes)
    result = {"forward0": {counter: 0 for counter in FORWARD_COUNTERS},
              "forward1": {counter: 0 for counter in FORWARD_COUNTERS},
              "mac_crc_errors": (0, 0)}
    result[f"forward{direction}"] = counts
    return result


@pytest.mark.parametrize("direction", [0, 1])
@pytest.mark.parametrize("size", [60, 1514])
@pytest.mark.parametrize("kind", ["ipv4", "arp"])
def test_real_macs_preserve_untagged_ipv4_and_arp_at_size_limits(
    simulate_bridge, direction, size, kind
):
    body = ipv4_frame(size) if kind == "ipv4" else arp_frame(size)
    physical_frame = wire_frame(body)
    transmitted, counts = simulate_bridge([physical_frame], direction, 1)
    expected = [[], []]
    expected[1 - direction] = [physical_frame]

    assert len(body) == size
    assert transmitted == expected
    changes = {"rx_frames": 1, "tx_frames": 1, f"{kind}_frames": 1}
    assert counts == expected_forward_counts(direction, **changes)


@pytest.mark.parametrize("direction", [0, 1])
@pytest.mark.parametrize("kind", ["ipv6", "vlan", "qinq", "unsupported"])
def test_real_macs_drop_ipv6_vlan_and_unsupported_ethertypes(
    simulate_bridge, direction, kind
):
    transmitted, counts = simulate_bridge([wire_frame(filtered_frame(kind))], direction, 0)

    assert transmitted == [[], []]
    changes = {"rejected_frames": 1}
    if kind == "ipv6":
        changes["ipv6_drops"] = 1
    else:
        changes["unsupported_drops"] = 1
    assert counts == expected_forward_counts(direction, **changes)


@pytest.mark.parametrize("direction", [0, 1])
def test_real_macs_reject_bad_fcs_and_forward_the_next_valid_frame(
    simulate_bridge, direction
):
    valid = wire_frame(ipv4_frame(60))
    corrupt = bytearray(valid)
    corrupt[-1] ^= 1
    transmitted, counts = simulate_bridge([bytes(corrupt), valid], direction, 1)
    expected = [[], []]
    expected[1 - direction] = [valid]

    assert transmitted == expected
    expected_counts = expected_forward_counts(
        direction, rx_frames=1, tx_frames=1, ipv4_frames=1,
        rejected_frames=1, error_frames=1,
    )
    expected_counts["mac_crc_errors"] = (1, 0) if direction == 0 else (0, 1)
    assert counts == expected_counts
