# Standalone ISP-to-AP IPv6 filter

Use the CPU-free dual-SFP bridge between the ISP router's LAN port and the
Zyxel NWA50AX Ethernet uplink. The confirmed AP runs in bridge mode and the
link carries untagged IPv4, using DHCP or static addressing.

```text
ISP router LAN -> printed SFP1 [FPGA] printed SFP2 -> NWA50AX uplink
                                                        -> Wi-Fi clients
```

The FPGA passes complete untagged IPv4 and ARP frames in both directions.
It drops native IPv6 in both directions, including IPv6 router advertisements
and DHCPv6. It also drops VLAN/QinQ, PPPoE and other unsupported EtherTypes.
It does not remove local IPv6 communication within the AP's Wi-Fi network,
or inspect IPv6 tunneled inside IPv4/VPN traffic. Tagged guest SSIDs are
outside this image's forwarding contract.

## Power and connections

After programming and verifying the SPI boot image, ordinary operation needs
only the board's normal independent power supply and the two Ethernet cables.
No laptop, JTAG programmer or USB UART connection is required for forwarding.
The board's documented boot straps select Master SPI (M[2:0]=001).

Keep the programmer and UART connected during programming and verification.
JTAG is needed again for firmware changes/recovery; UART is useful for counters
and link diagnostics. If the board is currently powered from a computer,
provide its normal independent board supply before removing that connection.

The FPGA does not supply or pass through PoE. Power the NWA50AX using its
normal DC adapter, or place an appropriate PoE injector after the FPGA:
`SFP2 -> injector DATA IN -> injector PoE OUT -> AP`.
See [Zyxel power specifications](https://store.zyxel.com/nwa50ax-eu0102f.html?___store=emea_de_storeview).

## Program the qualified image

The user explicitly authorized replacing the previous cable-loop SPI image
with the qualified 125 MHz bridge. Backups and hashes of both images are in
`artifacts/bridge500-spi/`; the prior cable image also remains in
`build/sfp_cable`. The bridge image has no SPI controller CSR block, so load
the known cable image into volatile SRAM solely to read the physical flash
identity before programming the bridge. The two CSR maps serve different
stages and must match those images.

```bash
.venv/bin/python -m sfp_loopback.program --load \
  --output-dir build/sfp_cable --image-basename sfp_cable_loopback \
  --status-profile cable --report artifacts/bridge500-spi/helper-load.json

.venv/bin/python -m sfp_loopback.program --flash \
  --output-dir build/sfp_bridge500 --image-basename dual_sfp_bridge \
  --status-profile bridge --flash-id-csr-csv build/sfp_cable/csr.csv \
  --report artifacts/bridge500-spi/flash.json

.venv/bin/python -m sfp_loopback.program --boot \
  --output-dir build/sfp_bridge500 --image-basename dual_sfp_bridge \
  --status-profile bridge --report artifacts/bridge500-spi/reboot.json
```

The programming operation identifies the live flash chip, selects an approved
part descriptor, erases/programs/verifies the image at offset zero, boots from
SPI, checks DONE/configuration CRC and reads the bridge's UART status. A
subsequent `--boot` repeats SPI startup without erasing or rewriting flash.
Physical cold-power verification is distinct from this JTAG-triggered boot.

## Check the real network

Before insertion, connect a computer through the NWA50AX and confirm both
IPv4 and native IPv6 connectivity work. Then insert the FPGA, reconnect Wi-Fi,
and test again. Use only this AP's network path, without an alternate uplink
or VPN for the test. IPv4 addressing, DNS and websites should continue working;
previously working native IPv6 Internet access should fail. Existing IPv6
addresses can remain visible until their lifetimes expire.

With UART temporarily connected, read the counters:

```bash
.venv/bin/python -m sfp_loopback.bridge_diagnostics \
  --csr-csv build/sfp_bridge500/csr.csv
```

Both links should report `link_up: 1`. ISP-to-AP traffic uses
`sfp0_to_sfp1`; its `ipv6_drops` should increase when ISP IPv6 frames arrive,
while IPv4/ARP frames continue to forward. Compare counter increments after
links settle, because startup errors can remain in cumulative counters.
If IPv4 fails, reconnect the ISP router directly to the AP to restore service.

The same bridge image previously measured 941.405/941.403 Mbps of TCP payload
in separate 30-second directions using the two-adapter fixture. Actual ISP
service rate and Wi-Fi throughput are separate measurements. See the
[qualification and limitations](results/2026-10-14/bridge500/impact-review.md).
[SPI deployment evidence](results/2026-10-14/bridge500-spi/summary.json)
records successful Micron20ba18 erase/program/verify, SPI boot, a second boot
without rewriting, and both Ethernet links up afterward. The optional
physical power-cycle check is still pending. ISP/AP testing remains separate.
