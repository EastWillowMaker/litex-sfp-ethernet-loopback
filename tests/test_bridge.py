from migen.sim import run_simulation

from sfp_loopback.bridge import EtherTypeBridge


IPV4_FRAME = bytes.fromhex(
    "001122334455 66778899aabb 0800 "
    "4500001c1234000040110000c0a80101c0a80102 "
    "04d2162e00080000 "
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)
ARP_FRAME = bytes.fromhex(
    "001122334455 66778899aabb 0806 "
    "0001080006040001 66778899aabb c0a80101 "
    "000000000000 c0a80102 "
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)
IPV6_FRAME = bytes.fromhex(
    "001122334455 66778899aabb 86dd "
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)
VLAN_IPV4_FRAME = bytes.fromhex(
    "001122334455 66778899aabb 8100 0001 0800 "
    "4500001c1234000040110000c0a80101c0a80102 "
    "04d2162e00080000 aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)
QINQ_ARP_FRAME = bytes.fromhex(
    "001122334455 66778899aabb 88a8 0001 8100 0002 0806 "
    "0001080006040001 66778899aabb c0a80101 "
    "000000000000 c0a80102 aaaaaaaaaaaaaaaaaaaa"
)
UNSUPPORTED_FRAME = bytes.fromhex(
    "001122334455 66778899aabb 88cc "
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)


def test_untagged_ipv4_and_arp_forward_unchanged_in_both_directions():
    dut = EtherTypeBridge()
    forwarded0 = []
    forwarded1 = []
    counts = {}

    def drive(sink, frames):
        yield sink.valid.eq(0)
        for frame in frames:
            for index, byte in enumerate(frame):
                yield sink.valid.eq(1)
                yield sink.data.eq(byte)
                yield sink.be.eq(1)
                yield sink.first.eq(index == 0)
                yield sink.last.eq(index == len(frame) - 1)
                yield sink.error.eq(0)
                yield
            yield sink.valid.eq(0)
        for _ in range(100):
            yield

    def observe():
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(1)
        packets = [bytearray(), bytearray()]
        sources = [dut.sfp1_source, dut.sfp0_source]
        results = [forwarded0, forwarded1]
        for _ in range(500):
            for packet, source, result in zip(packets, sources, results):
                if (yield source.valid) and (yield source.ready):
                    packet.append((yield source.data))
                    if (yield source.last):
                        result.append(bytes(packet))
                        packet.clear()
            yield
        for direction, path in ((0, dut.forward0), (1, dut.forward1)):
            for counter in (
                "rx_frames", "tx_frames", "ipv4_frames", "arp_frames",
                "rejected_frames", "error_frames", "overflow_frames",
                "ipv6_drops", "unsupported_drops",
            ):
                counts[f"forward{direction}_{counter}"] = (yield getattr(path, counter))

    run_simulation(
        dut,
        [
            drive(dut.sfp0_sink, [IPV4_FRAME, ARP_FRAME]),
            drive(dut.sfp1_sink, [ARP_FRAME, IPV4_FRAME]),
            observe(),
        ],
    )

    assert len(IPV4_FRAME) == len(ARP_FRAME) == 60
    assert forwarded0 == [IPV4_FRAME, ARP_FRAME]
    assert forwarded1 == [ARP_FRAME, IPV4_FRAME]
    for direction in (0, 1):
        assert counts[f"forward{direction}_rx_frames"] == 2
        assert counts[f"forward{direction}_tx_frames"] == 2
        assert counts[f"forward{direction}_ipv4_frames"] == 1
        assert counts[f"forward{direction}_arp_frames"] == 1
        assert counts[f"forward{direction}_rejected_frames"] == 0


def test_ipv6_vlan_and_other_ethertypes_are_dropped_in_full_in_both_directions():
    dut = EtherTypeBridge()
    outputs = [[], []]
    counts = {}
    unsupported = [IPV6_FRAME, VLAN_IPV4_FRAME, QINQ_ARP_FRAME, UNSUPPORTED_FRAME]

    def drive(sink):
        yield sink.valid.eq(0)
        for frame in unsupported:
            for index, byte in enumerate(frame):
                yield sink.valid.eq(1)
                yield sink.data.eq(byte)
                yield sink.be.eq(1)
                yield sink.first.eq(index == 0)
                yield sink.last.eq(index == len(frame) - 1)
                yield sink.error.eq(0)
                yield
            yield sink.valid.eq(0)
            for _ in range(100):
                yield
        for _ in range(100):
            yield

    def observe():
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(1)
        sources = [dut.sfp1_source, dut.sfp0_source]
        for _ in range(1000):
            for index, source in enumerate(sources):
                if (yield source.valid) and (yield source.ready):
                    outputs[index].append((yield source.data))
            yield
        for direction, path in ((0, dut.forward0), (1, dut.forward1)):
            for counter in (
                "rejected_frames", "ipv6_drops", "unsupported_drops",
            ):
                counts[f"forward{direction}_{counter}"] = (yield getattr(path, counter))

    run_simulation(
        dut,
        [drive(dut.sfp0_sink), drive(dut.sfp1_sink), observe()],
    )

    assert [len(IPV6_FRAME), len(VLAN_IPV4_FRAME), len(QINQ_ARP_FRAME), len(UNSUPPORTED_FRAME)] == [60] * 4
    assert outputs == [[], []]
    assert counts == {
        "forward0_rejected_frames": 4,
        "forward0_ipv6_drops": 1,
        "forward0_unsupported_drops": 3,
        "forward1_rejected_frames": 4,
        "forward1_ipv6_drops": 1,
        "forward1_unsupported_drops": 3,
    }


def test_runt_mac_error_and_oversized_frames_are_rejected_without_partial_output():
    dut = EtherTypeBridge()
    outputs = [[], []]
    counts = {}
    runt = IPV4_FRAME[:59]
    oversized = bytes.fromhex("001122334455 66778899aabb 0800") + b"\xaa" * 1501

    def drive(sink):
        yield sink.valid.eq(0)
        frames = ((runt, None), (IPV4_FRAME, 25), (oversized, None), (ARP_FRAME, None))
        for frame, error_index in frames:
            for index, byte in enumerate(frame):
                yield sink.valid.eq(1)
                yield sink.data.eq(byte)
                yield sink.be.eq(1)
                yield sink.first.eq(index == 0)
                yield sink.last.eq(index == len(frame) - 1)
                yield sink.error.eq(index == error_index)
                yield
            yield sink.valid.eq(0)
            for _ in range(100):
                yield
        for _ in range(100):
            yield

    def observe():
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(1)
        sources = [dut.sfp1_source, dut.sfp0_source]
        for _ in range(3000):
            for index, source in enumerate(sources):
                if (yield source.valid) and (yield source.ready):
                    outputs[index].append((yield source.data))
            yield
        for direction, path in ((0, dut.forward0), (1, dut.forward1)):
            for counter in (
                "rx_frames", "tx_frames", "rejected_frames", "error_frames",
                "overflow_frames",
            ):
                counts[f"forward{direction}_{counter}"] = (yield getattr(path, counter))

    run_simulation(
        dut,
        [drive(dut.sfp0_sink), drive(dut.sfp1_sink), observe()],
    )

    assert len(runt) == 59 and len(oversized) == 1515
    assert outputs == [list(ARP_FRAME), list(ARP_FRAME)]
    assert counts == {
        "forward0_rx_frames": 1,
        "forward0_tx_frames": 1,
        "forward0_rejected_frames": 3,
        "forward0_error_frames": 1,
        "forward0_overflow_frames": 1,
        "forward1_rx_frames": 1,
        "forward1_tx_frames": 1,
        "forward1_rejected_frames": 3,
        "forward1_error_frames": 1,
        "forward1_overflow_frames": 1,
    }


def test_backpressure_preserves_output_and_queued_frames_recover_cleanly():
    dut = EtherTypeBridge()
    forwarded = []
    counts = {}
    held_beat = None
    packet = bytearray()

    def send_frame(sink, frame):
        for index, byte in enumerate(frame):
            yield sink.valid.eq(1)
            yield sink.data.eq(byte)
            yield sink.be.eq(1)
            yield sink.first.eq(index == 0)
            yield sink.last.eq(index == len(frame) - 1)
            yield sink.error.eq(0)
            yield
        yield sink.valid.eq(0)
        yield

    def traffic():
        yield dut.sfp0_sink.valid.eq(0)
        yield dut.sfp1_sink.valid.eq(0)
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(0)
        yield from send_frame(dut.sfp0_sink, IPV4_FRAME)
        for _ in range(10):
            if (yield dut.sfp1_source.valid):
                break
            yield
        yield from send_frame(dut.sfp0_sink, ARP_FRAME)
        for _ in range(10):
            yield
        yield dut.sfp1_source.ready.eq(1)
        for _ in range(100):
            yield
        yield from send_frame(dut.sfp0_sink, ARP_FRAME)
        for _ in range(150):
            yield
        for counter in ("rx_frames", "tx_frames", "rejected_frames", "overflow_frames"):
            counts[counter] = (yield getattr(dut.forward0, counter))

    def observe():
        nonlocal held_beat
        for _ in range(500):
            source = dut.sfp1_source
            beat = (
                (yield source.data), (yield source.be),
                (yield source.first), (yield source.last), (yield source.error),
            )
            if held_beat is not None:
                assert (yield source.valid), "forwarded output must remain valid while stalled"
                assert beat == held_beat, "forwarded bytes and boundaries must remain stable while stalled"
            if (yield source.valid) and (yield source.ready):
                packet.append((yield source.data))
                if (yield source.last):
                    forwarded.append(bytes(packet))
                    packet.clear()
            held_beat = beat if (yield source.valid) and not (yield source.ready) else None
            yield

    run_simulation(dut, [traffic(), observe()])

    assert forwarded == [IPV4_FRAME, ARP_FRAME, ARP_FRAME]
    assert counts == {"rx_frames": 3, "tx_frames": 3, "rejected_frames": 0, "overflow_frames": 0}


def test_consecutive_frames_are_queued_and_forwarded_in_order_both_directions():
    dut = EtherTypeBridge()
    frames0 = [IPV4_FRAME, ARP_FRAME, IPV4_FRAME, ARP_FRAME, IPV4_FRAME, ARP_FRAME]
    frames1 = [ARP_FRAME, IPV4_FRAME, ARP_FRAME, IPV4_FRAME, ARP_FRAME, IPV4_FRAME]
    forwarded0 = []
    forwarded1 = []
    counts = {}

    def drive(sink, frames):
        for frame in frames:
            for index, byte in enumerate(frame):
                yield sink.valid.eq(1)
                yield sink.data.eq(byte)
                yield sink.be.eq(1)
                yield sink.first.eq(index == 0)
                yield sink.last.eq(index == len(frame) - 1)
                yield sink.error.eq(0)
                yield
        yield sink.valid.eq(0)
        for _ in range(100):
            yield

    def observe():
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(1)
        packets = [bytearray(), bytearray()]
        sources = [dut.sfp1_source, dut.sfp0_source]
        results = [forwarded0, forwarded1]
        for _ in range(1000):
            for packet, source, result in zip(packets, sources, results):
                if (yield source.valid) and (yield source.ready):
                    packet.append((yield source.data))
                    if (yield source.last):
                        result.append(bytes(packet))
                        packet.clear()
            yield
        for direction, path in ((0, dut.forward0), (1, dut.forward1)):
            for counter in ("rx_frames", "tx_frames", "rejected_frames", "overflow_frames"):
                counts[f"forward{direction}_{counter}"] = (yield getattr(path, counter))

    run_simulation(
        dut,
        [drive(dut.sfp0_sink, frames0), drive(dut.sfp1_sink, frames1), observe()],
    )

    assert forwarded0 == frames0
    assert forwarded1 == frames1
    assert counts == {
        "forward0_rx_frames": 6,
        "forward0_tx_frames": 6,
        "forward0_rejected_frames": 0,
        "forward0_overflow_frames": 0,
        "forward1_rx_frames": 6,
        "forward1_tx_frames": 6,
        "forward1_rejected_frames": 0,
        "forward1_overflow_frames": 0,
    }


def test_full_eight_frame_queue_drops_whole_frame_then_wraps_and_recovers():
    dut = EtherTypeBridge()
    input_frames = [IPV4_FRAME, ARP_FRAME] * 4 + [IPV4_FRAME]
    forwarded = []
    counts = {}
    packet = bytearray()

    def send_frame(sink, frame, error_index=None):
        for index, byte in enumerate(frame):
            yield sink.valid.eq(1)
            yield sink.data.eq(byte)
            yield sink.be.eq(1)
            yield sink.first.eq(index == 0)
            yield sink.last.eq(index == len(frame) - 1)
            yield sink.error.eq(index == error_index)
            yield
        yield sink.valid.eq(0)

    def traffic():
        yield dut.sfp0_sink.valid.eq(0)
        yield dut.sfp1_sink.valid.eq(0)
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(0)
        for frame in input_frames:
            yield from send_frame(dut.sfp0_sink, frame)
        # Invalid traffic at full capacity is rejected for its own reason;
        # only otherwise eligible frames count as queue overflow.
        yield from send_frame(dut.sfp0_sink, IPV4_FRAME[:59])
        yield from send_frame(dut.sfp0_sink, IPV4_FRAME, error_index=25)
        yield from send_frame(dut.sfp0_sink, IPV6_FRAME)
        for _ in range(10):
            yield
        yield dut.sfp1_source.ready.eq(1)
        for _ in range(700):
            yield
        yield from send_frame(dut.sfp0_sink, ARP_FRAME)
        for _ in range(200):
            yield
        for counter in (
            "rx_frames", "tx_frames", "rejected_frames", "overflow_frames",
            "error_frames", "ipv6_drops",
        ):
            counts[counter] = (yield getattr(dut.forward0, counter))

    def observe():
        for _ in range(1800):
            source = dut.sfp1_source
            if (yield source.valid) and (yield source.ready):
                packet.append((yield source.data))
                if (yield source.last):
                    forwarded.append(bytes(packet))
                    packet.clear()
            yield

    run_simulation(dut, [traffic(), observe()])

    assert forwarded == [IPV4_FRAME, ARP_FRAME] * 4 + [ARP_FRAME]
    assert counts == {
        "rx_frames": 9,
        "tx_frames": 9,
        "rejected_frames": 4,
        "overflow_frames": 1,
        "error_frames": 1,
        "ipv6_drops": 1,
    }


def test_rx_commit_while_tx_is_stalled_midframe_preserves_current_output():
    dut = EtherTypeBridge()
    forwarded = []
    counts = {}
    packet = bytearray()
    held_beat = None

    def send_frame(sink, frame):
        for index, byte in enumerate(frame):
            yield sink.valid.eq(1)
            yield sink.data.eq(byte)
            yield sink.be.eq(1)
            yield sink.first.eq(index == 0)
            yield sink.last.eq(index == len(frame) - 1)
            yield sink.error.eq(0)
            yield
        yield sink.valid.eq(0)
        yield

    def traffic():
        yield dut.sfp0_sink.valid.eq(0)
        yield dut.sfp1_sink.valid.eq(0)
        yield dut.sfp0_source.ready.eq(1)
        yield dut.sfp1_source.ready.eq(1)
        yield from send_frame(dut.sfp0_sink, IPV4_FRAME)
        consumed = 0
        while consumed < 20:
            if (yield dut.sfp1_source.valid) and (yield dut.sfp1_source.ready):
                consumed += 1
            if consumed == 20:
                yield dut.sfp1_source.ready.eq(0)
            yield
        yield from send_frame(dut.sfp0_sink, ARP_FRAME)
        for _ in range(5):
            yield
        yield dut.sfp1_source.ready.eq(1)
        for _ in range(200):
            yield
        counts["rx"] = (yield dut.forward0.rx_frames)
        counts["tx"] = (yield dut.forward0.tx_frames)
        counts["rejected"] = (yield dut.forward0.rejected_frames)

    def observe():
        nonlocal held_beat
        for _ in range(500):
            source = dut.sfp1_source
            beat = (
                (yield source.data), (yield source.be),
                (yield source.first), (yield source.last), (yield source.error),
            )
            if held_beat is not None:
                assert (yield source.valid), "stalled output must remain valid during RX commit"
                assert beat == held_beat, "RX commit must not disturb the active TX beat"
            if (yield source.valid) and (yield source.ready):
                packet.append((yield source.data))
                if (yield source.last):
                    forwarded.append(bytes(packet))
                    packet.clear()
            held_beat = beat if (yield source.valid) and not (yield source.ready) else None
            yield

    run_simulation(dut, [traffic(), observe()])

    assert forwarded == [IPV4_FRAME, ARP_FRAME]
    assert counts == {"rx": 2, "tx": 2, "rejected": 0}
