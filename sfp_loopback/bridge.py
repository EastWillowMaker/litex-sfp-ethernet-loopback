"""Two-port, store-and-forward Ethernet EtherType bridge."""

from migen import Cat, If, Memory, Module, Signal
from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description


class _EthernetForwarder(Module):
    """Buffer and filter one direction of an 8-bit raw MAC stream."""

    MAX_FRAME_BYTES = 1514
    MIN_FRAME_BYTES = 60

    def __init__(self):
        self.sink = sink = stream.Endpoint(eth_phy_description(8))
        self.source = source = stream.Endpoint(eth_phy_description(8))

        self.rx_frames = Signal(32)
        self.tx_frames = Signal(32)
        self.ipv4_frames = Signal(32)
        self.arp_frames = Signal(32)
        self.rejected_frames = Signal(32)
        self.error_frames = Signal(32)
        self.overflow_frames = Signal(32)
        self.ipv6_drops = Signal(32)
        self.unsupported_drops = Signal(32)

        memory = Memory(8, self.MAX_FRAME_BYTES)
        write = memory.get_port(write_capable=True)
        read = memory.get_port(async_read=True)
        self.specials += memory, write, read

        rx_position = Signal(max=self.MAX_FRAME_BYTES + 1)
        rx_length = Signal(max=self.MAX_FRAME_BYTES + 1)
        rx_ethertype_high = Signal(8)
        rx_ethertype = Signal(16)
        rx_error = Signal()
        rx_buffer_busy = Signal()
        tx_position = Signal(max=self.MAX_FRAME_BYTES)
        tx_active = Signal()

        frame_length_valid = (
            (rx_position >= self.MIN_FRAME_BYTES - 1) &
            (rx_position < self.MAX_FRAME_BYTES)
        )
        frame_too_long = rx_position >= self.MAX_FRAME_BYTES
        frame_has_error = rx_error | sink.error
        frame_buffer_busy = rx_buffer_busy | ((rx_position == 0) & tx_active)
        frame_ipv4 = rx_ethertype == 0x0800
        frame_arp = rx_ethertype == 0x0806
        frame_ipv6 = rx_ethertype == 0x86dd
        frame_supported = frame_ipv4 | frame_arp
        frame_passes = (
            frame_length_valid & (frame_has_error == 0) &
            (frame_buffer_busy == 0) & frame_supported
        )

        self.comb += [
            sink.ready.eq(1),
            write.adr.eq(rx_position),
            write.dat_w.eq(sink.data),
            write.we.eq(
                sink.valid & (tx_active == 0) &
                (rx_position < self.MAX_FRAME_BYTES)
            ),
            read.adr.eq(tx_position),
            source.data.eq(read.dat_r),
            source.be.eq(1),
            source.valid.eq(tx_active),
            source.first.eq(tx_position == 0),
            source.last.eq(tx_active & (tx_position == rx_length - 1)),
            source.error.eq(0),
        ]

        self.sync += If(
            sink.valid,
            If(
                sink.last,
                rx_position.eq(0),
                rx_error.eq(0),
                rx_buffer_busy.eq(0),
                If(frame_has_error,
                    self.error_frames.eq(self.error_frames + 1),
                ),
                If(frame_passes,
                    self.rx_frames.eq(self.rx_frames + 1),
                    rx_length.eq(rx_position + 1),
                    tx_position.eq(0),
                    tx_active.eq(1),
                    If(frame_ipv4,
                        self.ipv4_frames.eq(self.ipv4_frames + 1),
                    ).Else(
                        self.arp_frames.eq(self.arp_frames + 1),
                    ),
                ).Else(
                    self.rejected_frames.eq(self.rejected_frames + 1),
                    If(frame_too_long | (frame_supported & frame_buffer_busy),
                        self.overflow_frames.eq(self.overflow_frames + 1),
                    ),
                    If(frame_length_valid & (frame_has_error == 0),
                        If(frame_ipv6,
                            self.ipv6_drops.eq(self.ipv6_drops + 1),
                        ).Elif(frame_supported == 0,
                            self.unsupported_drops.eq(
                                self.unsupported_drops + 1
                            ),
                        ),
                    ),
                ),
            ).Else(
                If(rx_position == 0,
                    rx_buffer_busy.eq(tx_active),
                    rx_error.eq(sink.error),
                ).Else(
                    rx_error.eq(rx_error | sink.error),
                ),
                If(rx_position == 12,
                    rx_ethertype_high.eq(sink.data),
                ).Elif(rx_position == 13,
                    rx_ethertype.eq(Cat(sink.data, rx_ethertype_high)),
                ),
                If(rx_position < self.MAX_FRAME_BYTES,
                    rx_position.eq(rx_position + 1),
                ),
            ),
        )

        self.sync += If(
            tx_active & source.ready,
            If(
                source.last,
                tx_active.eq(0),
                self.tx_frames.eq(self.tx_frames + 1),
            ).Else(tx_position.eq(tx_position + 1)),
        )


class EtherTypeBridge(Module):
    """Forward untagged IPv4 and ARP frames between two raw MAC endpoints.

    ``sfp0_sink``/``sfp1_sink`` receive frames from the corresponding MAC
    source. ``sfp0_source``/``sfp1_source`` send forwarded frames to the
    corresponding MAC sink. Each direction owns an independent single-frame
    buffer and exposes counters through ``forward0`` (SFP0 RX to SFP1 TX) and
    ``forward1`` (SFP1 RX to SFP0 TX).

    Frames are forwarded byte-for-byte only when their untagged outer
    EtherType is IPv4 (0x0800) or ARP (0x0806), their length is 60..1514 bytes,
    and the MAC reports no receive error. IPv6, VLAN, unsupported, malformed,
    errored, oversized, and buffer-busy frames are dropped in full. Reason
    counters overlap ``rejected_frames``. The single buffer per direction
    bounds storage and drops an otherwise eligible frame that arrives while
    its previous frame is being transmitted.
    """

    def __init__(self):
        self.sfp0_sink = stream.Endpoint(eth_phy_description(8))
        self.sfp1_sink = stream.Endpoint(eth_phy_description(8))
        self.sfp0_source = stream.Endpoint(eth_phy_description(8))
        self.sfp1_source = stream.Endpoint(eth_phy_description(8))

        self.submodules.forward0 = forward0 = _EthernetForwarder()
        self.submodules.forward1 = forward1 = _EthernetForwarder()

        self.comb += [
            self.sfp0_sink.connect(forward0.sink),
            forward0.source.connect(self.sfp1_source),
            self.sfp1_sink.connect(forward1.sink),
            forward1.source.connect(self.sfp0_source),
        ]
