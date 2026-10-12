"""Read cable-target status and inspect the RTL8211F over bounded MDIO."""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import time
from typing import Iterator

from sfp_loopback.diagnostics import BoundedUART


PHY_ADDRESS = 1
RTL8211F_ID = 0x001CC916
RTL8211F_ID_MASK = 0xFFFFFFF0
RTL8211F_PAGE_REGISTER = 0x1F
RTL8211F_RGMII_PAGE = 0x0D08
RTL8211F_TX_DELAY_REGISTER = 0x11
RTL8211F_TX_DELAY_BIT = 1 << 8
RTL8211F_RX_DELAY_REGISTER = 0x15
RTL8211F_RX_DELAY_BIT = 1 << 3


def _status_names():
    names = [
        "cable_control_direction", "cable_control_enable",
        "cable_status_active_direction", "cable_status_busy",
        "rtl8211f_reset_done",
        "host_phy_activity_rx_clock_cycles",
        "host_phy_activity_rx_source_valid_beats",
        "host_tx_frames", "sfp0_tx_frames", "sfp1_tx_frames",
    ]
    for port in ("sfp0", "sfp1"):
        names.extend((
            f"{port}_link_up", f"{port}_qpll_locked", f"{port}_tx_ready",
            f"{port}_rx_ready", f"{port}_loss_of_signal",
            f"{port}_control_tx_disable",
        ))
        names.append(f"{port}_unexpected_test_frames")
    for path in ("host_ingress", "sfp0_return", "sfp1_return"):
        for counter in ("rx", "tx", "rejected", "error", "overflow"):
            names.append(f"{path}_{counter}_frames")
    for path in ("host", "sfp0", "sfp1"):
        for counter in ("preamble_errors", "crc_errors", "drops"):
            names.append(f"{path}_mac_rx_datapath_{counter}")
    return tuple(names)


CABLE_STATUS_CSR_NAMES = _status_names()
ERROR_COUNTER_NAMES = tuple(
    name for name in CABLE_STATUS_CSR_NAMES
    if name.endswith(("_error_frames", "_overflow_frames", "_preamble_errors",
                      "_crc_errors", "_drops", "_unexpected_test_frames"))
)
REJECTION_COUNTER_NAMES = tuple(
    name for name in CABLE_STATUS_CSR_NAMES if name.endswith("_rejected_frames")
)
LINK_STATUS_CSR_NAMES = (
    "rtl8211f_reset_done", "sfp0_link_up", "sfp1_link_up", "sfp0_qpll_locked", "sfp1_qpll_locked",
    "sfp0_tx_ready", "sfp1_tx_ready", "sfp0_rx_ready", "sfp1_rx_ready",
)


def read_cable_status(bus):
    """Return one snapshot of cable controls, path counters, and link flags."""
    return {name: getattr(bus.regs, name).read() for name in CABLE_STATUS_CSR_NAMES}


class Clause22MDIO:
    """Bounded Clause 22 transactions through LiteEth's CSR bit-bang pins."""

    def __init__(self, regs):
        self._write_csr = regs.host_phy_mdio_w
        self._read_csr = regs.host_phy_mdio_r

    def _drive(self, *, clock, output_enable, value=1):
        # LiteEth declares CSR fields in mdc, oe, w order.
        self._write_csr.write(clock | (output_enable << 1) | (value << 2))

    def _send_bit(self, bit):
        self._drive(clock=0, output_enable=1, value=bit)
        self._drive(clock=1, output_enable=1, value=bit)

    def _send_bits(self, value, count):
        for shift in range(count - 1, -1, -1):
            self._send_bit((value >> shift) & 1)

    def _idle(self):
        self._drive(clock=0, output_enable=0)

    def _sample_bit(self):
        self._drive(clock=0, output_enable=0)
        # RTL8211F updates MDIO on MDC's rising edge, so sample the current bit
        # while MDC is low before raising the clock to advance to the next bit.
        result = self._read_csr.read() & 1
        self._drive(clock=1, output_enable=0)
        self._drive(clock=0, output_enable=0)
        return result

    @staticmethod
    def _validate(phy_address, register):
        if not 0 <= phy_address <= 31 or not 0 <= register <= 31:
            raise ValueError("Clause 22 PHY address and register must be in 0..31")

    def _start(self, phy_address, register, opcode):
        # Start with MDC low and MDIO released, independent of prior traffic or
        # the CSR reset value, before the host begins the Clause 22 preamble.
        self._idle()
        self._send_bits(0xFFFFFFFF, 32)
        self._send_bits(0b01, 2)
        self._send_bits(opcode, 2)
        self._send_bits(phy_address, 5)
        self._send_bits(register, 5)

    def read(self, phy_address, register):
        """Read one 16-bit register; no retry loop or unbounded wait is used."""
        self._validate(phy_address, register)
        self._start(phy_address, register, opcode=0b10)
        self._sample_bit()  # Turnaround high-impedance bit.
        if self._sample_bit() != 0:
            # Complete the read data phase even when nobody drives the bus.
            # This keeps MDC framing aligned for the next transaction.
            for _ in range(16):
                self._sample_bit()
            self._idle()
            raise RuntimeError(
                f"MDIO PHY at address {phy_address} did not acknowledge register {register}"
            )
        value = 0
        for _ in range(16):
            value = (value << 1) | self._sample_bit()
        self._drive(clock=0, output_enable=0)
        return value

    def write(self, phy_address, register, value):
        """Write one 16-bit register using one finite Clause 22 frame."""
        self._validate(phy_address, register)
        if not 0 <= value <= 0xFFFF:
            raise ValueError("Clause 22 register value must be in 0..65535")
        self._start(phy_address, register, opcode=0b01)
        self._send_bits(0b10, 2)
        self._send_bits(value, 16)
        self._drive(clock=0, output_enable=0)


def _rtl8211f_identity(mdio):
    id1 = mdio.read(PHY_ADDRESS, 2)
    id2 = mdio.read(PHY_ADDRESS, 3)
    phy_id = (id1 << 16) | id2
    if (phy_id & RTL8211F_ID_MASK) != (RTL8211F_ID & RTL8211F_ID_MASK):
        raise RuntimeError(f"PHY ID {phy_id:08x} is not an RTL8211F")
    return id1, id2, phy_id


@contextmanager
def _selected_phy_page(mdio, selected_page: int) -> Iterator[int]:
    prior_page = mdio.read(PHY_ADDRESS, RTL8211F_PAGE_REGISTER)
    try:
        mdio.write(PHY_ADDRESS, RTL8211F_PAGE_REGISTER, selected_page)
        actual_page = mdio.read(PHY_ADDRESS, RTL8211F_PAGE_REGISTER)
        if actual_page != selected_page:
            raise RuntimeError(
                f"PHY page select failed: {actual_page:04x} != {selected_page:04x}"
            )
        yield prior_page
    finally:
        mdio.write(PHY_ADDRESS, RTL8211F_PAGE_REGISTER, prior_page)
        restored_page = mdio.read(PHY_ADDRESS, RTL8211F_PAGE_REGISTER)
        if restored_page != prior_page:
            raise RuntimeError(f"PHY page restore failed: {restored_page:04x} != {prior_page:04x}")


def _read_delay_registers(mdio):
    with _selected_phy_page(mdio, RTL8211F_RGMII_PAGE) as prior_page:
        tx = mdio.read(PHY_ADDRESS, RTL8211F_TX_DELAY_REGISTER)
        rx = mdio.read(PHY_ADDRESS, RTL8211F_RX_DELAY_REGISTER)
    return prior_page, tx, rx


def read_rtl8211f_status(mdio):
    """Read PHY identity, control/link state, and the documented RGMII delays."""
    id1, id2, phy_id = _rtl8211f_identity(mdio)
    bmcr = mdio.read(PHY_ADDRESS, 0)
    bmsr_latched = mdio.read(PHY_ADDRESS, 1)
    bmsr = mdio.read(PHY_ADDRESS, 1)
    page, tx, rx = _read_delay_registers(mdio)
    return {
        "phy_address": PHY_ADDRESS,
        "phy_id": f"{phy_id:08x}",
        "phy_id1": f"{id1:04x}",
        "phy_id2": f"{id2:04x}",
        "bmcr": f"{bmcr:04x}",
        "bmsr_latched": f"{bmsr_latched:04x}",
        "bmsr": f"{bmsr:04x}",
        "link_up": bool(bmsr & (1 << 2)),
        "page_before": f"{page:04x}",
        "rgmii_page": f"{RTL8211F_RGMII_PAGE:04x}",
        "tx_delay_register": f"{tx:04x}",
        "rx_delay_register": f"{rx:04x}",
        "phy_tx_delay_enabled": bool(tx & RTL8211F_TX_DELAY_BIT),
        "phy_rx_delay_enabled": bool(rx & RTL8211F_RX_DELAY_BIT),
        "delay_configuration_passed": not bool(tx & RTL8211F_TX_DELAY_BIT)
        and bool(rx & RTL8211F_RX_DELAY_BIT),
    }


def configure_rtl8211f_delays(mdio):
    """Clear PHY-internal TX delay, enable RX delay, and restore the page."""
    _rtl8211f_identity(mdio)
    with _selected_phy_page(mdio, RTL8211F_RGMII_PAGE) as prior_page:
        tx_before = mdio.read(PHY_ADDRESS, RTL8211F_TX_DELAY_REGISTER)
        rx_before = mdio.read(PHY_ADDRESS, RTL8211F_RX_DELAY_REGISTER)
        tx_after = tx_before & ~RTL8211F_TX_DELAY_BIT
        rx_after = rx_before | RTL8211F_RX_DELAY_BIT
        mdio.write(PHY_ADDRESS, RTL8211F_TX_DELAY_REGISTER, tx_after)
        mdio.write(PHY_ADDRESS, RTL8211F_RX_DELAY_REGISTER, rx_after)
        tx_verified = mdio.read(PHY_ADDRESS, RTL8211F_TX_DELAY_REGISTER)
        rx_verified = mdio.read(PHY_ADDRESS, RTL8211F_RX_DELAY_REGISTER)
        if tx_verified != tx_after or rx_verified != rx_after:
            raise RuntimeError("RTL8211F RGMII delay register readback did not match")
    return {
        "page_restored": f"{prior_page:04x}",
        "tx_register_before": f"{tx_before:04x}",
        "tx_register_after": f"{tx_verified:04x}",
        "rx_register_before": f"{rx_before:04x}",
        "rx_register_after": f"{rx_verified:04x}",
        "phy_tx_delay_enabled": bool(tx_verified & RTL8211F_TX_DELAY_BIT),
        "phy_rx_delay_enabled": bool(rx_verified & RTL8211F_RX_DELAY_BIT),
        "passed": not bool(tx_verified & RTL8211F_TX_DELAY_BIT)
        and bool(rx_verified & RTL8211F_RX_DELAY_BIT),
    }


def main(argv=None, *, uart_factory=BoundedUART, phy_status_reader=read_rtl8211f_status):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
    parser.add_argument("--csr-csv", default="build/sfp_cable/csr.csv")
    parser.add_argument("--phy", action="store_true", help="Read RTL8211F ID, BMCR/BMSR, and delay registers")
    parser.add_argument("--configure-delays", action="store_true",
                        help="Clear PHY TX delay, enable RX delay, and verify both settings")
    parser.add_argument("--output", type=Path, help="Save the JSON status report")
    args = parser.parse_args(argv)
    errors: list[str] = []
    report: dict[str, object] = {"passed": False, "errors": errors}
    final_phy_report: dict[str, object] | None = None
    try:
        with uart_factory(args.port, csr_csv=args.csr_csv) as bus:
            report["cable_status"] = read_cable_status(bus)
            if args.phy or args.configure_delays:
                mdio = Clause22MDIO(bus.regs)
                final_phy_report = phy_status_reader(mdio)
                report["rtl8211f"] = final_phy_report
            if args.configure_delays:
                assert final_phy_report is not None
                final_phy_report["delay_configuration"] = configure_rtl8211f_delays(mdio)
                final_phy_report = phy_status_reader(mdio)
                report["rtl8211f_after_configuration"] = final_phy_report
            if final_phy_report is not None:
                if not final_phy_report["link_up"]:
                    errors.append("RTL8211F reports no copper link")
                if not final_phy_report["delay_configuration_passed"]:
                    errors.append("RTL8211F delay bits do not match FPGA RGMII timing")
            report["passed"] = not errors
    except (OSError, RuntimeError, ValueError, AttributeError, TimeoutError) as error:
        errors.append(f"{type(error).__name__}: {error}")
    report["timestamp_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    serialized = json.dumps(report, indent=2)
    print(serialized)
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n")
        except OSError as error:
            print(f"Cannot write status report: {error}")
            return 2
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
