"""Public build-seam checks for the three-port cable target."""

import csv

from sfp_loopback.cable_target import build_target


def test_build_emits_three_port_csr_map_and_shared_qpll(tmp_path):
    builder = build_target(output_dir=tmp_path, run=False)

    with open(builder.csr_csv, newline="", encoding="utf-8") as csr_file:
        csr_names = {
            row[1]
            for row in csv.reader(csr_file)
            if row and row[0] == "csr_register"
        }

    expected = {
        "cable_control_direction",
        "cable_control_enable",
        "cable_status_active_direction",
        "cable_status_busy",
        "host_ingress_rx_frames",
        "host_ingress_tx_frames",
        "host_ingress_rejected_frames",
        "host_ingress_error_frames",
        "host_ingress_overflow_frames",
        "sfp0_return_rx_frames",
        "sfp0_return_tx_frames",
        "sfp0_return_rejected_frames",
        "sfp0_return_error_frames",
        "sfp0_return_overflow_frames",
        "sfp1_return_rx_frames",
        "sfp1_return_tx_frames",
        "sfp1_return_rejected_frames",
        "sfp1_return_error_frames",
        "sfp1_return_overflow_frames",
        "host_tx_frames",
        "sfp0_tx_frames",
        "sfp1_tx_frames",
        "host_phy_mdio_w",
        "host_phy_mdio_r",
        "host_phy_crg_reset",
        "rtl8211f_reset_done",
        "sfp0_control_tx_disable",
        "sfp1_control_tx_disable",
        "sfp0_link_up",
        "sfp1_link_up",
        "sfp0_qpll_locked",
        "sfp1_qpll_locked",
        "sfp0_tx_ready",
        "sfp0_rx_ready",
        "sfp1_tx_ready",
        "sfp1_rx_ready",
        "sfp0_loss_of_signal",
        "sfp1_loss_of_signal",
        "host_mac_rx_datapath_crc_errors",
        "host_mac_rx_datapath_preamble_errors",
        "host_mac_rx_datapath_drops",
        "sfp0_mac_rx_datapath_crc_errors",
        "sfp0_mac_rx_datapath_preamble_errors",
        "sfp0_mac_rx_datapath_drops",
        "sfp1_mac_rx_datapath_crc_errors",
        "sfp1_mac_rx_datapath_preamble_errors",
        "sfp1_mac_rx_datapath_drops",
    }
    assert expected <= csr_names

    gateware = tmp_path / "gateware"
    verilog = (gateware / "sfp_cable_loopback.v").read_text(encoding="utf-8")
    assert sum(line.strip() == "GTPE2_COMMON #(" for line in verilog.splitlines()) == 1
    assert sum(line.strip() == "GTPE2_CHANNEL #(" for line in verilog.splitlines()) == 2
    assert sum(line.strip() == "IDELAYCTRL IDELAYCTRL(" for line in verilog.splitlines()) == 1


def test_build_constrains_documented_reference_and_rgmii_clocks(tmp_path):
    build_target(output_dir=tmp_path, run=False)

    gateware = tmp_path / "gateware"
    xdc = (gateware / "sfp_cable_loopback.xdc").read_text(encoding="utf-8")
    tcl = (gateware / "sfp_cable_loopback.tcl").read_text(encoding="utf-8")
    verilog = (gateware / "sfp_cable_loopback.v").read_text(encoding="utf-8")

    assert "create_clock -name onboard_gtp_refclk_p -period 8.0" in xdc
    assert "create_clock -name eth_clocks_rx -period 8.0" in xdc
    assert (
        "set_property CLOCK_DEDICATED_ROUTE FALSE "
        "[get_nets rtl8211f_rgmii_rx_ibuf]"
    ) in xdc
    assert "set_clock_groups -asynchronous" in tcl
    assert "get_timing_paths -delay_type max -max_paths 1" in tcl
    assert "get_timing_paths -delay_type min -max_paths 1" in tcl
    assert "Cable target timing gate failed: no setup or hold timing path." in tcl
    assert "Cable target timing gate failed: setup slack" in tcl
    assert tcl.index("Cable target timing gate passed") < tcl.index("write_bitstream -force")
    assert "create_clock -name sfp0_txoutclk -period 16.0" in xdc
    assert "create_clock -name sfp0_rxoutclk -period 16.0" in xdc
    assert "create_clock -name sfp1_txoutclk -period 16.0" in xdc
    assert "create_clock -name sfp1_rxoutclk -period 16.0" in xdc
    assert "create_generated_clock -name rtl8211f_rgmii_tx" not in xdc
    assert "create_generated_clock -name rtl8211f_rgmii_tx" in tcl
    assert (
        "-source [get_pins rtl8211f_tx_clock_oddr/C] -divide_by 1 "
        "[get_ports eth_clocks_tx]"
    ) in tcl
    assert "set_input_delay -clock [get_clocks eth_clocks_rx] -max 2.8" in xdc
    assert "set_input_delay -clock [get_clocks eth_clocks_rx] -min 1.2" in xdc
    assert "-clock_fall -max 2.8" in xdc
    assert "-clock_fall -min 1.2" in xdc
    assert "set_output_delay -clock [get_clocks rtl8211f_rgmii_tx] -max 1.0" in tcl
    assert "-clock_fall -min -1.0 -add_delay" in tcl
    assert "create_clock -name eth_clocks_rx" in xdc
    assert tcl.index("create_generated_clock -name rtl8211f_rgmii_tx") < tcl.index(
        "set_clock_groups -asynchronous"
    )
    assert tcl.index("set_output_delay -clock [get_clocks rtl8211f_rgmii_tx]") < tcl.index(
        "set_clock_groups -asynchronous"
    )
    assert "rtl8211f_rgmii_rx_ibuf" in verilog
    assert "rtl8211f_rgmii_rx_regional_clk" in verilog
    assert "BUFR" in verilog
    assert ".I (eth_clocks_rx)" in verilog
    assert ".O (rtl8211f_rgmii_rx_ibuf)" in verilog
    assert ".I   (rtl8211f_rgmii_rx_ibuf)" in verilog
    assert ".O   (rtl8211f_rgmii_rx_regional_clk)" in verilog
    assert "rtl8211f_tx_clock_oddr" in verilog
    assert "build_target_host_phy_clkout0" not in verilog
    assert verilog.count("IDELAYE2 #(") == 5
    assert verilog.count(".IDELAY_VALUE     (4'd10)") == 5
    assert "-group [get_clocks -include_generated_clocks clk50]" in tcl
    assert "-group [get_clocks -include_generated_clocks eth_clocks_rx]" in tcl
    for root_clock in (
        "sfp0_txoutclk",
        "sfp0_rxoutclk",
        "sfp1_txoutclk",
        "sfp1_rxoutclk",
    ):
        assert f"-group [get_clocks -include_generated_clocks {root_clock}]" in tcl
