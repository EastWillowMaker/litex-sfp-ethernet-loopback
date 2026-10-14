# Bridge behavior and validation impact

Baseline: `7d3ef99`. The FPGA datapath and built image are unchanged by the
host-tool fixes. Hardware image SHA256 is recorded in the SRAM load receipt.

## Initial physical run

Both independent RTL8153 adapters and both FPGA PCS links negotiated up.
ARP resolved the expected peer MAC and ping passed in both directions.
Each host capture received exactly100IPv4 probe frames unchanged and zero
of100IPv6 probes from the opposite NIC. Correctness-probe MAC CRC/preamble
error and drop deltas were zero.

The operator harness assigned the new NIC to SFP0, but UART snapshots showed
its100IPv4 and100IPv6 increments in SFP1's direction. The opposite NIC's
increments appeared in SFP0's direction. Therefore the initial report correctly
failed its assigned hardware-counter checks; it is retained as initial-run/report.json.
The retry assigns original MAC98:fc:84:e0:c4:e8 to SFP0 and new
MAC00:e0:4c:58:28:c8 to SFP1 based on those measured counters.

The first tool also ran TCP despite failed hardware qualification. It measured
2.306 and0.629Mbps with570 and280 sender retransmissions. These are actual
unpaced FPGA-path measurements, but were taken before a valid overall report.
A later snapshot had278 and574 additional directional buffer overflows and
zero additional forwarding error frames; that interval also includes restoration
and background host traffic. It is not an exact isolated performance counter window.

## Runtime coverage and limits

The filter contract accepts only outer untagged EtherTypes0x0800/0x0806 and
drops0x86dd, VLAN/QinQ, unsupported, runt, oversize, errored or busy frames
whole. The raw and compiled real-MAC suites establish those classes in
simulation. Actual hardware probes cover IPv4, ARP and IPv6 only; malformed,
VLAN/QinQ and unsupported hardware injection remain unexecuted.

Correctness at paced traffic does not establish line-rate forwarding. The
single-buffer design intentionally rejects eligible arrivals while transmitting;
TCP burst loss and low throughput are known limitations. No hosted CI, cold
power cycle or bridge flash programming is claimed.

The host restore comparison and performance-dispatch changes are reviewed
separately below after focused tests and the physical retry.

## Host-tool before/after review

Source locations: `bridge_host.py` `_restore_link_local_addresses` (around799),
`_route_signature` (around851), `_restore_link_local_routes` (around879),
and `run_bridge_test` performance dispatch (around1622).

- Device-filtered IPv4/IPv6 route JSON without `dev`: previously an otherwise
  matching saved route looked different and cleanup could fail or try a duplicate
  add. Now the already-filtered device supplies the missing comparison context.
  A different metric, table, gateway, source, scope, protocol or IPv6 preference
  remains a conflict; conflicting routes are retained and restoration reports failure.
- Saved IPv6 addresses with `noprefixroute`: previously address replay dropped
  that flag and could create a prefix route with a different metric. Now replay
  preserves it. IPv6 preference is compared and replayed; missing preference
  defaults to medium. Invalid/non-link-local inputs retain their earlier rejection
  or exclusion. No new management/default-route interface is permitted.
- Failed route adds: previously always reported failure. Now a successful reread
  proving the entire selected route signature exists can satisfy restoration;
  a missing/mismatched route or failed reread still fails, with command stderr.
  Exact-route EEXIST is reproduced in fixtures. The earlier physical add-error
  stderr was not captured, and later successful runs did not reproduce it:
  its original cause remains unverified.
- Failed or unavailable correctness checks: previously still reached TCP server/
  client execution. Now performance is BLOCKED with no iperf spawn. A valid host
  and hardware PASS still dispatches both bounded TCP runs. Missing iperf stays
  BLOCKED; nonzero commands/malformed responses keep their existing FAIL path.
  The raw capture PASS stage alone is explicitly insufficient for overall PASS.

Focused public Runner-boundary regressions passed41cases, including matched,
mismatched and forbidden dispatch classes. First-party mypy passed17source files.
These are local checks; no hosted CI is claimed. The prior146-case complete
suite is preserved from2026-10-13; it was not rerun after these host-only changes.
Gateware and its qualified routed build are unchanged.

### Unresolved route-flags class

A saved link-local route with nonempty JSON `flags` can differ only in flags
from its returned route. The signature does not compare or replay those flags.
Before device normalization, a missing `dev` could expose a mismatch; afterward
normalization can allow an otherwise matching route to skip replay and report
success despite that flag difference. Flags such as kernel-managed linkdown or
offload status need classification before a safe replay policy can be defined.
Required follow-up: distinguish configuration flags from runtime flags, then
reject unsupported configuration flags or preserve them with execution coverage.
That class remains unverified and is a known standalone restoration limitation.
The qualified fixture saved empty route flags; this gap did not affect its result.

### NetworkManager profiles

Standalone setup rejects active NetworkManager profiles. If an operator manually
unmanages/disconnects an active profile first, standalone restore cannot recover
that external previous profile by itself. The initial harness toggled management
only, which allowed profile drift. The final physical harness explicitly requests
each original profile on its saved adapter and verifies both names and management.
Those wired profiles have no DHCP server in the isolated fixture, so restoring a
profile request is not a claim that DHCP address acquisition subsequently succeeds.

## Confirmed duplicate-prefix cause and final evidence

The exact-source traffic run passed the full host/hardware criteria and recorded
0.734Mbps in each direction, but cleanup still failed. The short cleanup trace
showed the first NIC route successfully installed and the second NIC's filtered
query containing no saved prefix route; its add failed with EEXIST. This is the
same-prefix/metric route on another device, not an exact-own-route race.
Linux's [route selftests](https://github.com/torvalds/linux/blob/master/tools/testing/selftests/net/fib_tests.sh)
and [iproute2 documentation](https://github.com/iproute2/iproute2/blob/main/man/man8/ip-route.8.in)
explain the add-versus-append behavior. IPv6 restore now appends the saved
per-device route; IPv4 continues to add. Existing other-device routes remain,
and exact own-device matches are skipped. Conflicting own-device metadata
remains a reported failure. The public setup/restore regression failed before
this change and passed afterward with an untouched same-prefix uplink route.

Final `cleanup-verified/completion.json` confirms namespace/link-local restoration
with no errors and both original NM management/profile checks true. Its saved
client hash matches the final host source. This short run performs no traffic
or TCP measurement; traffic qualification remains the separately saved
`qualification/report.json`. The final source delta after that run affects only
route restore add/append dispatch, not forwarding, probes or throughput.
The exact same-device EEXIST race is covered by fixtures; it was not the cause
of the observed repeated second-device restoration failure.
