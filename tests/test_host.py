import json
import pytest

from sfp_loopback.host import make_frame, run_test, main


def test_minimum_frame_has_the_agreed_wire_protocol():
    frame = make_frame(bytes.fromhex('102030405060'), 0x0102030405060708, 0, 60)
    assert frame.hex() == (
        '02000000000210203040506088b54c584c50010203040506070800000000'
        '00254a6f94b9de03284d7297bce1062b50759abfe4092e53789dc2e70c31'
    )


class EthernetCable:
    """Packet socket boundary, including Linux's copy of outgoing traffic."""

    def __init__(self):
        self.packets = []
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)
        self.packets.extend([(frame, 4), (frame[6:12] + frame[:6] + frame[12:], 0)])

    def receive(self, timeout):
        return self.packets.pop(0) if self.packets else None


def test_host_verifies_all_1000_returned_frames():
    cable = EthernetCable()
    report = run_test(cable, bytes.fromhex('102030405060'), 42)
    assert report['passed'] is True
    assert report['received'] == report['sent'] == 1000
    assert [len(cable.sent[i]) for i in (0, 199, 200, 400, 600, 800, 999)] == [
        60, 60, 128, 256, 512, 1514, 1514
    ]


def test_outgoing_packets_cannot_produce_a_false_pass():
    class DisconnectedCable(EthernetCable):
        def send(self, frame):
            self.packets.append((frame, 4))

    report = run_test(DisconnectedCable(), bytes.fromhex('102030405060'), 42,
                      sizes=(60,), per_size=1)
    assert report['passed'] is False
    assert report['missing'] == 1
    assert report['received'] == 0


def test_host_reports_corrupt_duplicate_stale_and_unexpected_replies():
    class NoisyCable(EthernetCable):
        def send(self, frame):
            reply = frame[6:12] + frame[:6] + frame[12:]
            if self.sent:
                old = self.sent[-1]
                self.packets.append((old[6:12] + old[:6] + old[12:], 0))
            self.sent.append(frame)
            stale = reply[:18] + bytes.fromhex('000000000000002b') + reply[26:]
            future = reply[:26] + bytes.fromhex('00000100') + reply[30:]
            self.packets.extend([(stale, 0), (future, 0), (reply[:-1] + b'\xff', 0),
                                 (reply[:-1], 0), (reply, 0)])

    report = run_test(NoisyCable(), bytes.fromhex('102030405060'), 42,
                      sizes=(60,), per_size=2)
    assert report['received'] == 2
    assert report['duplicate'] == 1
    assert report['corrupt'] == 4
    assert report['stale'] == 2
    assert report['unexpected'] == 2
    assert report['passed'] is False


def test_transport_failure_is_reported_without_a_false_pass():
    class BrokenCable(EthernetCable):
        def send(self, frame):
            raise OSError('USB Ethernet device disappeared')

    report = run_test(BrokenCable(), bytes.fromhex('102030405060'), 42,
                      sizes=(60,), per_size=1)
    assert report['passed'] is False
    assert report['errors'] == ['OSError: USB Ethernet device disappeared']
    assert report['sent'] == report['received'] == 0


def test_final_duplicate_is_observed_after_the_last_good_reply():
    class DuplicateCable(EthernetCable):
        def send(self, frame):
            reply = frame[6:12] + frame[:6] + frame[12:]
            self.packets.extend([(reply, 0), (reply, 0)])

    report = run_test(DuplicateCable(), bytes.fromhex('102030405060'), 42,
                      sizes=(60,), per_size=1)
    assert report['duplicate'] == 1
    assert report['passed'] is False


def test_cli_saves_failure_report_when_usb_interface_is_absent(tmp_path):
    report_path = tmp_path / 'report.json'
    status = main(['--interface', 'enxmissing', '--report', str(report_path)],
                  interface_root=tmp_path)
    report = json.loads(report_path.read_text())
    assert status == 2
    assert report['passed'] is False
    assert report['interface'] == 'enxmissing'
    assert 'does not exist' in report['errors'][0]


def usb_interface(root, *, wireless=False, carrier='1'):
    interface = root / 'enxtest'
    interface.mkdir()
    for name, value in dict(type='1', flags='0x1003', carrier=carrier, mtu='1500',
                            address='10:20:30:40:50:60').items():
        (interface / name).write_text(value)
    device = root / 'devices' / 'usb1' / '1-1' / 'net'
    device.mkdir(parents=True)
    (interface / 'device').symlink_to(device)
    if wireless:
        (interface / 'wireless').mkdir()
    return interface


def test_cli_verifies_the_wired_usb_link_and_saves_a_success_artifact(tmp_path):
    usb_interface(tmp_path)
    report_path = tmp_path / 'report.json'

    class Cable(EthernetCable):
        def close(self):
            pass

    status = main(['--interface', 'enxtest', '--report', str(report_path),
                   '--run-id', '0x2a'], interface_root=tmp_path,
                  transport_factory=lambda name: Cable())
    report = json.loads(report_path.read_text())
    assert status == 0
    assert report['passed'] is True
    assert report['host_mac'] == '10:20:30:40:50:60'
    assert report['run_id'] == '000000000000002a'
    assert report['received'] == 1000


def test_cli_refuses_a_usb_wifi_adapter(tmp_path):
    usb_interface(tmp_path, wireless=True)
    report_path = tmp_path / 'report.json'
    status = main(['--interface', 'enxtest', '--report', str(report_path)],
                  interface_root=tmp_path)
    assert status == 2
    assert 'not a wired Ethernet interface' in json.loads(report_path.read_text())['errors'][0]


def test_cli_refuses_to_test_when_the_cable_has_no_carrier(tmp_path):
    usb_interface(tmp_path, carrier='0')
    report_path = tmp_path / 'report.json'
    status = main(['--interface', 'enxtest', '--report', str(report_path)],
                  interface_root=tmp_path)
    assert status == 2
    assert 'no Ethernet carrier' in json.loads(report_path.read_text())['errors'][0]


def test_cli_socket_close_failure_still_saves_a_failed_report(tmp_path):
    usb_interface(tmp_path)
    report_path = tmp_path / 'report.json'

    class Cable(EthernetCable):
        def close(self):
            raise OSError('device disconnected on close')

    status = main(['--interface', 'enxtest', '--report', str(report_path)],
                  interface_root=tmp_path, transport_factory=lambda name: Cable())
    report = json.loads(report_path.read_text())
    assert status == 2
    assert report['passed'] is False
    assert 'device disconnected on close' in report['errors'][0]


class TimedCable(EthernetCable):
    def __init__(self, delay=0.125):
        super().__init__()
        self.now = 0.0
        self.delay = delay

    def clock(self):
        return self.now

    def receive(self, timeout):
        if self.packets:
            packet = self.packets.pop(0)
            if packet[1] != 4:
                self.now += self.delay
            return packet
        self.now += timeout
        return None


def test_sustained_mode_cycles_sizes_until_deadline_and_reports_measured_rate():
    cable = TimedCable()
    report = run_test(cable, bytes.fromhex('102030405060'), 42,
                      duration_seconds=0.75, clock=cable.clock)
    assert report['passed'] is True
    assert [len(frame) for frame in cable.sent] == [60, 128, 256, 512, 1514, 60]
    assert report['mode'] == 'sustained_correctness'
    assert report['requested_duration_seconds'] == 0.75
    assert report['active_seconds'] == 0.75
    assert report['elapsed_seconds'] == 1.75
    assert report['sent'] == report['received'] == report['expected'] == 6
    assert report['sent_bytes'] == report['received_bytes'] == 2530
    assert report['frames_by_size'] == {'60': 2, '128': 1, '256': 1, '512': 1, '1514': 1}
    assert report['received_mbps'] == 0.026987
    assert report['received_frames_per_second'] == 8.0


def test_sustained_missing_reply_finishes_current_timeout_then_final_drain():
    class DisconnectedCable(TimedCable):
        def send(self, frame):
            self.sent.append(frame)

    cable = DisconnectedCable()
    report = run_test(cable, bytes.fromhex('102030405060'), 42,
                      duration_seconds=0.25, clock=cable.clock)
    assert report['passed'] is False
    assert report['sent'] == report['missing'] == 1
    assert report['active_seconds'] == 1.0
    assert report['elapsed_seconds'] == 2.0
    assert report['elapsed_seconds'] <= 0.25 + 1.0 + 1.0


def test_sustained_recognizes_a_duplicate_from_far_earlier_in_the_run():
    class DelayedDuplicateCable(TimedCable):
        def send(self, frame):
            super().send(frame)
            if len(self.sent) == 1200:
                first = self.sent[0]
                self.packets.append((bytes.fromhex('102030405060020000000002') + first[12:], 0))

    cable = DelayedDuplicateCable(delay=0.001)
    report = run_test(cable, bytes.fromhex('102030405060'), 42,
                      duration_seconds=1.5, clock=cable.clock)
    assert report['sent'] > 1200
    assert report['received'] == report['sent']
    assert report['duplicate'] == 1
    assert report['passed'] is False


def test_timed_cli_saves_duration_and_measured_rate_in_report(tmp_path):
    usb_interface(tmp_path)
    report_path = tmp_path / 'report.json'

    class Cable(TimedCable):
        def close(self):
            pass

    cable = Cable()
    status = main(['--interface', 'enxtest', '--report', str(report_path),
                   '--duration', '0.75'], interface_root=tmp_path,
                  transport_factory=lambda name: cable, clock=cable.clock)
    report = json.loads(report_path.read_text())
    assert status == 0
    assert report['passed'] is True
    assert report['completed'] is True
    assert report['requested_duration_seconds'] == report['active_seconds'] == 0.75
    assert report['sent'] == report['received'] == 6
    assert report['frames_per_size'] is None
    assert report['received_mbps'] == 0.026987


@pytest.mark.parametrize('duration', ['nan', 'inf', '0', '-1'])
def test_cli_invalid_duration_saves_failure_artifact(tmp_path, duration):
    report_path = tmp_path / 'report.json'
    status = main(['--interface', 'enxmissing', '--report', str(report_path),
                   '--duration', duration], interface_root=tmp_path)
    report = json.loads(report_path.read_text())
    assert status == 2
    assert report['passed'] is False
    assert 'Duration must be a finite positive number' in report['errors'][0]


def test_late_reply_to_a_timed_out_sequence_is_unexpected_not_duplicate():
    class LateCable(TimedCable):
        def send(self, frame):
            super().send(frame)
            if len(self.sent) == 1:
                self.packets.clear()
            elif len(self.sent) == 2:
                first = self.sent[0]
                self.packets.insert(0, (bytes.fromhex('102030405060020000000002') + first[12:], 0))

    cable = LateCable()
    report = run_test(cable, bytes.fromhex('102030405060'), 42,
                      duration_seconds=1.5, clock=cable.clock)
    assert report['missing'] == report['unexpected'] == 1
    assert report['duplicate'] == 0
    assert report['received'] == report['sent'] - 1
    assert report['passed'] is False


def test_timed_cli_interruption_saves_partial_counts_and_cannot_pass(tmp_path):
    usb_interface(tmp_path)
    report_path = tmp_path / 'report.json'

    class InterruptedCable(TimedCable):
        def receive(self, timeout):
            if len(self.sent) == 3:
                raise KeyboardInterrupt
            return super().receive(timeout)

        def close(self):
            pass

    cable = InterruptedCable()
    status = main(['--interface', 'enxtest', '--report', str(report_path),
                   '--duration', '60'], interface_root=tmp_path,
                  transport_factory=lambda name: cable, clock=cable.clock)
    report = json.loads(report_path.read_text())
    assert status == 1
    assert report['passed'] is report['completed'] is False
    assert report['sent'] == 3
    assert report['received'] == 2
    assert report['active_seconds'] == 0.25
    assert report['errors'] == ['Test interrupted']


def test_duration_with_no_sent_frames_cannot_pass():
    cable = TimedCable()
    times = iter([0.0, 2.0])

    def jumped_clock():
        return next(times, 2.0)

    report = run_test(cable, bytes.fromhex('102030405060'), 42,
                      duration_seconds=0.75, clock=jumped_clock)
    assert report['sent'] == report['received'] == 0
    assert report['passed'] is False


def test_sustained_transport_failure_retains_partial_report_and_cannot_pass():
    class BrokenCable(TimedCable):
        def receive(self, timeout):
            if len(self.sent) == 3:
                raise OSError('USB adapter disconnected')
            return super().receive(timeout)

    cable = BrokenCable()
    report = run_test(cable, bytes.fromhex('102030405060'), 42,
                      duration_seconds=60, clock=cable.clock)
    assert report['sent'] == 3
    assert report['received'] == 2
    assert report['passed'] is report['completed'] is False
    assert report['errors'] == ['OSError: USB adapter disconnected']
