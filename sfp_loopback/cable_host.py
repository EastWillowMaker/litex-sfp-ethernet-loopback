"""Run one direction of the host-verified SFP-to-SFP cable test."""

import argparse
import json
from pathlib import Path
import secrets
import time

from sfp_loopback import host
from sfp_loopback.cable_diagnostics import (
    Clause22MDIO,
    ERROR_COUNTER_NAMES,
    LINK_STATUS_CSR_NAMES,
    REJECTION_COUNTER_NAMES,
    read_cable_status,
    read_rtl8211f_status,
)
from sfp_loopback.diagnostics import BoundedUART


DIRECTIONS = {
    "sfp1-to-sfp2": (0, 1, 0),
    "sfp2-to-sfp1": (1, 0, 1),
}
DEFAULT_CSR_CSV = Path("build/sfp_cable/csr.csv")


def _wait_not_busy(regs, *, clock, sleep, timeout=2.0):
    deadline = clock() + timeout
    while regs.cable_status_busy.read():
        if clock() >= deadline:
            raise TimeoutError("Cable direction control remained busy")
        sleep(0.01)


def _counter_deltas(before, after):
    deltas = {}
    for name, old_value in before.items():
        if name.endswith("_frames") or name.endswith("_errors") or name.endswith("_drops"):
            new_value = after[name]
            deltas[name] = (new_value - old_value) & 0xFFFFFFFF
    return deltas


def _read_host_link(root, name):
    interface = root / name
    carrier = int((interface / "carrier").read_text().strip())
    speed_mbps = None
    try:
        speed_mbps = int((interface / "speed").read_text().strip())
        if speed_mbps <= 0:
            speed_mbps = None
    except (OSError, ValueError):
        speed_mbps = None
    return {"carrier": carrier, "speed_mbps": speed_mbps}


def _evaluate_hardware(before, after, host_result, direction):
    source_port, return_port, direction_value = DIRECTIONS[direction]
    sent = host_result["sent"]
    received = host_result["received"]
    deltas = _counter_deltas(before, after)
    expected_routes = {
        "host_ingress_rx_frames": sent,
        "host_ingress_tx_frames": sent,
        f"sfp{source_port}_tx_frames": sent,
        f"sfp{return_port}_return_rx_frames": received,
        f"sfp{return_port}_return_tx_frames": received,
        "host_tx_frames": received,
        f"sfp{return_port}_tx_frames": 0,
        f"sfp{source_port}_return_rx_frames": 0,
    }
    route_assertions = {
        name: {"expected": expected, "actual": deltas.get(name),
               "passed": deltas.get(name) == expected}
        for name, expected in expected_routes.items()
    }
    error_deltas = {name: deltas.get(name, 0) for name in ERROR_COUNTER_NAMES}
    rejection_deltas = {name: deltas.get(name, 0) for name in REJECTION_COUNTER_NAMES}
    link_ok = all(before[name] == 1 and after[name] == 1 for name in LINK_STATUS_CSR_NAMES)
    signal_ok = all(before[f"sfp{port}_loss_of_signal"] == 0
                    and after[f"sfp{port}_loss_of_signal"] == 0
                    and before[f"sfp{port}_control_tx_disable"] == 0
                    and after[f"sfp{port}_control_tx_disable"] == 0
                    for port in (0, 1))
    direction_ok = (after["cable_status_active_direction"] == direction_value
                    and after["cable_status_busy"] == 0)
    counters_ok = all(value == 0 for value in error_deltas.values())
    route_ok = all(check["passed"] for check in route_assertions.values())
    return {
        "direction": direction,
        "counter_deltas": deltas,
        "route_deltas": {name: deltas.get(name) for name in expected_routes},
        "route_assertions": route_assertions,
        "error_deltas": error_deltas,
        "rejection_deltas": rejection_deltas,
        "link_status_passed": link_ok and signal_ok,
        "direction_status_passed": direction_ok,
        "passed": link_ok and signal_ok and direction_ok and counters_ok and route_ok,
    }


def main(argv=None, *, interface_root=Path("/sys/class/net"),
         transport_factory=host.RawEthernet, bus_factory=BoundedUART,
         phy_status_reader=read_rtl8211f_status, clock=time.monotonic, sleep=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True, help="Explicit wired USB Ethernet interface")
    parser.add_argument("--direction", required=True, choices=tuple(DIRECTIONS),
                        help="One cable direction to exercise in this invocation")
    parser.add_argument("--report", required=True, type=Path, help="JSON host and hardware evidence")
    parser.add_argument("--port", default="/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
    parser.add_argument("--csr-csv", type=Path, default=DEFAULT_CSR_CSV)
    parser.add_argument("--run-id", type=lambda value: int(value, 0),
                        help="Optional unsigned 64-bit run identifier")
    parser.add_argument("--duration", type=float, choices=(60.0,),
                        help="Run the accepted 60-second sustained correctness mode")
    args = parser.parse_args(argv)
    run_id = args.run_id if args.run_id is not None else secrets.randbits(64)
    report = {
        "passed": False,
        "interface": args.interface,
        "direction": args.direction,
        "run_id": f"{run_id:016x}",
        "host_result": None,
        "hardware_evidence": {"passed": False, "errors": []},
        "errors": [],
        "mode": "sustained_correctness" if args.duration is not None else "fixed_correctness",
    }
    transport = None
    try:
        if not 0 <= run_id < 1 << 64:
            raise ValueError("Run identifier must be an unsigned 64-bit integer")
        report["requested_duration_seconds"] = args.duration
        mac = host.inspect_interface(args.interface, interface_root)
        host_link_before = _read_host_link(interface_root, args.interface)
        report["hardware_evidence"]["host_link_before"] = host_link_before
        if host_link_before["carrier"] != 1 or host_link_before["speed_mbps"] != 1000:
            report["hardware_evidence"]["host_link_passed"] = False
            raise RuntimeError("Host Ethernet interface must have carrier at 1000 Mbps")
        with bus_factory(args.port, csr_csv=str(args.csr_csv)) as bus:
            hardware = report["hardware_evidence"]
            phy_mdio = Clause22MDIO(bus.regs)
            try:
                bus.regs.cable_control_enable.write(0)
                _wait_not_busy(bus.regs, clock=clock, sleep=sleep)
                if bus.regs.cable_control_enable.read() != 0:
                    raise RuntimeError("Cable control did not remain disabled")
                direction_value = DIRECTIONS[args.direction][2]
                bus.regs.cable_control_direction.write(direction_value)
                if bus.regs.cable_control_direction.read() != direction_value:
                    raise RuntimeError("Cable direction CSR did not retain the requested direction")
                hardware["status_before"] = read_cable_status(bus)
                if not all(hardware["status_before"][name] == 1 for name in LINK_STATUS_CSR_NAMES):
                    raise RuntimeError("RTL8211F reset, SFP link, QPLL, or MAC readiness is not complete")
                if any(hardware["status_before"][f"sfp{port}_loss_of_signal"]
                       or hardware["status_before"][f"sfp{port}_control_tx_disable"]
                       for port in (0, 1)):
                    raise RuntimeError("An SFP reports loss of signal or transmit disabled")
                hardware["rtl8211f_before"] = phy_status_reader(phy_mdio)
                if not hardware["rtl8211f_before"]["link_up"]:
                    raise RuntimeError("RTL8211F reports no copper link")
                if not hardware["rtl8211f_before"]["delay_configuration_passed"]:
                    raise RuntimeError("RTL8211F internal delays do not match the FPGA RGMII timing")
                bus.regs.cable_control_enable.write(1)
                _wait_not_busy(bus.regs, clock=clock, sleep=sleep)
                if bus.regs.cable_control_enable.read() != 1:
                    raise RuntimeError("Cable control did not enable")
                if bus.regs.cable_status_active_direction.read() != direction_value:
                    raise RuntimeError("Cable target did not activate the requested direction")
                hardware["active_direction"] = bus.regs.cable_status_active_direction.read()
                transport = transport_factory(args.interface)
                report["host_result"] = host.run_test(
                    transport, mac, run_id, duration_seconds=args.duration,
                    timeout=1.0, clock=clock,
                )
            finally:
                if transport is not None:
                    try:
                        transport.close()
                    except OSError as close_error:
                        report["errors"].append(
                            f"{type(close_error).__name__}: {close_error}"
                        )
                    transport = None
                try:
                    bus.regs.cable_control_enable.write(0)
                    _wait_not_busy(bus.regs, clock=clock, sleep=sleep)
                    if bus.regs.cable_control_enable.read() != 0:
                        raise RuntimeError("Cable control did not disable after the run")
                    hardware["status_after"] = read_cable_status(bus)
                    hardware["rtl8211f_after"] = phy_status_reader(phy_mdio)
                    if "status_before" in hardware and report["host_result"] is not None:
                        hardware.update(_evaluate_hardware(
                            hardware["status_before"], hardware["status_after"],
                            report["host_result"], args.direction,
                        ))
                    host_link_after = _read_host_link(interface_root, args.interface)
                    hardware["host_link_after"] = host_link_after
                    hardware["host_link_passed"] = (
                        host_link_before["carrier"] == host_link_after["carrier"] == 1
                        and host_link_before["speed_mbps"] == host_link_after["speed_mbps"] == 1000
                    )
                    phy_before = hardware.get("rtl8211f_before")
                    phy_after = hardware.get("rtl8211f_after")
                    hardware["rtl8211f_status_passed"] = bool(
                        phy_before and phy_after
                        and phy_before["link_up"] and phy_after["link_up"]
                        and phy_before["delay_configuration_passed"]
                        and phy_after["delay_configuration_passed"]
                    )
                    hardware["passed"] = (
                        hardware["passed"] and hardware["host_link_passed"]
                        and hardware["rtl8211f_status_passed"]
                    )
                except (OSError, RuntimeError, AttributeError, TimeoutError) as cleanup_error:
                    hardware["errors"].append(
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                    hardware["passed"] = False
            if report["host_result"] is not None:
                report["host_mac"] = mac.hex(":")
                report["passed"] = bool(
                    report["host_result"]["passed"] and hardware["passed"]
                    and not report["errors"] and not hardware["errors"]
                )
    except (OSError, ValueError, RuntimeError, AttributeError, TimeoutError) as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
        report["passed"] = False
    except KeyboardInterrupt:
        report["errors"].append("Test interrupted")
        report["passed"] = False
    finally:
        if transport is not None:
            try:
                transport.close()
            except OSError as error:
                report["errors"].append(f"{type(error).__name__}: {error}")
                report["passed"] = False
    report["timestamp_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    except OSError as error:
        print(f"FAIL: cannot write result artifact: {error}")
        return 2
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {args.direction} over {args.interface}; "
          f"host received {report['host_result'].get('received', 0) if report['host_result'] else 0}; "
          f"hardware evidence={'PASS' if report['hardware_evidence']['passed'] else 'FAIL'}")
    print(f"Report: {args.report}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
