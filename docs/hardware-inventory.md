# Hardware evidence

Verified 2026-10-12 on the connected host. NAS originals were read without modification.

## Connected FPGA

Read-only Vivado 2025.2 enumeration found Digilent target
`localhost:3121/xilinx_tcf/Digilent/210357A7D00EA`, device `xc7a35t_0`,
part `xc7a35t`, IDCODE `0362D093`. Current configuration reported DONE=1,
EOS=1 and no CRC, IDCODE or security errors. Mode register pins were
M0=1, M1=0, M2=0. This query loaded no bitstream and programmed no flash.

Run `vivado -mode batch -nolog -nojournal -source scripts/inventory-jtag.tcl`
to repeat the query. Connecting can start Vivado's local hardware server and
claim the Digilent JTAG USB interface from its serial driver.

## Board wiring

Original source directory:
`/run/user/1000/gvfs/smb-share:server=eastwillow-nas.local,share=home/FPGA/ebf_artix7_hardware_2026`.

- Pro schematic (`ebf_xc7a pro board/ebf_xc7a pro board_原理图.pdf`), pages 4 and 7:
  SFP2 is J6; TX D5/C5 and RX D11/C11 match LiteX platform `sfp` index 1.
- Pro spreadsheet (`升腾Pro引脚绑定映射表.xlsx`), sheet 1 rows 85–92:
  SFP2 TX disable U20, LOS V20, I²C U17/U18; data pins match the schematic.
- Core schematic (`ebf_xc7a core board/ebf_xc7a core board_原理图.pdf`), pages 11 and 14:
  U12 DSC1103CE2-125.00 supplies 125 MHz to MGT_CLK0 on F6/E6.
  F10/E10 is MGT_CLK1, connected to PCIe REFCLK by Pro schematic pages 4 and 9.
- Core schematic page 7: fixed Master SPI boot, M[2:0]=001;
  M0 is pulled high through R27, M1/M2 grounded.
- Core schematic page 8 (visually checked): SPI chip select is T19,
  `IO_L6P_T0_FCS_B_14`; DQ0/DQ1/DQ2/DQ3 are P22/R22/P21/R21.
  Clock is dedicated CCLK L12 (page 7), accessed through STARTUPE2.
  Pro XLSX row 8 instead lists T10 for CS, which the original LiteX platform
  copied. This conflicts with the schematic; the SPI probe build's unconstrained
  CS port was reported with the T10 assignment. Use the schematic's T19 pad.

## Flash identification remains unresolved

Core schematic page 14 names U13 `N25Q128A13ESE40G`, capacity 16 MiB.
Pro hardware specification (`[野火]升腾_Pro开发板硬件规格书V1.0.1.pdf`)
page 14 instead names W25Q128, capacity 128 Mbit. Capacity agrees; populated
manufacturer/model does not follow conclusively from these conflicting sources.

Vivado 2025.2 `get_cfgmem_parts` supports `mt25ql128-spi-x1_x2_x4`,
`w25q128jvm-spi-x1_x2_x4`, and `w25q128jvq-spi-x1_x2_x4`; its N25Q128 query
returned no entries. Availability does not establish electrical compatibility
with the installed chip. The installed Vivado part table explicitly aliases
`mt25ql128-spi-x1_x2_x4` to `n25q128-3.3v-spi-x1_x2_x4` and
`w25q128jvq-spi-x1_x2_x4` to `w25q128bvq`. Its `data/xicom/spi.cfg` lists
N25Q128 ID `20ba18` and W25Q128BV/FV ID `ef4018`. These establish the intended
descriptor families for those read IDs; they do not prove the board's ID.

JTAG device enumeration exposes no flash JEDEC ID. Reading opcode 0x9f needs
SPI access through temporary FPGA gateware or a JTAG-to-SPI bridge. Loading
that bridge changes volatile FPGA configuration, although an ID read need not
erase/program flash. Obtain the actual ID before selecting a flash part.

Read-only descriptor creation and property setting were checked in Vivado
2025.2 without calling any programming command. `PROGRAM.BIN_OFFSET=0`,
`PROGRAM.ADDRESS_RANGE=use_file`, and `PROGRAM.UNUSED_PIN_TERMINATION=pull-none`
were accepted (these properties do not appear in the default property report).
`program_hw_cfgmem` accepts the cfgmem object positionally. After successful
erase/program/verify, `boot_hw_device -timeout 30 $device` triggers startup
using the physical mode pins. Although its help claims a boolean return, the
installed Vivado 2025.2 returned an empty string on successful boot with DONE
high. Validate refreshed `REGISTER.CONFIG_STATUS.BIT14_DONE_PIN=1`,
`REGISTER.CONFIG_STATUS.BIT00_CRC_ERROR=0`, and no boot fallback/errors instead
of testing its return value. It can validate
Master SPI boot without a physical power cycle; it changes FPGA configuration
and is intentionally absent from the inventory script.

## Host Ethernet adapter

Current USB sysfs inventory exposes no Ethernet adapter. Kernel journal records
Realtek RTL8153 (`0bda:8153`) at USB port `6-1.2`, with driver `r8152` and
interface `enx98fc84e0c4e8` created at 21:06:08. At 21:10:10 a register read
failed with error -110; at 21:10:21 the device rejected its address with error
-62; at 21:10:22 MAC-address reading failed and USB disconnected. The host
currently exposes Wi-Fi and virtual interfaces only. `r8152`, `r8153_ecm`, and
`cdc_ether` drivers are present. Reconnect the adapter/dock and verify USB
enumeration before a physical Ethernet loop test.

After the user's reconnect during implementation, `lsusb` and sysfs again
expose RTL8153 and `enx98fc84e0c4e8`, MAC `98:fc:84:e0:c4:e8`.
This supersedes the earlier missing-adapter snapshot above.
