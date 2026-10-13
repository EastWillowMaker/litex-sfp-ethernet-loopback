"""Read the two SFP links and directional forwarding counters over UART."""

import argparse
import json
from pathlib import Path

from sfp_loopback.diagnostics import BoundedUART


PORT_STATUS_FIELDS = (
    "link_up",
    "qpll_locked",
    "tx_ready",
    "rx_ready",
    "loss_of_signal",
    "control_tx_disable",
)
MAC_ERROR_FIELDS = (
    "preamble_errors",
    "crc_errors",
    "drops",
)
FORWARD_COUNTER_FIELDS = (
    "rx_frames",
    "tx_frames",
    "ipv4_frames",
    "arp_frames",
    "rejected_frames",
    "error_frames",
    "overflow_frames",
    "ipv6_drops",
    "unsupported_drops",
)

BRIDGE_STATUS_CSR_NAMES = tuple(
    [
        f"sfp{index}_{field}"
        for index in range(2)
        for field in PORT_STATUS_FIELDS
    ]
    + [
        f"sfp{index}_mac_rx_datapath_{field}"
        for index in range(2)
        for field in MAC_ERROR_FIELDS
    ]
    + [
        f"{direction}_{field}"
        for direction in ("sfp0_to_sfp1", "sfp1_to_sfp0")
        for field in FORWARD_COUNTER_FIELDS
    ]
)


def read_bridge_status(bus):
    """Read one read-only snapshot of both links and both bridge paths."""
    read = lambda name: getattr(bus.regs, name).read()
    return {
        "links": {
            f"sfp{index}": {
                field: read(f"sfp{index}_{field}") for field in PORT_STATUS_FIELDS
            }
            for index in range(2)
        },
        "mac_rx": {
            f"sfp{index}": {
                field: read(f"sfp{index}_mac_rx_datapath_{field}")
                for field in MAC_ERROR_FIELDS
            }
            for index in range(2)
        },
        "forwarding": {
            direction: {
                field: read(f"{direction}_{field}")
                for field in FORWARD_COUNTER_FIELDS
            }
            for direction in ("sfp0_to_sfp1", "sfp1_to_sfp0")
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        default="/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0",
    )
    parser.add_argument("--csr-csv", default="build/sfp_bridge/csr.csv")
    parser.add_argument("--output", help="Save the status snapshot as JSON.")
    args = parser.parse_args()
    with BoundedUART(args.port, csr_csv=args.csr_csv) as bus:
        report = json.dumps(read_bridge_status(bus), indent=2)
        print(report)
        if args.output:
            Path(args.output).write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
