"""Dedicated, CPU-free Rise Pro a7-35 SFP2 frame-test gateware."""

import argparse
from pathlib import Path

from migen import ClockDomain, Instance, Signal
from migen.genlib.cdc import MultiReg
from litex.gen import LiteXModule
from litex.build.generic_platform import IOStandard, Pins, Subsignal
from litex.soc.cores.clock import S7PLL
from litex.soc.cores.spi_flash import S7SPIFlash
from litex.soc.integration.builder import Builder
from litex.soc.integration.soc_core import SoCCore
from litex.soc.interconnect.csr import CSRStatus
from litex_boards.platforms.embedfire_rise_pro import Platform as RiseProPlatform
from liteeth.mac.core import LiteEthMACCore
from liteeth.phy import A7_1000BASEX
from liteeth.phy.serial.gtp_7series import QPLL, QPLLSettings

from sfp_loopback.responder import FrameResponder


class Platform(RiseProPlatform):
    """Retain the board definition and add the documented onboard clock route."""

    def __init__(self):
        super().__init__(variant="a7-35")
        self.add_extension([
            ("onboard_gtp_refclk", 0,
                Subsignal("p", Pins("F6")),
                Subsignal("n", Pins("E6"))),
            # CCLK is driven through STARTUPE2, not a fabric output pin.
            ("flash_probe", 0,
                Subsignal("cs_n", Pins("T19")),
                Subsignal("mosi", Pins("P22")),
                Subsignal("miso", Pins("R22")),
                Subsignal("wp", Pins("P21")),
                Subsignal("hold", Pins("R21")),
                IOStandard("LVCMOS33")),
        ])
        self.add_platform_command("set_property CFGBVS VCCO [current_design]")
        self.add_platform_command("set_property CONFIG_VOLTAGE 3.3 [current_design]")


class ClockReset(LiteXModule):
    def __init__(self, platform, sys_clk_freq):
        self.cd_sys = ClockDomain("sys")
        self.pll = pll = S7PLL(speedgrade=-2)
        clk = platform.request("clk50")
        self.comb += pll.reset.eq(~platform.request("cpu_reset_n"))
        pll.register_clkin(clk, 50e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)
        platform.add_false_path_constraints(self.cd_sys.clk, pll.clkin)


class TestStatus(LiteXModule):
    def __init__(self, phy, qpll_channel, responder, los):
        self.link = CSRStatus(name="link")
        self.lock = CSRStatus(name="lock")
        self.tx_ready = CSRStatus(name="tx_ready")
        self.rx_ready = CSRStatus(name="rx_ready")
        self.loss_of_signal = CSRStatus(name="loss_of_signal")
        for source, target in [
            (phy.link_up, self.link.status),
            (qpll_channel.lock, self.lock.status),
            (phy.tx_init.done, self.tx_ready.status),
            (phy.rx_init.done, self.rx_ready.status),
            (los, self.loss_of_signal.status),
        ]:
            self.specials += MultiReg(source, target)
        for name in ("rx_frames", "tx_frames", "rejected_frames", "error_frames", "overflow_frames"):
            counter = CSRStatus(32, name=name)
            setattr(self, name, counter)
            self.comb += counter.status.eq(getattr(responder, name))


class LoopbackSoC(SoCCore):
    def __init__(self):
        platform = Platform()
        sys_clk_freq = 50e6
        self.crg = ClockReset(platform, sys_clk_freq)
        super().__init__(platform, sys_clk_freq,
            cpu_type=None, integrated_rom_size=0, integrated_sram_size=0,
            with_uart=False, with_timer=False, with_ctrl=False,
            ident="Rise Pro SFP2 frame loopback v1")
        self.add_uartbone(baudrate=115200)

        refclk_pads = platform.request("onboard_gtp_refclk")
        refclk = Signal()
        self.specials += Instance("IBUFDS_GTE2",
            i_CEB=0, i_I=refclk_pads.p, i_IB=refclk_pads.n, o_O=refclk)
        platform.add_period_constraint(refclk_pads.p, 8.0)
        self.qpll = QPLL(gtrefclk0=refclk,
            qpllsettings0=QPLLSettings(refclksel=0b001, fbdiv=4, fbdiv_45=5, refclk_div=1))
        self.ethphy = A7_1000BASEX(self.qpll.channels[0],
            platform.request("sfp", 1), sys_clk_freq=sys_clk_freq)
        # GTP output clocks are not inferred as root clocks by Vivado.
        platform.add_period_constraint(self.ethphy.txoutclk, 16.0)
        platform.add_period_constraint(self.ethphy.rxoutclk, 16.0)
        self.comb += platform.request("sfp_tx_disable", 1).eq(0)
        self.mac = LiteEthMACCore(self.ethphy, dw=8, with_sys_datapath=True,
            with_store_and_forward=True, tx_cdc_depth=64, rx_cdc_depth=64)
        self.responder = FrameResponder()
        self.comb += [
            self.mac.source.connect(self.responder.sink),
            self.responder.source.connect(self.mac.sink),
        ]
        self.test_status = TestStatus(self.ethphy, self.qpll.channels[0],
            self.responder, platform.request("sfp_rx_los", 1))

        flash = platform.request("flash_probe")
        self.comb += [flash.wp.eq(1), flash.hold.eq(1)]
        self.spiflash = S7SPIFlash(flash, sys_clk_freq, spi_clk_freq=1e6)

        # LED0: clock heartbeat, LED1: PCS link established.
        heartbeat = Signal(26)
        self.sync += heartbeat.eq(heartbeat + 1)
        self.comb += [
            platform.request("user_led", 0).eq(heartbeat[25]),
            platform.request("user_led", 1).eq(self.test_status.link.status),
        ]
        # The user and Ethernet clock domains cross only through proper CDC stages.
        platform.add_false_path_constraints(self.crg.cd_sys.clk,
            self.ethphy.cd_eth_tx.clk, self.ethphy.cd_eth_rx.clk)
        # Include both 62.5 MHz gearbox clocks and 125 MHz PCS clocks in each
        # asynchronous group, retaining timing within each related family.
        platform.toolchain.pre_optimize_commands.add(
            "set_clock_groups -asynchronous "
            "-group [get_clocks -include_generated_clocks -of_objects [get_nets {sys}]] "
            "-group [get_clocks -include_generated_clocks -of_objects [get_nets {tx}]] "
            "-group [get_clocks -include_generated_clocks -of_objects [get_nets {rx}]]",
            sys=self.crg.cd_sys.clk, tx=self.ethphy.txoutclk, rx=self.ethphy.rxoutclk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="Run synthesis and implementation.")
    parser.add_argument("--output-dir", default="build/sfp2")
    args = parser.parse_args()
    soc = LoopbackSoC()
    builder = Builder(soc, output_dir=args.output_dir, compile_software=False)
    builder.build(run=args.build, build_name="sfp2_loopback")
    print(f"CSR map: {Path(builder.csr_csv)}")


if __name__ == "__main__":
    main()
