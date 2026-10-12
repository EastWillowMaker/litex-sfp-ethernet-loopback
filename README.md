# Rise Pro SFP2 frame loopback

This project tests an EmbedFire Rise Pro XC7A35T with a Dawnray SFP-GE-T
copper module in SFP2. A USB Gigabit Ethernet adapter sends 1,000 Ethernet
test frames and verifies the returned frames, with source/destination MAC
addresses exchanged and the payload preserved.

The dedicated LiteX target uses onboard RAM, a 1000BASE-X PHY, and a UART
Wishbone bridge for diagnostics. It builds no CPU firmware and needs no DDR3
or IP addresses. The original LiteX-Boards clones remain unchanged; the
target adds corrected onboard reference-clock and SPI chip-select resources.

## Setup and build

Install Git, uv, and Vivado with Artix-7 support, then run from this directory:

```bash
python3 scripts/bootstrap.py
.venv/bin/python -m sfp_loopback.target --build
```

Source revisions are pinned in `sources.json`. Migen's pinned source supports
Python 3.12 clock-domain naming; its older PyPI release does not. Vivado must
be on PATH, or set `LITEX_ENV_VIVADO` to its settings script for building.
Generated files are in `build/sfp2/`; the CSR map is `build/sfp2/csr.csv`.

## Load, identify flash, and program

The defaults select the connected Digilent cable and CH340 serial port; use
`--target` or `--port` if these differ on another machine.

```bash
.venv/bin/python -m sfp_loopback.program --load --report artifacts/sram-load.json
.venv/bin/python -m sfp_loopback.diagnostics --flash-id --output artifacts/identity.json
.venv/bin/python -m sfp_loopback.program --flash --report artifacts/spi-flash.json
```

The initial SRAM load enables a read-only JEDEC identity query. Flashing selects
a Vivado descriptor from the actual identity, erases/programs only the image
range at address zero, verifies it, and boots from SPI using the board's mode
straps. Unknown IDs stop before programming. The flash command replaces the
existing boot image; this programming method was explicitly authorized for
this test. It writes a JSON report and Vivado log.

To recheck boot from the existing flash image without programming it:

```bash
.venv/bin/python -m sfp_loopback.program --boot --report artifacts/spi-boot.json
```

## Cable test

Connect SFP2 through its copper module to the USB Ethernet adapter. Identify
the adapter with `ip -br link`. LED0 is a clock heartbeat and LED1 indicates
the FPGA PCS link. Read link/PLL readiness, LOS and frame/error counters:

```bash
.venv/bin/python -m sfp_loopback.diagnostics --output artifacts/before.json
```

The test needs an active USB wired interface, carrier, and MTU of at least
1500. For the adapter seen on this PC, run:

```bash
sudo ip link set dev enx98fc84e0c4e8 up
sudo .venv/bin/python -m sfp_loopback.host \
  --interface enx98fc84e0c4e8 --report artifacts/host-loopback.json
.venv/bin/python -m sfp_loopback.diagnostics --output artifacts/after.json
```

The raw packet socket requires root or CAP_NET_RAW. The host tool makes no IP
or network-configuration changes. Outgoing packet copies cannot count as
replies. Missing, duplicate, corrupt, stale or unexpected test replies cause
failure. Sizes are 60, 128, 256, 512 and 1514 bytes excluding FCS, with 200
frames per size and a one-second reply timeout. A final receive interval
checks for trailing duplicates.

To keep sending and checking replies for 60 seconds, cycling all five sizes:

```bash
sudo .venv/bin/python -m sfp_loopback.host \
  --interface enx98fc84e0c4e8 --duration 60 \
  --report artifacts/host-sustained.json
```

The timed run finishes its outstanding reply and final duplicate check after
the sending deadline. Its report includes traffic duration, counts by size,
and observed frame rate and Mbps excluding FCS, preamble, and inter-frame gap.
These measurements describe the stop-and-wait test path; maximum throughput
needs a separate burst test. Expected reply bytes are reconstructed, so
successful traffic does not accumulate a history in memory.

If the USB adapter disappears, inspect the kernel journal and reconnect it.
If carrier remains absent, check `lock`, `tx_ready`, `rx_ready`, `link` and
`loss_of_signal` in the UART report before attempting the frame test.

## Checks

```bash
.venv/bin/python -m mypy --follow-imports skip --ignore-missing-imports \
  --check-untyped-defs sfp_loopback scripts/bootstrap.py
.venv/bin/python -m pytest tests
```

Tests cover the byte-stream responder, the host transport/report boundary,
and real LiteEth MAC simulation with FCS and clock-domain crossings. Vendor
HDL libraries lack Python type stubs, so their imports are skipped by mypy.
Vivado synthesis, timing, flash boot and real cable results are distinct
validation stages; a software or simulation pass does not establish a
hardware cable pass.

See [the agreed design](docs/ethernet-loop-test.md) and
[hardware evidence](docs/hardware-inventory.md) for wiring and test decisions.
