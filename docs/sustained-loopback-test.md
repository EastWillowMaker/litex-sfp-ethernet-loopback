# Sustained SFP2 correctness test

## Scope

Continuation of the completed correctness milestone at commit `7d149fc`.
The next milestone in the agreed plan is sustained testing. The default
continuation is a 60-second, stop-and-wait correctness run on the existing
SPI-booted image. Burst throughput and gateware capacity changes remain a
separate design choice.

## Behavior

- Cycle frame sizes 60, 128, 256, 512 and 1514 bytes excluding FCS throughout
  the sending interval, with one outstanding frame at a time.
- Preserve the exact wire protocol and all anomaly checks from the initial
  test: complete returned bytes, MAC swaps, run/sequence identifiers, missing,
  duplicate, corrupt, stale and unexpected replies. Outgoing packet copies
  cannot count as replies.
- Stop issuing frames at the 60-second deadline. Finish the outstanding
  frame's one-second reply timeout, then perform the final one-second
  duplicate check. Report traffic time separately from total elapsed time.
- Keep memory independent of successfully transmitted frame count by
  reconstructing expected frames. Retain only timed-out sequence identifiers;
  their count is limited by the one-second timeout and finite run duration.
- Reject nonfinite or nonpositive requested durations. Transport, setup,
  interruption and artifact failures must not produce a pass.
- Keep the original 1,000-frame default invocation and ordering compatible.

## Measurements and acceptance

- Record actual sent/verified frame counts, counts by size, byte totals,
  duration, observed stop-and-wait frame rate and Ethernet-frame Mbps
  excluding FCS/preamble/inter-frame gap.
- These rates characterize this host/software/test path. They do not measure
  the board's maximum capacity or establish line-rate operation.
- Save UART counters before and after. Accepted/transmitted frame deltas
  must agree with host counts. Receive-error, CRC-error, preamble-error,
  overflow and MAC-drop deltas must be zero. Unrelated rejected traffic is
  reported separately.
- Confirm the link remains established at 1 Gbps after the run.
- Save the report, counter snapshots and comparison in the repository.

## Verification seams

Use the previously agreed host runner and CLI/report boundaries. Simulated
transport and monotonic clock exercise deadlines, size cycling, historical
duplicates, missed replies and failures. No FPGA rebuild or flash write is
needed for this milestone.
