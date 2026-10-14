# 500 Mbps bridge impact review

## Scope and before/after contract

The earlier single-frame bridge rejects otherwise valid IPv4/ARP frames
arriving while the preceding frame is transmitting. The new bridge queues
up to eight complete frames per direction and receives while transmitting.
The supported frame contract remains untagged EtherType 0x0800 or 0x0806,
60..1514 bytes, with no MAC receive error. IPv6, VLAN/QinQ, other EtherTypes,
runts, oversize and errored frames must still be rejected in full. A frame
that starts while all eight slots are occupied is dropped even if a slot
becomes free before its last byte; admission is decided at its first byte.

`bridge.py:_EthernetForwarder` stores an admitted frame in its current write
slot, but publishes its length and advances the write pointer only after
complete-frame validation. The independent read pointer drains committed
slots through a synchronous memory port into the opposite LiteEth MAC.
Stalled output must preserve data and framing. Slot release happens only
when the last output byte is accepted. Concurrent commit/release updates
occupancy atomically. Rejected frames reuse the unpublished write slot.

The overflow counter also becomes more specific: previously, a supported
EtherType received while the buffer was busy could count as both an error
(or runt rejection) and an overflow. Now queue-full overflow counts only
otherwise eligible frames; oversized frames still increment overflow.
The full-queue test explicitly rejects a valid ninth frame, a runt, an
MAC-errored frame and IPv6 without partial output, and distinguishes their
reason counters before checking drain/recovery.

Newly admitted burst frames now reach LiteEth TX CDC, padding/FCS/preamble
and physical transmission. This remains a fixed two-port bridge with no
operation dispatch, implementation lookup, CPU, MAC learning, ACL or IP
routing. Thus IPv4 acceptance is EtherType filtering, not validation of an
IPv4 header or its payload. No broader network-security contract is claimed.

`bridge_target.py:BridgeSoC` changes the default system clock from 50 to
125 MHz, including MAC system streams, UARTBone and PHY reset timers. The
50 MHz comparison option is retained; the performance build uses a separate
output directory and the same SRAM-only programming guard. Physical MAC
clocks and CDC structures remain in the path and require simulation plus
routed timing and hardware evidence at the new system frequency.

## Measured red loop

The existing image measured approximately 0.699 Mbps received TCP in each
direction in two 3-second runs, reproducing the user's throughput symptom.
The repeat `python artifacts/bridge500/submit.py baseline` exited 1 for the
500 Mbps threshold, with restoration passing and overflow increments
116/123. The first baseline also exposed a wrapper restoration failure:
historical profile names were associated with opposite interface bindings.
The benchmark wrapper now restores each run's captured management/active
profile state and checks that current profile configuration is unchanged;
it does not alter profile bindings to match a stale artifact. A subsequent
run showed these runtime automatic profiles are removed/recreated across
namespace movement, changing UUIDs and swapping names. The wrapper now
preserves the two existing keyfiles and reloads them before re-enabling
management. The next run preserved profile UUIDs, bindings and settings.
Standalone restoration reported a transient link-local-route conflict in
that run; final NetworkManager activation restored the expected route.
The final wrapper checks addresses, routes, MTU, link state, MAC identity,
absence of test addresses and owned namespaces, and saved profile state,
while retaining any intermediate restoration errors in the receipt. When no
profile was active before a run, NetworkManager may normally start DHCP
after management is restored. This is distinct from claiming a successful
DHCP lease on these isolated FPGA links.

## Verification and remaining limits

The new queue's initial back-to-back test failed on the single-buffer
source and passed after queueing was implemented. Review found a new
concurrency defect during implementation: RX commit reset the active TX
position while stalled. The added stall regression failed before removing
that assignment and passes on the frozen RTL. This was corrected before
the routed build or SRAM load.

The real-MAC burst test isolates the two changes. At 125 MHz the previous
single-frame source delivered 18/36 valid frames and 371 Mbps per direction;
the queued source delivered 36/36 and 743 Mbps. At 50 MHz the queued source
reached only 265 Mbps, with upstream frame loss: not all driven frames
reached the bridge counters. The test exposes MAC CRC but no dedicated
CDC-drop counter, so that loss mechanism is not conclusively localized.
The final 125 MHz test checks all valid frames exactly, rejects six IPv6
and six corrupt-FCS frames per direction, and sees zero queue overflow.
These are simulation rates, distinct from TCP payload rates on hardware.

Local validation: all 171 repository tests passed (142.04 seconds), the
post-default-change host/diagnostics suite passed 41 cases, and prescribed
mypy passed all 17 source files. After the full run, the existing full-queue
test was extended to check invalid-at-capacity counter semantics; the final
seven-case queue suite passed (12.50 seconds), with no RTL change. No hosted
CI ran. The 125 MHz routed image
met setup/hold with WNS 0.685 ns and WHS 0.055 ns, used 16/50 BRAM tiles,
2,306 LUTs and 3,350 registers, and passed the build timing gate. DRC has
no errors and the two existing clock-input LUT warnings (PDRC-153 and
PLHOLDVIO-2). STA reports four inputs and five outputs without I/O delay
constraints; asynchronous board controls/UART are not a claim of complete
external synchronous-interface timing coverage.

The final physical qualification passed 100/100 unchanged IPv4 frames each
way, ARP/ping in both directions, and zero forwarded IPv6 probes. It then
measured 941.405 and 941.403 Mbps of received TCP payload over sequential
30-second runs. MAC preamble/CRC/drop and bridge error/overflow deltas were
zero for the whole qualification. The fixed benchmark threshold was 500 Mbps
per direction; `submit.py qualify` returned zero. The generic host report
records rate without enforcing that threshold; `qualification/result.json`
contains the wrapper's separate throughput verdict.

The final run restored both owned namespaces/NICs and saved management/
active profiles without intermediate errors; profile UUIDs, interface bindings
and checked settings matched. A subsequent read-only audit confirmed exact
address and link-local route-set equality. Earlier restoration failures are
retained under `baseline-initial`, `benchmark-initial` and `benchmark-repeat`,
not erased or counted as successful runs. The authenticated worker was
stopped after qualification. The new image remains loaded only in SRAM;
the previous cable-loop SPI image and the old bridge build are preserved.

Physical TCP directions were tested sequentially; concurrent duplex traffic
was verified only in compiled-MAC simulation. VLAN, malformed and bad-FCS
negative cases passed simulation, but were not physically injected in this
qualification. No bridge cold-power cycle or flash qualification ran.

Existing route-restoration limitations from the previous bridge
review remain: nonempty complex link-local route flags are not fully
restored/verified by the host tool. This change does not expand that host
acceptance contract. The queue is bounded; sustained oversubscription or
unbounded downstream backpressure can still drop complete frames. No
jumbo frames, VLAN forwarding, IP routing, bridge SPI write or cold-power
qualification is intended by this performance change.
