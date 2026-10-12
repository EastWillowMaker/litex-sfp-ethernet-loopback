from migen.sim import run_simulation
import pytest

from sfp_loopback.cable import CableLoop


TEST_FRAME = bytes.fromhex(
    "020000000002 102030405060 88b5 4c584c50 "
    "0102030405060708 00000001"
) + bytes(range(30))
EXPECTED_REPLY = bytes.fromhex(
    "102030405060 020000000002 88b5 4c584c50 "
    "0102030405060708 00000001"
) + bytes(range(30))


@pytest.mark.parametrize("direction, sender, receiver", [(0, 0, 1), (1, 1, 0)])
def test_selected_cable_path_preserves_frame_and_swaps_macs_on_return(direction, sender, receiver):
    dut = CableLoop(transaction_timeout_cycles=500)
    sent = []
    returned = []
    counts = {}
    busy_seen = False
    saw_reply_before_return = False
    active_direction_changed_while_busy = False

    def host():
        yield dut.enable.eq(0)
        yield dut.direction.eq(direction)
        yield
        yield dut.enable.eq(1)
        yield
        for index, byte in enumerate(TEST_FRAME):
            yield dut.host_sink.valid.eq(1)
            yield dut.host_sink.data.eq(byte)
            yield dut.host_sink.be.eq(1)
            yield dut.host_sink.first.eq(index == 0)
            yield dut.host_sink.last.eq(index == len(TEST_FRAME) - 1)
            yield dut.host_sink.error.eq(0)
            yield
        yield dut.host_sink.valid.eq(0)

    def cable_and_monitor():
        nonlocal busy_seen, saw_reply_before_return, active_direction_changed_while_busy
        egress = getattr(dut, f"sfp{sender}_source")
        return_sink = getattr(dut, f"sfp{receiver}_sink")
        unused_sink = getattr(dut, f"sfp{sender}_sink")
        wrong_egress = getattr(dut, f"sfp{1 - sender}_source")
        yield dut.host_source.ready.eq(1)
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(1)
        yield dut.sfp0_sink.valid.eq(0)
        yield dut.sfp1_sink.valid.eq(0)
        yield dut.sfp0_sink.error.eq(0)
        yield dut.sfp1_sink.error.eq(0)
        frame = bytearray()
        reply = bytearray()
        return_started = False
        return_index = 0
        return_delay = 0
        unused_index = 0
        unused_started = False
        unused_finished = False
        sent_complete = False
        held_egress = None
        held_host = None
        for cycle in range(1000):
            if (yield dut.busy):
                busy_seen = True
                if (yield dut.active_direction) != direction:
                    active_direction_changed_while_busy = True

            egress_beat = (
                (yield egress.data), (yield egress.be), (yield egress.error),
                (yield egress.first), (yield egress.last),
            )
            if held_egress is not None:
                assert (yield egress.valid), "SFP output must stay valid during backpressure"
                assert egress_beat == held_egress, "SFP output changed while stalled"
            if (yield egress.valid) and (yield egress.ready):
                frame.append((yield egress.data))
                if (yield egress.last):
                    sent.append(bytes(frame))
                    frame.clear()
                    sent_complete = True
            held_egress = egress_beat if (yield egress.valid) and not (yield egress.ready) else None

            if (yield wrong_egress.valid):
                raise AssertionError(f"direction {direction} must use only SFP{sender} TX")

            host_beat = (
                (yield dut.host_source.data), (yield dut.host_source.be),
                (yield dut.host_source.error), (yield dut.host_source.first),
                (yield dut.host_source.last),
            )
            if held_host is not None:
                assert (yield dut.host_source.valid), "Host output must stay valid during backpressure"
                assert host_beat == held_host, "Host output changed while stalled"
            if (yield dut.host_source.valid) and (yield dut.host_source.ready):
                if not return_started:
                    saw_reply_before_return = True
                reply.append((yield dut.host_source.data))
                if (yield dut.host_source.last):
                    returned.append(bytes(reply))
                    reply.clear()
            held_host = host_beat if (yield dut.host_source.valid) and not (yield dut.host_source.ready) else None

            if sent_complete and not return_started:
                if return_delay == 2 and unused_finished:
                    return_started = True
                    return_index = 0
                else:
                    if not unused_started and return_delay == 2:
                        unused_started = True
                        yield dut.enable.eq(0)
                        yield dut.direction.eq(1 - direction)
                    elif return_delay < 2:
                        return_delay += 1

            if unused_started and not unused_finished:
                yield unused_sink.valid.eq(1)
                yield unused_sink.data.eq(TEST_FRAME[unused_index])
                yield unused_sink.be.eq(1)
                yield unused_sink.first.eq(unused_index == 0)
                yield unused_sink.last.eq(unused_index == len(TEST_FRAME) - 1)
                yield unused_sink.error.eq(0)
                unused_index += 1
                if unused_index == len(TEST_FRAME):
                    unused_finished = True
            else:
                yield unused_sink.valid.eq(0)

            if return_started and return_index < len(sent[0]):
                yield return_sink.valid.eq(1)
                yield return_sink.data.eq(sent[0][return_index])
                yield return_sink.be.eq(1)
                yield return_sink.first.eq(return_index == 0)
                yield return_sink.last.eq(return_index == len(sent[0]) - 1)
                yield return_sink.error.eq(0)
                return_index += 1
            else:
                yield return_sink.valid.eq(0)
            yield egress.ready.eq(cycle % 7 == 0)
            yield dut.host_source.ready.eq(cycle % 5 == 0)
            yield
        for name in (
            "host_ingress_rx_frames", "host_ingress_tx_frames",
            "host_tx_frames", "active_direction",
        ):
            counts[name] = (yield getattr(dut, name))
        counts[f"sfp{sender}_tx_frames"] = (yield getattr(dut, f"sfp{sender}_tx_frames"))
        counts[f"sfp{receiver}_return_rx_frames"] = (
            yield getattr(dut, f"sfp{receiver}_return_rx_frames")
        )
        counts[f"sfp{receiver}_return_tx_frames"] = (
            yield getattr(dut, f"sfp{receiver}_return_tx_frames")
        )
        counts[f"sfp{receiver}_return_rejected_frames"] = (
            yield getattr(dut, f"sfp{receiver}_return_rejected_frames")
        )
        counts[f"sfp{sender}_return_rejected_frames"] = (
            yield getattr(dut, f"sfp{sender}_return_rejected_frames")
        )
        counts[f"sfp{sender}_unexpected_test_frames"] = (
            yield getattr(dut, f"sfp{sender}_unexpected_test_frames")
        )
        counts[f"sfp{receiver}_unexpected_test_frames"] = (
            yield getattr(dut, f"sfp{receiver}_unexpected_test_frames")
        )

    run_simulation(dut, [host(), cable_and_monitor()])

    assert sent == [TEST_FRAME]
    assert returned == [EXPECTED_REPLY], (counts, busy_seen)
    assert busy_seen
    assert not active_direction_changed_while_busy
    assert not saw_reply_before_return
    assert counts == {
        "host_ingress_rx_frames": 1,
        "host_ingress_tx_frames": 1,
        "host_tx_frames": 1,
        "active_direction": 1 - direction,
        f"sfp{sender}_tx_frames": 1,
        f"sfp{receiver}_return_rx_frames": 1,
        f"sfp{receiver}_return_tx_frames": 1,
        f"sfp{receiver}_return_rejected_frames": 0,
        f"sfp{sender}_return_rejected_frames": 1,
        f"sfp{sender}_unexpected_test_frames": 1,
        f"sfp{receiver}_unexpected_test_frames": 0,
    }


@pytest.mark.parametrize("offset", [12, 14])
def test_unrelated_host_frame_is_rejected_without_sfp_output(offset):
    dut = CableLoop(transaction_timeout_cycles=200)
    frame = bytearray(TEST_FRAME)
    frame[offset] ^= 1
    outputs = []
    counts = {}

    def host():
        yield dut.enable.eq(0)
        yield dut.direction.eq(0)
        yield
        yield dut.enable.eq(1)
        for index, byte in enumerate(frame):
            yield dut.host_sink.valid.eq(1)
            yield dut.host_sink.data.eq(byte)
            yield dut.host_sink.be.eq(1)
            yield dut.host_sink.first.eq(index == 0)
            yield dut.host_sink.last.eq(index == len(frame) - 1)
            yield dut.host_sink.error.eq(0)
            yield
        yield dut.host_sink.valid.eq(0)
        for _ in range(150):
            yield
        for name in (
            "host_ingress_rx_frames", "host_ingress_rejected_frames",
            "host_ingress_error_frames", "host_ingress_overflow_frames",
        ):
            counts[name] = (yield getattr(dut, name))
        counts["busy"] = (yield dut.busy)

    def observe():
        yield dut.host_source.ready.eq(1)
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(1)
        yield dut.sfp0_sink.valid.eq(0)
        yield dut.sfp1_sink.valid.eq(0)
        for _ in range(250):
            for endpoint in (dut.host_source, dut.sfp0_source, dut.sfp1_source):
                if (yield endpoint.valid) and (yield endpoint.ready):
                    outputs.append((yield endpoint.data))
            yield

    run_simulation(dut, [host(), observe()])

    assert outputs == []
    assert counts == {
        "host_ingress_rx_frames": 0,
        "host_ingress_rejected_frames": 1,
        "host_ingress_error_frames": 0,
        "host_ingress_overflow_frames": 0,
        "busy": 0,
    }


def test_busy_packet_overflow_is_drained_and_return_wait_times_out():
    dut = CableLoop(transaction_timeout_cycles=100)
    outputs = []
    counts = {}

    def host():
        yield dut.enable.eq(0)
        yield dut.direction.eq(0)
        yield
        yield dut.enable.eq(1)
        for frame in (TEST_FRAME, TEST_FRAME):
            for index, byte in enumerate(frame):
                yield dut.host_sink.valid.eq(1)
                yield dut.host_sink.data.eq(byte)
                yield dut.host_sink.be.eq(1)
                yield dut.host_sink.first.eq(index == 0)
                yield dut.host_sink.last.eq(index == len(frame) - 1)
                yield dut.host_sink.error.eq(0)
                yield
        yield dut.host_sink.valid.eq(0)
        for _ in range(350):
            yield
        for name in (
            "host_ingress_rx_frames", "host_ingress_tx_frames",
            "host_ingress_rejected_frames", "host_ingress_overflow_frames",
            "sfp0_tx_frames", "host_tx_frames",
        ):
            counts[name] = (yield getattr(dut, name))
        counts["busy"] = (yield dut.busy)

    def observe():
        yield dut.host_source.ready.eq(1)
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(1)
        yield dut.sfp0_sink.valid.eq(0)
        yield dut.sfp1_sink.valid.eq(0)
        packet = bytearray()
        for _ in range(500):
            if (yield dut.sfp0_source.valid) and (yield dut.sfp0_source.ready):
                packet.append((yield dut.sfp0_source.data))
                if (yield dut.sfp0_source.last):
                    outputs.append(bytes(packet))
                    packet.clear()
            if (yield dut.host_source.valid) and (yield dut.host_source.ready):
                pytest.fail("a cable return was absent, so the host must receive no reply")
            yield

    run_simulation(dut, [host(), observe()])

    assert outputs == [TEST_FRAME]
    assert counts == {
        "host_ingress_rx_frames": 1,
        "host_ingress_tx_frames": 1,
        "host_ingress_rejected_frames": 1,
        "host_ingress_overflow_frames": 1,
        "sfp0_tx_frames": 1,
        "host_tx_frames": 0,
        "busy": 0,
    }


def test_mac_error_on_selected_cable_return_is_rejected():
    dut = CableLoop(transaction_timeout_cycles=100)
    transmitted = []
    counts = {}

    def host():
        yield dut.enable.eq(0)
        yield dut.direction.eq(0)
        yield
        yield dut.enable.eq(1)
        for index, byte in enumerate(TEST_FRAME):
            yield dut.host_sink.valid.eq(1)
            yield dut.host_sink.data.eq(byte)
            yield dut.host_sink.be.eq(1)
            yield dut.host_sink.first.eq(index == 0)
            yield dut.host_sink.last.eq(index == len(TEST_FRAME) - 1)
            yield dut.host_sink.error.eq(0)
            yield
        yield dut.host_sink.valid.eq(0)
        for _ in range(250):
            yield
        counts["return_rx"] = (yield dut.sfp1_return_rx_frames)
        counts["return_rejected"] = (yield dut.sfp1_return_rejected_frames)
        counts["return_errors"] = (yield dut.sfp1_return_error_frames)
        counts["host_tx"] = (yield dut.host_tx_frames)
        counts["busy"] = (yield dut.busy)

    def cable():
        yield dut.host_source.ready.eq(1)
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(1)
        yield dut.sfp0_sink.valid.eq(0)
        yield dut.sfp1_sink.valid.eq(0)
        packet = bytearray()
        sent_complete = False
        return_delay = 0
        return_index = 0
        return_started = False
        for _ in range(500):
            if (yield dut.sfp0_source.valid) and (yield dut.sfp0_source.ready):
                packet.append((yield dut.sfp0_source.data))
                if (yield dut.sfp0_source.last):
                    transmitted.append(bytes(packet))
                    packet.clear()
                    sent_complete = True
            if (yield dut.host_source.valid):
                pytest.fail("a cable frame with a receive error must not reach the host")
            if sent_complete and return_delay < 2:
                return_delay += 1
            elif sent_complete:
                return_started = True
            if return_started and return_index < len(TEST_FRAME):
                yield dut.sfp1_sink.valid.eq(1)
                yield dut.sfp1_sink.data.eq(TEST_FRAME[return_index])
                yield dut.sfp1_sink.be.eq(1)
                yield dut.sfp1_sink.first.eq(return_index == 0)
                yield dut.sfp1_sink.last.eq(return_index == len(TEST_FRAME) - 1)
                yield dut.sfp1_sink.error.eq(return_index == 25)
                return_index += 1
            else:
                yield dut.sfp1_sink.valid.eq(0)
            yield

    run_simulation(dut, [host(), cable()])

    assert transmitted == [TEST_FRAME]
    assert counts == {
        "return_rx": 0,
        "return_rejected": 1,
        "return_errors": 1,
        "host_tx": 0,
        "busy": 0,
    }


def test_single_beat_malformed_host_frame_does_not_stall_next_transaction():
    dut = CableLoop(transaction_timeout_cycles=200)
    transmitted = []
    returned = []
    counts = {}

    def host():
        yield dut.enable.eq(0)
        yield dut.direction.eq(0)
        yield
        yield dut.enable.eq(1)
        # A one-byte Ethernet frame has frame_start and frame_done together.
        yield dut.host_sink.valid.eq(1)
        yield dut.host_sink.data.eq(0x02)
        yield dut.host_sink.be.eq(1)
        yield dut.host_sink.first.eq(1)
        yield dut.host_sink.last.eq(1)
        yield dut.host_sink.error.eq(0)
        yield
        for index, byte in enumerate(TEST_FRAME):
            yield dut.host_sink.valid.eq(1)
            yield dut.host_sink.data.eq(byte)
            yield dut.host_sink.be.eq(1)
            yield dut.host_sink.first.eq(index == 0)
            yield dut.host_sink.last.eq(index == len(TEST_FRAME) - 1)
            yield dut.host_sink.error.eq(0)
            yield
        yield dut.host_sink.valid.eq(0)
        for _ in range(300):
            yield
        counts["busy"] = (yield dut.busy)
        counts["host_tx"] = (yield dut.host_tx_frames)
        counts["host_rejected"] = (yield dut.host_ingress_rejected_frames)

    def cable():
        yield dut.host_source.ready.eq(1)
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(1)
        yield dut.sfp0_sink.valid.eq(0)
        yield dut.sfp1_sink.valid.eq(0)
        packet = bytearray()
        return_index = 0
        returning = False
        for _ in range(600):
            if (yield dut.sfp0_source.valid) and (yield dut.sfp0_source.ready):
                packet.append((yield dut.sfp0_source.data))
                if (yield dut.sfp0_source.last):
                    transmitted.append(bytes(packet))
                    packet.clear()
                    returning = True
            if (yield dut.host_source.valid) and (yield dut.host_source.ready):
                returned.append((yield dut.host_source.data))
            if returning and return_index < len(TEST_FRAME):
                yield dut.sfp1_sink.valid.eq(1)
                yield dut.sfp1_sink.data.eq(TEST_FRAME[return_index])
                yield dut.sfp1_sink.be.eq(1)
                yield dut.sfp1_sink.first.eq(return_index == 0)
                yield dut.sfp1_sink.last.eq(return_index == len(TEST_FRAME) - 1)
                yield dut.sfp1_sink.error.eq(0)
                return_index += 1
            else:
                yield dut.sfp1_sink.valid.eq(0)
            yield

    run_simulation(dut, [host(), cable()])

    assert transmitted == [TEST_FRAME]
    assert bytes(returned) == EXPECTED_REPLY
    assert counts == {"busy": 0, "host_tx": 1, "host_rejected": 1}


def test_delayed_duplicate_is_counted_but_unrelated_cable_frame_is_not():
    dut = CableLoop(transaction_timeout_cycles=300)
    returned = []
    counts = {}
    unrelated = bytearray(TEST_FRAME)
    unrelated[12] ^= 1

    def host():
        yield dut.enable.eq(0)
        yield dut.direction.eq(0)
        yield
        yield dut.enable.eq(1)
        for index, byte in enumerate(TEST_FRAME):
            yield dut.host_sink.valid.eq(1)
            yield dut.host_sink.data.eq(byte)
            yield dut.host_sink.be.eq(1)
            yield dut.host_sink.first.eq(index == 0)
            yield dut.host_sink.last.eq(index == len(TEST_FRAME) - 1)
            yield dut.host_sink.error.eq(0)
            yield
        yield dut.host_sink.valid.eq(0)
        for _ in range(600):
            yield
        counts["unexpected0"] = (yield dut.sfp0_unexpected_test_frames)
        counts["unexpected1"] = (yield dut.sfp1_unexpected_test_frames)
        counts["busy"] = (yield dut.busy)

    def cable():
        yield dut.host_source.ready.eq(1)
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(1)
        yield dut.sfp0_sink.valid.eq(0)
        yield dut.sfp1_sink.valid.eq(0)
        index = 0
        phase = "wait_egress"
        reply = bytearray()
        for _ in range(900):
            if (yield dut.host_source.valid) and (yield dut.host_source.ready):
                reply.append((yield dut.host_source.data))
                if (yield dut.host_source.last):
                    returned.append(bytes(reply))
                    reply.clear()
                    phase = "wait_idle"
            if phase == "wait_egress" and (yield dut.sfp0_source.valid) and (yield dut.sfp0_source.ready):
                if (yield dut.sfp0_source.last):
                    phase = "first_return"
                    index = 0
            if phase in ("first_return", "duplicate"):
                if index < len(TEST_FRAME):
                    yield dut.sfp1_sink.valid.eq(1)
                    yield dut.sfp1_sink.data.eq(TEST_FRAME[index])
                    yield dut.sfp1_sink.be.eq(1)
                    yield dut.sfp1_sink.first.eq(index == 0)
                    yield dut.sfp1_sink.last.eq(index == len(TEST_FRAME) - 1)
                    yield dut.sfp1_sink.error.eq(0)
                    index += 1
                else:
                    yield dut.sfp1_sink.valid.eq(0)
                    index = 0
                    if phase == "first_return":
                        phase = "wait_idle"
                    else:
                        phase = "unrelated"
            elif phase == "wait_idle":
                yield dut.sfp1_sink.valid.eq(0)
                if not (yield dut.busy) and returned:
                    phase = "duplicate"
                    index = 0
            elif phase == "unrelated":
                if index < len(unrelated):
                    yield dut.sfp1_sink.valid.eq(1)
                    yield dut.sfp1_sink.data.eq(unrelated[index])
                    yield dut.sfp1_sink.be.eq(1)
                    yield dut.sfp1_sink.first.eq(index == 0)
                    yield dut.sfp1_sink.last.eq(index == len(unrelated) - 1)
                    yield dut.sfp1_sink.error.eq(0)
                    index += 1
                else:
                    yield dut.sfp1_sink.valid.eq(0)
                    phase = "done"
            else:
                yield dut.sfp1_sink.valid.eq(0)
            yield

    run_simulation(dut, [host(), cable()])

    assert returned == [EXPECTED_REPLY]
    assert counts == {"unexpected0": 0, "unexpected1": 1, "busy": 0}
