"""Public build-seam checks for the isolated dual-SFP bridge target."""

import csv

from sfp_loopback.bridge_target import build_bridge


def test_bridge_build_has_two_ports_one_shared_qpll_and_typed_counters(tmp_path):
    builder = build_bridge(output_dir=tmp_path, run=False)

    with open(builder.csr_csv, newline="", encoding="utf-8") as csr_file:
        rows = list(csv.reader(csr_file))
    csr_names = {row[1] for row in rows if row and row[0] == "csr_register"}
    constants = {row[1]: row[2] for row in rows if row and row[0] == "constant"}

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
