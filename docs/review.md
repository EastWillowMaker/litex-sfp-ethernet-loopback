# Implementation review

Fixed point: agreed design commit `35654a8f8c94f996a61eba5bc2d6ae57de6c8528`.
Implementation checkpoint: `6f4aeba`.
Independent standards/spec subagents reviewed `git diff 35654a8...6f4aeba`.

## Standards

Two findings:

- Medium: UART constructor/identity errors could abort before saving a failed programming report. Operational phases now share a report-writing error boundary. A regression test demonstrated the old failure and passes after the fix.
- Low, Duplicated Code judgment call: UART timeout setup was repeated. `BoundedUART` now sets bounds intrinsically and supports context-managed cleanup.

The standards reviewer inspected the working-tree fixes and confirmed both
findings resolved, with no new findings. No documented standards breaches
remain.

## Spec

No actionable implementation mismatches found. Filtering, address swapping,
payload preservation, frame sizes, stop-and-wait verification, anomaly
detection, UART diagnostics, corrected pins, and flash programming match the
agreed design.

Flash boot and cable testing were pending at review time. Subsequent hardware
validation completed both: SPI programming/verification and boot succeeded,
and the flash-loaded image returned all 1,000 expected frames without errors.

## Hardware-driven corrections after the checkpoint

- Safe inactive-CS clock priming handles STARTUPE2 takeover before read-ID.
- Vivado's seven identical flash descriptors are accepted after validating
  their names; the existing LiteX programming flow also selects the first.
- Boot completion is checked with actual DONE/CRC registers because Vivado
  2025.2 returns an empty string from `boot_hw_device` despite success.
- No additional flash erase was needed after correcting boot-result handling.

Totals: Standards 2 findings, highest medium, both fixed; Spec 0 findings.
