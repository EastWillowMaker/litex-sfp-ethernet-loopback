# SFP2 Ethernet frame loopback test

## Confirmed scope

- 2026-10-12: The Host PC sends Ethernet frames through the board's SFP2 connection; the board returns frames for host verification.
- The first milestone proves correctness at low traffic. Sustained-load testing comes afterward.
- The user confirms the installed hardware is the XC7A35T board with a Dawnray SFP-GE-T module.
- The user authorizes programming the test image into SPI flash; this supersedes the earlier recommendation of temporary SRAM loading.
- The board swaps source/destination MAC addresses and preserves the test payload in its reply.
- The initial test sends 1,000 frames of varied standard sizes, one at a time. Every expected reply must arrive with the correct payload; missing, duplicate, and corrupted replies are reported separately.
- The user identifies the test host connection as the new USB Ethernet adapter attached to this PC.
- Q9 accepts dedicated test-frame filtering, the five test sizes, UART counters, the one-second timeout, and the host report below.
- The user confirmed the design by invoking `$implement With Subagents`; implementation is authorized.

## Source baseline

- `litex/`: https://github.com/eastWillow/litex, commit `b2759f69bcae8be0357fb2ee5eebe216365d1f00`.
- `litex-boards/`: https://github.com/eastWillow/litex-boards, commit `c2abe2465dbe72de385a4c8b453ffb31c2d86bbb`.
- `litex-boards/litex_boards/targets/embedfire_rise_pro.py` uses `LiteEthPHYMII` and the `eth` resource for its existing Ethernet option. It does not select an SFP resource.
- `litex-boards/litex_boards/platforms/embedfire_rise_pro.py` defines separate RGMII Ethernet pads, two SFP resources, and a GTP reference-clock resource. Resource indices alone do not confirm board connector labels or the actual clock frequency.

## Remaining physical documentation

- Installed PCB revision and its correspondence with the supplied originals.
- Physical correspondence of the installed board with the documented onboard 125 MHz clock route.

The installed PCB's revision was not read. Functional tests below validate
the selected clock and port routes on the connected board; the oscillator's
frequency was not measured with an external instrument.

## Hardware sources supplied by the user

- `smb://eastwillow-nas.local/home/FPGA/ebf_artix7_hardware_2026`
- `smb://eastwillow-nas.local/home/FPGA/ebf_artix7_hardware_2026/ebf_xc7a%20pro%20board`
- Original core/Pro schematics and the Pro pin spreadsheet are accessible through the existing SMB mount. They establish documented wiring; correspondence with the installed PCB remains to be checked.
- NAS memory became readable on 2026-10-12. Its historical XC7A35T and Dawnray SFP-GE-T identifiers were subsequently reconfirmed by the user in Q6.

## Verified schematic findings (2026-10-12)

- Pro schematic `ebf_xc7a pro board/ebf_xc7a pro board_原理图.pdf`, pages 4 and 7: printed SFP2 is J6; its TX D5/C5 and RX D11/C11 match LiteX `sfp` resource index 1.
- Pro pin spreadsheet `升腾Pro引脚绑定映射表.xlsx`, sheet 1 rows 85–92: SFP2 I²C U17/U18, TX disable U20, loss-of-signal V20, and differential pins agree with the platform.
- Core schematic `ebf_xc7a core board/ebf_xc7a core board_原理图.pdf`, pages 11 and 14: U12 `DSC1103CE2-125.00` supplies a 125 MHz GTP clock on `MGT_CLK0_P/N`, FPGA pins F6/E6.
- Core page 11 and Pro pages 4/9: F10/E10 carry `MGT_CLK1_P/N` from the PCIe reference clock. The current platform's `gtp_refclk` therefore selects the PCIe input rather than the documented onboard oscillator.
- A standalone SFP2 implementation needs the documented F6/E6 onboard-clock resource. Physical board revision and actual signal presence are still unverified.

## Module and programming investigation

- Dawnray's [SFP-GE-T product documentation](https://www.dawnraytech.com/product/sfp-ge-t/) explicitly identifies host-side 1000BASE-X and cable-side 1000BASE-T. The intended link is therefore Gigabit Ethernet using a 1000BASE-X FPGA PHY.
- Rise Pro platform selects `xc7a35tfgg484-2` for `a7-35`, configures SPI x4, and emits a configuration `.bin` at offset zero with a 16 MiB capacity setting.
- Its `create_programmer()` selects Vivado configuration-memory part `mt25ql128-spi-x1_x2_x4`. This is a source setting, not proof of the installed flash model; compare against the schematic before use.
- Existing target `--flash` calls `prog.flash(0, builder.get_bitstream_filename(mode="flash"))`.
- `VivadoProgrammer.flash()` enables erase, programming, and verification over the image's address range. Its `address` argument is not applied in the generated script; offset zero in the generated image matches this target's existing usage.
- Core schematic page 7 documents Master SPI x4 boot mode (`M[2:0]=001`).
- Core schematic page 14 identifies U13 as `N25Q128A13ESE40G`, while the Pro hardware specification page 14 says `W25Q128`. Both specify 16 MiB. Actual JEDEC ID `20ba18` established the Micron descriptor used for programming.
- Implementation inspection found SPI CS on T19 in core schematic page 8. The platform and Pro spreadsheet's T10 assignment fails Vivado pin placement. The dedicated test target uses T19 and leaves the original clones unchanged.

## Accepted test details

Accepted through the user's recommendations preference and Q9:

- Use a dedicated hardware frame responder with packet buffering, avoiding a DDR3 dependency for this first test.
- Accept only the test Ethernet protocol (`0x88B5`), a test magic value, and destination MAC `02:00:00:00:00:02`; return valid test frames to the sender with swapped MAC addresses. Do not return unrelated traffic or frames with a MAC receive error.
- Include a run identifier and sequence number in the preserved payload to detect stale replies, duplicates, and mismatches.
- Send 200 frames at each of 60, 128, 256, 512, and 1514 bytes, measured from the destination MAC through payload, excluding FCS. Use one outstanding frame and a one-second reply timeout.
- Provide UART-readable link, receive/transmit, rejection, and error status, plus a host test report.
- Before persistent programming, identify the flash and check the built image/timing. Program and verify the SPI image, then confirm it boots from flash.
- Report simulation/build/flash-boot results separately from cable-loopback results. An end-to-end pass requires the actual Host PC Ethernet interface and observed replies.

## Implementation and validation sequence

1. Create an isolated build environment with the LiteX Ethernet/transceiver dependencies; record source versions.
2. Add the onboard GTP reference-clock resource and a dedicated SFP2 frame-test target for `a7-35`, using a 1000BASE-X PHY, buffered responder, and UART diagnostics.
3. Build the host test tool with an explicitly selected USB Ethernet interface. Ignore locally transmitted packets and validate returned addressing, protocol, run/sequence identifiers, length, and payload.
4. Simulate frame handling, filtering, MAC swaps, malformed/error-frame rejection, and buffer backpressure; build and check FPGA timing and placement constraints.
5. Identify the actual flash part, select compatible configuration-memory settings, program and verify the boot image through JTAG, and verify SPI boot.
6. Identify the USB Ethernet adapter once it enumerates, bring up the isolated test link, and run all 1,000 frames. Save host results and board counters. Report any physical step that cannot be verified as incomplete.

The physical checks were completed during implementation without reopening the settled design choices. The adapter became available after reconnecting, enabling cable-loopback validation.

## Completed validation (2026-10-12)

The USB adapter reappeared after reconnecting as Realtek RTL8153,
`enx98fc84e0c4e8`, MAC `98:fc:84:e0:c4:e8`. Its link negotiated 1 Gbps.
The absence-of-adapter note above describes the earlier inspection only.

- 39 tests passed, including real LiteEth MAC/FCS simulations at minimum and maximum frame sizes, receive-error rejection/recovery, and host false-pass protection.
- Mypy passed on seven first-party Python files; vendor imports lack stubs and were skipped.
- Vivado 2025.2 built the image with setup slack 1.358 ns and hold slack 0.079 ns, zero unclocked registers, and zero unconstrained internal endpoints. External UART, flash and GPIO I/O are asynchronous or slow interfaces without source-synchronous timing constraints.
- SRAM-loaded test: 1,000/1,000 replies, with all host anomaly counters zero.
- SPI erase/program/verify succeeded for Micron `20ba18` at offset zero. Vivado's boot command returned an empty string despite DONE high; its wrapper initially reported failure. The wrapper was corrected to check refreshed DONE and CRC registers. SPI boot was separately verified without another erase.
- Flash-loaded test: 1,000/1,000 replies, with all host anomaly counters zero. Board accepted/transmitted counters each increased by 1,000; receive errors, CRC errors, preamble errors, overflow and MAC drops did not increase. Twenty-four unrelated frames were rejected between counter snapshots.
- The board remains configured with the SPI-booted loopback image. A physical cold power cycle was not performed.

Evidence and image hashes are saved in [results](results/2026-10-12/summary.json).
