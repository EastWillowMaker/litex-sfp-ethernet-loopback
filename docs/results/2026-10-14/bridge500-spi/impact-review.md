# Persistent bridge programming impact review

The user explicitly requested SPI flash deployment so the FPGA can run
between the distant ISP router and NWA50AX without a laptop. This supersedes
the former SRAM-only restriction. The authorized write replaces the existing
cable-loop boot image at flash offset zero. Both prior cable and qualified
bridge binaries, their bitstreams and CSR maps were copied with hashes to
`artifacts/bridge500-spi` before any persistent write.

## Changed input classes and execution paths

Before this change, `program.py:main` rejected bridge flash and boot requests
before hardware access. Now `--flash --status-profile bridge` can use
`--flash-id-csr-csv` to read the JEDEC identity through the currently loaded
cable helper, while selecting the actual bridge binary and its own CSR map
for the post-boot status reader. `--boot` needs no SPI controller in the user
image: Vivado triggers configuration startup through JTAG. Existing image
basename validation and non-bridge programming routes remain available.

The new preflight checks that the five SPI controller CSR names exist in the
identity CSV before opening UART. Missing maps/registers produce a saved
failure report. This is a required-register presence check, not proof of
CSR addresses, a full CSV schema validator, or proof of the currently loaded
image. The operator must supply the map matching the already-loaded helper.
Live hardware was loaded from the preserved cable bitstream and identified
as Micron20ba18. Unknown/invalid IDs still prevent the Vivado programming
subprocess. Canonical `dual_sfp_bridge` requests with a non-bridge status
profile now fail before hardware access, including previously accepted SRAM
loads. A helper-map argument on load/boot is also rejected as inapplicable.
Custom image basenames retain explicit operator-selected status profiles.

After identity lookup selects the existing flash allowlist descriptor,
`scripts/program.tcl` opens the specified XC7A35T, validates configuration-memory
part matches, programs at offset zero with erase/program/verify enabled and
boots using the physical mode pins. It checks DONE=1 and CRC_ERROR=0, then
Python reads bridge status using the bridge CSV. No RTL change or rebuild is
part of this deployment; the previously qualified image is reused exactly.

The old SRAM guard hid a real downstream mismatch: the bridge target has no
SPI CSRs, so merely deleting the guard would fail flash identification.
The separate identity map fixes that path without adding SPI logic to the
qualified bridge. Another pre-existing limitation remains: a readable status
snapshot does not prove links are up, traffic forwards or the expected image
identity matches. Programming success and post-startup link/traffic evidence
must be reported separately. Post-program UART failure means the report fails
even though persistent flash may already have changed; no automatic rollback
is claimed. Custom renamed images with mismatched status maps are still the
operator's responsibility, and do not gain image-identity authentication.

## Evidence

Focused programming tests pass17 cases: separate helper and target maps,
bridge boot, missing SPI registers, unknown/invalid JEDEC rejection, canonical
image/profile mismatch and legacy load/status/failure behavior. Prescribed
mypy passes17 source files. These tests mock UART/Vivado; physical programming
and boot evidence is recorded separately. No hosted CI or new RTL timing run
is claimed. The earlier complete bridge suite and 941 Mbps physical traffic
qualification apply to the unchanged image, not a new post-flash throughput
measurement.

The physical flash operation identified20ba18, erased/programmed/verified
the bridge binary successfully, then booted with DONE high and CRC_ERROR=0.
A separate `--boot` passed without rewriting flash. Both links subsequently
reported ready/up with no loss of signal; background IPv4 forwarding and
IPv6 rejection counters were nonzero. Startup MAC errors are present in the
cumulative snapshots, so this is not a claim of zero errors since boot or a
new controlled forwarding/throughput qualification. Physical cold-power
verification is pending the user's optional action. Live ISP/AP connectivity
is not yet qualified.

Independent read-only spec review found no blocking dispatch/Tcl defect and
confirmed the map-provenance and status-only limits above. The reviewer ran
no hardware/tests. One validator docstring was clarified after the flash
command started; source hashes record both invocation text and final source.
Executable programming logic was unchanged; the repeat boot used final source.
