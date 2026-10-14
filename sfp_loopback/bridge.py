"""Two-port, store-and-forward Ethernet EtherType bridge."""

from migen import Array, Cat, If, Memory, Module, Signal
from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description
from migen.fhdl.specials import READ_FIRST


class _EthernetForwarder(Module):
    """Queue complete filtered frames in one direction of a raw MAC stream."""

    MAX_FRAME_BYTES = 1514
    MIN_FRAME_BYTES = 60
    SLOT_BYTES = 2048
    QUEUE_SLOTS = 8

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

        memory = Memory(8, self.SLOT_BYTES * self.QUEUE_SLOTS)
        write = memory.get_port(write_capable=True)
        read = memory.get_port(has_re=True, mode=READ_FIRST)
        self.specials += memory, write, read

        rx_position = Signal(max=self.MAX_FRAME_BYTES + 1)
        rx_ethertype_high = Signal(8)
        rx_ethertype = Signal(16)
        rx_error = Signal()
        rx_admission = Signal()
        write_pointer = Signal(max=self.QUEUE_SLOTS)
        read_pointer = Signal(max=self.QUEUE_SLOTS)
        queue_level = Signal(max=self.QUEUE_SLOTS + 1)
        slot_lengths = Array(
            Signal(max=self.SLOT_BYTES + 1, name=f"slot_length_{index}")
            for index in range(self.QUEUE_SLOTS)
        )
        tx_position = Signal(max=self.MAX_FRAME_BYTES)
        tx_valid = Signal()

        frame_length_valid = (
            (rx_position >= self.MIN_FRAME_BYTES - 1) &
            (rx_position < self.MAX_FRAME_BYTES)
        )
        frame_too_long = rx_position >= self.MAX_FRAME_BYTES
        frame_has_error = rx_error | sink.error
        frame_admitted = rx_admission | (
            (rx_position == 0) & (queue_level < self.QUEUE_SLOTS)
        )
        frame_ipv4 = rx_ethertype == 0x0800
        frame_arp = rx_ethertype == 0x0806
        frame_ipv6 = rx_ethertype == 0x86dd
        frame_supported = frame_ipv4 | frame_arp
        frame_passes = (
            frame_length_valid & (frame_has_error == 0) &
            frame_admitted & frame_supported
        )
        frame_commit = sink.valid & sink.last & frame_passes
        tx_last = tx_valid & (tx_position == slot_lengths[read_pointer] - 1)
        tx_pop = source.valid & source.ready & tx_last
        start_read = (tx_valid == 0) & (queue_level != 0)
        continue_read = tx_valid & source.ready & (tx_last == 0)
        read_request = start_read | continue_read
        read_offset = Signal(max=self.SLOT_BYTES)

        self.comb += [
            sink.ready.eq(1),
            write.adr.eq(Cat(rx_position, write_pointer)),
            write.dat_w.eq(sink.data),
            write.we.eq(
                sink.valid & frame_admitted &
                (rx_position < self.MAX_FRAME_BYTES)
            ),
            read.adr.eq(Cat(read_offset, read_pointer)),
            read.re.eq(read_request),
            source.data.eq(read.dat_r),
            source.be.eq(1),
            source.valid.eq(tx_valid),
            source.first.eq(tx_position == 0),
            source.last.eq(tx_last),
            source.error.eq(0),
            If(start_read,
                read_offset.eq(0),
            ).Elif(continue_read,
                read_offset.eq(tx_position + 1),
            ).Else(read_offset.eq(0)),
        ]

        self.sync += If(
            sink.valid,
            If(
                sink.last,
                rx_position.eq(0),
                rx_error.eq(0),
                rx_admission.eq(0),
                If(frame_has_error,
                    self.error_frames.eq(self.error_frames + 1),
                ),
                If(frame_passes,
                    self.rx_frames.eq(self.rx_frames + 1),
                    slot_lengths[write_pointer].eq(rx_position + 1),
                    write_pointer.eq(write_pointer + 1),
                    If(frame_ipv4,
                        self.ipv4_frames.eq(self.ipv4_frames + 1),
                    ).Else(
                        self.arp_frames.eq(self.arp_frames + 1),
                    ),
                ).Else(
                    self.rejected_frames.eq(self.rejected_frames + 1),
                    If(
                        frame_too_long | (
                            frame_length_valid & (frame_has_error == 0) &
                            frame_supported & (frame_admitted == 0)
                        ),
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
                    rx_admission.eq(queue_level < self.QUEUE_SLOTS),
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

        self.sync += [
            If(
                read_request,
                tx_valid.eq(1),
                If(start_read,
                    tx_position.eq(0),
                ).Else(tx_position.eq(tx_position + 1)),
            ).Elif(tx_pop,
                tx_valid.eq(0),
                tx_position.eq(0),
            ),
            If(tx_pop,
                read_pointer.eq(read_pointer + 1),
                self.tx_frames.eq(self.tx_frames + 1),
            ),
            If(frame_commit & (tx_pop == 0),
                queue_level.eq(queue_level + 1),
            ).Elif(tx_pop & (frame_commit == 0),
                queue_level.eq(queue_level - 1),
            ),
        ]


class EtherTypeBridge(Module):
    """Forward untagged IPv4 and ARP frames between two raw MAC endpoints.

    ``sfp0_sink``/``sfp1_sink`` receive frames from the corresponding MAC
    source. ``sfp0_source``/``sfp1_source`` send forwarded frames to the
    corresponding MAC sink. Each direction owns an independent eight-slot,
    2048-byte-per-slot queue and exposes counters through ``forward0`` (SFP0
    RX to SFP1 TX) and ``forward1`` (SFP1 RX to SFP0 TX).

    Frames are forwarded byte-for-byte only when their untagged outer
    EtherType is IPv4 (0x0800) or ARP (0x0806), their length is 60..1514 bytes,
    and the MAC reports no receive error. IPv6, VLAN, unsupported, malformed,
    errored, oversized, and queue-full frames are dropped in full. A frame's
    queue slot is committed only after its final byte passes validation, so
    rejected data is never visible at the output. Reason counters overlap
    ``rejected_frames``; ``overflow_frames`` counts oversized frames and
    otherwise eligible frames dropped when no queue slot was available.
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
