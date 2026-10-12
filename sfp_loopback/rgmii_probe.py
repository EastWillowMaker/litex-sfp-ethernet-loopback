"""Passive CPU-free probe for Rise Pro RTL8211F RGMII clock routing."""

import argparse
from pathlib import Path

from migen import If, Signal
from migen.genlib.cdc import MultiReg
from litex.build.generic_platform import IOStandard, Pins, Subsignal
from litex.gen import LiteXModule
from litex.soc.cores.spi_flash import S7SPIFlash
from litex.soc.integration.builder import Builder
from litex.soc.integration.soc_core import SoCCore
from litex.soc.interconnect.csr import CSRStatus, CSRStorage
from liteeth.phy.common import LiteEthPHYMDIO

from sfp_loopback.target import ClockReset, Platform


class ClockActivity(LiteXModule):
    """Count sampled rising transitions on one asynchronous candidate pin."""

    def __init__(self, pad):
        self.rising_edges = CSRStatus(
            32,
            name="rising_edges",
            description="Sampled rising transitions; activity indicator, not frequency.",
        )
        synchronized = Signal()
        previous = Signal()
        count = Signal(32)
        self.specials += MultiReg(pad, synchronized)
        self.sync += [
            If(synchronized & ~previous, count.eq(count + 1)),
            previous.eq(synchronized),
        ]
        self.comb += self.rising_edges.status.eq(count)


class HostPHYReset(LiteXModule):
    """Hold the RTL8211F in reset for 10 ms after configuration."""

    def __init__(self, sys_clk_freq):
        self.reset = CSRStorage(
            1,
            reset=0,
            name="reset",
            description="Assert RTL8211F reset while set; reset is also held for 10 ms at startup.",
        )
        self.reset_n = Signal()
        self.reset_done = CSRStatus(
            1,
            name="reset_done",
            description="The initial 10 ms hardware reset interval has completed.",
        )

        reset_cycles = round(10e-3 * sys_clk_freq)
        counter = Signal(max=reset_cycles + 1)
        done = Signal()
        self.comb += [
            done.eq(counter == reset_cycles),
            self.reset_n.eq(done & ~self.reset.storage),
            self.reset_done.status.eq(done),
        ]
        self.sync += If(~done, counter.eq(counter + 1))


class RGMIIClockProbeSoC(SoCCore):
    """Observe three possible RX-clock pins without driving any of them."""

    def __init__(self):
        platform = Platform()
        platform.add_extension(
            [
                (
                    "rgmii_clock_candidates",
                    0,
                    Subsignal("c18", Pins("C18")),
                    Subsignal("c19", Pins("C19")),
                    Subsignal("e19", Pins("E19")),
                    IOStandard("LVCMOS33"),
                ),
                ("probe_phy_reset_n", 0, Pins("E21"), IOStandard("LVCMOS33")),
                (
                    "probe_mdio",
                    0,
                    Subsignal("mdc", Pins("G21")),
                    Subsignal("mdio", Pins("G22")),
                    IOStandard("LVCMOS33"),
                ),
            ]
        )

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
            ident="Rise Pro RGMII clock probe v1",
        )
        self.add_uartbone(baudrate=115200)

        clocks = platform.request("rgmii_clock_candidates")
        self.rgmii_clock_probe = LiteXModule()
        self.rgmii_clock_probe.c18 = ClockActivity(clocks.c18)
        self.rgmii_clock_probe.c19 = ClockActivity(clocks.c19)
        self.rgmii_clock_probe.e19 = ClockActivity(clocks.e19)

        self.host_phy_crg = HostPHYReset(sys_clk_freq)
        self.comb += platform.request("probe_phy_reset_n").eq(
            self.host_phy_crg.reset_n
        )
        self.host_phy = LiteXModule()
        self.host_phy.mdio = LiteEthPHYMDIO(platform.request("probe_mdio"))

        # Keep the existing board programming path available, without adding
        # any activity or output drivers to the RGMII clock candidates.
        flash = platform.request("flash_probe")
        self.comb += [flash.wp.eq(1), flash.hold.eq(1)]
        self.spiflash = S7SPIFlash(flash, sys_clk_freq, spi_clk_freq=1e6)


def build_probe(output_dir="build/rgmii_probe", run=False):
    """Elaborate or build the passive RGMII clock probe."""
    builder = Builder(
        RGMIIClockProbeSoC(),
        output_dir=str(Path(output_dir)),
        compile_software=False,
    )
    builder.build(run=run, build_name="rgmii_clock_probe")
    print(f"CSR map: {Path(builder.csr_csv)}")
    return builder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="Run synthesis and implementation.")
    parser.add_argument("--output-dir", default="build/rgmii_probe")
    args = parser.parse_args()
    build_probe(output_dir=args.output_dir, run=args.build)


if __name__ == "__main__":
    main()
