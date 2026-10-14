"""Public build-seam checks for the isolated dual-SFP bridge target."""

import csv
import inspect
import math
import re
from pathlib import Path

from sfp_loopback.bridge_target import (
    DEFAULT_SYS_CLK_FREQ,
    build_bridge,
)


def _assert_clock_configuration(builder, sys_clk_freq):
    with open(builder.csr_csv, newline="", encoding="utf-8") as csr_file:
        rows = list(csv.reader(csr_file))
    constants = {row[1]: row[2] for row in rows if row and row[0] == "constant"}
    assert constants["config_clock_frequency"] == str(int(sys_clk_freq))

    verilog = (
        Path(builder.output_dir) / "gateware" / "dual_sfp_bridge.v"
    ).read_text(encoding="utf-8")

    # Check the CRG's generated 50 MHz -> sys clock, rather than relying only
    # on LiteX's CSR metadata. This is the first PLL in the emitted design.
    crg_pll = re.search(r"PLLE2_ADV #\((.*?)\) PLLE2_ADV \(", verilog, re.S)
    assert crg_pll is not None

    def pll_parameter(name):
        match = re.search(rf"\.{name}\s+\(\d+'d(\d+)\)", crg_pll.group(1))
        assert match is not None, f"missing CRG PLL parameter {name}"
        return int(match.group(1))

    pll_output_freq = (
        50e6
        * pll_parameter("CLKFBOUT_MULT")
        / pll_parameter("DIVCLK_DIVIDE")
        / pll_parameter("CLKOUT0_DIVIDE")
    )
    assert pll_output_freq == sys_clk_freq

    # UARTBone's RS232 PHY phase accumulator must match this system clock.
    phase_add = re.search(
        r"bridgesoc_tx_phase \+ (\d+)'([dh])([0-9a-fA-F]+)", verilog
    )
    assert phase_add is not None
    phase_value = int(
        phase_add.group(3), 16 if phase_add.group(2) == "h" else 10
    )
    assert phase_value == int((115200 / sys_clk_freq) * 2**32)

    # LiteEth's transceiver reset wait is ten microseconds in sys-clock cycles.
    tx_reset_cycles = math.ceil(10e-6 * sys_clk_freq)
    reset_counts = re.findall(
        r"sfp[01]_phy_pma_a7_gtp_basex_tx_init_timer == (\d+)'d(\d+)",
        verilog,
    )
    assert len(reset_counts) == 2
    assert {int(value) for _, value in reset_counts} == {tx_reset_cycles}


def test_bridge_build_has_two_ports_one_shared_qpll_and_typed_counters(tmp_path):
    builder = build_bridge(output_dir=tmp_path, run=False)

    with open(builder.csr_csv, newline="", encoding="utf-8") as csr_file:
        rows = list(csv.reader(csr_file))
    csr_names = {row[1] for row in rows if row and row[0] == "csr_register"}
    constants = {row[1]: row[2] for row in rows if row and row[0] == "constant"}
    assert constants["config_clock_frequency"] == str(int(DEFAULT_SYS_CLK_FREQ))
    assert inspect.signature(build_bridge).parameters["output_dir"].default == (
        "build/sfp_bridge500"
    )

    expected = {
        "sfp0_link_up",
        "sfp0_qpll_locked",
        "sfp0_tx_ready",
        "sfp0_rx_ready",
        "sfp0_loss_of_signal",
        "sfp0_control_tx_disable",
        "sfp1_link_up",
        "sfp1_qpll_locked",
        "sfp1_tx_ready",
        "sfp1_rx_ready",
        "sfp1_loss_of_signal",
        "sfp1_control_tx_disable",
        "sfp0_mac_rx_datapath_preamble_errors",
        "sfp0_mac_rx_datapath_crc_errors",
        "sfp0_mac_rx_datapath_drops",
        "sfp1_mac_rx_datapath_preamble_errors",
        "sfp1_mac_rx_datapath_crc_errors",
        "sfp1_mac_rx_datapath_drops",
    }
    for direction in ("sfp0_to_sfp1", "sfp1_to_sfp0"):
        expected.update(
            f"{direction}_{counter}"
            for counter in (
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
        )
    assert expected <= csr_names
    assert constants["config_cpu_type_none"] == "None"
    assert not any(name.startswith("cpu_") for name in csr_names)

    gateware = tmp_path / "gateware"
    verilog = (gateware / "dual_sfp_bridge.v").read_text(encoding="utf-8")
    xdc = (gateware / "dual_sfp_bridge.xdc").read_text(encoding="utf-8")
    tcl = (gateware / "dual_sfp_bridge.tcl").read_text(encoding="utf-8")

    assert sum(line.strip() == "GTPE2_COMMON #(" for line in verilog.splitlines()) == 1
    assert sum(line.strip() == "GTPE2_CHANNEL #(" for line in verilog.splitlines()) == 2
    assert "set_property LOC F6 [get_ports {onboard_gtp_refclk_p}]" in xdc
    assert "set_property LOC E6 [get_ports {onboard_gtp_refclk_n}]" in xdc
    assert (
        "create_clock -name onboard_gtp_refclk_p -period 8.0 "
        "[get_ports onboard_gtp_refclk_p]"
    ) in xdc
    for pin, port in (
        ("B4", "sfp0_txp"),
        ("A4", "sfp0_txn"),
        ("B8", "sfp0_rxp"),
        ("A8", "sfp0_rxn"),
        ("D5", "sfp1_txp"),
        ("C5", "sfp1_txn"),
        ("D11", "sfp1_rxp"),
        ("C11", "sfp1_rxn"),
        ("V18", "sfp_tx_disable0"),
        ("U20", "sfp_tx_disable1"),
        ("V19", "sfp_rx_los0"),
        ("V20", "sfp_rx_los1"),
    ):
        assert f"set_property LOC {pin} [get_ports {{{port}}}]" in xdc
    for clock in ("sfp0_txoutclk", "sfp0_rxoutclk", "sfp1_txoutclk", "sfp1_rxoutclk"):
        assert f"create_clock -name {clock} -period 16.0" in xdc
        assert f"-group [get_clocks -include_generated_clocks {clock}]" in tcl
    assert "-group [get_clocks -include_generated_clocks clk50]" in tcl
    assert "get_timing_paths -delay_type max -max_paths 1" in tcl
    assert "get_timing_paths -delay_type min -max_paths 1" in tcl
    assert "Bridge target timing gate failed: no setup or hold timing path." in tcl
    assert "Bridge target timing gate failed: setup slack" in tcl
    assert tcl.index("Bridge target timing gate passed") < tcl.index("write_bitstream -force")
    assert "rtl8211f" not in verilog
    assert "eth_rx_ctl" not in verilog
    _assert_clock_configuration(builder, DEFAULT_SYS_CLK_FREQ)


def test_bridge_supports_qualified_50mhz_comparison_clock(tmp_path):
    builder = build_bridge(
        output_dir=tmp_path / "bridge50",
        run=False,
        sys_clk_freq=50e6,
    )
    _assert_clock_configuration(builder, 50e6)
