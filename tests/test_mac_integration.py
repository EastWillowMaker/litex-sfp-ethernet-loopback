"""Check the assembled MAC/responder at its physical Ethernet byte stream."""

import zlib

import pytest
from migen import Memory, Module, Signal
from migen.sim import passive, run_simulation
from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description
from liteeth.mac.core import LiteEthMACCore

from sfp_loopback.responder import FrameResponder


REQUEST = bytes.fromhex(
    "020000000002 102030405060 88b5 4c584c50 "
    "0102030405060708 00000001"
) + bytes(range(30))
REPLY = bytes.fromhex(
    "102030405060 020000000002 88b5 4c584c50 "
    "0102030405060708 00000001"
) + bytes(range(30))
PREAMBLE = bytes.fromhex("55555555555555d5")


def on_wire(body):
    # Independent Ethernet CRC reference: reflected CRC-32, least-significant byte first.
    return PREAMBLE + body + zlib.crc32(body).to_bytes(4, "little")


class RawPHY(Module):
    dw = 8
    rx_clk_freq = 125e6
    tx_clk_freq = 125e6

    def __init__(self):
        self.source = stream.Endpoint(eth_phy_description(8))
        self.sink = stream.Endpoint(eth_phy_description(8))
        self.comb += self.sink.ready.eq(1)


class Loopback(Module):
    def __init__(self):
        self.submodules.phy = RawPHY()
        # Match the target's MAC parameters, including full-frame FIFOs before CDC.
        self.submodules.mac = LiteEthMACCore(self.phy, dw=8,
            with_sys_datapath=True, with_store_and_forward=True,
            tx_cdc_depth=64, rx_cdc_depth=64)
        self.submodules.responder = FrameResponder()
        self.comb += [self.mac.source.connect(self.responder.sink),
                      self.responder.source.connect(self.mac.sink)]


def simulate_cable(frames):
    dut = Loopback()
    replies = []
    snapshots = []
    counts = {}

    def drive_rx():
        for frame in frames:
            for index, byte in enumerate(frame):
                yield dut.phy.source.valid.eq(1)
                yield dut.phy.source.data.eq(byte)
                yield dut.phy.source.be.eq(1)
                yield dut.phy.source.first.eq(index == 0)
                yield dut.phy.source.last.eq(index == len(frame) - 1)
                yield dut.phy.source.error.eq(0)
                yield
                # A real PHY cannot pause during a frame: the MAC must keep up.
                assert (yield dut.phy.source.ready)
            yield dut.phy.source.valid.eq(0)
            yield dut.phy.source.last.eq(0)
            # sys runs at 50 MHz; allow reception, response and CDC/FIFO drainage.
            for _ in range(len(frame) * 8 + 300):
                yield
            snapshots.append(len(replies))
        counts['crc_errors'] = (yield dut.mac.rx_datapath.crc_errors.status)
        for name in ('rx_frames', 'tx_frames', 'rejected_frames', 'error_frames'):
            counts[name] = (yield getattr(dut.responder, name))

    @passive
    def collect_tx():
        packet = bytearray()
        while True:
            if (yield dut.phy.sink.valid):
                packet.append((yield dut.phy.sink.data))
                assert not (yield dut.phy.sink.error)
                if (yield dut.phy.sink.last):
                    replies.append(bytes(packet))
                    packet.clear()
            elif packet:
                pytest.fail("MAC interrupted a physical Ethernet frame")
            yield

    fragment = dut.get_fragment()
    # Migen's Python simulator predates its write-only RAM-port support and tries
    # to lower every port's read signal. Add unused read outputs for simulation;
    # this changes neither the connected RAM writes nor the gateware datapath.
    for special in fragment.specials:
        if isinstance(special, Memory):
            for port in special.ports:
                if port.dat_r is None:
                    port.dat_r = Signal(special.width)
    run_simulation(fragment, {'eth_rx': drive_rx(), 'eth_tx': collect_tx()},
                   clocks={'sys': 20, 'eth_rx': 8, 'eth_tx': 8})
    return replies, snapshots, counts


@pytest.mark.parametrize('size', [60, 1514])
def test_real_mac_returns_swapped_frame_with_correct_ethernet_fcs(size):
    payload = bytes(index % 256 for index in range(size - 60))
    replies, snapshots, counts = simulate_cable([on_wire(REQUEST + payload)])
    assert replies == [on_wire(REPLY + payload)]
    assert snapshots == [1]
    assert counts == dict(crc_errors=0, rx_frames=1, tx_frames=1,
                          rejected_frames=0, error_frames=0)


def test_corrupt_wire_fcs_is_rejected_and_next_valid_frame_recovers():
    bad = bytearray(on_wire(REQUEST))
    bad[-1] ^= 1
    replies, snapshots, counts = simulate_cable([bytes(bad), on_wire(REQUEST)])
    assert snapshots == [0, 1], "A frame with bad FCS must never be returned"
    assert replies == [on_wire(REPLY)]
    assert counts == dict(crc_errors=1, rx_frames=1, tx_frames=1,
                          rejected_frames=1, error_frames=1)
