# Dual-SFP transparent bridge experiment

The RTL8211F cable-loop milestone is qualified in
[the saved cable results](results/2026-10-13/cable/summary.json).
This separate CPU-free target connects two host NICs through the SFP ports.
Printed SFP1 is LiteX SFP0; printed SFP2 is LiteX SFP1.

Each direction buffers a complete frame, then forwards untagged IPv4
(EtherType 0x0800) and ARP (0x0806) unchanged. IPv6, VLAN/QinQ, unsupported
EtherTypes, runts, oversize frames and MAC-reported errors are dropped whole.
Each direction has one 1,514-byte buffer. An arrival while that buffer is
occupied is rejected whole. There is no MAC learning or CPU forwarding.

## Build and temporary load

Pinned dependencies in `sources.json` and all previous targets remain in use.

```bash
.venv/bin/python -m sfp_loopback.bridge_target --build --output-dir build/sfp_bridge
.venv/bin/python -m sfp_loopback.program --load \
  --output-dir build/sfp_bridge --image-basename dual_sfp_bridge \
  --status-profile bridge --report artifacts/bridge/sram-load.json
.venv/bin/python -m sfp_loopback.bridge_diagnostics \
  --csr-csv build/sfp_bridge/csr.csv --output artifacts/bridge/status.json
```

The bridge profile supports SRAM loading only. The qualified cable-loop SPI
image remains the persistent boot image. Loading replaces the running image;
a later power cycle returns to the existing SPI image.

## Physical fixture and host isolation

Use two independent dedicated Gigabit NICs: left to printed SFP1, right to
printed SFP2. Remove the cable joining the SFP modules. This experiment does
not use the board RTL8211F RJ45 port. Inspect before changing anything:

```bash
.venv/bin/python -m sfp_loopback.bridge_host inspect \
  --json artifacts/bridge/host-inventory.json
```

Setup requires explicit operator confirmation that the named NICs are dedicated
and may be moved. Never select a management, default-route or SSH interface.
Setup accepts only NICs without an active NetworkManager connection or
non-link-local configuration. The tool checks physical identity and network use, records initial settings,
and creates two owned namespaces with 10.77.0.1/24 and 10.77.0.2/24.
It does not create a Linux bridge between them. Replace LEFT and RIGHT with
reviewed interface names, then run only after that confirmation:

```bash
sudo .venv/bin/python -m sfp_loopback.bridge_host setup \
  --left-interface LEFT --right-interface RIGHT \
  --state artifacts/bridge/host-state.json --confirm-isolation
sudo .venv/bin/python -m sfp_loopback.bridge_host test \
  --state artifacts/bridge/host-state.json \
  --csr-csv build/sfp_bridge/csr.csv --output-dir artifacts/bridge/hardware
sudo .venv/bin/python -m sfp_loopback.bridge_host restore \
  --state artifacts/bridge/host-state.json
```

Run restore after success or failure. Keep the state file until restoration is
verified. Restore returns namespace-owned NICs, MTU/up state, link-local
addresses/routes and NetworkManager management. If an operator first disconnected
an active NetworkManager profile to satisfy setup, record and reactivate that
profile on its original NIC afterward; standalone restore does not do that
external step. The physical validation harness records and checks those profiles.
Complex link-local route flags remain outside the validated restoration fixture. Setup rolls back its owned resources when an intermediate step fails.
The manifest and reports record restoration errors instead of hiding them.

The test captures destination-side PCAPs, checks ARP and ping in both directions,
and sends 100 paced raw IPv4 frames and 100 IPv6 frames in each direction.
Correctness requires exact IPv4 delivery, no IPv6 delivery, matching hardware
filter counters and no unexpected CRC/error/overflow/drop increments.
Correctness probes use 10 ms spacing; throughput tests use normal unpaced TCP.

Install `iperf3` or provide an absolute executable path with `--iperf3 PATH`
to inspect and test. The current session extracted Ubuntu's matching packages
locally under ignored `artifacts/bridge/iperf-runtime`; its executable wrapper is
`artifacts/bridge/iperf3`. This changes no system packages or services.

## Performance and evidence

Measure TCP throughput in both directions only after correctness passes.
500 Mbps is a target rather than a functional acceptance threshold. The reused
8-bit, 50 MHz MAC datapath has a 400 Mbps byte-transfer ceiling, and the single
buffer serializes receive and transmit, giving roughly 200 Mbps before further
overhead. Saved iperf output must establish actual throughput; simulation and
host loopback benchmarks do not establish FPGA performance.

The routed build passed setup/hold timing with 1.347/0.059 ns slack and no
unconstrained internal endpoints. DRC reports two warnings for the reused
clock-input LUT, with no errors. Real-MAC compiled simulation passed 18 cases,
including bad-FCS rejection and recovery. Physical IPv4/ARP/IPv6 qualification now passes with two RTL8153 adapters.
The saved exact-client run measured 0.734 Mbps TCP in both directions;
500 Mbps was not achieved. A separate final cleanup-only run restored both
NICs and original NetworkManager profiles without reported errors. See
[the dated results and impact review](results/2026-10-14/bridge/summary.json).
