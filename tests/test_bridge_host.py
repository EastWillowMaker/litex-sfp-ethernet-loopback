"""Operator-facing bridge frame and safety contracts."""

import ipaddress
import json
import os
import subprocess
import struct
from pathlib import Path

import pytest

import sfp_loopback.bridge_host as bridge_host
from sfp_loopback.bridge_host import (
    ETHERTYPE_IPV4,
    ETHERTYPE_IPV6,
    SafetyError,
    build_probe_frame,
    evaluate_packet_capture,
    _hardware_case_check,
    inspect_host,
    main,
    restore_state,
    setup_pair,
    _run_packet_case,
)


def checksum(data):
    if len(data) % 2:
        data += b"\x00"
    words = struct.unpack(f"!{len(data) // 2}H", data)
    total = sum(words)
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def route_version(route):
    destination = route.get("dst")
    if not destination or destination == "default":
        destination = route.get("gateway", "0.0.0.0")
    try:
        return ipaddress.ip_network(destination, strict=False).version
    except ValueError:
        return ipaddress.ip_address(destination).version


@pytest.mark.parametrize(
    "ethertype,source,destination",
    [
        (ETHERTYPE_IPV4, "10.77.0.1", "10.77.0.2"),
        (ETHERTYPE_IPV6, "2001:db8:77::1", "2001:db8:77::2"),
    ],
)
def test_probe_frames_are_well_formed_and_carry_the_requested_identity(
    ethertype, source, destination
):
    source_mac = bytes.fromhex("020000000001")
    destination_mac = bytes.fromhex("020000000002")

    frame = build_probe_frame(
        source_mac,
        destination_mac,
        source,
        destination,
        ethertype,
        run_id=0x0123456789ABCDEF,
        sequence=37,
    )

    assert frame[:6] == destination_mac
    assert frame[6:12] == source_mac
    assert int.from_bytes(frame[12:14], "big") == ethertype
    if ethertype == ETHERTYPE_IPV4:
        ip = frame[14:34]
        assert ip[0] == 0x45
        assert checksum(ip) == 0
        assert ipaddress.ip_address(ip[12:16]) == ipaddress.ip_address(source)
        assert ipaddress.ip_address(ip[16:20]) == ipaddress.ip_address(destination)
        payload_offset = 42
    else:
        ip = frame[14:54]
        assert ip[0] >> 4 == 6
        assert ipaddress.ip_address(ip[8:24]) == ipaddress.ip_address(source)
        assert ipaddress.ip_address(ip[24:40]) == ipaddress.ip_address(destination)
        payload_offset = 62
        udp = frame[54:]
        pseudo_header = ip[8:24] + ip[24:40] + struct.pack("!I3xB", len(udp), 17)
        assert checksum(pseudo_header + udp) == 0
    assert frame[payload_offset : payload_offset + 4] == b"BRDG"
    assert int.from_bytes(frame[payload_offset + 4 : payload_offset + 12], "big") == 0x0123456789ABCDEF
    assert int.from_bytes(frame[payload_offset + 12 : payload_offset + 16], "big") == 37


def test_capture_report_counts_only_incoming_exact_run_frames():
    source_mac = bytes.fromhex("020000000001")
    destination_mac = bytes.fromhex("020000000002")
    frame = build_probe_frame(
        source_mac,
        destination_mac,
        "10.77.0.1",
        "10.77.0.2",
        ETHERTYPE_IPV4,
        run_id=9,
        sequence=0,
    )
    other_run = build_probe_frame(
        source_mac,
        destination_mac,
        "10.77.0.1",
        "10.77.0.2",
        ETHERTYPE_IPV4,
        run_id=10,
        sequence=0,
    )

    report = evaluate_packet_capture(
        [frame],
        [(frame, 4), (frame, 0), (frame, 0), (other_run, 0)],
        forwarding_expected=True,
    )

    assert report["forwarded"] == 2
    assert report["missing"] == 0
    assert report["duplicates"] == 1
    assert report["ignored_outgoing"] == 1
    assert report["passed"] is False


def test_ipv6_capture_fails_if_any_test_frame_crosses_the_bridge():
    frame = build_probe_frame(
        bytes.fromhex("020000000001"),
        bytes.fromhex("020000000002"),
        "2001:db8:77::1",
        "2001:db8:77::2",
        ETHERTYPE_IPV6,
        run_id=11,
        sequence=2,
    )

    report = evaluate_packet_capture(
        [frame], [(frame, 0)], forwarding_expected=False
    )

    assert report["forwarded"] == 1
    assert report["unexpected_forwarded"] == 1
    assert report["passed"] is False


def test_ipv6_capture_passes_when_no_test_frame_reaches_the_other_nic():
    frame = build_probe_frame(
        bytes.fromhex("020000000001"),
        bytes.fromhex("020000000002"),
        "2001:db8:77::1",
        "2001:db8:77::2",
        ETHERTYPE_IPV6,
        run_id=12,
        sequence=4,
    )

    report = evaluate_packet_capture(
        [frame], [], forwarding_expected=False
    )

    assert report["sent"] == 1
    assert report["forwarded"] == 0
    assert report["unexpected_forwarded"] == 0
    assert report["passed"] is True


@pytest.mark.parametrize("corruption", ["truncated", "marker"])
def test_ipv6_drop_capture_rejects_peer_frame_without_a_valid_probe_identity(corruption):
    frame = build_probe_frame(
        bytes.fromhex("020000000001"),
        bytes.fromhex("020000000002"),
        "2001:db8:77::1",
        "2001:db8:77::2",
        ETHERTYPE_IPV6,
        run_id=21,
        sequence=0,
    )
    if corruption == "truncated":
        malformed = frame[:70]
    else:
        malformed = frame[:62] + b"BAD!" + frame[66:]

    report = evaluate_packet_capture([frame], [(malformed, 0)], forwarding_expected=False)

    assert report["malformed_peer_frames"] == 1
    assert report["forwarded"] == 1
    assert report["unexpected_forwarded"] == 1
    assert report["passed"] is False


def test_forward_capture_rejects_corrupt_peer_candidate_but_allows_valid_arp_control():
    source_mac = bytes.fromhex("020000000001")
    destination_mac = bytes.fromhex("020000000002")
    frame = build_probe_frame(
        source_mac,
        destination_mac,
        "10.77.0.1",
        "10.77.0.2",
        ETHERTYPE_IPV4,
        run_id=22,
        sequence=0,
    )
    arp = (
        destination_mac
        + source_mac
        + b"\x08\x06"
        + struct.pack("!HHBBH", 1, ETHERTYPE_IPV4, 6, 4, 1)
        + source_mac
        + ipaddress.IPv4Address("10.77.0.1").packed
        + bytes(6)
        + ipaddress.IPv4Address("10.77.0.2").packed
    )
    corrupt = frame[:42] + b"BAD!" + frame[46:]

    report = evaluate_packet_capture(
        [frame], [(frame, 0), (arp, 0), (corrupt, 0)], forwarding_expected=True
    )

    assert report["unique_forwarded"] == 1
    assert report["malformed_peer_frames"] == 1
    assert report["unexpected"] == 1
    assert report["passed"] is False


def test_ipv6_drop_counter_allows_only_explained_ipv4_or_arp_transmits():
    before = {
        "forwarding": {
            "sfp0_to_sfp1": {
                "rx_frames": 0,
                "tx_frames": 0,
                "ipv4_frames": 0,
                "arp_frames": 0,
                "rejected_frames": 0,
                "error_frames": 0,
                "overflow_frames": 0,
                "ipv6_drops": 0,
                "unsupported_drops": 0,
            }
        },
        "mac_rx": {
            f"sfp{index}": {"preamble_errors": 0, "crc_errors": 0, "drops": 0}
            for index in range(2)
        },
    }
    explained = json.loads(json.dumps(before))
    explained["forwarding"]["sfp0_to_sfp1"].update(
        {"tx_frames": 1, "ipv4_frames": 1, "ipv6_drops": 100}
    )
    allowed = _hardware_case_check(
        before, explained, direction="sfp0_to_sfp1", ethertype=ETHERTYPE_IPV6, count=100
    )

    unexplained = json.loads(json.dumps(before))
    unexplained["forwarding"]["sfp0_to_sfp1"].update(
        {"tx_frames": 1, "ipv6_drops": 100}
    )
    rejected = _hardware_case_check(
        before, unexplained, direction="sfp0_to_sfp1", ethertype=ETHERTYPE_IPV6, count=100
    )

    assert allowed["counter_deltas"]["ipv4_frames"] == 1
    assert allowed["requirements"]["tx_frames_explained_by_ipv4_or_arp"] is True
    assert allowed["passed"] is True
    assert rejected["requirements"]["tx_frames_explained_by_ipv4_or_arp"] is False
    assert rejected["passed"] is False


def test_packet_case_removes_stale_readiness_and_result_before_capture_starts(tmp_path):
    label = "left_to_right_ipv4"
    output_dir = tmp_path / "case"
    output_dir.mkdir()
    stale_paths = {
        "ready": output_dir / f"{label}-ready",
        "result": output_dir / f"{label}-capture.json",
        "pcap": output_dir / f"{label}-rx.pcap",
        "sender": output_dir / f"{label}-sender.json",
    }
    for path in stale_paths.values():
        path.write_text("stale artifact")

    class FakeCaptureProcess:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.returncode = 0
            return self.returncode

    class FakeRunner:
        def popen(self, command, **kwargs):
            ready = Path(command[command.index("--ready") + 1])
            result = Path(command[command.index("--result") + 1])
            pcap = Path(command[command.index("--pcap") + 1])
            assert not ready.exists()
            assert not result.exists()
            assert not pcap.exists()
            ready.write_text("ready\n")
            return FakeCaptureProcess()

        def run(self, command, *, check=True, timeout=30):
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"sent": 1, "errors": []}), ""
            )

    state = {
        "interfaces": {
            "left": {
                "namespace": "bridge-l-test",
                "interface": "left0",
                "mac": "02:00:00:00:00:01",
            },
            "right": {
                "namespace": "bridge-r-test",
                "interface": "right0",
                "mac": "02:00:00:00:00:02",
            },
        }
    }

    result = _run_packet_case(
        FakeRunner(),
        state=state,
        source_side="left",
        destination_side="right",
        ethertype=ETHERTYPE_IPV4,
        count=1,
        interval_seconds=0.01,
        output_dir=output_dir,
    )

    assert all(not path.read_text().startswith("stale") for path in stale_paths.values() if path.exists())
    assert result["capture"]["passed"] is False
    assert "without a valid report" in result["capture"]["errors"][0]


class ReadOnlyInventoryRunner:
    def __init__(self, *, ipv6_routes=None, ipv4_routes=None):
        self.commands = []
        self.ipv6_routes = list(ipv6_routes or [])
        self.ipv4_routes = list(
            [{"dst": "default", "dev": "left0"}] if ipv4_routes is None else ipv4_routes
        )

    def run(self, argv, *, check=True, timeout=30):
        command = list(argv)
        self.commands.append(command)
        if command[:3] == ["ip", "-j", "link"]:
            result = [
                {"ifname": "left0", "flags": ["UP"], "mtu": 1500},
                {"ifname": "right0", "flags": ["UP"], "mtu": 1500},
            ]
        elif command[:3] == ["ip", "-j", "address"]:
            result = [
                {"ifname": "left0", "addr_info": []},
                {"ifname": "right0", "addr_info": []},
            ]
        elif command[:3] == ["ip", "-j", "route"]:
            result = list(self.ipv4_routes)
        elif command[:4] == ["ip", "-j", "-6", "route"]:
            result = list(self.ipv6_routes)
        elif command[0] == "nmcli":
            return subprocess.CompletedProcess(command, 0, "yes\n--\n", "")
        elif command[:3] == ["ip", "netns", "list"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        else:
            pytest.fail(f"unexpected command during safety preflight: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")


def make_physical_sysfs(root, name, device_name):
    interface = root / name
    interface.mkdir(parents=True)
    device = root.parent / "devices" / device_name
    device.mkdir(parents=True)
    (interface / "device").symlink_to(device)
    (interface / "type").write_text("1\n")
    (interface / "carrier").write_text("1\n")
    (interface / "speed").write_text("1000\n")
    suffix = "01" if name.startswith("left") else "02"
    (interface / "address").write_text(f"02:00:00:00:00:{suffix}\n")


def test_setup_refuses_a_default_route_nic_before_any_network_mutation(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    runner = ReadOnlyInventoryRunner()

    with pytest.raises(SafetyError, match="default route"):
        setup_pair(
            "left0",
            "right0",
            state_path=tmp_path / "state.json",
            confirm_isolation=True,
            runner=runner,
            sysfs_root=sysfs,
            env={},
            is_root=True,
        )

    assert not (tmp_path / "state.json").exists()
    assert not any(command[:3] in (["ip", "netns", "add"], ["nmcli", "device", "set"]) for command in runner.commands)


def test_setup_requires_explicit_isolation_confirmation_before_inventory_access(tmp_path):
    runner = ReadOnlyInventoryRunner()

    with pytest.raises(SafetyError, match="confirm-isolation"):
        setup_pair(
            "left0",
            "right0",
            state_path=tmp_path / "state.json",
            confirm_isolation=False,
            runner=runner,
            sysfs_root=tmp_path / "missing-sysfs",
            env={},
            is_root=True,
        )

    assert runner.commands == []
    assert not (tmp_path / "state.json").exists()


def test_setup_claims_state_file_exclusively_before_network_mutations(tmp_path, monkeypatch):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    runner = NamespaceModelRunner(fail_on_second_nic_move=False)
    state_path = tmp_path / "state.json"
    real_open = bridge_host.os.open

    def competing_setup_claim(path, flags, mode=0o777):
        if Path(path) == state_path and flags & os.O_EXCL:
            state_path.write_text("owned by competing setup")
        return real_open(path, flags, mode)

    monkeypatch.setattr(bridge_host.os, "open", competing_setup_claim)

    with pytest.raises(SafetyError, match="Another setup claimed"):
        setup_pair(
            "left0",
            "right0",
            state_path=state_path,
            confirm_isolation=True,
            runner=runner,
            sysfs_root=sysfs,
            env={},
            is_root=True,
        )

    assert state_path.read_text() == "owned by competing setup"
    assert not any(
        command[:3] == ["ip", "netns", "add"]
        or command[:2] == ["nmcli", "device"] and command[2] == "set"
        or command[:3] == ["ip", "link", "set"]
        for command in runner.commands
    )


def test_inspect_marks_missing_iperf_and_protected_nic_without_mutation(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    runner = ReadOnlyInventoryRunner()

    report = inspect_host(
        runner,
        sysfs_root=sysfs,
        env={},
        program_lookup=lambda _: None,
    )

    assert report["status"] == "BLOCKED"
    assert report["physical_interfaces"]["left0"]["isolation_ready"] is False
    assert report["physical_interfaces"]["left0"]["isolation_reasons"] == ["default route"]
    assert any("iperf3" in reason for reason in report["readiness_reasons"])
    assert all(command[0] not in {"nmcli", "ip"} or "set" not in command for command in runner.commands)


def test_inspect_accepts_an_explicit_executable_iperf3_path(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    iperf = tmp_path / "iperf3-wrapper"
    iperf.write_text("#!/bin/sh\nexit 0\n")
    iperf.chmod(0o755)

    report = inspect_host(
        ReadOnlyInventoryRunner(),
        sysfs_root=sysfs,
        env={},
        iperf3_path=str(iperf),
        program_lookup=lambda _: None,
    )

    assert report["iperf3"] == str(iperf.resolve())
    assert not any("iperf3" in reason for reason in report["readiness_reasons"])


def test_inspect_keeps_unlinked_physical_nic_when_speed_read_fails(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    (sysfs / "left0" / "carrier").write_text("0\n")
    (sysfs / "left0" / "speed").unlink()
    (sysfs / "left0" / "speed").mkdir()

    report = inspect_host(
        ReadOnlyInventoryRunner(),
        sysfs_root=sysfs,
        env={},
        program_lookup=lambda _: "/usr/bin/iperf3",
    )

    left = report["physical_interfaces"]["left0"]
    assert left["carrier"] is False
    assert left["speed_mbps"] is None
    assert left["isolation_ready"] is False
    assert "no 1 Gbps carrier" in left["isolation_reasons"]


def test_ipv6_default_route_is_reported_as_management_and_blocks_setup(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    runner = ReadOnlyInventoryRunner(
        ipv4_routes=[],
        ipv6_routes=[
            {"dst": "default", "gateway": "2001:db8::1", "dev": "left0", "table": "main"}
        ],
    )

    report = inspect_host(runner, sysfs_root=sysfs, env={}, program_lookup=lambda _: None)

    assert report["default_route_interfaces"] == ["left0"]
    assert "default route" in report["physical_interfaces"]["left0"]["isolation_reasons"]

    with pytest.raises(SafetyError, match="default route"):
        setup_pair(
            "left0",
            "right0",
            state_path=tmp_path / "state.json",
            confirm_isolation=True,
            runner=runner,
            sysfs_root=sysfs,
            env={},
            is_root=True,
        )


def test_inspect_cli_accepts_the_runtime_wrapper_path(tmp_path, capsys):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    iperf = tmp_path / "iperf3-wrapper"
    iperf.write_text("#!/bin/sh\nexit 0\n")
    iperf.chmod(0o755)

    result = main(
        ["inspect", "--iperf3", str(iperf)],
        runner=ReadOnlyInventoryRunner(),
        sysfs_root=sysfs,
        env={},
        program_lookup=lambda _: None,
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["iperf3"] == str(iperf.absolute())


class NamespaceModelRunner:
    """Model the operator's external ip/nmcli boundary for rollback behavior."""

    def __init__(
        self,
        *,
        collide_on_second_namespace=False,
        fail_on_second_nic_move=True,
        fail_namespace_link_query_at=None,
        fail_namespace_list=False,
    ):
        self.commands = []
        self.root = {
            name: {
                "up": True,
                "mtu": 1600,
                "mac": f"02:00:00:00:00:{'01' if name == 'left0' else '02'}",
                "addresses": [{"local": f"fe80::{'1' if name == 'left0' else '2'}", "prefixlen": 64, "scope": "link"}],
            }
            for name in ("left0", "right0")
        }
        self.namespaces = {}
        self.managed = {"left0": True, "right0": True}
        self.collide_on_second_namespace = collide_on_second_namespace
        self.fail_on_second_nic_move = fail_on_second_nic_move
        self.fail_namespace_link_query_at = fail_namespace_link_query_at
        self.namespace_link_queries = 0
        self.fail_namespace_list = fail_namespace_list
        self.namespace_adds = 0
        self.routes = [
            {
                "dst": "fe80::/64",
                "dev": name,
                "protocol": "kernel",
                "metric": 256,
                "scope": "link",
                "table": "main",
            }
            for name in ("left0", "right0")
        ]
        self.ssh_route_device = None

    def run(self, argv, *, check=True, timeout=30):
        command = list(argv)
        self.commands.append(command)
        if command[:3] == ["ip", "-j", "link"]:
            if "dev" in command:
                name = command[-1]
                if name not in self.root:
                    return subprocess.CompletedProcess(command, 1, "", "missing interface")
                return subprocess.CompletedProcess(
                    command, 0, json.dumps([self._link(name, self.root[name])]), ""
                )
            value = [self._link(name, item) for name, item in self.root.items()]
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        if command[:3] == ["ip", "-j", "address"]:
            if "dev" in command:
                name = command[-1]
                if name not in self.root:
                    return subprocess.CompletedProcess(command, 1, "", "missing interface")
                value = [self._address(name, self.root[name])]
                return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
            value = [self._address(name, item) for name, item in self.root.items()]
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        if (command[:3] == ["ip", "-j", "route"] or command[:4] == ["ip", "-j", "-6", "route"]) and "get" in command:
            if self.ssh_route_device is None:
                value = []
            else:
                value = [{"dst": command[-1], "dev": self.ssh_route_device}]
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        if command[:3] == ["ip", "-j", "route"] or command[:4] == ["ip", "-j", "-6", "route"]:
            is_ipv6 = "-6" in command
            route_list = [
                route
                for route in self.routes
                if route_version(route) == (6 if is_ipv6 else 4)
            ]
            if "table" in command and "all" in command:
                route_list = list(self.routes)
                route_list = [
                    route
                    for route in route_list
                    if route_version(route) == (6 if is_ipv6 else 4)
                ]
            if "dev" in command:
                route_list = [route for route in route_list if route.get("dev") == command[-1]]
            return subprocess.CompletedProcess(command, 0, json.dumps(route_list), "")
        if command[:3] == ["ip", "route", "get"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if command[:2] == ["ip", "netns"] and command[2] == "list":
            if self.fail_namespace_list:
                return subprocess.CompletedProcess(command, 1, "", "simulated namespace list failure")
            text = "".join(f"{name}\n" for name in self.namespaces)
            return subprocess.CompletedProcess(command, 0, text, "")
        if command[:1] == ["nmcli"] and command[1:3] == ["-g", "GENERAL.NM-MANAGED,GENERAL.CONNECTION"]:
            name = command[-1]
            return subprocess.CompletedProcess(command, 0, f"{'yes' if self.managed[name] else 'no'}\n--\n", "")
        if command[:2] == ["nmcli", "device"] and command[2:4] == ["set", "left0"] or (
            command[:2] == ["nmcli", "device"] and command[2:4] == ["set", "right0"]
        ):
            self.managed[command[3]] = command[5] == "yes"
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["ip", "netns", "add"]:
            self.namespace_adds += 1
            if self.collide_on_second_namespace and self.namespace_adds == 2:
                self.namespaces[command[3]] = {
                    "lo": {"up": True, "mtu": 65536, "addresses": []},
                    "unrelated0": {"up": True, "mtu": 1500, "addresses": []},
                }
                raise RuntimeError("simulated namespace name collision")
            self.namespaces[command[3]] = {"lo": {"up": False, "mtu": 65536, "addresses": []}}
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["ip", "netns", "del"]:
            del self.namespaces[command[3]]
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["ip", "-n", command[2]] and "-j" in command and "link" in command:
            self.namespace_link_queries += 1
            if self.namespace_link_queries == self.fail_namespace_link_query_at:
                return subprocess.CompletedProcess(command, 1, "", "simulated namespace link query failure")
            namespace = command[2]
            value = [self._link(name, item) for name, item in self.namespaces[namespace].items()]
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        if command[:3] == ["ip", "-j", "-n"] and command[4:6] == ["address", "show"]:
            namespace, interface = command[3], command[-1]
            item = self.namespaces[namespace].get(interface)
            if item is None:
                return subprocess.CompletedProcess(command, 1, "", "missing interface")
            return subprocess.CompletedProcess(command, 0, json.dumps([self._address(interface, item)]), "")
        if command[:4] == ["ip", "-j", "-6", "address"] and "show" in command:
            interface = command[-1]
            if interface not in self.root:
                return subprocess.CompletedProcess(command, 1, "", "missing interface")
            return subprocess.CompletedProcess(command, 0, json.dumps([self._address(interface, self.root[interface])]), "")
        if command[:2] == ["ip", "-6"] and command[2:4] == ["route", "add"]:
            destination = command[4]
            interface = command[command.index("dev") + 1]
            metric = int(command[command.index("metric") + 1]) if "metric" in command else None
            protocol = command[command.index("proto") + 1] if "proto" in command else "static"
            self.routes.append(
                {
                    "dst": destination,
                    "dev": interface,
                    "metric": metric,
                    "protocol": protocol,
                    "scope": "link" if "scope" in command else None,
                    "table": "main",
                }
            )
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["ip", "-6"] and command[2:4] == ["address", "add"]:
            address, interface = command[4], command[6]
            local, prefix = address.split("/")
            self.root[interface]["addresses"].append(
                {"local": local, "prefixlen": int(prefix), "scope": "link"}
            )
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["ip", "-n"]:
            namespace = command[2]
            rest = command[3:]
            if rest[:3] == ["link", "set", "lo"]:
                self.namespaces[namespace]["lo"]["up"] = True
            elif rest[:3] == ["link", "set", "dev"]:
                interface = rest[3]
                if rest[4:6] == ["netns", "1"]:
                    item = self.namespaces[namespace].pop(interface)
                    self.root[interface] = item
                elif rest[4] == "mtu":
                    self.namespaces[namespace][interface]["mtu"] = int(rest[5])
                elif rest[4] in ("up", "down"):
                    self.namespaces[namespace][interface]["up"] = rest[4] == "up"
            elif rest[:2] == ["address", "add"]:
                cidr, interface = rest[2], rest[4]
                address, prefix = cidr.split("/")
                self.namespaces[namespace][interface]["addresses"].append(
                    {"local": address, "prefixlen": int(prefix), "scope": "global"}
                )
            elif rest[:2] == ["address", "del"]:
                address_prefix, interface = rest[2], rest[4]
                address = address_prefix.split("/")[0]
                self.namespaces[namespace][interface]["addresses"] = [
                    item for item in self.namespaces[namespace][interface]["addresses"]
                    if item["local"] != address
                ]
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["ip", "link"] and command[2:4] == ["set", "dev"]:
            interface = command[4]
            setting = command[5]
            if setting == "netns":
                namespace = command[6]
                if interface == "right0" and self.fail_on_second_nic_move:
                    raise RuntimeError("simulated failure moving the second NIC")
                item = self.root.pop(interface)
                item["addresses"] = [address for address in item["addresses"] if address.get("scope") != "link"]
                self.routes = [route for route in self.routes if route.get("dev") != interface]
                self.namespaces[namespace][interface] = item
            elif setting in ("up", "down"):
                self.root[interface]["up"] = setting == "up"
            elif setting == "mtu":
                self.root[interface]["mtu"] = int(command[6])
            return subprocess.CompletedProcess(command, 0, "", "")
        pytest.fail(f"unexpected command in namespace model: {command}")

    @staticmethod
    def _link(name, item):
        return {
            "ifname": name,
            "address": item.get("mac", "00:00:00:00:00:00"),
            "flags": ["UP"] if item["up"] else [],
            "mtu": item["mtu"],
        }

    @classmethod
    def _address(cls, name, item):
        return {
            **cls._link(name, item),
            "addr_info": list(item["addresses"]),
        }


def test_setup_failure_returns_only_its_nics_and_namespaces_to_saved_state(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    runner = NamespaceModelRunner()
    state_path = tmp_path / "state.json"

    with pytest.raises(RuntimeError, match="simulated failure"):
        setup_pair(
            "left0",
            "right0",
            state_path=state_path,
            confirm_isolation=True,
            runner=runner,
            sysfs_root=sysfs,
            env={},
            is_root=True,
        )

    saved = json.loads(state_path.read_text())
    assert saved["restored"] is True
    assert saved["restore_errors"] == []
    assert runner.namespaces == {}
    assert all(item["up"] and item["mtu"] == 1600 for item in runner.root.values())
    assert runner.root["left0"]["addresses"] == [
        {"local": "fe80::1", "prefixlen": 64, "scope": "link"}
    ]
    assert runner.managed == {"left0": True, "right0": True}


def test_setup_restore_recreates_saved_link_local_addresses_and_routes(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    runner = NamespaceModelRunner(fail_on_second_nic_move=False)
    state_path = tmp_path / "state.json"
    original_addresses = {
        name: list(item["addresses"]) for name, item in runner.root.items()
    }
    original_routes = list(runner.routes)

    state = setup_pair(
        "left0",
        "right0",
        state_path=state_path,
        confirm_isolation=True,
        runner=runner,
        sysfs_root=sysfs,
        env={},
        is_root=True,
    )
    restored = restore_state(state, state_path=state_path, runner=runner, is_root=True, env={})

    assert restored["restored"] is True
    assert runner.namespaces == {}
    assert {name: item["addresses"] for name, item in runner.root.items()} == original_addresses
    assert runner.routes == original_routes
    assert runner.managed == {"left0": True, "right0": True}


def test_setup_rollback_never_deletes_a_namespace_created_by_a_name_collision(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    runner = NamespaceModelRunner(collide_on_second_namespace=True)
    state_path = tmp_path / "state.json"

    with pytest.raises(RuntimeError, match="namespace name collision"):
        setup_pair(
            "left0",
            "right0",
            state_path=state_path,
            confirm_isolation=True,
            runner=runner,
            sysfs_root=sysfs,
            env={},
            is_root=True,
        )

    saved = json.loads(state_path.read_text())
    colliding_namespace = saved["namespaces"]["right"]
    assert saved["owned_namespaces"] == [saved["namespaces"]["left"]]
    assert saved["restored"] is True
    assert saved["namespaces"]["left"] not in runner.namespaces
    assert runner.namespaces[colliding_namespace]["unrelated0"]["up"] is True
    assert runner.root.keys() == {"left0", "right0"}


def _setup_namespaced_pair(tmp_path, runner):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    state_path = tmp_path / "state.json"
    state = setup_pair(
        "left0",
        "right0",
        state_path=state_path,
        confirm_isolation=True,
        runner=runner,
        sysfs_root=sysfs,
        env={},
        is_root=True,
    )
    return state, state_path


@pytest.mark.parametrize("failed_query", [1, 3])
def test_restore_keeps_namespace_when_link_inventory_query_fails(tmp_path, failed_query):
    runner = NamespaceModelRunner(fail_on_second_nic_move=False)
    state, state_path = _setup_namespaced_pair(tmp_path, runner)
    left_namespace = state["namespaces"]["left"]
    runner.namespace_link_queries = 0
    runner.fail_namespace_link_query_at = failed_query
    runner.commands.clear()

    restored = restore_state(state, state_path=state_path, runner=runner, is_root=True, env={})

    assert restored["restored"] is False
    assert runner.namespaces.get(left_namespace) is not None
    assert any("could not verify" in error for error in restored["restore_errors"])
    assert not any(
        command[:3] == ["ip", "netns", "del"] and command[3] == left_namespace
        for command in runner.commands
    )
    if failed_query == 1:
        assert "left0" in runner.namespaces[left_namespace]
        assert not any(
            command[:4] == ["ip", "-n", left_namespace, "address"]
            and command[4] == "del"
            for command in runner.commands
        )
    else:
        assert set(runner.namespaces[left_namespace]) == {"lo"}


def test_restore_makes_no_namespace_changes_when_namespace_list_fails(tmp_path):
    runner = NamespaceModelRunner(fail_on_second_nic_move=False)
    state, state_path = _setup_namespaced_pair(tmp_path, runner)
    runner.fail_namespace_list = True
    runner.commands.clear()

    restored = restore_state(state, state_path=state_path, runner=runner, is_root=True, env={})

    assert restored["restored"] is False
    assert all(
        state["interfaces"][side]["namespace"] in runner.namespaces
        and state["interfaces"][side]["interface"] in runner.namespaces[state["interfaces"][side]["namespace"]]
        for side in ("left", "right")
    )
    assert not any(
        command[:3] == ["ip", "netns", "del"]
        or command[:2] == ["ip", "-n"] and "set" in command
        for command in runner.commands
    )


@pytest.mark.parametrize("protection", ["default", "ssh"])
def test_restore_skips_a_nic_that_became_default_route_or_ssh_path(tmp_path, protection):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    runner = NamespaceModelRunner(fail_on_second_nic_move=False)
    state_path = tmp_path / "state.json"
    state = setup_pair(
        "left0",
        "right0",
        state_path=state_path,
        confirm_isolation=True,
        runner=runner,
        sysfs_root=sysfs,
        env={},
        is_root=True,
    )
    left_namespace = state["namespaces"]["left"]
    if protection == "default":
        runner.routes.append(
            {"dst": "default", "gateway": "2001:db8::1", "dev": "left0", "table": "main"}
        )
        env = {}
    else:
        runner.ssh_route_device = "left0"
        env = {"SSH_CONNECTION": "2001:db8::99 22 2001:db8::1 22"}

    restored = restore_state(
        state, state_path=state_path, runner=runner, is_root=True, env=env
    )

    assert restored["restored"] is False
    assert any("default-route/SSH" in error for error in restored["restore_errors"])
    assert "left0" in runner.namespaces[left_namespace]
    assert "10.77.0.1" in [
        address["local"] for address in runner.namespaces[left_namespace]["left0"]["addresses"]
    ]


@pytest.mark.parametrize("location", ["namespace", "host"])
def test_restore_checks_saved_mac_before_mutating_either_interface(location, tmp_path):
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    make_physical_sysfs(sysfs, "left0", "usb-left")
    make_physical_sysfs(sysfs, "right0", "usb-right")
    runner = NamespaceModelRunner(fail_on_second_nic_move=False)
    state_path = tmp_path / "state.json"
    state = setup_pair(
        "left0",
        "right0",
        state_path=state_path,
        confirm_isolation=True,
        runner=runner,
        sysfs_root=sysfs,
        env={},
        is_root=True,
    )
    left_namespace = state["namespaces"]["left"]
    if location == "namespace":
        item = runner.namespaces[left_namespace]["left0"]
    else:
        item = runner.namespaces[left_namespace].pop("left0")
        runner.root["left0"] = item
        item["up"] = False
        item["mtu"] = 9000
    item["mac"] = "02:00:00:00:00:ff"

    restored = restore_state(state, state_path=state_path, runner=runner, is_root=True, env={})

    assert restored["restored"] is False
    assert any("does not match saved MAC" in error for error in restored["restore_errors"])
    if location == "namespace":
        assert "left0" in runner.namespaces[left_namespace]
        assert runner.namespaces[left_namespace]["left0"]["addresses"][-1]["local"] == "10.77.0.1"
    else:
        assert runner.root["left0"]["up"] is False
        assert runner.root["left0"]["mtu"] == 9000
