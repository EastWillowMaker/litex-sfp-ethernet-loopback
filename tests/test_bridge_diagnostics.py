"""Read-only diagnostics contract for the two-port bridge."""

from sfp_loopback.bridge_diagnostics import (
    BRIDGE_STATUS_CSR_NAMES,
    read_bridge_status,
)


class Register:
    def __init__(self, value):
        self.value = value
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.value

    def write(self, value):
        raise AssertionError("bridge diagnostics must not write hardware CSRs")


def make_bus():
    regs = type("Regs", (), {})()
    for index in range(2):
        for field in (
            "link_up",
            "qpll_locked",
            "tx_ready",
            "rx_ready",
            "loss_of_signal",
            "control_tx_disable",
        ):
            setattr(regs, f"sfp{index}_{field}", Register(index + 1))
        for field in ("preamble_errors", "crc_errors", "drops"):
            setattr(regs, f"sfp{index}_mac_rx_datapath_{field}", Register(index + 2))
    for path_index, direction in enumerate(("sfp0_to_sfp1", "sfp1_to_sfp0")):
        for field_index, field in enumerate(
            (
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
        ):
            setattr(regs, f"{direction}_{field}", Register(path_index * 10 + field_index))
    return type("Bus", (), {"regs": regs})()


def test_bridge_status_reader_reports_both_links_macs_and_forward_paths():
    bus = make_bus()

    status = read_bridge_status(bus)

    assert status["links"]["sfp0"]["link_up"] == 1
    assert status["links"]["sfp1"]["loss_of_signal"] == 2
    assert status["mac_rx"]["sfp0"]["crc_errors"] == 2
    assert status["mac_rx"]["sfp1"]["drops"] == 3
    assert status["forwarding"]["sfp0_to_sfp1"] == {
        "rx_frames": 0,
        "tx_frames": 1,
        "ipv4_frames": 2,
        "arp_frames": 3,
        "rejected_frames": 4,
        "error_frames": 5,
        "overflow_frames": 6,
        "ipv6_drops": 7,
        "unsupported_drops": 8,
    }
    assert status["forwarding"]["sfp1_to_sfp0"]["rx_frames"] == 10
    assert all(getattr(bus.regs, name).reads == 1 for name in BRIDGE_STATUS_CSR_NAMES)


def test_bridge_csr_status_name_list_is_unique_and_directional():
    assert len(BRIDGE_STATUS_CSR_NAMES) == len(set(BRIDGE_STATUS_CSR_NAMES))
    assert "sfp0_link_up" in BRIDGE_STATUS_CSR_NAMES
    assert "sfp1_link_up" in BRIDGE_STATUS_CSR_NAMES
    assert "sfp0_to_sfp1_ipv6_drops" in BRIDGE_STATUS_CSR_NAMES
    assert "sfp1_to_sfp0_unsupported_drops" in BRIDGE_STATUS_CSR_NAMES
