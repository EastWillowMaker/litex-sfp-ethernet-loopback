"""Buffered test-frame routing for a physical SFP cable loop."""

from migen import Case, If, Module, Mux, Signal
from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description

from sfp_loopback.responder import FrameResponder


class _UnexpectedTestFrameMonitor(Module):
    """Count valid LXLP frames arriving outside an accepted return window."""

    def __init__(self):
        self.valid = Signal()
        self.data = Signal(8)
        self.last = Signal()
        self.error = Signal()
        self.expected = Signal()
        self.unexpected_test_frames = Signal(32)

        position = Signal(max=1515)
        mismatch = Signal()
        receive_error = Signal()
        expected = Signal()
        byte_mismatch = Signal()
        self.comb += [
            byte_mismatch.eq(0),
            Case(position, {
                0: byte_mismatch.eq(self.data != 0x02),
                1: byte_mismatch.eq(self.data != 0x00),
                2: byte_mismatch.eq(self.data != 0x00),
                3: byte_mismatch.eq(self.data != 0x00),
                4: byte_mismatch.eq(self.data != 0x00),
                5: byte_mismatch.eq(self.data != 0x02),
                12: byte_mismatch.eq(self.data != 0x88),
                13: byte_mismatch.eq(self.data != 0xb5),
                14: byte_mismatch.eq(self.data != ord("L")),
                15: byte_mismatch.eq(self.data != ord("X")),
                16: byte_mismatch.eq(self.data != ord("L")),
                17: byte_mismatch.eq(self.data != ord("P")),
            }),
        ]
        self.sync += If(
            self.valid,
            If(
                self.last,
                If(
                    (position >= 59) & (position < 1514) &
                    ~mismatch & ~receive_error & ~self.error & ~expected,
                    self.unexpected_test_frames.eq(self.unexpected_test_frames + 1),
                ),
                position.eq(0),
                mismatch.eq(0),
                receive_error.eq(0),
                expected.eq(0),
            ).Else(
                If(position == 0,
                    mismatch.eq(byte_mismatch),
                    expected.eq(self.expected),
                ).Else(mismatch.eq(mismatch | byte_mismatch)),
                receive_error.eq(receive_error | self.error),
                If(position < 1514, position.eq(position + 1)),
            ),
        )


class CableLoop(Module):
    """Route host test frames across the selected SFP cable direction.

    Direction 0 sends host frames through LiteX SFP0 (board SFP1) TX and
    accepts their cable return on LiteX SFP1 (board SFP2) RX. Direction 1
    reverses those ports. The host ingress
    filter preserves the complete frame; the selected return filter exchanges
    the destination and source MAC addresses once before returning it to the
    host.

    ``enable`` starts disabled. While disabled, and while the transaction and
    packet buffers are idle, ``direction`` is copied to ``active_direction``.
    A transaction keeps that selection fixed until its returned frame reaches
    the host or the return wait times out. All receive endpoints remain ready
    so their MACs can drain unrelated and disabled traffic.

    ``busy`` covers both the cable transaction and local packet handling. The
    timeout counts idle wait cycles while waiting for a cable return and pauses
    during a selected return frame; it cannot interrupt active frame transfer.

    ``host_ingress_*`` counters describe the host-to-SFP filter. Each
    ``sfpN_return_*`` group describes frames received on that SFP MAC and
    returned to the host. ``sfpN_tx_frames`` counts complete frames accepted
    by that SFP MAC sink; ``host_tx_frames`` counts complete frames accepted
    by the host MAC sink.
    """

    IDLE = 0
    HOST_INGRESS = 1
    SFP_EGRESS = 2
    CABLE_RETURN = 3
    HOST_EGRESS = 4

    def __init__(self, *, transaction_timeout_cycles=50_000_000):
        if transaction_timeout_cycles < 1:
            raise ValueError("transaction_timeout_cycles must be positive")

        self.host_sink = stream.Endpoint(eth_phy_description(8))
        self.host_source = stream.Endpoint(eth_phy_description(8))
        self.sfp0_sink = stream.Endpoint(eth_phy_description(8))
        self.sfp0_source = stream.Endpoint(eth_phy_description(8))
        self.sfp1_sink = stream.Endpoint(eth_phy_description(8))
        self.sfp1_source = stream.Endpoint(eth_phy_description(8))

        self.enable = Signal(reset=0)
        self.direction = Signal()
        self.active_direction = Signal()
        self.busy = Signal()

        self.submodules.host_ingress = host_ingress = FrameResponder(swap_macs=False)
        self.submodules.sfp0_return = sfp0_return = FrameResponder()
        self.submodules.sfp1_return = sfp1_return = FrameResponder()

        self.host_ingress_rx_frames = host_ingress.rx_frames
        self.host_ingress_tx_frames = host_ingress.tx_frames
        self.host_ingress_rejected_frames = host_ingress.rejected_frames
        self.host_ingress_error_frames = host_ingress.error_frames
        self.host_ingress_overflow_frames = host_ingress.overflow_frames

        self.sfp0_return_rx_frames = sfp0_return.rx_frames
        self.sfp0_return_tx_frames = sfp0_return.tx_frames
        self.sfp0_return_rejected_frames = sfp0_return.rejected_frames
        self.sfp0_return_error_frames = sfp0_return.error_frames
        self.sfp0_return_overflow_frames = sfp0_return.overflow_frames

        self.sfp1_return_rx_frames = sfp1_return.rx_frames
        self.sfp1_return_tx_frames = sfp1_return.tx_frames
        self.sfp1_return_rejected_frames = sfp1_return.rejected_frames
        self.sfp1_return_error_frames = sfp1_return.error_frames
        self.sfp1_return_overflow_frames = sfp1_return.overflow_frames

        self.sfp0_unexpected_test_frames = Signal(32)
        self.sfp1_unexpected_test_frames = Signal(32)
        self.submodules.sfp0_test_monitor = sfp0_test_monitor = _UnexpectedTestFrameMonitor()
        self.submodules.sfp1_test_monitor = sfp1_test_monitor = _UnexpectedTestFrameMonitor()

        self.host_tx_frames = Signal(32)
        self.sfp0_tx_frames = Signal(32)
        self.sfp1_tx_frames = Signal(32)

        transaction_state = Signal(3, reset=self.IDLE)
        return_wait = Signal(max=transaction_timeout_cycles)
        local_busy = Signal()

        self.comb += [
            self.host_sink.connect(host_ingress.sink),
            self.sfp0_sink.connect(sfp0_return.sink),
            self.sfp1_sink.connect(sfp1_return.sink),
            host_ingress.enable.eq(
                self.enable & (transaction_state == self.IDLE)
            ),
            sfp0_return.enable.eq(
                (transaction_state == self.CABLE_RETURN) & self.active_direction
            ),
            sfp1_return.enable.eq(
                (transaction_state == self.CABLE_RETURN) & ~self.active_direction
            ),
            sfp0_test_monitor.valid.eq(self.sfp0_sink.valid),
            sfp0_test_monitor.data.eq(self.sfp0_sink.data),
            sfp0_test_monitor.last.eq(self.sfp0_sink.last),
            sfp0_test_monitor.error.eq(self.sfp0_sink.error),
            sfp0_test_monitor.expected.eq(
                (transaction_state == self.CABLE_RETURN) & self.active_direction
            ),
            sfp1_test_monitor.valid.eq(self.sfp1_sink.valid),
            sfp1_test_monitor.data.eq(self.sfp1_sink.data),
            sfp1_test_monitor.last.eq(self.sfp1_sink.last),
            sfp1_test_monitor.error.eq(self.sfp1_sink.error),
            sfp1_test_monitor.expected.eq(
                (transaction_state == self.CABLE_RETURN) & ~self.active_direction
            ),
            self.sfp0_unexpected_test_frames.eq(sfp0_test_monitor.unexpected_test_frames),
            self.sfp1_unexpected_test_frames.eq(sfp1_test_monitor.unexpected_test_frames),
            local_busy.eq(host_ingress.busy | sfp0_return.busy | sfp1_return.busy),
            self.busy.eq((transaction_state != self.IDLE) | local_busy),
        ]

        selected_return_valid = Mux(
            self.active_direction, sfp0_return.source.valid, sfp1_return.source.valid
        )
        selected_return_data = Mux(
            self.active_direction, sfp0_return.source.data, sfp1_return.source.data
        )
        selected_return_be = Mux(
            self.active_direction, sfp0_return.source.be, sfp1_return.source.be
        )
        selected_return_first = Mux(
            self.active_direction, sfp0_return.source.first, sfp1_return.source.first
        )
        selected_return_last = Mux(
            self.active_direction, sfp0_return.source.last, sfp1_return.source.last
        )
        selected_return_error = Mux(
            self.active_direction, sfp0_return.source.error, sfp1_return.source.error
        )

        self.comb += [
            self.host_source.valid.eq(selected_return_valid),
            self.host_source.data.eq(selected_return_data),
            self.host_source.be.eq(selected_return_be),
            self.host_source.first.eq(selected_return_first),
            self.host_source.last.eq(selected_return_last),
            self.host_source.error.eq(selected_return_error),
            sfp0_return.source.ready.eq(self.host_source.ready & self.active_direction),
            sfp1_return.source.ready.eq(self.host_source.ready & ~self.active_direction),
        ]

        self.comb += [
            self.sfp0_source.valid.eq(host_ingress.source.valid & ~self.active_direction),
            self.sfp0_source.data.eq(host_ingress.source.data),
            self.sfp0_source.be.eq(host_ingress.source.be),
            self.sfp0_source.first.eq(host_ingress.source.first),
            self.sfp0_source.last.eq(host_ingress.source.last),
            self.sfp0_source.error.eq(host_ingress.source.error),
            self.sfp1_source.valid.eq(host_ingress.source.valid & self.active_direction),
            self.sfp1_source.data.eq(host_ingress.source.data),
            self.sfp1_source.be.eq(host_ingress.source.be),
            self.sfp1_source.first.eq(host_ingress.source.first),
            self.sfp1_source.last.eq(host_ingress.source.last),
            self.sfp1_source.error.eq(host_ingress.source.error),
            host_ingress.source.ready.eq(
                (self.sfp0_source.ready & ~self.active_direction) |
                (self.sfp1_source.ready & self.active_direction)
            ),
        ]

        self.sync += If(
            ~self.enable & (transaction_state == self.IDLE) & ~local_busy,
            self.active_direction.eq(self.direction),
        )

        self.sync += [
            If(
                self.host_source.valid & self.host_source.ready & self.host_source.last,
                self.host_tx_frames.eq(self.host_tx_frames + 1),
            ),
            If(
                self.sfp0_source.valid & self.sfp0_source.ready & self.sfp0_source.last,
                self.sfp0_tx_frames.eq(self.sfp0_tx_frames + 1),
            ),
            If(
                self.sfp1_source.valid & self.sfp1_source.ready & self.sfp1_source.last,
                self.sfp1_tx_frames.eq(self.sfp1_tx_frames + 1),
            ),
        ]

        self.sync += If(
            transaction_state == self.IDLE,
            If(
                self.enable & host_ingress.frame_start & ~host_ingress.frame_done,
                transaction_state.eq(self.HOST_INGRESS),
            ),
            return_wait.eq(0),
        ).Elif(
            transaction_state == self.HOST_INGRESS,
            If(
                host_ingress.frame_done,
                If(
                    host_ingress.frame_accepted,
                    transaction_state.eq(self.SFP_EGRESS),
                ).Else(transaction_state.eq(self.IDLE)),
            ),
        ).Elif(
            transaction_state == self.SFP_EGRESS,
            If(
                host_ingress.frame_transmitted,
                transaction_state.eq(self.CABLE_RETURN),
                return_wait.eq(0),
            ),
        ).Elif(
            transaction_state == self.CABLE_RETURN,
            If(
                Mux(self.active_direction,
                    sfp0_return.frame_accepted, sfp1_return.frame_accepted),
                transaction_state.eq(self.HOST_EGRESS),
                return_wait.eq(0),
            ).Elif(
                ~Mux(
                    self.active_direction,
                    sfp0_return.busy | self.sfp0_sink.valid,
                    sfp1_return.busy | self.sfp1_sink.valid,
                ),
                If(
                    return_wait == transaction_timeout_cycles - 1,
                    transaction_state.eq(self.IDLE),
                    return_wait.eq(0),
                ).Else(return_wait.eq(return_wait + 1)),
            ),
        ).Elif(
            transaction_state == self.HOST_EGRESS,
            If(
                Mux(self.active_direction,
                    sfp0_return.frame_transmitted, sfp1_return.frame_transmitted),
                transaction_state.eq(self.IDLE),
            ),
        )
