"""Elaboration checks for the passive RTL8211F clock-pin probe."""

import csv

from sfp_loopback.rgmii_probe import build_probe


def test_probe_exposes_clock_activity_and_phy_diagnostics_without_tx_clock(tmp_path):
    builder = build_probe(output_dir=tmp_path, run=False)

    with open(builder.csr_csv, newline="", encoding="utf-8") as csr_file:
        csr_names = {
            row[1]
            for row in csv.reader(csr_file)
            if row and row[0] == "csr_register"
        }

    assert {
        "host_phy_crg_reset",
        "host_phy_crg_reset_done",
        "host_phy_mdio_w",
        "host_phy_mdio_r",
        "rgmii_clock_probe_c18_rising_edges",
        "rgmii_clock_probe_c19_rising_edges",
        "rgmii_clock_probe_e19_rising_edges",
    } <= csr_names

    gateware = tmp_path / "gateware"
    xdc = (gateware / "rgmii_clock_probe.xdc").read_text(encoding="utf-8")
    verilog = (gateware / "rgmii_clock_probe.v").read_text(encoding="utf-8")

    for pin, port in (
        ("C18", "rgmii_clock_candidates_c18"),
        ("C19", "rgmii_clock_candidates_c19"),
        ("E19", "rgmii_clock_candidates_e19"),
    ):
        assert f"set_property LOC {pin} [get_ports {{{port}}}]" in xdc
        assert f"input  wire          {port}" in verilog

    assert "ODDR" not in verilog
    assert "eth_tx" not in verilog

