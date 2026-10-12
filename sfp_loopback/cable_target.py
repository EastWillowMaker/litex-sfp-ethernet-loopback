"""Three-port, CPU-free Rise Pro SFP cable-loop test gateware."""

import argparse
import csv
from pathlib import Path

from migen import ClockDomain, Instance, Signal
from migen.genlib.cdc import MultiReg
from litex.gen import ClockDomainsRenamer, LiteXModule
from litex.soc.cores.clock import S7IDELAYCTRL
from litex.soc.cores.spi_flash import S7SPIFlash
from litex.soc.integration.builder import Builder
from litex.soc.integration.soc_core import SoCCore
from litex.soc.interconnect.csr import CSRStatus, CSRStorage
from liteeth.mac.core import LiteEthMACCore
from liteeth.phy import A7_1000BASEX
from liteeth.phy.serial.gtp_7series import QPLL, QPLLSettings

from sfp_loopback.cable import CableLoop
from sfp_loopback.rgmii import RegionalS7PHYRGMII
from sfp_loopback.target import ClockReset, Platform


class CableControl(LiteXModule):
    def __init__(self, cable):
        self.direction = CSRStorage(
            1,
            reset=0,
            name="direction",
            description="0: SFP1 TX to SFP2 RX; 1: SFP2 TX to SFP1 RX.",
        )
        self.enable = CSRStorage(
            1,
            reset=0,
            name="enable",
            description="Enable cable test traffic after selecting a direction.",
        )
        self.comb += [
            cable.direction.eq(self.direction.storage),
            cable.enable.eq(self.enable.storage),
        ]


class CableStatus(LiteXModule):
    def __init__(self, cable):
        self.active_direction = CSRStatus(
            1,
            name="active_direction",
            description="Direction latched for the current cable transaction.",
        )
        self.busy = CSRStatus(
            1,
            name="busy",
            description="A cable transaction is awaiting its return frame.",
        )
        self.comb += [
            self.active_direction.status.eq(cable.active_direction),
            self.busy.status.eq(cable.busy),
        ]


class FrameCounterBank(LiteXModule):
    """Expose one packet path's independent 32-bit counters as CSRs."""

    def __init__(self, counters):
        for name, source in counters.items():
            counter = CSRStatus(32, name=name)
            setattr(self, name, counter)
            self.comb += counter.status.eq(source)


class SFPStatus(LiteXModule):
    def __init__(self, index, phy, qpll_channel, los):
        self.link_up = CSRStatus(1, name="link_up")
        self.qpll_locked = CSRStatus(1, name="qpll_locked")
        self.tx_ready = CSRStatus(1, name="tx_ready")
        self.rx_ready = CSRStatus(1, name="rx_ready")
        self.loss_of_signal = CSRStatus(1, name="loss_of_signal")
        self.control_tx_disable = CSRStorage(
            1,
            reset=0,
            name="control_tx_disable",
            description=f"Drive SFP{index + 1} TX_DISABLE high for a negative control.",
        )
        for source, target in (
            (phy.link_up, self.link_up.status),
            (qpll_channel.lock, self.qpll_locked.status),
            (phy.tx_init.done, self.tx_ready.status),
            (phy.rx_init.done, self.rx_ready.status),
            (los, self.loss_of_signal.status),
        ):
            self.specials += MultiReg(source, target)


class PHYResetStatus(LiteXModule):
    def __init__(self, phy):
        self.reset_done = CSRStatus(
            1,
            name="reset_done",
            description="RTL8211F reset interval completed and reset deasserted.",
        )
        self.comb += self.reset_done.status.eq(~phy.crg.reset)


class CableClockReset(ClockReset):
    """Add the calibrated 200 MHz IDELAY reference to the existing board CRG."""

    def __init__(self, platform, sys_clk_freq):
        super().__init__(platform, sys_clk_freq)
        self.cd_idelay = ClockDomain("idelay", reset_less=True)
        self.pll.create_clkout(self.cd_idelay, 200e6)
        self.idelayctrl = S7IDELAYCTRL(self.cd_idelay)


class CableSoC(SoCCore):
    """Host-facing RTL8211F and two shared-reference SFP PHY/MAC paths."""

    def __init__(self):
        platform = Platform()
        sys_clk_freq = 50e6
        self.crg = CableClockReset(platform, sys_clk_freq)
        super().__init__(
            platform,
            sys_clk_freq,
            cpu_type=None,
            integrated_rom_size=0,
            integrated_sram_size=0,
            with_uart=False,
            with_timer=False,
            with_ctrl=False,
            ident="Rise Pro SFP cable loopback v1",
        )
        self.add_uartbone(baudrate=115200)

        # One differential input clock feeds both QPLL engines in one
        # GTPE2_COMMON. Each GTP channel gets its own QPLL reset/lock signals.
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

        self.sfp0_phy = self._make_sfp_phy(platform, index=0)
        self.sfp1_phy = self._make_sfp_phy(platform, index=1)
        self.sfp0_mac = self._make_mac(self.sfp0_phy, index=0)
        self.sfp1_mac = self._make_mac(self.sfp1_phy, index=1)

        rgmii_clocks = platform.request("eth_clocks")
        self.host_phy = RegionalS7PHYRGMII(
            clock_pads=rgmii_clocks,
            pads=platform.request("eth"),
            tx_clock_pll=self.crg.pll,
            with_hw_init_reset=True,
            tx_delay=2e-9,
            # RTL8211F RXCR bit 3 supplies the RGMII receive clock delay.
            # A 0.75 ns data IDELAY (10 taps at 200 MHz) balances the measured
            # residual hold deficit after regional BUFR clock routing.
            rx_delay=0.75e-9,
            iodelay_clk_freq=200e6,
            hw_reset_cycles=round(10e-3 * sys_clk_freq),
        )
        # The RGMII receive clock is sourced by the RTL8211F. RX capture is
        # local to its bank; the host TX MAC uses two outputs of the system
        # PLL so its clock can reach the whole fabric and forwarded TXC pin.
        self.host_mac = self._make_mac(self.host_phy, index=None)

        self.cable = CableLoop(transaction_timeout_cycles=round(sys_clk_freq))
        self.comb += [
            self.host_mac.source.connect(self.cable.host_sink),
            self.cable.host_source.connect(self.host_mac.sink),
            self.sfp0_mac.source.connect(self.cable.sfp0_sink),
            self.cable.sfp0_source.connect(self.sfp0_mac.sink),
            self.sfp1_mac.source.connect(self.cable.sfp1_sink),
            self.cable.sfp1_source.connect(self.sfp1_mac.sink),
        ]

        self._add_cable_control()
        self._add_path_status(platform)
        self._add_path_counters()
        self._add_flash(platform, sys_clk_freq)
        self._add_leds(platform)
        self._add_clock_constraints(platform)

    def _make_sfp_phy(self, platform, index):
        phy = A7_1000BASEX(
            qpll_channel=self.qpll.channels[index],
            data_pads=platform.request("sfp", index),
            sys_clk_freq=50e6,
            with_pcs_buffers=True,
            tx_cm_type="MMCM",
            tx_cm_buf_type="BUFG",
            rx_cm_buf_type="BUFG",
        )
        prefix = f"sfp{index}"
        phy.txoutclk.name_override = f"{prefix}_txoutclk"
        phy.rxoutclk.name_override = f"{prefix}_rxoutclk"
        remap = {
            "eth_tx": f"{prefix}_tx",
            "eth_rx": f"{prefix}_rx",
            "eth_tx_half": f"{prefix}_tx_half",
            "eth_rx_half": f"{prefix}_rx_half",
        }
        return ClockDomainsRenamer(remap)(phy)

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
        if index is None:
            return mac
        prefix = f"sfp{index}"
        remap = {
            "eth_tx": f"{prefix}_tx",
            "eth_rx": f"{prefix}_rx",
            "eth_tx_half": f"{prefix}_tx_half",
            "eth_rx_half": f"{prefix}_rx_half",
        }
        return ClockDomainsRenamer(remap)(mac)

    def _add_cable_control(self):
        self.cable_control = CableControl(self.cable)
        self.cable_status = CableStatus(self.cable)

    def _add_path_status(self, platform):
        self.sfp0 = SFPStatus(
            0, self.sfp0_phy, self.qpll.channels[0], platform.request("sfp_rx_los", 0)
        )
        self.sfp1 = SFPStatus(
            1, self.sfp1_phy, self.qpll.channels[1], platform.request("sfp_rx_los", 1)
        )
        self.comb += [
            platform.request("sfp_tx_disable", 0).eq(self.sfp0.control_tx_disable.storage),
            platform.request("sfp_tx_disable", 1).eq(self.sfp1.control_tx_disable.storage),
        ]

        # Verify the onboard RGMII PHY's reset released after its bounded
        # 10 ms hardware reset interval. The active-low pad is driven by PHY CRG.
        self.rtl8211f = PHYResetStatus(self.host_phy)

    def _add_path_counters(self):
        counter_names = (
            "rx_frames",
            "tx_frames",
            "rejected_frames",
            "error_frames",
            "overflow_frames",
        )
        self.host_ingress = FrameCounterBank({
            name: getattr(self.cable, f"host_ingress_{name}") for name in counter_names
        })
        self.sfp0_return = FrameCounterBank({
            name: getattr(self.cable, f"sfp0_return_{name}") for name in counter_names
        })
        self.sfp1_return = FrameCounterBank({
            name: getattr(self.cable, f"sfp1_return_{name}") for name in counter_names
        })
        self.sfp0_unexpected_test = FrameCounterBank({
            "frames": self.cable.sfp0_unexpected_test_frames
        })
        self.sfp1_unexpected_test = FrameCounterBank({
            "frames": self.cable.sfp1_unexpected_test_frames
        })
        self.host_tx = FrameCounterBank({"frames": self.cable.host_tx_frames})
        self.sfp0_tx = FrameCounterBank({"frames": self.cable.sfp0_tx_frames})
        self.sfp1_tx = FrameCounterBank({"frames": self.cable.sfp1_tx_frames})

    def _add_flash(self, platform, sys_clk_freq):
        flash = platform.request("flash_probe")
        self.comb += [flash.wp.eq(1), flash.hold.eq(1)]
        self.spiflash = S7SPIFlash(flash, sys_clk_freq, spi_clk_freq=1e6)

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
        # These GTP outputs are 62.5 MHz gearbox clocks. Each SFP PHY also
        # derives its 125 MHz PCS clocks from them through local MMCM/PLL blocks.
        for phy in (self.sfp0_phy, self.sfp1_phy):
            platform.add_period_constraint(phy.txoutclk, 16.0)
            platform.add_period_constraint(phy.rxoutclk, 16.0)

        # The board routes RTL8211F RXC to C19, the N side of the C18/C19
        # CCIO pair; C18 is occupied by the separate TXC output. The custom
        # PHY uses IBUF -> regional BUFR, then keeps RX capture and associated
        # BRAMs local to the source bank. Allow fabric routing only on this
        # named IBUF-output segment. AMD UG903 warns this can still make clock
        # skew unpredictable, so routed setup/hold are checked before emitting
        # a bitstream.
        platform.add_platform_command(
            "set_property CLOCK_DEDICATED_ROUTE FALSE "
            "[get_nets rtl8211f_rgmii_rx_ibuf]"
        )

        # Keep crossings between the system, onboard RGMII, and independent
        # recovered SFP clocks asynchronous. Generated clocks within a path
        # remain grouped with their root clock. Use clock names, not derived
        # net names: synthesis may optimize clock-domain nets into aliases.
        clock_roots = (
            "clk50",
            "eth_clocks_rx",
            "sfp0_txoutclk",
            "sfp0_rxoutclk",
            "sfp1_txoutclk",
            "sfp1_rxoutclk",
        )
        groups = " ".join(
            f"-group [get_clocks -include_generated_clocks {name}]"
            for name in clock_roots
        )

        # RTL8211F Rev. 1.4 Table 60 gives internal-delay TsetupT/TholdT
        # minima of 1.2 ns and typical values of 2 ns, with no published max.
        # The RXC delay mode is enabled over MDIO while TX delay is disabled.
        # Model both DDR transition windows as +1.2 to +2.8 ns relative to the
        # preceding RXC edge; +2.8 ns is this project's assumed upper bound
        # around the 2 ns typical value, not a datasheet maximum. Board traces
        # are assumed matched; pairwise skew has not been measured.
        # These commands belong in XDC: Vivado reads XDC with an open design,
        # while pre-synthesis Tcl runs before synthesis has opened one.
        # Timing values are relative to the PHY-facing clock at each port.
        platform.add_platform_command(
            "create_clock -name eth_clocks_rx -period 8.0 [get_ports eth_clocks_rx]"
        )
        platform.add_platform_command(
            "set_input_delay -clock [get_clocks eth_clocks_rx] -max 2.8 "
            "[get_ports {{eth_rx_ctl eth_rx_data[*]}}]"
        )
        platform.add_platform_command(
            "set_input_delay -clock [get_clocks eth_clocks_rx] -min 1.2 "
            "[get_ports {{eth_rx_ctl eth_rx_data[*]}}]"
        )
        platform.add_platform_command(
            "set_input_delay -clock [get_clocks eth_clocks_rx] -clock_fall -max 2.8 "
            "[get_ports {{eth_rx_ctl eth_rx_data[*]}}]"
        )
        platform.add_platform_command(
            "set_input_delay -clock [get_clocks eth_clocks_rx] -clock_fall -min 1.2 "
            "[get_ports {{eth_rx_ctl eth_rx_data[*]}}]"
        )
        # Create the forwarded output clock from the actual ODDR input pin.
        # The pin's master clock is generated by the system PLL and is only
        # available after synthesis opens the design; place these commands
        # before asynchronous clock grouping in the post-synthesis hook.
        platform.toolchain.pre_optimize_commands.add(
            "create_generated_clock -name rtl8211f_rgmii_tx "
            "-source [get_pins rtl8211f_tx_clock_oddr/C] -divide_by 1 "
            "[get_ports eth_clocks_tx]"
        )
        platform.toolchain.pre_optimize_commands.add(
            "set_output_delay -clock [get_clocks rtl8211f_rgmii_tx] -max 1.0 "
            "[get_ports {{{{eth_tx_ctl eth_tx_data[*]}}}}]"
        )
        platform.toolchain.pre_optimize_commands.add(
            "set_output_delay -clock [get_clocks rtl8211f_rgmii_tx] -min -1.0 -add_delay "
            "[get_ports {{{{eth_tx_ctl eth_tx_data[*]}}}}]"
        )
        platform.toolchain.pre_optimize_commands.add(
            "set_output_delay -clock [get_clocks rtl8211f_rgmii_tx] -clock_fall -max 1.0 -add_delay "
            "[get_ports {{{{eth_tx_ctl eth_tx_data[*]}}}}]"
        )
        platform.toolchain.pre_optimize_commands.add(
            "set_output_delay -clock [get_clocks rtl8211f_rgmii_tx] -clock_fall -min -1.0 -add_delay "
            "[get_ports {{{{eth_tx_ctl eth_tx_data[*]}}}}]"
        )
        platform.toolchain.pre_optimize_commands.add(
            f"set_clock_groups -asynchronous {groups}"
        )

        # Vivado otherwise writes a bitstream even when routed timing fails.
        # Require constrained setup and hold paths, then reject any negative
        # worst slack before the generated write_bitstream command can run.
        platform.toolchain.bitstream_commands.append(
            "set cable_setup_paths [get_timing_paths -delay_type max -max_paths 1]\n"
            "set cable_hold_paths [get_timing_paths -delay_type min -max_paths 1]\n"
            "if {{[llength $cable_setup_paths] == 0 || [llength $cable_hold_paths] == 0}} {{\n"
            "  error \"Cable target timing gate failed: no setup or hold timing path.\"\n"
            "}}\n"
            "set cable_setup_slack [get_property SLACK [lindex $cable_setup_paths 0]]\n"
            "set cable_hold_slack [get_property SLACK [lindex $cable_hold_paths 0]]\n"
            "if {{$cable_setup_slack < 0.0 || $cable_hold_slack < 0.0}} {{\n"
            "  error [format \"Cable target timing gate failed: setup slack %.3f ns, hold slack %.3f ns.\" $cable_setup_slack $cable_hold_slack]\n"
            "}}\n"
            "puts [format \"Cable target timing gate passed: setup slack %.3f ns, hold slack %.3f ns.\" $cable_setup_slack $cable_hold_slack]"
        )


def build_target(output_dir="build/sfp_cable", run=False):
    """Elaborate or build the separate SFP cable-loop target."""
    output_dir = Path(output_dir)
    if run:
        gateware_dir = output_dir / "gateware"
        for suffix in (".bit", ".bin"):
            stale_image = gateware_dir / f"sfp_cable_loopback{suffix}"
            if stale_image.exists():
                stale_image.unlink()
    soc = CableSoC()
    builder = Builder(soc, output_dir=str(output_dir), compile_software=False)
    builder.build(run=run, build_name="sfp_cable_loopback")
    print(f"CSR map: {Path(builder.csr_csv)}")
    return builder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="Run synthesis and implementation.")
    parser.add_argument("--output-dir", default="build/sfp_cable")
    args = parser.parse_args()
    build_target(output_dir=args.output_dir, run=args.build)


if __name__ == "__main__":
    main()
