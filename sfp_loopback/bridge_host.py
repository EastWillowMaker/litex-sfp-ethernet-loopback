"""Inspect, isolate, test, and restore two NICs for the Dual-SFP bridge.

Run with ``python -m sfp_loopback.bridge_host --help``.  Setup is deliberately
separate from testing and requires ``--confirm-isolation``.  The raw probes
use isolated network namespaces, a unique run identifier, paced frames, and
destination-side captures that exclude Linux PACKET_OUTGOING copies.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Protocol, Sequence

from sfp_loopback.bridge_diagnostics import read_bridge_status
from sfp_loopback.diagnostics import BoundedUART


ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_IPV6 = 0x86DD
PROBE_MARKER = b"BRDG"
IPV4_ADDRESSES = {"left": "10.77.0.1", "right": "10.77.0.2"}
IPV6_ADDRESSES = {"left": "2001:db8:77::1", "right": "2001:db8:77::2"}
DEFAULT_STATE = Path("artifacts/bridge-host/state.json")
DEFAULT_OUTPUT = Path("artifacts/bridge-host/latest")
DEFAULT_PORT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
DEFAULT_CSR_CSV = Path("build/sfp_bridge500/csr.csv")
PACKET_OUTGOING = getattr(socket, "PACKET_OUTGOING", 4)
COUNTER_MODULUS = 1 << 32


class SafetyError(RuntimeError):
    """An operator request failed a pre-mutation isolation check."""


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        timeout: float | None = 30,
    ) -> subprocess.CompletedProcess[str]: ...

    def popen(self, argv: Sequence[str], **kwargs: Any) -> Any: ...


class SystemRunner:
    """Subprocess boundary used by the operator commands and their tests."""

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        timeout: float | None = 30,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=check,
            timeout=timeout,
            capture_output=True,
            text=True,
        )

    def popen(self, argv: Sequence[str], **kwargs: Any) -> Any:
        return subprocess.Popen(list(argv), **kwargs)


def _stop_process(process: Any, *, timeout: float = 2) -> int:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)
    return int(process.returncode if process.returncode is not None else -1)


def _resolve_iperf3(path: str | None, program_lookup=shutil.which) -> str | None:
    if path is None:
        detected = program_lookup("iperf3")
        return str(Path(detected).absolute()) if detected else None
    executable = Path(path).expanduser()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"iperf3 path is not an executable file: {path}")
    return str(executable.absolute())


def _checksum(data: bytes) -> int:
    if len(data) & 1:
        data += b"\x00"
    words = struct.unpack(f"!{len(data) // 2}H", data)
    total = sum(words)
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _udp_checksum_ipv6(
    source: ipaddress.IPv6Address,
    destination: ipaddress.IPv6Address,
    udp: bytes,
) -> int:
    pseudo_header = (
        source.packed
        + destination.packed
        + struct.pack("!I3xB", len(udp), socket.IPPROTO_UDP)
    )
    checksum = _checksum(pseudo_header + udp)
    return checksum or 0xFFFF


def build_probe_frame(
    source_mac: bytes,
    destination_mac: bytes,
    source_ip: str,
    destination_ip: str,
    ethertype: int,
    *,
    run_id: int,
    sequence: int,
) -> bytes:
    """Build a valid UDP Ethernet probe whose payload identifies this run."""
    if len(source_mac) != 6 or len(destination_mac) != 6:
        raise ValueError("Ethernet addresses must each be six bytes")
    if not 0 <= run_id < 1 << 64 or not 0 <= sequence < 1 << 32:
        raise ValueError("Probe run identifier or sequence is out of range")
    application = PROBE_MARKER + run_id.to_bytes(8, "big") + sequence.to_bytes(4, "big") + b"\x01\x02"
    source_port = 40000 + sequence % 20000
    destination_port = 4444
    udp_length = 8 + len(application)
    udp = struct.pack("!HHHH", source_port, destination_port, udp_length, 0) + application

    if ethertype == ETHERTYPE_IPV4:
        source = ipaddress.IPv4Address(source_ip)
        destination = ipaddress.IPv4Address(destination_ip)
        ip_length = 20 + len(udp)
        ip_header = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            ip_length,
            sequence & 0xFFFF,
            0x4000,
            64,
            socket.IPPROTO_UDP,
            0,
            source.packed,
            destination.packed,
        )
        ip_header = ip_header[:10] + struct.pack("!H", _checksum(ip_header)) + ip_header[12:]
        network = ip_header + udp
    elif ethertype == ETHERTYPE_IPV6:
        source6 = ipaddress.IPv6Address(source_ip)
        destination6 = ipaddress.IPv6Address(destination_ip)
        udp_checksum = _udp_checksum_ipv6(source6, destination6, udp)
        udp = udp[:6] + struct.pack("!H", udp_checksum) + udp[8:]
        network = struct.pack(
            "!IHBB16s16s",
            6 << 28,
            len(udp),
            socket.IPPROTO_UDP,
            64,
            source6.packed,
            destination6.packed,
        ) + udp
    else:
        raise ValueError("Only IPv4 and IPv6 bridge probes are supported")
    return destination_mac + source_mac + struct.pack("!H", ethertype) + network


def _probe_identity(frame: bytes) -> tuple[int, int] | None:
    if len(frame) < 14:
        return None
    ethertype = int.from_bytes(frame[12:14], "big")
    payload_offset = {ETHERTYPE_IPV4: 42, ETHERTYPE_IPV6: 62}.get(ethertype)
    if payload_offset is None or len(frame) < payload_offset + 16:
        return None
    if frame[payload_offset : payload_offset + 4] != PROBE_MARKER:
        return None
    run_id = int.from_bytes(frame[payload_offset + 4 : payload_offset + 12], "big")
    sequence = int.from_bytes(frame[payload_offset + 12 : payload_offset + 16], "big")
    return run_id, sequence


def _legitimate_arp(frame: bytes) -> bool:
    """Allow well-formed Ethernet/IPv4 ARP control traffic during a capture."""
    if len(frame) < 42 or int.from_bytes(frame[12:14], "big") != 0x0806:
        return False
    hardware_type, protocol_type, hardware_len, protocol_len, operation = struct.unpack(
        "!HHBBH", frame[14:22]
    )
    return (
        hardware_type == 1
        and protocol_type == ETHERTYPE_IPV4
        and hardware_len == 6
        and protocol_len == 4
        and operation in (1, 2)
    )


def evaluate_packet_capture(
    expected_frames: Sequence[bytes],
    captured_packets: Sequence[tuple[bytes, int]],
    *,
    forwarding_expected: bool,
) -> dict[str, Any]:
    """Evaluate destination-side packets; outgoing loopback copies never count."""
    if not expected_frames:
        raise ValueError("At least one expected probe frame is required")
    identities = {_probe_identity(frame): frame for frame in expected_frames}
    if None in identities or len(identities) != len(expected_frames):
        raise ValueError("Expected probe frames must have unique valid identities")
    run_id = next(iter(identities))[0]  # type: ignore[index]
    reference_frame = expected_frames[0]
    expected_peer_pair = (reference_frame[6:12], reference_frame[:6])
    expected_ethertype = int.from_bytes(reference_frame[12:14], "big")
    seen: set[tuple[int, int]] = set()
    report: dict[str, Any] = {
        "sent": len(expected_frames),
        "forwarded": 0,
        "unique_forwarded": 0,
        "missing": 0,
        "duplicates": 0,
        "stale": 0,
        "unexpected": 0,
        "unexpected_forwarded": 0,
        "malformed_peer_frames": 0,
        "ignored_outgoing": 0,
        "forwarding_expected": forwarding_expected,
    }
    for frame, packet_type in captured_packets:
        if packet_type == PACKET_OUTGOING:
            report["ignored_outgoing"] += 1
            continue
        identity = _probe_identity(frame)
        same_peer_pair = (
            len(frame) >= 12 and (frame[6:12], frame[:6]) == expected_peer_pair
        )
        if same_peer_pair and _legitimate_arp(frame):
            continue
        if identity is None:
            if same_peer_pair:
                report["malformed_peer_frames"] += 1
                report["unexpected"] += 1
                if not forwarding_expected or expected_ethertype == ETHERTYPE_IPV6:
                    report["forwarded"] += 1
                    report["unexpected_forwarded"] += 1
            continue
        if identity[0] != run_id:
            report["stale"] += 1
            if same_peer_pair and (
                not forwarding_expected or expected_ethertype == ETHERTYPE_IPV6
            ):
                report["forwarded"] += 1
                report["unexpected_forwarded"] += 1
            continue
        report["forwarded"] += 1
        if not forwarding_expected:
            report["unexpected_forwarded"] += 1
            continue
        expected_frame = identities.get(identity)
        if expected_frame is None or frame != expected_frame:
            report["unexpected"] += 1
        elif identity in seen:
            report["duplicates"] += 1
        else:
            seen.add(identity)
            report["unique_forwarded"] += 1
    if forwarding_expected:
        report["missing"] = len(expected_frames) - report["unique_forwarded"]
    report["passed"] = (
        report["forwarded"] == 0
        if not forwarding_expected
        else report["missing"] == 0
        and report["duplicates"] == 0
        and report["unexpected"] == 0
        and report["stale"] == 0
    )
    return report


class RawEthernet:
    """Linux AF_PACKET endpoint bound to one namespace-local interface."""

    def __init__(self, interface: str):
        self.socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        try:
            self.socket.bind((interface, 0))
        except BaseException:
            self.socket.close()
            raise

    def send(self, frame: bytes) -> None:
        if self.socket.send(frame) != len(frame):
            raise OSError("Raw Ethernet socket sent a partial frame")

    def receive(self, timeout: float) -> tuple[bytes, int] | None:
        self.socket.settimeout(timeout)
        try:
            frame, address = self.socket.recvfrom(65535)
        except TimeoutError:
            return None
        return frame, int(address[2])

    def close(self) -> None:
        self.socket.close()


def _write_pcap_header(stream: Any) -> None:
    stream.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))


def _write_pcap_packet(stream: Any, packet: bytes) -> None:
    timestamp = time.time()
    seconds = int(timestamp)
    microseconds = int((timestamp - seconds) * 1_000_000)
    stream.write(struct.pack("<IIII", seconds, microseconds, len(packet), len(packet)))
    stream.write(packet)


def _capture_worker(
    interface: str,
    frames_path: Path,
    pcap_path: Path,
    ready_path: Path,
    result_path: Path,
    *,
    listen_seconds: float,
    forwarding_expected: bool,
) -> int:
    frames = [bytes.fromhex(frame) for frame in json.loads(frames_path.read_text())]
    if not frames:
        raise ValueError("Capture worker received no expected frames")
    identity = _probe_identity(frames[0])
    if identity is None:
        raise ValueError("Capture worker received an invalid probe frame")
    run_id = identity[0]
    candidate_packets: list[tuple[bytes, int]] = []
    transport = RawEthernet(interface)
    try:
        with pcap_path.open("wb") as pcap:
            _write_pcap_header(pcap)
            ready_path.write_text("ready\n", encoding="ascii")
            deadline = time.monotonic() + listen_seconds
            while time.monotonic() < deadline:
                packet = transport.receive(max(0.001, deadline - time.monotonic()))
                if packet is None:
                    continue
                frame, packet_type = packet
                if packet_type != PACKET_OUTGOING:
                    _write_pcap_packet(pcap, frame)
                candidate_packets.append((frame, packet_type))
    finally:
        transport.close()
    report = evaluate_packet_capture(
        frames, candidate_packets, forwarding_expected=forwarding_expected
    )
    result_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


def _send_worker(interface: str, frames_path: Path, interval: float) -> int:
    frames = [bytes.fromhex(frame) for frame in json.loads(frames_path.read_text())]
    report: dict[str, Any] = {"sent": 0, "errors": []}
    transport = None
    try:
        transport = RawEthernet(interface)
        for index, frame in enumerate(frames):
            transport.send(frame)
            report["sent"] += 1
            if index + 1 < len(frames):
                time.sleep(interval)
    except (OSError, ValueError) as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        if transport is not None:
            transport.close()
    print(json.dumps(report))
    return 0 if report["sent"] == len(frames) else 1


def _run_ip_json(runner: Runner, args: Sequence[str]) -> Any:
    result = runner.run(["ip", "-j", *args])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"ip returned invalid JSON for {' '.join(args)}") from error


def _nm_state(runner: Runner, interface: str) -> dict[str, Any]:
    try:
        result = runner.run(
            ["nmcli", "-g", "GENERAL.NM-MANAGED,GENERAL.CONNECTION", "device", "show", interface],
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "managed": None, "connection": None}
    if result.returncode != 0:
        return {"available": False, "managed": None, "connection": None}
    lines = result.stdout.splitlines()
    managed = lines[0].strip().lower() if lines else ""
    connection = lines[1].strip() if len(lines) > 1 else ""
    return {
        "available": True,
        "managed": managed == "yes" if managed in ("yes", "no") else None,
        "connection": None if not connection or connection == "--" else connection,
    }


def _ssh_interfaces(runner: Runner, env: dict[str, str]) -> set[str]:
    connection = env.get("SSH_CONNECTION", "").split()
    client = connection[0] if connection else env.get("SSH_CLIENT", "").split()[:1]
    if isinstance(client, list):
        client = client[0] if client else ""
    if not client:
        return set()
    try:
        address = ipaddress.ip_address(str(client))
        family = ["-6"] if address.version == 6 else []
        routes = _run_ip_json(runner, [*family, "route", "get", str(client)])
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
        raise SafetyError(f"Cannot verify the active SSH interface; refusing setup: {error}") from error
    devices = {str(route["dev"]) for route in routes if route.get("dev")}
    if not devices:
        raise SafetyError("Cannot resolve the active SSH interface; refusing setup")
    return devices


def _physical_interfaces(sysfs_root: Path) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    if not sysfs_root.exists():
        return candidates
    for interface in sorted(sysfs_root.iterdir()):
        try:
            if (interface / "wireless").exists() or (interface / "phy80211").exists():
                continue
            if (interface / "type").read_text(encoding="ascii").strip() != "1":
                continue
            device = (interface / "device").resolve(strict=True)
            mac = (interface / "address").read_text(encoding="ascii").strip()
            layers = [
                item.name
                for item in interface.iterdir()
                if item.name == "master" or item.name.startswith(("upper_", "lower_"))
            ]
        except (OSError, RuntimeError, ValueError):
            continue
        try:
            carrier_text = (interface / "carrier").read_text(encoding="ascii").strip()
            carrier: bool | None = carrier_text == "1" if carrier_text in ("0", "1") else None
        except OSError:
            carrier = None
        try:
            speed_text = (interface / "speed").read_text(encoding="ascii").strip()
            speed = int(speed_text) if speed_text.isdigit() else None
        except OSError:
            speed = None
        candidates[interface.name] = {
            "device": str(device),
            "carrier": carrier,
            "speed_mbps": speed,
            "mac": mac,
            "layers": layers,
        }
    return candidates


def inspect_host(
    runner: Runner,
    *,
    sysfs_root: Path = Path("/sys/class/net"),
    env: dict[str, str] | None = None,
    iperf3_path: str | None = None,
    program_lookup=shutil.which,
) -> dict[str, Any]:
    """Read the host network inventory without changing it."""
    environment = os.environ if env is None else env
    inventory = _read_host_inventory(
        runner, sysfs_root=sysfs_root, env=dict(environment)
    )
    physical = inventory["physical_interfaces"]
    usable: dict[str, dict[str, Any]] = {}
    for name, item in physical.items():
        interface_reasons: list[str] = []
        if name in inventory["default_route_interfaces"]:
            interface_reasons.append("default route")
        if name in inventory["ssh_route_interfaces"]:
            interface_reasons.append("active SSH route")
        if item["layers"]:
            interface_reasons.append("host link layer membership")
        if not item["carrier"] or item["speed_mbps"] != 1000:
            interface_reasons.append("no 1 Gbps carrier")
        if _address_entries(inventory["address_snapshot"], name):
            interface_reasons.append("configured address")
        if (
            _routes_for(inventory["routes"], name)
            and name not in inventory["default_route_interfaces"]
        ):
            interface_reasons.append("configured route")
        if _nm_state(runner, name)["connection"]:
            interface_reasons.append("active NetworkManager connection")
        item["isolation_ready"] = not interface_reasons
        item["isolation_reasons"] = interface_reasons
        if not interface_reasons:
            usable[name] = item
    distinct_devices = {item["device"] for item in usable.values()}
    iperf3 = _resolve_iperf3(iperf3_path, program_lookup)
    reasons = []
    if len(distinct_devices) < 2:
        reasons.append("Two independent unconfigured physical 1 Gbps Ethernet NICs are required")
    if not iperf3:
        reasons.append("iperf3 is not installed; throughput measurement will be blocked")
    return {
        "status": "READY" if not reasons else "BLOCKED",
        "physical_interfaces": physical,
        "default_route_interfaces": inventory["default_route_interfaces"],
        "ssh_route_interfaces": inventory["ssh_route_interfaces"],
        "iperf3": iperf3,
        "readiness_reasons": reasons,
        "note": "Read-only inventory; setup separately requires --confirm-isolation.",
    }


def _read_host_inventory(
    runner: Runner,
    *,
    sysfs_root: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    links = _run_ip_json(runner, ["link", "show"])
    addresses = _run_ip_json(runner, ["address", "show"])
    routes = [
        *_run_ip_json(runner, ["route", "show", "table", "all"]),
        *_run_ip_json(runner, ["-6", "route", "show", "table", "all"]),
    ]
    default_devices = sorted(
        {str(route["dev"]) for route in routes if route.get("dst") == "default" and route.get("dev")}
    )
    ssh_devices = sorted(_ssh_interfaces(runner, env))
    physical = _physical_interfaces(sysfs_root)
    address_devices = {
        str(link.get("ifname")): link
        for link in addresses
        if link.get("ifname")
    }
    return {
        "physical_interfaces": physical,
        "default_route_interfaces": default_devices,
        "ssh_route_interfaces": ssh_devices,
        "links": links,
        "address_snapshot": address_devices,
        "routes": routes,
    }


def _interface_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", value):
        raise SafetyError(f"Invalid interface name: {value!r}")
    return value


def _address_entries(address_snapshot: dict[str, Any], interface: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for info in address_snapshot.get(interface, {}).get("addr_info", []):
        local = info.get("local")
        scope = info.get("scope")
        if local and scope != "link":
            entries.append(info)
    return entries


def _routes_for(routes: Sequence[dict[str, Any]], interface: str) -> list[dict[str, Any]]:
    return [
        route
        for route in routes
        if route.get("dev") == interface
        and route.get("table") != "local"
        and not str(route.get("dst", "")).startswith("fe80::")
    ]


def _all_routes_for(routes: Sequence[dict[str, Any]], interface: str) -> list[dict[str, Any]]:
    return [route for route in routes if route.get("dev") == interface]


def _preflight_pair(
    left: str,
    right: str,
    inventory: dict[str, Any],
    runner: Runner,
) -> dict[str, Any]:
    left = _interface_name(left)
    right = _interface_name(right)
    if left == right:
        raise SafetyError("Choose two different physical Ethernet interfaces")
    physical = inventory["physical_interfaces"]
    for name in (left, right):
        if name not in physical:
            raise SafetyError(f"{name} is not an enumerated wired physical Ethernet interface")
        item = physical[name]
        if item["layers"]:
            raise SafetyError(f"Refusing to move {name}: it participates in host link layers {item['layers']}")
        if not item["carrier"] or item["speed_mbps"] != 1000:
            raise SafetyError(f"{name} must have carrier and negotiate 1000 Mbps before setup")
        if name in inventory["default_route_interfaces"]:
            raise SafetyError(f"Refusing to move {name}: it carries the host default route")
        if name in inventory["ssh_route_interfaces"]:
            raise SafetyError(f"Refusing to move {name}: it carries the active SSH connection")
        nm = _nm_state(runner, name)
        if nm["connection"]:
            raise SafetyError(f"Refusing to move {name}: NetworkManager has active connection {nm['connection']!r}")
        addresses = _address_entries(inventory["address_snapshot"], name)
        if addresses:
            raise SafetyError(f"Refusing to move {name}: it has configured non-link-local addresses")
        routes = _routes_for(inventory["routes"], name)
        if routes:
            raise SafetyError(f"Refusing to move {name}: it has configured non-link-local routes")
    if physical[left]["device"] == physical[right]["device"]:
        raise SafetyError("The selected interfaces share one physical device; two independent NICs are required")
    link_by_name = {link.get("ifname"): link for link in inventory["links"]}
    return {
        side: {
            "interface": name,
            "mac": physical[name]["mac"],
            "device": physical[name]["device"],
            "was_up": "UP" in link_by_name[name].get("flags", []),
            "mtu": link_by_name[name]["mtu"],
            "addresses": inventory["address_snapshot"].get(name, {}).get("addr_info", []),
            "routes": _all_routes_for(inventory["routes"], name),
            "network_manager": _nm_state(runner, name),
            "namespace": "",
            "test_ipv4": IPV4_ADDRESSES["left" if name == left else "right"],
        }
        for side, name in (("left", left), ("right", right))
    }


def _save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run_mutation(runner: Runner, argv: Sequence[str]) -> None:
    runner.run(argv, check=True, timeout=30)


def setup_pair(
    left: str,
    right: str,
    *,
    state_path: Path = DEFAULT_STATE,
    confirm_isolation: bool,
    runner: Runner,
    sysfs_root: Path = Path("/sys/class/net"),
    env: dict[str, str] | None = None,
    is_root: bool | None = None,
) -> dict[str, Any]:
    """Move a preflighted, unconfigured NIC pair into owned namespaces."""
    if not confirm_isolation:
        raise SafetyError("Setup requires the explicit --confirm-isolation option")
    root = os.geteuid() == 0 if is_root is None else is_root
    if not root:
        raise SafetyError("Setup requires root; rerun from a local root-capable terminal")
    if state_path.exists():
        raise SafetyError(f"State file already exists; restore or move it first: {state_path}")
    inventory = _read_host_inventory(
        runner,
        sysfs_root=sysfs_root,
        env=dict(os.environ if env is None else env),
    )
    snapshots = _preflight_pair(left, right, inventory, runner)
    namespaces_text = runner.run(["ip", "netns", "list"]).stdout
    token = secrets.token_hex(3)
    namespaces = {"left": f"bridge-l-{token}", "right": f"bridge-r-{token}"}
    if any(name in {line.split()[0] for line in namespaces_text.splitlines() if line.split()} for name in namespaces.values()):
        raise SafetyError("Generated namespace name already exists; retry setup")
    for side, name in namespaces.items():
        snapshots["left" if side == "left" else "right"]["namespace"] = name
    state: dict[str, Any] = {
        "schema": 1,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "owner_token": token,
        "namespaces": namespaces,
        "owned_namespaces": [],
        "interfaces": snapshots,
        "restored": False,
        "confirmation": "--confirm-isolation",
        "safety_snapshot": {
            "default_route_interfaces": inventory["default_route_interfaces"],
            "ssh_route_interfaces": inventory["ssh_route_interfaces"],
        },
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        claim_fd = os.open(state_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise SafetyError(f"Another setup claimed the state file; refusing to overwrite it: {state_path}") from error
    with os.fdopen(claim_fd, "w", encoding="utf-8") as stream:
        json.dump(state, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        for namespace in namespaces.values():
            _run_mutation(runner, ["ip", "netns", "add", namespace])
            state["owned_namespaces"].append(namespace)
            _save_json(state_path, state)
            _run_mutation(runner, ["ip", "-n", namespace, "link", "set", "lo", "up"])
        for side in ("left", "right"):
            record = snapshots[side]
            interface = record["interface"]
            nm = record["network_manager"]
            if nm["available"] and nm["managed"] is True:
                _run_mutation(runner, ["nmcli", "device", "set", interface, "managed", "no"])
            _run_mutation(runner, ["ip", "link", "set", "dev", interface, "down"])
            _run_mutation(runner, ["ip", "link", "set", "dev", interface, "netns", namespaces[side]])
            _run_mutation(runner, ["ip", "-n", namespaces[side], "link", "set", "dev", interface, "mtu", "1500"])
            _run_mutation(
                runner,
                ["ip", "-n", namespaces[side], "address", "add", f"{IPV4_ADDRESSES[side]}/24", "dev", interface],
            )
            _run_mutation(runner, ["ip", "-n", namespaces[side], "link", "set", "dev", interface, "up"])
            _save_json(state_path, state)
    except BaseException:
        try:
            restore_state(state, state_path=state_path, runner=runner, is_root=root)
        except BaseException:
            pass
        raise
    state["setup_complete"] = True
    _save_json(state_path, state)
    return state


def _namespace_links(runner: Runner, namespace: str) -> list[dict[str, Any]]:
    result = runner.run(["ip", "-n", namespace, "-j", "link", "show"], check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"could not inventory namespace {namespace}: {detail}")
    links = json.loads(result.stdout or "[]")
    if not isinstance(links, list) or any(not isinstance(link, dict) for link in links):
        raise RuntimeError(f"namespace {namespace} returned invalid link inventory")
    return links


def _has_root_interface(runner: Runner, interface: str) -> bool:
    result = runner.run(["ip", "-j", "link", "show", "dev", interface], check=False)
    return result.returncode == 0


def _root_link(runner: Runner, interface: str) -> dict[str, Any] | None:
    result = runner.run(["ip", "-j", "link", "show", "dev", interface], check=False)
    if result.returncode != 0:
        return None
    links = json.loads(result.stdout or "[]")
    return links[0] if links else None


def _link_local_addresses(addresses: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for address in addresses:
        try:
            is_link_local = ipaddress.ip_address(str(address.get("local", ""))).is_link_local
        except ValueError:
            continue
        if address.get("scope") == "link" and is_link_local:
            result.append(address)
    return result


def _restore_link_local_addresses(
    runner: Runner,
    interface: str,
    saved_addresses: Sequence[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    saved = _link_local_addresses(saved_addresses)
    if not saved:
        return errors
    current_links = _run_ip_json(runner, ["address", "show", "dev", interface])
    current = [
        address
        for link in current_links
        for address in link.get("addr_info", [])
    ]
    for address in saved:
        local = str(address["local"])
        prefix = int(address["prefixlen"])
        same_local = [item for item in current if item.get("local") == local]
        if any(int(item.get("prefixlen", -1)) == prefix for item in same_local):
            continue
        if same_local:
            errors.append(f"{interface}: link-local address {local} exists with a different prefix")
            continue
        ip = ipaddress.ip_address(local)
        family = ["-6"] if ip.version == 6 else []
        args = ["ip", *family, "address", "add", f"{local}/{prefix}", "dev", interface]
        saved_flags = address.get("flags", [])
        if address.get("noprefixroute") is True or (
            isinstance(saved_flags, list) and "noprefixroute" in saved_flags
        ):
            args.append("noprefixroute")
        try:
            _run_mutation(runner, args)
            current.append({"local": local, "prefixlen": prefix})
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(f"{interface}: could not restore link-local address {local}/{prefix}: {error}")
    return errors


def _is_link_local_route(route: dict[str, Any]) -> bool:
    if route.get("table") == "local" or route.get("type", "unicast") != "unicast":
        return False
    destination = route.get("dst")
    if not destination or destination == "default":
        return False
    try:
        return ipaddress.ip_network(str(destination), strict=False).network_address.is_link_local
    except ValueError:
        return False


def _route_signature(route: dict[str, Any]) -> tuple[Any, ...]:
    destination = str(route.get("dst", "default"))
    try:
        is_ipv6 = ipaddress.ip_network(destination, strict=False).version == 6
    except ValueError:
        is_ipv6 = False
    preference = route.get("pref", "medium") if is_ipv6 else route.get("pref")
    return (
        destination,
        _route_table(route),
        route.get("dev"),
        route.get("gateway"),
        route.get("prefsrc", route.get("src")),
        route.get("metric"),
        route.get("scope"),
        route.get("protocol", route.get("proto")),
        preference,
    )


def _route_table(route: dict[str, Any]) -> str:
    table = str(route.get("table", "main"))
    return "main" if table == "254" else table


def _restore_link_local_routes(
    runner: Runner,
    interface: str,
    saved_routes: Sequence[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    routes = [route for route in saved_routes if _is_link_local_route(route)]
    if not routes:
        return errors
    for route in routes:
        destination = str(route["dst"])
        version = ipaddress.ip_network(destination, strict=False).version
        family = ["-6"] if version == 6 else []

        def current_device_routes() -> list[dict[str, Any]]:
            current_routes = _run_ip_json(
                runner, [*family, "route", "show", "table", "all", "dev", interface]
            )
            # `ip route ... dev IFACE` filters the result but can omit `dev`
            # from each JSON object. Reapply that query context while keeping
            # route metadata for exact matching intact.
            return [
                item if item.get("dev") is not None else {**item, "dev": interface}
                for item in current_routes
            ]

        current = current_device_routes()
        signature = _route_signature(route)
        if any(_route_signature(item) == signature for item in current):
            continue
        conflicting = [
            item
            for item in current
            if item.get("dst", "default") == destination
            and _route_table(item) == _route_table(route)
        ]
        if conflicting:
            errors.append(
                f"{interface}: link-local route {destination} conflicts with an existing route; left unchanged"
            )
            continue
        operation = "append" if version == 6 else "add"
        args = ["ip", *family, "route", operation, destination]
        if route.get("gateway"):
            args.extend(("via", str(route["gateway"])))
        args.extend(("dev", interface))
        source = route.get("prefsrc", route.get("src"))
        if source:
            args.extend(("src", str(source)))
        if route.get("metric") is not None:
            args.extend(("metric", str(route["metric"])))
        table = route.get("table")
        if table is not None and str(table) not in ("main", "254"):
            args.extend(("table", str(table)))
        if route.get("scope"):
            args.extend(("scope", str(route["scope"])))
        if version == 6 and route.get("pref") is not None:
            args.extend(("pref", str(route["pref"])))
        protocol = route.get("protocol", route.get("proto"))
        if protocol:
            args.extend(("proto", str(protocol)))
        try:
            _run_mutation(runner, args)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            try:
                appeared = current_device_routes()
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as verify_error:
                appeared = []
                verification_detail = f"; route verification also failed: {verify_error}"
            else:
                verification_detail = ""
            if any(_route_signature(item) == signature for item in appeared):
                continue
            stderr = getattr(error, "stderr", None)
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            detail = str(error)
            if isinstance(stderr, str) and stderr.strip():
                detail = f"{detail}: {stderr.strip()}"
            errors.append(
                f"{interface}: could not restore link-local route {destination}: "
                f"{detail}{verification_detail}"
            )
    return errors


def restore_state(
    state: dict[str, Any],
    *,
    state_path: Path,
    runner: Runner,
    is_root: bool | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return only manifest-owned NICs and namespaces to their saved host state."""
    _validate_state(state)
    root = os.geteuid() == 0 if is_root is None else is_root
    if not root:
        raise SafetyError("Restore requires root")
    errors: list[str] = []
    interfaces = {record["interface"] for record in state["interfaces"].values()}
    saved_safety = state["safety_snapshot"]
    protected = set(saved_safety["default_route_interfaces"])
    protected.update(saved_safety["ssh_route_interfaces"])
    try:
        host_routes = [
            *_run_ip_json(runner, ["route", "show", "table", "all"]),
            *_run_ip_json(runner, ["-6", "route", "show", "table", "all"]),
        ]
        protected.update(
            str(route["dev"])
            for route in host_routes
            if route.get("dst") == "default" and route.get("dev")
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        errors.append(f"Could not verify current default routes; protected the selected interfaces: {error}")
        protected.update(interfaces)
    try:
        protected.update(_ssh_interfaces(runner, dict(os.environ if env is None else env)))
    except SafetyError as error:
        errors.append(f"Could not verify the active SSH interface; protected the selected interfaces: {error}")
        protected.update(interfaces)
    namespace_result = runner.run(["ip", "netns", "list"], check=False)
    if namespace_result.returncode != 0:
        errors.append("Could not inventory network namespaces; left all namespace-owned interfaces unchanged")
        state["restore_errors"] = errors
        state["restored"] = False
        state["restored_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_json(state_path, state)
        return state
    namespace_lines = namespace_result.stdout.splitlines()
    existing_namespaces = {line.split()[0] for line in namespace_lines if line.split()}
    owned_namespaces = set(state["owned_namespaces"])
    for side in ("left", "right"):
        record = state["interfaces"][side]
        interface = record["interface"]
        namespace = record["namespace"]
        if interface in protected:
            errors.append(f"{interface}: skipped restoration because it is or was a default-route/SSH interface")
            continue
        if namespace in owned_namespaces and namespace in existing_namespaces:
            try:
                namespace_links = _namespace_links(runner, namespace)
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
                errors.append(f"{namespace}: could not verify namespace links; left it unchanged: {error}")
                continue
            namespace_link = next(
                (link for link in namespace_links if link.get("ifname") == interface), None
            )
            if namespace_link is not None:
                root_link = _root_link(runner, interface)
                if root_link is not None:
                    errors.append(f"{interface}: skipped restoration because the name exists in both namespaces")
                    continue
                actual_mac = str(namespace_link.get("address", "")).lower()
                expected_mac = str(record["mac"]).lower()
                if actual_mac != expected_mac:
                    errors.append(
                        f"{interface}: skipped namespace cleanup because MAC {actual_mac or 'unknown'} "
                        f"does not match saved MAC {expected_mac}"
                    )
                    continue
                try:
                    current = _run_ip_json(
                        runner, ["-n", namespace, "address", "show", "dev", interface]
                    )
                    for link in current:
                        for info in link.get("addr_info", []):
                            if info.get("local") == record["test_ipv4"]:
                                _run_mutation(
                                    runner,
                                    [
                                        "ip", "-n", namespace, "address", "del",
                                        f"{info['local']}/{info['prefixlen']}", "dev", interface,
                                    ],
                                )
                    _run_mutation(
                        runner,
                        ["ip", "-n", namespace, "link", "set", "dev", interface, "netns", "1"],
                    )
                except (OSError, RuntimeError, subprocess.SubprocessError, KeyError, ValueError) as error:
                    errors.append(f"{interface}: could not return from {namespace}: {error}")
        root_link = _root_link(runner, interface)
        if root_link is None:
            errors.append(f"{interface}: interface is not present in the host namespace")
            continue
        actual_mac = str(root_link.get("address", "")).lower()
        expected_mac = str(record["mac"]).lower()
        if actual_mac != expected_mac:
            errors.append(
                f"{interface}: skipped host cleanup because MAC {actual_mac or 'unknown'} "
                f"does not match saved MAC {expected_mac}"
            )
            continue
        try:
            _run_mutation(runner, ["ip", "link", "set", "dev", interface, "mtu", str(record["mtu"])])
            _run_mutation(
                runner,
                ["ip", "link", "set", "dev", interface, "up" if record["was_up"] else "down"],
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(f"{interface}: could not restore link settings: {error}")
        try:
            errors.extend(
                _restore_link_local_addresses(runner, interface, record.get("addresses", []))
            )
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            errors.append(f"{interface}: could not restore link-local addresses: {error}")
        try:
            errors.extend(_restore_link_local_routes(runner, interface, record.get("routes", [])))
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            errors.append(f"{interface}: could not restore link-local routes: {error}")
        try:
            nm = record["network_manager"]
            if nm["available"] and nm["managed"] is not None:
                _run_mutation(
                    runner,
                    ["nmcli", "device", "set", interface, "managed", "yes" if nm["managed"] else "no"],
                )
        except (OSError, RuntimeError, subprocess.SubprocessError, KeyError) as error:
            errors.append(f"{interface}: could not restore NetworkManager state: {error}")
    for namespace in state["owned_namespaces"]:
        if namespace not in existing_namespaces:
            continue
        try:
            links = _namespace_links(runner, namespace)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            errors.append(f"{namespace}: could not verify contents; kept namespace: {error}")
            continue
        unexpected = [link.get("ifname") for link in links if link.get("ifname") != "lo"]
        if unexpected:
            errors.append(f"{namespace}: kept because it contains unexpected interfaces {unexpected}")
            continue
        try:
            _run_mutation(runner, ["ip", "netns", "del", namespace])
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(f"{namespace}: could not remove owned namespace: {error}")
    state["restore_errors"] = errors
    state["restored"] = not errors
    state["restored_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_json(state_path, state)
    return state


def _read_state(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    _validate_state(state)
    return state


def _validate_state(state: dict[str, Any]) -> None:
    if state.get("schema") != 1 or state.get("confirmation") != "--confirm-isolation":
        raise SafetyError("Unsupported or unconfirmed bridge host state file")
    owner = state.get("owner_token")
    namespaces = state.get("namespaces")
    owned_namespaces = state.get("owned_namespaces")
    interfaces = state.get("interfaces")
    if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{6}", owner):
        raise SafetyError("Bridge host state has an invalid ownership token")
    if not isinstance(namespaces, dict) or not isinstance(interfaces, dict):
        raise SafetyError("Bridge host state is missing namespace or interface ownership")
    if namespaces != {"left": f"bridge-l-{owner}", "right": f"bridge-r-{owner}"}:
        raise SafetyError("Bridge host state does not own the expected unique namespaces")
    if (
        not isinstance(owned_namespaces, list)
        or any(not isinstance(namespace, str) for namespace in owned_namespaces)
        or len(owned_namespaces) != len(set(owned_namespaces))
        or any(namespace not in namespaces.values() for namespace in owned_namespaces)
    ):
        raise SafetyError("Bridge host state has an invalid created-namespace ownership list")
    safety_snapshot = state.get("safety_snapshot")
    if not isinstance(safety_snapshot, dict) or any(
        not isinstance(safety_snapshot.get(key), list)
        or any(not isinstance(name, str) for name in safety_snapshot[key])
        for key in ("default_route_interfaces", "ssh_route_interfaces")
    ):
        raise SafetyError("Bridge host state has an invalid route/SSH safety snapshot")
    if set(interfaces) != {"left", "right"}:
        raise SafetyError("Bridge host state must describe exactly the left and right NICs")
    for side in ("left", "right"):
        record = interfaces[side]
        if not isinstance(record, dict):
            raise SafetyError(f"Bridge host state for {side} is invalid")
        _interface_name(str(record.get("interface", "")))
        if record.get("namespace") != namespaces[side]:
            raise SafetyError(f"Bridge host state for {side} does not match its owned namespace")
        if record.get("test_ipv4") != IPV4_ADDRESSES[side]:
            raise SafetyError(f"Bridge host state for {side} has an unexpected test address")
        mac = str(record.get("mac", "")).replace(":", "")
        if not re.fullmatch(r"[0-9a-fA-F]{12}", mac):
            raise SafetyError(f"Bridge host state for {side} has an invalid MAC address")
        if not isinstance(record.get("mtu"), int) or not isinstance(record.get("was_up"), bool):
            raise SafetyError(f"Bridge host state for {side} has invalid link settings")


def _read_hardware_status(port: str, csr_csv: Path) -> dict[str, Any]:
    with BoundedUART(port, csr_csv=str(csr_csv)) as bus:
        return read_bridge_status(bus)


def _counter_delta(before: int, after: int) -> int:
    return (int(after) - int(before)) % COUNTER_MODULUS


def _hardware_case_check(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    direction: str,
    ethertype: int,
    count: int,
) -> dict[str, Any]:
    before_path = before["forwarding"][direction]
    after_path = after["forwarding"][direction]
    deltas = {
        name: _counter_delta(before_path[name], after_path[name])
        for name in before_path
    }
    mac_deltas: dict[str, Any] = {}
    for index in range(2):
        port = f"sfp{index}"
        mac_deltas[port] = {
            field: _counter_delta(before["mac_rx"][port][field], after["mac_rx"][port][field])
            for field in before["mac_rx"][port]
        }
    if ethertype == ETHERTYPE_IPV4:
        requirements = {
            "rx_frames": deltas["rx_frames"] >= count,
            "tx_frames": deltas["tx_frames"] >= count,
            "ipv4_frames": deltas["ipv4_frames"] >= count,
        }
    else:
        allowlisted_tx = deltas["ipv4_frames"] + deltas["arp_frames"]
        requirements = {
            "ipv6_drops": deltas["ipv6_drops"] >= count,
            "tx_frames_explained_by_ipv4_or_arp": deltas["tx_frames"] <= allowlisted_tx,
        }
    requirements["no_path_errors"] = deltas["error_frames"] == 0
    requirements["no_path_overflows"] = deltas["overflow_frames"] == 0
    requirements["no_mac_errors_or_drops"] = all(
        value == 0 for port in mac_deltas.values() for value in port.values()
    )
    return {
        "direction": direction,
        "counter_deltas": deltas,
        "allowlisted_background_frames": {
            "ipv4_frames": deltas["ipv4_frames"],
            "arp_frames": deltas["arp_frames"],
            "maximum_explained_tx_frames": deltas["ipv4_frames"] + deltas["arp_frames"],
        },
        "mac_counter_deltas": mac_deltas,
        "requirements": requirements,
        "passed": all(requirements.values()),
    }


def _run_packet_case(
    runner: Runner,
    *,
    state: dict[str, Any],
    source_side: str,
    destination_side: str,
    ethertype: int,
    count: int,
    interval_seconds: float,
    output_dir: Path,
) -> dict[str, Any]:
    source = state["interfaces"][source_side]
    destination = state["interfaces"][destination_side]
    run_id = secrets.randbits(64)
    frames = [
        build_probe_frame(
            bytes.fromhex(source["mac"].replace(":", "")),
            bytes.fromhex(destination["mac"].replace(":", "")),
            IPV4_ADDRESSES[source_side] if ethertype == ETHERTYPE_IPV4 else IPV6_ADDRESSES[source_side],
            IPV4_ADDRESSES[destination_side] if ethertype == ETHERTYPE_IPV4 else IPV6_ADDRESSES[destination_side],
            ethertype,
            run_id=run_id,
            sequence=sequence,
        )
        for sequence in range(count)
    ]
    label = "ipv4" if ethertype == ETHERTYPE_IPV4 else "ipv6_drop"
    label = f"{source_side}_to_{destination_side}_{label}"
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_path = output_dir / f"{label}-frames.json"
    pcap_path = output_dir / f"{label}-rx.pcap"
    ready_path = output_dir / f"{label}-ready"
    capture_result_path = output_dir / f"{label}-capture.json"
    sender_result_path = output_dir / f"{label}-sender.json"
    log_path = output_dir / f"{label}-capture.log"
    for stale_path in (pcap_path, ready_path, capture_result_path, sender_result_path):
        try:
            stale_path.unlink()
        except FileNotFoundError:
            pass
    frames_path.write_text(json.dumps([frame.hex() for frame in frames]), encoding="utf-8")
    destination_ns = destination["namespace"]
    source_ns = source["namespace"]
    listen_seconds = max(3.0, count * interval_seconds + 2.0)
    capture_command = [
        "ip", "netns", "exec", destination_ns, sys.executable,
        "-m", "sfp_loopback.bridge_host", "_capture_worker",
        "--interface", destination["interface"],
        "--frames", str(frames_path.resolve()),
        "--pcap", str(pcap_path.resolve()),
        "--ready", str(ready_path.resolve()),
        "--result", str(capture_result_path.resolve()),
        "--listen-seconds", str(listen_seconds),
    ]
    if ethertype == ETHERTYPE_IPV6:
        capture_command.append("--expect-drop")
    sender_command = [
        "ip", "netns", "exec", source_ns, sys.executable,
        "-m", "sfp_loopback.bridge_host", "_send_worker",
        "--interface", source["interface"],
        "--frames", str(frames_path.resolve()),
        "--interval", str(interval_seconds),
    ]
    with log_path.open("w", encoding="utf-8") as log:
        receiver = runner.popen(capture_command, stdout=log, stderr=subprocess.STDOUT, text=True)
        sender_result: dict[str, Any] = {"sent": 0, "errors": []}
        try:
            deadline = time.monotonic() + 5
            while not ready_path.exists() and receiver.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if ready_path.exists():
                sent = runner.run(
                    sender_command,
                    check=False,
                    timeout=max(30, count * interval_seconds + 10),
                )
                sender_result_path.write_text(sent.stdout or "", encoding="utf-8")
                try:
                    sender_result = json.loads(sent.stdout)
                except json.JSONDecodeError:
                    sender_result["errors"].append(sent.stderr or f"sender exited {sent.returncode}")
            else:
                sender_result["errors"].append("destination capture did not become ready")
            try:
                receiver_code = receiver.wait(timeout=listen_seconds + 5)
            except subprocess.TimeoutExpired:
                receiver_code = _stop_process(receiver)
        finally:
            if receiver.poll() is None:
                _stop_process(receiver)
    try:
        capture_result = json.loads(capture_result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        capture_result = {
            "passed": False,
            "forwarded": 0,
            "missing": count if ethertype == ETHERTYPE_IPV4 else 0,
            "errors": [f"capture worker exited {receiver_code} without a valid report"],
        }
    host_passed = (
        sender_result.get("sent") == count
        and not sender_result.get("errors")
        and receiver_code == 0
        and capture_result.get("passed") is True
    )
    return {
        "source": source_side,
        "destination": destination_side,
        "ethertype": f"0x{ethertype:04x}",
        "run_id": f"{run_id:016x}",
        "frame_count": count,
        "pace_seconds": interval_seconds,
        "sender": sender_result,
        "capture": capture_result,
        "pcap": str(pcap_path),
        "host_passed": host_passed,
    }


def _ping_and_arp(
    runner: Runner,
    state: dict[str, Any],
    source_side: str,
    destination_side: str,
    output_dir: Path,
) -> dict[str, Any]:
    source = state["interfaces"][source_side]
    destination = state["interfaces"][destination_side]
    destination_ip = IPV4_ADDRESSES[destination_side]
    result = runner.run(
        [
            "ip", "netns", "exec", source["namespace"], "ping", "-n", "-c", "2", "-W", "2", destination_ip,
        ],
        check=False,
        timeout=8,
    )
    log = output_dir / f"{source_side}_to_{destination_side}_ping.log"
    log.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
    neighbor = runner.run(
        ["ip", "-n", source["namespace"], "neigh", "show", "to", destination_ip, "dev", source["interface"]],
        check=False,
        timeout=5,
    )
    neighbor_text = neighbor.stdout.strip()
    expected_mac = destination["mac"].lower()
    neighbor_ok = "lladdr" in neighbor_text and expected_mac in neighbor_text.lower() and "FAILED" not in neighbor_text
    return {
        "source": source_side,
        "destination": destination_side,
        "ping_returncode": result.returncode,
        "ping_log": str(log),
        "arp_neighbor": neighbor_text,
        "arp_resolved_expected_mac": neighbor_ok,
        "passed": result.returncode == 0 and neighbor_ok,
    }


def _run_iperf(
    runner: Runner,
    state: dict[str, Any],
    source_side: str,
    destination_side: str,
    duration: int,
    output_dir: Path,
    iperf3_path: str,
) -> dict[str, Any]:
    source = state["interfaces"][source_side]
    destination = state["interfaces"][destination_side]
    server_log_path = output_dir / f"iperf-{source_side}-to-{destination_side}-server.json"
    client_json_path = output_dir / f"iperf-{source_side}-to-{destination_side}.json"
    with server_log_path.open("w", encoding="utf-8") as server_log:
        server = runner.popen(
            [
                "ip", "netns", "exec", destination["namespace"], iperf3_path, "-s", "-1", "-J",
                "--bind", IPV4_ADDRESSES[destination_side],
            ],
            stdout=server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            time.sleep(0.2)
            client = runner.run(
                [
                    "ip", "netns", "exec", source["namespace"], iperf3_path, "-c",
                    IPV4_ADDRESSES[destination_side], "-J", "-t", str(duration),
                ],
                check=False,
                timeout=duration + 20,
            )
            try:
                server_code = server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_code = _stop_process(server)
        finally:
            if server.poll() is None:
                _stop_process(server)
    client_json_path.write_text(client.stdout or "", encoding="utf-8")
    try:
        response = json.loads(client.stdout)
    except json.JSONDecodeError:
        return {
            "source": source_side,
            "destination": destination_side,
            "status": "FAIL",
            "error": client.stderr or f"iperf3 client exited {client.returncode} without JSON",
            "json": str(client_json_path),
            "server_json": str(server_log_path),
        }
    end = response.get("end", {})
    received = end.get("sum_received", {})
    sent = end.get("sum_sent", {})
    bits_per_second = received.get("bits_per_second")
    return {
        "source": source_side,
        "destination": destination_side,
        "status": "PASS" if client.returncode == 0 and server_code == 0 and bits_per_second is not None else "FAIL",
        "throughput_mbps_received": round(bits_per_second / 1e6, 3) if bits_per_second is not None else None,
        "throughput_basis": "iperf3 end.sum_received.bits_per_second; measured TCP payload rate",
        "sender_bits_per_second": sent.get("bits_per_second"),
        "receiver_bits_per_second": bits_per_second,
        "client_returncode": client.returncode,
        "server_returncode": server_code,
        "json": str(client_json_path),
        "server_json": str(server_log_path),
    }


def run_bridge_test(
    state: dict[str, Any],
    *,
    output_dir: Path,
    runner: Runner,
    port: str = DEFAULT_PORT,
    csr_csv: Path = DEFAULT_CSR_CSV,
    status_reader=_read_hardware_status,
    iperf3_path: str | None = None,
    program_lookup=shutil.which,
    frame_count: int = 100,
    interval_seconds: float = 0.01,
    iperf_duration: int = 10,
) -> dict[str, Any]:
    """Run host correctness checks and collect independent bridge CSR evidence."""
    if state.get("restored"):
        raise SafetyError("The saved NIC pair has already been restored")
    if state.get("setup_complete") is not True:
        raise SafetyError("The saved NIC pair does not record a completed setup")
    if frame_count < 1 or not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("Frame count and pacing interval must be positive")
    iperf = _resolve_iperf3(iperf3_path, program_lookup)
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "state_file_owner": state.get("owner_token"),
        "host_result": {"status": "NOT RUN"},
        "hardware_evidence": {"status": "BLOCKED", "snapshots": [], "cases": []},
        "performance": {"status": "BLOCKED", "runs": []},
        "artifacts_directory": str(output_dir),
    }
    status_snapshots: list[dict[str, Any]] = report["hardware_evidence"]["snapshots"]

    def read_status(label: str) -> dict[str, Any] | None:
        try:
            value = status_reader(port, csr_csv)
            status_snapshots.append({"label": label, "status": value})
            return value
        except (OSError, TimeoutError, RuntimeError, ValueError, AttributeError, KeyError) as error:
            status_snapshots.append({"label": label, "error": f"{type(error).__name__}: {error}"})
            return None

    initial_status = read_status("before_host_tests")
    links_ready = False
    if initial_status is not None:
        links_ready = all(
            initial_status["links"][f"sfp{index}"]["link_up"]
            and initial_status["links"][f"sfp{index}"]["qpll_locked"]
            and initial_status["links"][f"sfp{index}"]["tx_ready"]
            and initial_status["links"][f"sfp{index}"]["rx_ready"]
            and not initial_status["links"][f"sfp{index}"]["loss_of_signal"]
            and not initial_status["links"][f"sfp{index}"]["control_tx_disable"]
            for index in range(2)
        )
    else:
        report["hardware_evidence"]["link_status"] = "UNAVAILABLE"
    if initial_status is not None:
        report["hardware_evidence"]["link_status"] = "PASS" if links_ready else "FAIL"

    ping_results = []
    for source_side, destination_side in (("left", "right"), ("right", "left")):
        try:
            ping_results.append(
                _ping_and_arp(runner, state, source_side, destination_side, output_dir)
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            ping_results.append(
                {
                    "source": source_side,
                    "destination": destination_side,
                    "passed": False,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    packet_results: list[dict[str, Any]] = []
    for direction, source_side, destination_side in (
        ("sfp0_to_sfp1", "left", "right"),
        ("sfp1_to_sfp0", "right", "left"),
    ):
        for ethertype in (ETHERTYPE_IPV4, ETHERTYPE_IPV6):
            label = f"{direction}_{ethertype:04x}"
            before = read_status(label + "_before")
            try:
                result = _run_packet_case(
                    runner,
                    state=state,
                    source_side=source_side,
                    destination_side=destination_side,
                    ethertype=ethertype,
                    count=frame_count,
                    interval_seconds=interval_seconds,
                    output_dir=output_dir,
                )
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                result = {
                    "source": source_side,
                    "destination": destination_side,
                    "ethertype": f"0x{ethertype:04x}",
                    "frame_count": frame_count,
                    "host_passed": False,
                    "error": f"{type(error).__name__}: {error}",
                }
            after = read_status(label + "_after")
            if before is not None and after is not None:
                hardware_case = _hardware_case_check(
                    before,
                    after,
                    direction=direction,
                    ethertype=ethertype,
                    count=frame_count,
                )
                result["hardware"] = hardware_case
                report["hardware_evidence"]["cases"].append(hardware_case)
            else:
                result["hardware"] = {"status": "BLOCKED", "reason": "CSR snapshot unavailable"}
            packet_results.append(result)

    host_passed = all(item["passed"] for item in ping_results) and all(
        item["host_passed"] for item in packet_results
    )
    host_failures = []
    if not host_passed:
        host_failures = [
            f"ARP/ping {item['source']} to {item['destination']} failed"
            for item in ping_results
            if not item["passed"]
        ] + [
            f"raw {item['ethertype']} {item['source']} to {item['destination']} failed"
            for item in packet_results
            if not item["host_passed"]
        ]
    report["host_result"] = {
        "status": "PASS" if host_passed else "FAIL",
        "arp_and_ping": ping_results,
        "raw_packet_cases": packet_results,
        "failures": host_failures,
    }
    all_hardware_cases = report["hardware_evidence"]["cases"]
    hardware_passed = (
        initial_status is not None
        and links_ready
        and len(all_hardware_cases) == 4
        and all(case["passed"] for case in all_hardware_cases)
    )
    report["hardware_evidence"]["status"] = (
        "PASS" if hardware_passed else "FAIL" if initial_status is not None and not links_ready
        or any(not case["passed"] for case in all_hardware_cases) else "BLOCKED"
    )

    if not iperf:
        report["performance"] = {
            "status": "BLOCKED",
            "iperf3_path": None,
            "reason": "iperf3 is not installed; no throughput measurement was performed",
            "runs": [],
        }
    elif not host_passed or not hardware_passed:
        failed_checks = [
            name
            for name, passed in (
                ("host_result", host_passed),
                ("hardware_evidence", hardware_passed),
            )
            if not passed
        ]
        report["performance"] = {
            "status": "BLOCKED",
            "iperf3_path": iperf,
            "reason": (
                "iperf3 not run because correctness did not pass: "
                + ", ".join(failed_checks)
            ),
            "runs": [],
        }
    else:
        runs = []
        for source_side, destination_side in (("left", "right"), ("right", "left")):
            try:
                runs.append(
                    _run_iperf(
                        runner, state, source_side, destination_side, iperf_duration, output_dir, iperf
                    )
                )
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                runs.append(
                    {
                        "source": source_side,
                        "destination": destination_side,
                        "status": "FAIL",
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
        report["performance"] = {
            "status": "PASS" if all(item["status"] == "PASS" for item in runs) else "FAIL",
            "iperf3_path": iperf,
            "runs": runs,
            "threshold_mbps": None,
            "threshold_note": "Throughput is recorded as measured; there is no 500 Mbps correctness gate.",
        }
    report["overall"] = (
        "FAIL"
        if report["host_result"]["status"] == "FAIL"
        or report["hardware_evidence"]["status"] == "FAIL"
        or report["performance"]["status"] == "FAIL"
        else "PASS"
        if report["host_result"]["status"] == "PASS"
        and report["hardware_evidence"]["status"] == "PASS"
        and report["performance"]["status"] == "PASS"
        else "BLOCKED"
    )
    _save_json(output_dir / "report.json", report)
    return report


def _print_report(report: dict[str, Any]) -> None:
    print(json.dumps(report, indent=2))


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
    sysfs_root: Path = Path("/sys/class/net"),
    env: dict[str, str] | None = None,
    is_root: bool | None = None,
    program_lookup=shutil.which,
    status_reader=_read_hardware_status,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="Read the host NIC and tool inventory")
    inspect_parser.add_argument("--json", type=Path, help="Optional inventory artifact")
    inspect_parser.add_argument("--iperf3", help="Use this executable instead of PATH lookup")
    setup_parser = subparsers.add_parser("setup", help="Isolate and configure two dedicated NICs")
    setup_parser.add_argument("--left-interface", required=True, help="NIC wired to SFP0; host address 10.77.0.1")
    setup_parser.add_argument("--right-interface", required=True, help="NIC wired to SFP1; host address 10.77.0.2")
    setup_parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    setup_parser.add_argument("--confirm-isolation", action="store_true", help="Confirm both NICs are dedicated and may be moved")
    restore_parser = subparsers.add_parser("restore", help="Return the manifest-owned NICs to saved host settings")
    restore_parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    test_parser = subparsers.add_parser("test", help="Run ARP, ping, raw IPv4/IPv6, and optional iperf tests")
    test_parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    test_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    test_parser.add_argument("--port", default=DEFAULT_PORT)
    test_parser.add_argument("--csr-csv", type=Path, default=DEFAULT_CSR_CSV)
    test_parser.add_argument("--frames", type=int, default=100)
    test_parser.add_argument("--pace-ms", type=float, default=10.0, help="Delay between raw frames; default 10 ms")
    test_parser.add_argument("--iperf-seconds", type=int, default=10)
    test_parser.add_argument("--iperf3", help="Use this executable inside both namespaces")
    worker_parser = subparsers.add_parser(
        "_capture_worker", help="Internal destination-side packet capture worker"
    )
    worker_parser.add_argument("--interface", required=True)
    worker_parser.add_argument("--frames", type=Path, required=True)
    worker_parser.add_argument("--pcap", type=Path, required=True)
    worker_parser.add_argument("--ready", type=Path, required=True)
    worker_parser.add_argument("--result", type=Path, required=True)
    worker_parser.add_argument("--listen-seconds", type=float, required=True)
    worker_parser.add_argument("--expect-drop", action="store_true")
    sender_parser = subparsers.add_parser(
        "_send_worker", help="Internal paced packet sender worker"
    )
    sender_parser.add_argument("--interface", required=True)
    sender_parser.add_argument("--frames", type=Path, required=True)
    sender_parser.add_argument("--interval", type=float, required=True)
    args = parser.parse_args(argv)
    active_runner = runner or SystemRunner()
    try:
        if args.command == "_capture_worker":
            return _capture_worker(
                args.interface, args.frames, args.pcap, args.ready, args.result,
                listen_seconds=args.listen_seconds,
                forwarding_expected=not args.expect_drop,
            )
        if args.command == "_send_worker":
            return _send_worker(args.interface, args.frames, args.interval)
        if args.command == "inspect":
            report = inspect_host(
                active_runner,
                sysfs_root=sysfs_root,
                env=env,
                iperf3_path=args.iperf3,
                program_lookup=program_lookup,
            )
            _print_report(report)
            if args.json:
                _save_json(args.json, report)
            return 0
        if args.command == "setup":
            state = setup_pair(
                args.left_interface,
                args.right_interface,
                state_path=args.state,
                confirm_isolation=args.confirm_isolation,
                runner=active_runner,
                sysfs_root=sysfs_root,
                env=env,
                is_root=is_root,
            )
            _print_report({"status": "PASS", "state_file": str(args.state), "namespaces": state["namespaces"]})
            return 0
        if args.command == "restore":
            state = _read_state(args.state)
            restored = restore_state(state, state_path=args.state, runner=active_runner, is_root=is_root)
            _print_report({"status": "PASS" if restored["restored"] else "FAIL", "restore_errors": restored["restore_errors"]})
            return 0 if restored["restored"] else 1
        if args.command == "test":
            if args.frames < 1 or args.pace_ms <= 0 or not math.isfinite(args.pace_ms):
                raise ValueError("--frames and --pace-ms must be positive")
            if args.iperf_seconds < 1:
                raise ValueError("--iperf-seconds must be positive")
            state = _read_state(args.state)
            report = run_bridge_test(
                state,
                output_dir=args.output_dir,
                runner=active_runner,
                port=args.port,
                csr_csv=args.csr_csv,
                status_reader=status_reader,
                iperf3_path=args.iperf3,
                program_lookup=program_lookup,
                frame_count=args.frames,
                interval_seconds=args.pace_ms / 1000,
                iperf_duration=args.iperf_seconds,
            )
            _print_report(report)
            return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[report["overall"]]
    except (OSError, SafetyError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
        failure = {"status": "FAIL", "error": f"{type(error).__name__}: {error}"}
        if args.command == "test":
            try:
                _save_json(args.output_dir / "report.json", failure)
            except OSError:
                pass
        _print_report(failure)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
