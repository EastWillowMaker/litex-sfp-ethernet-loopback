"""CPU-free, two-port IPv4/ARP Ethernet bridge for the Rise Pro SFPs."""

import argparse
from pathlib import Path

from migen import Instance, Signal
from litex.gen import ClockDomainsRenamer, LiteXModule
from litex.soc.integration.builder import Builder
from litex.soc.integration.soc_core import SoCCore
from liteeth.mac.core import LiteEthMACCore
from liteeth.phy import A7_1000BASEX
from liteeth.phy.serial.gtp_7series import QPLL, QPLLSettings

from sfp_loopback.bridge import EtherTypeBridge
from sfp_loopback.cable_target import FrameCounterBank, SFPStatus
from sfp_loopback.target import ClockReset, Platform


class BridgeSoC(SoCCore):
    """Two SFP PHY/MAC ports connected by a hardware EtherType bridge."""

    def __init__(self):
        platform = Platform()
        sys_clk_freq = 50e6
        self.crg = ClockReset(platform, sys_clk_freq)
        super().__init__(
            platform,
            sys_clk_freq,
            cpu_type=None,
            integrated_rom_size=0,
            integrated_sram_size=0,
            with_uart=False,
            with_timer=False,
            with_ctrl=False,
            ident="Rise Pro dual-SFP IPv4/ARP bridge v1",
        )
        self.add_uartbone(baudrate=115200)

        # Both channel-specific QPLL engines share this one GTPE2_COMMON and
        # the board's documented 125 MHz F6/E6 reference clock.
        refclk_pads = platform.request("onboard_gtp_refclk")
        refclk = Signal()
        self.specials += Instance(
            "IBUFDS_GTE2",
            i_CEB=0,
            i_I=refclk_pads.p,
            i_IB=refclk_pads.n,
            o_O=refclk,
        )
        platform.add_period_constraint(refclk_pads.p, 8.0)
        qpll_settings = QPLLSettings(
            refclksel=0b001,
            fbdiv=4,
            fbdiv_45=5,
            refclk_div=1,
        )
        self.qpll = QPLL(
            gtrefclk0=refclk,
            qpllsettings0=qpll_settings,
            qpllsettings1=qpll_settings,
        )

        self.sfp0_phy = self._make_sfp_phy(platform, index=0, sys_clk_freq=sys_clk_freq)
        self.sfp1_phy = self._make_sfp_phy(platform, index=1, sys_clk_freq=sys_clk_freq)
        self.sfp0_mac = self._make_mac(self.sfp0_phy, index=0)
        self.sfp1_mac = self._make_mac(self.sfp1_phy, index=1)

        self.bridge = EtherTypeBridge()
        self.comb += [
            self.sfp0_mac.source.connect(self.bridge.sfp0_sink),
            self.bridge.sfp0_source.connect(self.sfp0_mac.sink),
            self.sfp1_mac.source.connect(self.bridge.sfp1_sink),
            self.bridge.sfp1_source.connect(self.sfp1_mac.sink),
        ]

        self._add_port_status(platform)
        self._add_forward_status()
        self._add_leds(platform)
        self._add_clock_constraints(platform)

    def _make_sfp_phy(self, platform, index, sys_clk_freq):
        phy = A7_1000BASEX(
            qpll_channel=self.qpll.channels[index],
            data_pads=platform.request("sfp", index),
            sys_clk_freq=sys_clk_freq,
            with_pcs_buffers=True,
            tx_cm_type="MMCM",
            tx_cm_buf_type="BUFG",
            rx_cm_buf_type="BUFG",
        )
        prefix = f"sfp{index}"
        phy.txoutclk.name_override = f"{prefix}_txoutclk"
        phy.rxoutclk.name_override = f"{prefix}_rxoutclk"
        return ClockDomainsRenamer(
            {
                "eth_tx": f"{prefix}_tx",
                "eth_rx": f"{prefix}_rx",
                "eth_tx_half": f"{prefix}_tx_half",
                "eth_rx_half": f"{prefix}_rx_half",
            }
        )(phy)

    @staticmethod
    def _make_mac(phy, index):
        mac = LiteEthMACCore(
            phy,
            dw=8,
            with_sys_datapath=True,
            with_store_and_forward=True,
            tx_cdc_depth=2048,
            rx_cdc_depth=2048,
        )
        prefix = f"sfp{index}"
        return ClockDomainsRenamer(
            {
                "eth_tx": f"{prefix}_tx",
                "eth_rx": f"{prefix}_rx",
                "eth_tx_half": f"{prefix}_tx_half",
                "eth_rx_half": f"{prefix}_rx_half",
            }
        )(mac)

    def _add_port_status(self, platform):
        self.sfp0 = SFPStatus(
            0,
            self.sfp0_phy,
            self.qpll.channels[0],
            platform.request("sfp_rx_los", 0),
        )
        self.sfp1 = SFPStatus(
            1,
            self.sfp1_phy,
            self.qpll.channels[1],
            platform.request("sfp_rx_los", 1),
        )
        self.comb += [
            platform.request("sfp_tx_disable", 0).eq(self.sfp0.control_tx_disable.storage),
            platform.request("sfp_tx_disable", 1).eq(self.sfp1.control_tx_disable.storage),
        ]

    @staticmethod
    def _forward_counters(forwarder):
        return {
            "rx_frames": forwarder.rx_frames,
            "tx_frames": forwarder.tx_frames,
            "ipv4_frames": forwarder.ipv4_frames,
            "arp_frames": forwarder.arp_frames,
            "rejected_frames": forwarder.rejected_frames,
            "error_frames": forwarder.error_frames,
            "overflow_frames": forwarder.overflow_frames,
            "ipv6_drops": forwarder.ipv6_drops,
            "unsupported_drops": forwarder.unsupported_drops,
        }

    def _add_forward_status(self):
        self.sfp0_to_sfp1 = FrameCounterBank(
            self._forward_counters(self.bridge.forward0)
        )
        self.sfp1_to_sfp0 = FrameCounterBank(
            self._forward_counters(self.bridge.forward1)
        )

    def _add_leds(self, platform):
        heartbeat = Signal(26)
        self.sync += heartbeat.eq(heartbeat + 1)
        self.comb += [
            platform.request("user_led", 0).eq(heartbeat[25]),
            platform.request("user_led", 1).eq(
                self.sfp0.link_up.status & self.sfp1.link_up.status
            ),
        ]

    def _add_clock_constraints(self, platform):
        for phy in (self.sfp0_phy, self.sfp1_phy):
            platform.add_period_constraint(phy.txoutclk, 16.0)
            platform.add_period_constraint(phy.rxoutclk, 16.0)

        # The SFP clock families are independent of each other and of sys;
        # generated clocks within each family remain timed against their root.
        clock_roots = (
            "clk50",
            "sfp0_txoutclk",
            "sfp0_rxoutclk",
            "sfp1_txoutclk",
            "sfp1_rxoutclk",
        )
        groups = " ".join(
            f"-group [get_clocks -include_generated_clocks {name}]"
            for name in clock_roots
        )
        platform.toolchain.pre_optimize_commands.add(
            f"set_clock_groups -asynchronous {groups}"
        )

        # Vivado may otherwise emit an image after a failed routed timing run.
        platform.toolchain.bitstream_commands.append(
            "set bridge_setup_paths [get_timing_paths -delay_type max -max_paths 1]\n"
            "set bridge_hold_paths [get_timing_paths -delay_type min -max_paths 1]\n"
            "if {{[llength $bridge_setup_paths] == 0 || [llength $bridge_hold_paths] == 0}} {{\n"
            "  error \"Bridge target timing gate failed: no setup or hold timing path.\"\n"
            "}}\n"
            "set bridge_setup_slack [get_property SLACK [lindex $bridge_setup_paths 0]]\n"
            "set bridge_hold_slack [get_property SLACK [lindex $bridge_hold_paths 0]]\n"
            "if {{$bridge_setup_slack < 0.0 || $bridge_hold_slack < 0.0}} {{\n"
            "  error [format \"Bridge target timing gate failed: setup slack %.3f ns, hold slack %.3f ns.\" $bridge_setup_slack $bridge_hold_slack]\n"
            "}}\n"
            "puts [format \"Bridge target timing gate passed: setup slack %.3f ns, hold slack %.3f ns.\" $bridge_setup_slack $bridge_hold_slack]"
        )


def build_bridge(output_dir="build/sfp_bridge", run=False):
    """Elaborate or build the separate SRAM-only dual-SFP bridge target."""
    output_dir = Path(output_dir)
    if run:
        gateware_dir = output_dir / "gateware"
        for suffix in (".bit", ".bin"):
            stale_image = gateware_dir / f"dual_sfp_bridge{suffix}"
            if stale_image.exists():
                stale_image.unlink()
    builder = Builder(BridgeSoC(), output_dir=str(output_dir), compile_software=False)
    builder.build(run=run, build_name="dual_sfp_bridge")
    print(f"CSR map: {Path(builder.csr_csv)}")
    return builder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="Run synthesis and implementation.")
    parser.add_argument("--output-dir", default="build/sfp_bridge")
    args = parser.parse_args()
    build_bridge(output_dir=args.output_dir, run=args.build)


if __name__ == "__main__":
    main()
