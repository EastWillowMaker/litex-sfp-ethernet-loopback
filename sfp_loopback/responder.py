"""Buffered Ethernet test responder, operating on raw frames without their FCS."""

from migen import Case, FSM, If, Memory, Module, Mux, NextState, NextValue, Signal
from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description


class FrameResponder(Module):
    """Receive one complete frame and return it with exchanged MAC addresses.

    ``sink`` and ``source`` use the LiteEth 8-bit raw frame stream. Only
    60..1514-byte test frames addressed to 02:00:00:00:00:02, with EtherType
    0x88b5 and marker LXLP, are accepted. The run/sequence payload is opaque.
    ``swap_macs=False`` preserves all frame bytes for intermediate routing;
    the default exchanges the first two MAC addresses. ``enable`` is sampled
    at each frame's first byte, and disabled frames are drained and rejected.

    RX is always drained because a physical Ethernet sender cannot be paused.
    A frame arriving while the single packet buffer is busy is discarded in
    its entirety, even if transmission ends before that frame does. Counters
    wrap at 32 bits: rx_frames counts accepted frames; rejected_frames counts
    all rejected frames, including overflow_frames. error_frames counts frames
    carrying a MAC receive error, including discarded busy frames.
    """

    def __init__(self, *, swap_macs=True):
        self.sink = sink = stream.Endpoint(eth_phy_description(8))
        self.source = source = stream.Endpoint(eth_phy_description(8))
        self.enable = Signal(reset=1)
        self.busy = Signal()
        self.frame_start = Signal()
        self.frame_done = Signal()
        self.frame_accepted = Signal()
        self.frame_transmitted = Signal()
        self.rx_frames = Signal(32)
        self.tx_frames = Signal(32)
        self.rejected_frames = Signal(32)
        self.error_frames = Signal(32)
        self.overflow_frames = Signal(32)

        memory = Memory(8, 1514)
        write = memory.get_port(write_capable=True)
        read = memory.get_port(async_read=True)
        self.specials += memory, write, read
        position = Signal(max=1515)
        length = Signal(max=1515)
        transmitting = Signal()
        receiving = Signal()
        busy_packet = Signal()
        rejected = Signal()
        receive_error = Signal()
        mismatch = Signal()
        draining = Signal()
        self.comb += Case(position, {
            0: mismatch.eq(sink.data != 0x02),
            1: mismatch.eq(sink.data != 0x00),
            2: mismatch.eq(sink.data != 0x00),
            3: mismatch.eq(sink.data != 0x00),
            4: mismatch.eq(sink.data != 0x00),
            5: mismatch.eq(sink.data != 0x02),
            12: mismatch.eq(sink.data != 0x88),
            13: mismatch.eq(sink.data != 0xb5),
            14: mismatch.eq(sink.data != ord("L")),
            15: mismatch.eq(sink.data != ord("X")),
            16: mismatch.eq(sink.data != ord("L")),
            17: mismatch.eq(sink.data != ord("P")),
        })
        read_address = Mux(position < 6, position + 6,
                           Mux(position < 12, position - 6, position)) if swap_macs else position
        completed = receiving & sink.valid & sink.last
        self.comb += [
            sink.ready.eq(1),
            write.adr.eq(position),
            write.dat_w.eq(sink.data),
            write.we.eq(sink.valid & receiving & (position < 1514)),
            read.adr.eq(read_address),
            source.data.eq(read.dat_r),
            source.be.eq(1),
            source.valid.eq(transmitting),
            source.first.eq(position == 0),
            source.last.eq(position == length - 1),
            source.error.eq(0),
            self.frame_start.eq(receiving & sink.valid & (position == 0)),
            self.frame_done.eq(completed),
            self.frame_accepted.eq(
                completed & ~rejected & ~mismatch & ~receive_error & ~sink.error &
                (position >= 59) & (position < 1514)
            ),
            self.frame_transmitted.eq(source.valid & source.ready & source.last),
            self.busy.eq(transmitting | draining | (receiving & (position != 0))),
        ]
        self.submodules.fsm = fsm = FSM(reset_state="RECEIVE")
        fsm.act("RECEIVE",
            receiving.eq(1),
            If(sink.valid,
                If(sink.last,
                    NextValue(position, 0),
                    NextValue(rejected, 0),
                    NextValue(receive_error, 0),
                    If(receive_error | sink.error,
                        NextValue(self.error_frames, self.error_frames + 1),
                    ),
                    If(~self.frame_accepted,
                        NextValue(self.rejected_frames, self.rejected_frames + 1),
                    ).Else(
                        NextValue(length, position + 1),
                        NextValue(self.rx_frames, self.rx_frames + 1),
                        NextState("SEND"),
                    ),
                ).Else(
                    If(position == 0,
                        NextValue(rejected, mismatch | ~self.enable),
                    ).Else(
                        NextValue(rejected, rejected | mismatch),
                    ),
                    NextValue(receive_error, receive_error | sink.error),
                    If(position < 1514, NextValue(position, position + 1)),
                ),
            ),
        )
        fsm.act("SEND",
            transmitting.eq(1),
            If(sink.valid,
                NextValue(busy_packet, ~sink.last),
                NextValue(receive_error, receive_error | sink.error),
                If(sink.last,
                    NextValue(self.rejected_frames, self.rejected_frames + 1),
                    NextValue(self.overflow_frames, self.overflow_frames + 1),
                    NextValue(receive_error, 0),
                    If(receive_error | sink.error,
                        NextValue(self.error_frames, self.error_frames + 1),
                    ),
                ),
            ),
            If(source.ready,
                If(source.last,
                    NextValue(position, 0),
                    NextValue(self.tx_frames, self.tx_frames + 1),
                    If(Mux(sink.valid, ~sink.last, busy_packet),
                        NextState("DRAIN"),
                    ).Else(NextState("RECEIVE")),
                ).Else(NextValue(position, position + 1)),
            ),
        )
        fsm.act("DRAIN",
            draining.eq(1),
            If(sink.valid,
                NextValue(receive_error, receive_error | sink.error),
                If(sink.last,
                    NextValue(busy_packet, 0),
                    NextValue(receive_error, 0),
                    NextValue(self.rejected_frames, self.rejected_frames + 1),
                    NextValue(self.overflow_frames, self.overflow_frames + 1),
                    If(receive_error | sink.error,
                        NextValue(self.error_frames, self.error_frames + 1),
                    ),
                    NextState("RECEIVE"),
                ),
            ),
        )
