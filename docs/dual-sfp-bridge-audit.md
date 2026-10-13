# Dual-SFP bridge experiment: preserved-work audit

Audit date: 2026-10-13. Existing main: `89117c6`.
Request: attached `pasted-text-1.txt`, read before this audit.
The bridge experiment follows completion of the current RTL8211F cable loop.
This document records the initial preparation audit. The separate bridge is now
implemented and built; see [the test procedure](dual-sfp-bridge-test.md).
No bridge image has been loaded for hardware qualification.

## Current evidence

| Item | Status | Evidence |
|---|---|---|
| Original SFP2 echo and flash boot | PASS | `results/2026-10-12/summary.json` and sustained results |
| Three-port cable-loop SRAM traffic | PASS | 1,000 frames and 60 seconds in each direction; `results/2026-10-13/cable/summary.json` |
| Cable-loop physical disconnect control | PASS | Both directions sent five frames with zero cable returns; reconnect recovery returned five frames |
| Cable-loop SPI qualification | PASS | Program/verify/boot and all four traffic runs; saved cable results |
| Software regression suite | PASS | 95 tests; first-party mypy clean in saved evidence |
| Cable-loop routed timing | PASS | Setup 0.215 ns, hold 0.037 ns; documented nominal RGMII timing assumptions |
| Two-NIC bridge hardware fixture | BLOCKED | Only RTL8153 `enx98fc84e0c4e8` currently enumerates as a suitable physical Ethernet NIC |
| Bridge ARP/IPv4/IPv6/iperf tests | NOT RUN | Existing LXLP loop tests do not establish transparent bridge behavior |

## Reuse and smallest missing pieces

Preserve `target.py`, `responder.py`, `cable.py`, `cable_target.py`, existing
results and build artifacts. The two-port bridge needs a separate target;
the current cable target intentionally includes RTL8211F and swaps MACs on
return, which differs from the requested transparent bridge.

Reuse the corrected F6/E6 125 MHz reference, one `GTPE2_COMMON` with two QPLL
engines and independent resets, both `A7_1000BASEX` PHYs, clock-domain renaming,
LiteEth MAC store-and-forward/drop FIFOs and 2,048-byte CDC depths, UART link
and error diagnostics, and the compiled real-MAC simulation infrastructure.
Do not update vendor dependencies or modify prior targets to change their behavior.

Add independent hardware forwarding paths that preserve MAC addresses and
payloads. Allow only outer EtherTypes 0x0800 and 0x0806; reject 0x86dd,
VLAN tags and unsupported types before exposing any rejected frame. Validate
frame boundaries, bad FCS, malformed lengths, backpressure and whole-frame
overflow handling at both raw-stream and real-MAC seams.

The reused 50 MHz, eight-bit system datapath has a theoretical 400 Mbps
per-direction byte-transfer ceiling before overhead. Establish correctness
first and measure TCP throughput; 500 Mbps is a target, not a functional gate.

Add inspect/setup/test/restore scripts for two dedicated NICs in separate
network namespaces. Identify management interfaces and saved configuration
before moving NICs, and obtain the explicitly required confirmation for
those disruptive changes. Keep this experiment SRAM-only; existing flash
authorization applies to the cable-loop milestone, not automatically to the bridge.

## Verified installed dependency revisions

| Dependency | Commit |
|---|---|
| litex | `b2759f69bcae8be0357fb2ee5eebe216365d1f00` |
| litex-boards | `c2abe2465dbe72de385a4c8b453ffb31c2d86bbb` |
| liteeth | `c311f90f4b25b24be5bf3077e9ad37fcde6589a3` |
| liteiclink | `6e69216944e94c69f5a7d58d5c6d75dc3d754242` |
| migen | `e19524c963a8342952840983047557707fbe0b6a` |

No existing source or result was replaced, and no expensive build was repeated
for this audit. The cable loop is now qualified. The separate bridge experiment now passes independent simulation/build checks.
Physical bridge traffic still requires a second suitable NIC.
