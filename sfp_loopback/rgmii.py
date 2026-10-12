"""Board-specific RGMII PHY clocking for the Rise Pro."""

from migen import ClockDomain, If, Instance, Signal
from migen.genlib.resetsync import AsyncResetSynchronizer
from litex.gen import ClockDomainsRenamer, ClockSignal, LiteXModule
from litex.soc.interconnect.csr import CSRStorage
from liteeth.phy.common import LiteEthPHYHWReset, LiteEthPHYMDIO
from liteeth.phy.parallel.rgmii.s7 import LiteEthPHYRGMIIRX, LiteEthPHYRGMIITX


class RegionalRGMIIClockReset(LiteXModule):
    """Capture RX locally; derive host TX clocks from the system PLL."""

    def __init__(
        self, clock_pads, pads, tx_clock_pll, with_hw_init_reset, tx_delay, hw_reset_cycles
    ):
        self._reset = CSRStorage(description="PHY reset.")
        self.cd_eth_rx = ClockDomain()
        self.cd_eth_tx = ClockDomain()
        self.cd_eth_tx_delayed = ClockDomain(reset_less=True)

        # C19 is the N side of its CCIO pair, so it cannot directly drive a
        # BUFG. Route only its IBUF output to a regional BUFR near the RGMII
        # bank; the RX MAC and its BRAM FIFOs then remain on the local clock
        # network instead of adding a full-device BUFG distribution delay.
        rx_clk_ibuf = Signal(name="rtl8211f_rgmii_rx_ibuf")
        rx_clk_ibuf.name_override = "rtl8211f_rgmii_rx_ibuf"
        self.specials += Instance(
            "IBUF",
            i_I=clock_pads.rx,
            o_O=rx_clk_ibuf,
        )
        self.specials += Instance(
            "BUFR",
            p_BUFR_DIVIDE="BYPASS",
            p_SIM_DEVICE="7SERIES",
            i_CE=1,
            i_CLR=0,
            i_I=rx_clk_ibuf,
            o_O=self.cd_eth_rx.clk,
        )
        self.cd_eth_rx.clk.name_override = "rtl8211f_rgmii_rx_regional_clk"

        # Keep this PLL output global-capable for the host TX MAC and the
        # forwarded clock. The regional RX BUFR only needs to reach the RGMII
        # IDDRs and host RX logic in its bank-local clock region.
        tx_phase = 125e6 * tx_delay * 360
        assert tx_phase < 360
        tx_clock_pll.create_clkout(self.cd_eth_tx, 125e6, with_reset=False)
        tx_clock_pll.create_clkout(self.cd_eth_tx_delayed, 125e6, phase=tx_phase)

        tx_clk_obuf = Signal()
        self.specials += [
            Instance(
                "ODDR",
                name="rtl8211f_tx_clock_oddr",
                p_DDR_CLK_EDGE="SAME_EDGE",
                i_C=ClockSignal("eth_tx_delayed"),
                i_CE=1,
                i_S=0,
                i_R=0,
                i_D1=1,
                i_D2=0,
                o_Q=tx_clk_obuf,
            ),
            Instance("OBUF", i_I=tx_clk_obuf, o_O=clock_pads.tx),
        ]

        self.reset = Signal()
        if with_hw_init_reset:
            self.hw_reset = LiteEthPHYHWReset(cycles=hw_reset_cycles)
            self.comb += self.reset.eq(self._reset.storage | self.hw_reset.reset)
        else:
            self.comb += self.reset.eq(self._reset.storage)
        if hasattr(pads, "rst_n"):
            self.comb += pads.rst_n.eq(~self.reset)
        self.specials += [
            AsyncResetSynchronizer(self.cd_eth_tx, self.reset),
            AsyncResetSynchronizer(self.cd_eth_rx, self.reset),
        ]


class RGMIIActivityCounters(LiteXModule):
    """Count raw receive-clock and valid-byte activity in the RX domain."""

    def __init__(self, source_valid):
        self.rx_clock_cycles = Signal(32)
        self.rx_source_valid_beats = Signal(32)
        self.rx_clock_cycles_gray = Signal(32)
        self.rx_source_valid_beats_gray = Signal(32)

        next_cycles = Signal(32)
        next_valid_beats = Signal(32)
        self.comb += [
            next_cycles.eq(self.rx_clock_cycles + 1),
            next_valid_beats.eq(self.rx_source_valid_beats + 1),
        ]
        self.sync.eth_rx += [
            self.rx_clock_cycles.eq(next_cycles),
            self.rx_clock_cycles_gray.eq(next_cycles ^ (next_cycles >> 1)),
            If(
                source_valid,
                self.rx_source_valid_beats.eq(next_valid_beats),
                self.rx_source_valid_beats_gray.eq(
                    next_valid_beats ^ (next_valid_beats >> 1)
                ),
            ),
        ]


class RegionalS7PHYRGMII(LiteXModule):
    """LiteEth-compatible RGMII PHY with a regional RX clock domain."""

    dw = 8
    tx_clk_freq = 125e6
    rx_clk_freq = 125e6

    def __init__(
        self,
        clock_pads,
        pads,
        tx_clock_pll,
        with_hw_init_reset=True,
        tx_delay=2e-9,
        rx_delay=1.5e-9,
        iodelay_clk_freq=200e6,
        hw_reset_cycles=256,
    ):
        self.crg = RegionalRGMIIClockReset(
            clock_pads,
            pads,
            tx_clock_pll,
            with_hw_init_reset,
            tx_delay,
            hw_reset_cycles,
        )
        self.tx = ClockDomainsRenamer("eth_tx")(LiteEthPHYRGMIITX(pads))
        self.rx = ClockDomainsRenamer("eth_rx")(
            LiteEthPHYRGMIIRX(pads, rx_delay, iodelay_clk_freq)
        )
        self.sink, self.source = self.tx.sink, self.rx.source

        # Gray values are registered alongside the binary counts, so the
        # asynchronous CSR sampling sees only one changed bit per increment.
        self.submodules.rx_activity = RGMIIActivityCounters(self.source.valid)
        self.rx_clock_cycles_gray = self.rx_activity.rx_clock_cycles_gray
        self.rx_source_valid_beats_gray = self.rx_activity.rx_source_valid_beats_gray

        if hasattr(pads, "mdc"):
            self.mdio = LiteEthPHYMDIO(pads)
