"""Raw Ethernet correctness test; frame sizes exclude the Ethernet FCS."""

import argparse
import json
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
             per_size=200, timeout=1.0, clock=time.monotonic) -> dict:
    """Run a stop-and-wait test through a send/receive Ethernet transport.

    receive(timeout) returns (frame bytes, Linux packet type), or None on timeout.
    Every anomalous test reply fails the run, even if the correct reply follows.
    """
    if per_size < 1 or not sizes or timeout <= 0:
        raise ValueError("Positive frame count, sizes, and timeout are required")
    report = dict(run_id=f"{run_id:016x}", expected=len(sizes) * per_size,
                  sent=0, received=0, missing=0, duplicate=0, corrupt=0,
                  stale=0, unexpected=0, ignored=0, errors=[])
    replies: dict[int, bytes] = {}
    accepted = set()
    started = clock()

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
        if sequence not in replies:
            report["unexpected"] += 1
        elif frame != replies[sequence]:
            report["corrupt"] += 1
        elif sequence in accepted:
            report["duplicate"] += 1
        elif sequence == current:
            accepted.add(sequence)
            report["received"] += 1
            return True
        else:
            report["unexpected"] += 1
        return False

    try:
        for size in sizes:
            for _ in range(per_size):
                sequence = report["sent"]
                frame = make_frame(host_mac, run_id, sequence, size)
                replies[sequence] = frame[6:12] + frame[:6] + frame[12:]
                transport.send(frame)
                report["sent"] += 1
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
                    report["missing"] += 1
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
    except (OSError, ValueError) as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    report["elapsed_seconds"] = round(clock() - started, 6)
    report["passed"] = (report["received"] == report["expected"]
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


def main(argv=None, *, interface_root=Path("/sys/class/net"), transport_factory=RawEthernet) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True, help="Explicit USB Ethernet interface")
    parser.add_argument("--report", required=True, type=Path, help="JSON result artifact")
    parser.add_argument("--run-id", type=lambda value: int(value, 0),
                        help="Optional unsigned 64-bit run identifier (default: random)")
    args = parser.parse_args(argv)
    run_id = args.run_id if args.run_id is not None else secrets.randbits(64)
    report = dict(interface=args.interface, passed=False, errors=[])
    transport = None
    exit_code = 2
    try:
        if not 0 <= run_id < 1 << 64:
            raise ValueError("Run identifier must be an unsigned 64-bit integer")
        mac = inspect_interface(args.interface, interface_root)
        transport = transport_factory(args.interface)
        report.update(run_test(transport, mac, run_id))
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
    report["frames_per_size"] = 200
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
    for reported_error in report["errors"]:
        print(reported_error)
    print(f"Report: {args.report}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
