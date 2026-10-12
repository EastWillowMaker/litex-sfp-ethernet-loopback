"""Raw Ethernet correctness test; frame sizes exclude the Ethernet FCS."""

import argparse
import json
import math
from pathlib import Path
import secrets
import socket
import time
from typing import Protocol

BOARD_MAC = bytes.fromhex("020000000002")
ETHERTYPE = bytes.fromhex("88b5")
MARKER = b"LXLP"
FRAME_SIZES = (60, 128, 256, 512, 1514)


class EthernetTransport(Protocol):
    def send(self, frame: bytes) -> None: ...

    def receive(self, timeout: float) -> tuple[bytes, int] | None: ...


def make_frame(host_mac: bytes, run_id: int, sequence: int, size: int) -> bytes:
    """Create the specified test frame with a deterministic varying payload."""
    if len(host_mac) != 6 or not 60 <= size <= 1514:
        raise ValueError("Expected a six-byte MAC and frame size from 60 to 1514")
    return (
        BOARD_MAC + host_mac + ETHERTYPE + MARKER
        + run_id.to_bytes(8, "big") + sequence.to_bytes(4, "big")
        + bytes((sequence + offset * 37) & 255 for offset in range(size - 30))
    )


def run_test(transport: EthernetTransport, host_mac: bytes, run_id: int, *, sizes=FRAME_SIZES,
             per_size=200, timeout=1.0, duration_seconds=None, clock=time.monotonic) -> dict:
    """Verify frames with one outstanding request; optionally run for a duration.

    receive(timeout) returns (frame bytes, Linux packet type), or None on timeout.
    Timed runs cycle through sizes; fixed runs send per_size frames at each size.
    Finish the outstanding request after the sending deadline, then observe late
    duplicates for one timeout. Rates measure this stop-and-wait host workload.
    """
    if per_size < 1 or not sizes or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Positive frame count, sizes, and finite timeout are required")
    if duration_seconds is not None and (
        not math.isfinite(duration_seconds) or duration_seconds <= 0
    ):
        raise ValueError("Duration must be a finite positive number of seconds")
    timed = duration_seconds is not None
    report = dict(run_id=f"{run_id:016x}", expected=0 if timed else len(sizes) * per_size,
                  sent=0, received=0, missing=0, duplicate=0, corrupt=0,
                  stale=0, unexpected=0, ignored=0, errors=[],
                  mode="sustained_correctness" if timed else "fixed_correctness",
                  requested_duration_seconds=duration_seconds,
                  sent_bytes=0, received_bytes=0,
                  frames_by_size={str(size): 0 for size in sizes},
                  throughput_basis="Stop-and-wait returned Ethernet frame bytes excluding FCS, preamble and gap; "
                                   "measured host workload, not line capacity")
    # Successful requests need no history: regenerate their exact bytes from the
    # sequence. Retain only timed-out sequence numbers, whose growth is bounded
    # by receive timeouts during a duration run (about 60 entries in 60 seconds).
    missing_sequences: set[int] = set()
    started = clock()
    active_seconds = 0.0

    def frame_size(sequence):
        return sizes[sequence % len(sizes) if timed else sequence // per_size]

    def observe(packet, current):
        frame, packet_type = packet
        if packet_type == 4 or frame[12:14] != ETHERTYPE:
            report["ignored"] += 1
            return False
        if len(frame) < 30 or frame[14:18] != MARKER:
            report["corrupt"] += 1
            return False
        if int.from_bytes(frame[18:26], "big") != run_id:
            report["stale"] += 1
            return False
        sequence = int.from_bytes(frame[26:30], "big")
        if sequence >= report["sent"]:
            report["unexpected"] += 1
            return False
        request = make_frame(host_mac, run_id, sequence, frame_size(sequence))
        expected = request[6:12] + request[:6] + request[12:]
        if frame != expected:
            report["corrupt"] += 1
        elif sequence in missing_sequences:
            report["unexpected"] += 1
        elif sequence == current:
            report["received"] += 1
            report["received_bytes"] += len(frame)
            return True
        else:
            report["duplicate"] += 1
        return False

    try:
        while (clock() - started < duration_seconds if timed
               else report["sent"] < report["expected"]):
            sequence = report["sent"]
            size = frame_size(sequence)
            frame = make_frame(host_mac, run_id, sequence, size)
            transport.send(frame)
            report["sent"] += 1
            report["sent_bytes"] += size
            report["frames_by_size"][str(size)] += 1
            deadline = clock() + timeout
            matched = False
            while True:
                remaining = deadline - clock()
                if remaining <= 0:
                    break
                packet = transport.receive(remaining)
                if packet is None:
                    break
                if observe(packet, sequence):
                    matched = True
                    break
            if not matched:
                missing_sequences.add(sequence)
                report["missing"] += 1
        active_seconds = clock() - started
        # Observe late replies/duplicates after the final successful frame too.
        deadline = clock() + timeout
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            packet = transport.receive(remaining)
            if packet is None:
                break
            observe(packet, None)
    except (OSError, ValueError, OverflowError) as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
        if active_seconds == 0:
            active_seconds = clock() - started
    except KeyboardInterrupt:
        report["errors"].append("Test interrupted")
        if active_seconds == 0:
            active_seconds = clock() - started
    if timed:
        report["expected"] = report["sent"]
    report["active_seconds"] = round(active_seconds, 6)
    report["elapsed_seconds"] = round(clock() - started, 6)
    report["completed"] = (active_seconds >= duration_seconds if timed
                           else report["sent"] == report["expected"])
    report["received_mbps"] = round(report["received_bytes"] * 8 / active_seconds / 1e6, 6) if active_seconds > 0 else 0.0
    report["received_frames_per_second"] = round(report["received"] / active_seconds, 6) if active_seconds > 0 else 0.0
    report["passed"] = (report["completed"] and report["sent"] > 0
                        and report["received"] == report["expected"]
                        and not report["errors"]
                        and not any(report[key] for key in
                                    ("missing", "duplicate", "corrupt", "stale", "unexpected")))
    return report


def inspect_interface(name: str, root=Path("/sys/class/net")) -> bytes:
    """Require an active USB wired Ethernet interface for the isolated test."""
    if not name or "/" in name or name in (".", ".."):
        raise ValueError("Invalid Ethernet interface name")
    interface = Path(root) / name
    if not interface.is_dir():
        raise ValueError(f"Ethernet interface {name} does not exist")
    if (interface / "type").read_text().strip() != "1" or any(
        (interface / item).exists() for item in ("wireless", "phy80211")
    ):
        raise ValueError(f"{name} is not a wired Ethernet interface")
    device = (interface / "device").resolve()
    if not any(part.startswith("usb") for part in device.parts):
        raise ValueError(f"{name} is not a USB Ethernet interface")
    if not int((interface / "flags").read_text().strip(), 16) & 1:
        raise ValueError(f"{name} is down; bring the interface up before testing")
    if (interface / "carrier").read_text().strip() != "1":
        raise ValueError(f"{name} has no Ethernet carrier")
    if int((interface / "mtu").read_text()) < 1500:
        raise ValueError(f"{name} requires an MTU of at least 1500")
    mac = bytes.fromhex((interface / "address").read_text().strip().replace(":", ""))
    if len(mac) != 6 or mac[0] & 1 or mac == bytes(6):
        raise ValueError(f"{name} has an invalid source MAC")
    return mac


class RawEthernet:
    """Linux AF_PACKET adapter. Requires root or CAP_NET_RAW; no IP is needed."""

    def __init__(self, interface: str):
        self.socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        try:
            self.socket.bind((interface, 0))
        except BaseException:
            self.socket.close()
            raise

    def send(self, frame: bytes) -> None:
        if self.socket.send(frame) != len(frame):
            raise OSError("Ethernet socket sent a partial frame")

    def receive(self, timeout: float) -> tuple[bytes, int] | None:
        self.socket.settimeout(timeout)
        try:
            frame, address = self.socket.recvfrom(65535)
        except TimeoutError:
            return None
        return frame, address[2]

    def close(self) -> None:
        self.socket.close()


def main(argv=None, *, interface_root=Path("/sys/class/net"), transport_factory=RawEthernet,
         clock=time.monotonic) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True, help="Explicit USB Ethernet interface")
    parser.add_argument("--report", required=True, type=Path, help="JSON result artifact")
    parser.add_argument("--run-id", type=lambda value: int(value, 0),
                        help="Optional unsigned 64-bit run identifier (default: random)")
    parser.add_argument("--duration", type=float,
                        help="Run sustained stop-and-wait correctness for this many seconds")
    args = parser.parse_args(argv)
    run_id = args.run_id if args.run_id is not None else secrets.randbits(64)
    report = dict(interface=args.interface, passed=False, completed=False, errors=[],
                  mode="sustained_correctness" if args.duration is not None else "fixed_correctness",
                  requested_duration_seconds=None)
    transport = None
    exit_code = 2
    try:
        if not 0 <= run_id < 1 << 64:
            raise ValueError("Run identifier must be an unsigned 64-bit integer")
        if args.duration is not None:
            if not math.isfinite(args.duration) or args.duration <= 0:
                raise ValueError("Duration must be a finite positive number of seconds")
            report["requested_duration_seconds"] = args.duration
        mac = inspect_interface(args.interface, interface_root)
        transport = transport_factory(args.interface)
        report.update(run_test(transport, mac, run_id, duration_seconds=args.duration, clock=clock))
        report["host_mac"] = mac.hex(":")
        exit_code = 0 if report["passed"] else 1
    except (OSError, ValueError) as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    except KeyboardInterrupt:
        report["errors"].append("Test interrupted")
    finally:
        if transport is not None:
            try:
                transport.close()
            except OSError as error:
                report["errors"].append(f"{type(error).__name__}: {error}")
                report["passed"] = False
                exit_code = 2
    report["timestamp_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report["frame_sizes"] = list(FRAME_SIZES)
    report["frames_per_size"] = None if args.duration is not None else 200
    report["timeout_seconds"] = 1.0
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    except OSError as error:
        print(f"FAIL: cannot write result artifact: {error}")
        return 2
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {args.interface}; "
          f"received {report.get('received', 0)}/{report.get('expected', 1000)}; "
          + ", ".join(f"{key}={report.get(key, 0)}" for key in
                      ("missing", "duplicate", "corrupt", "stale", "unexpected")))
    if "received_mbps" in report:
        print(f"Measured stop-and-wait workload: {report['received_frames_per_second']} frames/s, "
              f"{report['received_mbps']} Mbps of returned Ethernet frame bytes "
              "(excludes FCS, preamble and gap; not line capacity)")
    for reported_error in report["errors"]:
        print(reported_error)
    print(f"Report: {args.report}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
