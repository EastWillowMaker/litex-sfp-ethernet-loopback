from migen.sim import run_simulation
import pytest

from sfp_loopback.responder import FrameResponder


TEST_FRAME = bytes.fromhex(
    "020000000002 102030405060 88b5 4c584c50 "
    "0102030405060708 00000001"
) + bytes(range(30))
EXPECTED_REPLY = bytes.fromhex(
    "102030405060 020000000002 88b5 4c584c50 "
    "0102030405060708 00000001"
) + bytes(range(30))


def simulate(frames, *, stall_output=False, input_gaps=False):
    dut = FrameResponder()
    replies = []
    counts = {}
    waits = [max(1600, len(frame) * 7 + 20) if stall_output else 1600
             for frame, _ in frames]

    def drive():
        yield dut.source.ready.eq(0 if stall_output else 1)
        for (frame, errors), wait in zip(frames, waits):
            for index, byte in enumerate(frame):
                yield dut.sink.valid.eq(1)
                yield dut.sink.data.eq(byte)
                yield dut.sink.be.eq(1)
                yield dut.sink.last.eq(index == len(frame) - 1)
                yield dut.sink.error.eq(index in errors)
                yield
                if input_gaps and index < len(frame) - 1:
                    yield dut.sink.valid.eq(0)
                    for _ in range(3):
                        assert not (yield dut.source.valid), "No reply before the complete RX frame"
                        yield
            yield dut.sink.valid.eq(0)
            for _ in range(wait):
                yield
        for name in ("rx_frames", "tx_frames", "rejected_frames", "error_frames", "overflow_frames"):
            counts[name] = (yield getattr(dut, name))

    def collect():
        frame = bytearray()
        held = None
        input_cycles = sum(len(frame) * (4 if input_gaps else 1) for frame, _ in frames)
        for cycle in range(input_cycles + sum(waits) + 50):
            if stall_output:
                yield dut.source.ready.eq(cycle % 7 == 0)
            if (yield dut.source.valid) and (yield dut.source.ready):
                assert (yield dut.source.be) == 1, "Every returned byte must be enabled"
                frame.append((yield dut.source.data))
                if (yield dut.source.last):
                    replies.append(bytes(frame))
                    frame.clear()
            if held is not None:
                assert (yield dut.source.valid)
                assert ((yield dut.source.data), (yield dut.source.last)) == held
            held = ((yield dut.source.data), (yield dut.source.last)) if (
                (yield dut.source.valid) and not (yield dut.source.ready)
            ) else None
            yield
        assert not frame, "Responder emitted an unterminated reply fragment"

    run_simulation(dut, [drive(), collect()])
    return replies, counts


def test_test_frame_returns_swapped_addresses_and_preserved_payload():
    replies, counts = simulate([(TEST_FRAME, set())])
    assert replies == [EXPECTED_REPLY]
    assert counts["rx_frames"] == counts["tx_frames"] == 1


def test_unrelated_ethernet_frame_is_rejected():
    frame = bytearray(TEST_FRAME)
    frame[12:14] = bytes.fromhex("0800")
    replies, counts = simulate([(bytes(frame), set())])
    assert replies == []
    assert counts["rejected_frames"] == 1


@pytest.mark.parametrize("offset", [0, 5, 14, 17])
def test_wrong_destination_or_marker_is_rejected(offset):
    frame = bytearray(TEST_FRAME)
    frame[offset] ^= 1
    replies, counts = simulate([(bytes(frame), set())])
    assert replies == []
    assert counts["rejected_frames"] == 1


@pytest.mark.parametrize("offset", [0, 35, 59])
def test_any_mac_receive_error_rejects_the_entire_frame(offset):
    replies, counts = simulate([(TEST_FRAME, {offset}), (TEST_FRAME, set())])
    assert replies == [EXPECTED_REPLY]
    assert counts["error_frames"] == counts["rejected_frames"] == 1


@pytest.mark.parametrize("length", [18, 30, 59, 1515, 4096])
def test_out_of_range_frame_is_rejected_and_next_frame_is_intact(length):
    frame = TEST_FRAME[:length] if length < 60 else TEST_FRAME + bytes(length - 60)
    replies, counts = simulate([(frame, set()), (TEST_FRAME, set())])
    assert replies == [EXPECTED_REPLY]
    assert counts["rejected_frames"] == 1


@pytest.mark.parametrize("length", [60, 128, 256, 512, 1514])
def test_standard_size_frame_survives_output_backpressure(length):
    padding = bytes(index % 256 for index in range(length - 60))
    replies, counts = simulate([(TEST_FRAME + padding, set())], stall_output=True)
    assert replies == [EXPECTED_REPLY + padding]
    assert counts["rx_frames"] == counts["tx_frames"] == 1


@pytest.mark.parametrize("ready_after", [0, 100, 1453, 1454])
def test_busy_packet_is_drained_across_transmit_completion_then_next_packet_recovers(ready_after):
    dut = FrameResponder()
    replies = []
    busy_frame = TEST_FRAME + bytes(1454)
    counts = {}

    def drive():
        yield dut.source.ready.eq(0)
        for packet_index, packet in enumerate((TEST_FRAME, busy_frame, TEST_FRAME)):
            for index, byte in enumerate(packet):
                yield dut.sink.valid.eq(1)
                yield dut.sink.data.eq(byte)
                yield dut.sink.be.eq(1)
                yield dut.sink.last.eq(index == len(packet) - 1)
                yield dut.sink.error.eq(packet_index == 1 and index == 300)
                if packet_index == 1 and index == ready_after:
                    yield dut.source.ready.eq(1)
                yield
                assert (yield dut.sink.ready), "MAC receive stream must always be drained"
        yield dut.sink.valid.eq(0)
        for _ in range(100):
            yield
        for name in ("rx_frames", "tx_frames", "rejected_frames", "error_frames", "overflow_frames"):
            counts[name] = (yield getattr(dut, name))

    def collect():
        packet = bytearray()
        for _ in range(1800):
            if (yield dut.source.valid) and (yield dut.source.ready):
                packet.append((yield dut.source.data))
                if (yield dut.source.last):
                    replies.append(bytes(packet))
                    packet.clear()
            yield

    run_simulation(dut, [drive(), collect()])
    assert replies == [EXPECTED_REPLY, EXPECTED_REPLY]
    assert counts == {
        "rx_frames": 2, "tx_frames": 2, "rejected_frames": 1,
        "error_frames": 1, "overflow_frames": 1,
    }


def test_paused_receive_stream_waits_for_the_whole_frame_before_replying():
    replies, counts = simulate([(TEST_FRAME, set())], input_gaps=True)
    assert replies == [EXPECTED_REPLY]
    assert counts["rx_frames"] == counts["tx_frames"] == 1
