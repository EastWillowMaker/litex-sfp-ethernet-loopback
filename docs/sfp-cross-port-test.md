# SFP cable-loop test through the board Ethernet port

## Confirmed physical topology

On 2026-10-12 the user replaced the two-USB-adapter plan with a direct SFP1–SFP2 connection, then reported connecting the board RTL8211F Ethernet port to the Host PC NIC.

```text
Host PC NIC ↔ board RTL8211F RJ45 ↔ FPGA
                                    ├─ SFP1 ─ external cable ─ SFP2 ─┤
```

The earlier two-host-NIC topology is superseded. A second USB Ethernet adapter is no longer required by this topology. In the Q5/Q6 reply “All yes,” the user confirmed the host-driven traffic path and an RJ45 Ethernet cable between two copper SFP modules. The exact model of the second module remains to be identified during hardware validation.

## Test criteria carried forward

The user accepted correctness-first testing with 1,000 frames across 60, 128, 256, 512 and 1514 bytes excluding FCS, followed by a 60-second sustained correctness run. The user also accepted sequential coverage of both SFP directions and dedicated test-protocol/marker filtering. Require zero missing, duplicate or corrupted test frames.

## Accepted traffic path

Use the Host PC as the frame generator and verifier through the RTL8211F port:

```text
Host → RTL8211F → FPGA → SFP1 TX
                         external cable
Host ← RTL8211F ← FPGA ← SFP2 RX
```

Then reverse the SFP roles to test SFP2 TX → cable → SFP1 RX. In each phase route host ingress to the selected transmitting SFP and the other SFP's receive stream back to the host-facing MAC. Each frame makes one cable traversal before returning to the host.

Preserve test-frame contents across the SFP cable; apply the existing source/destination MAC swap once on the return to the host. Verify the exact expected reply, test protocol `0x88B5`, marker, run identifier, sequence, length and payload. Do not accept host outgoing copies or a reply bypassing the SFP cable as test success. Per-port counters and a cable-disconnected negative control should establish that returned frames traverse the intended path.

Use one outstanding frame and a one-second reply timeout; send 200 frames per size in each fixed-count direction. Run 60 seconds per direction afterward. Report stale, duplicate, unexpected, missing and corrupt test frames; record RTL8211F and both SFP MAC/PHY status and counter deltas. Require no test-time increase in receive errors, overflow or drops. Report observed rate without claiming maximum throughput.

Retain the existing SFP2 echo target as a regression baseline. Build a separate target with the RTL8211F interface and both SFP MAC/PHY paths; simulate forwarding/filtering and build/check timing before SRAM loading. Follow the already authorized SPI programming workflow after SRAM validation and repeat tests after SPI boot. No IP addresses or host bridge are required for the raw-frame test.

## Verified starting point and prerequisites

- Recorded original schematic audit maps SFP1/J5 to LiteX resource 0 and SFP2/J6 to resource 1.
- The platform labels its onboard Ethernet resource as RGMII/RTL8211F, while the original board target instantiates an MII driver. The correct RGMII integration, clocks/delays, reset and management requirements need verification before building this path.
- Read-only inspection of the mounted original Pro schematic, sheet 8, confirms RTL8211F RGMII to board RJ45 J7, a 25 MHz PHY crystal, PHY address `001`, active-low reset, MDC/MDIO and a “TXC / RXC: delays 2ns” annotation. Platform clock pins C18/C19, reset E21, MDC G21, MDIO G22 and data/control pins match that schematic. Delay configuration and the installed board's behavior still need verification; the schematic annotation alone does not establish an active PHY register setting.
- The project's current dedicated test target instantiates SFP2 only; it does not instantiate RTL8211F or SFP1. Prior single-port test results do not validate this new path.
- Current host interface inspection shows the existing RTL8153 `enx98fc84e0c4e8` with carrier. The user reports connecting the host NIC to the RTL8211F port; carrier alone does not validate FPGA traffic routing or identify the attached board connector independently.
- The second USB adapter previously failed to enumerate with USB hub-port configuration errors. It is no longer required for this design.

## Implementation sequence

1. Add a separate three-port test target with correct RTL8211F RGMII integration and both SFP PHY/MAC paths, using the documented onboard GTP reference clock. Add bounded PHY reset/management and UART control for selecting the SFP direction.
2. Implement dedicated-test filtering and buffered routing: RTL8211F ingress → selected SFP TX; other SFP RX → RTL8211F egress, swapping MAC addresses once at host return. Expose counters for all three paths.
3. Extend host control/reporting for both SFP directions; simulate filtering, routing, frame integrity, backpressure, malformed/error rejection and both direction selections. Build and verify placement/timing/clock constraints.
4. SRAM-load the image, verify the RTL8211F and both SFP links, run the fixed-count and sustained tests in both directions, and confirm disconnecting the SFP cable prevents successful returns. Save UART snapshots and host evidence.
5. Program and verify SPI using the existing authorization, verify boot, repeat both-direction tests, and save final image/source identity and reports.

## Verification boundaries

The accepted simulation and host-verification requirements define the test boundaries for implementation:

- Raw MAC frame streams: check routing, filtering, one MAC swap, receive errors, backpressure and direction stability. This does not establish physical PHY timing or link operation.
- Physical Ethernet byte streams through real LiteEth MACs: check preamble/FCS, clock-domain crossings, both cable directions and absence of a host reply without a valid cable return. This models the cable and does not replace live testing.
- Operator commands and saved host/UART reports: check bounded control, mode selection, counter deltas and failure reporting. This does not establish that a physically connected board behaves like a simulated transport.
- Elaborated target and Vivado reports: check actual clock/resource integration and timing coverage. A successful build does not establish cable-loop correctness.

## RGMII timing basis

The target uses a 2 ns FPGA transmit-clock shift and ten receive-data IDELAY taps (nominally 0.78125 ns at a 200 MHz calibration reference). The PHY configuration clears the internal TX delay and enables the internal RX clock delay, with register readback before testing. Linux's [RTL8211F driver](https://github.com/torvalds/linux/blob/master/drivers/net/phy/realtek/realtek_main.c) implements those delay controls through paged registers. Realtek's [RTL8211F Rev. 1.4 datasheet](https://wiki.inmys.ru/_media/wiki:cmp:if:eth-phy:rtl8211fi.pdf), Table 60 on printed page 60, supplies the 1.2 ns transmit setup/hold minima. The input-delay envelope of +1.2 to +2.8 ns is derived for an assumed 8 ns, 50% waveform; 2.8 ns is not a separately specified maximum PHY register delay. This test target's STA does not cover the datasheet's full period/duty variation. The constraints also assume matched PCB data/clock routes; waveform and board trace skew have not been measured.

C19 is the negative side of a clock-capable input pair; C18 is required for TXC. The test target permits fabric routing only from the named C19 receive IBUF to a local BUFR, retaining timing analysis of the receive inputs. The receive MAC stays in that clock region, and the transmit clocks come from the system PLL. AMD's [clock-routing property documentation](https://docs.amd.com/r/2023.2-English/ug912-vivado-properties/CLOCK_DEDICATED_ROUTE) explains the resulting fabric-routing exception. This remains an experimental board-specific target, with routed timing checks enforced before bitstream generation.

The user's Q5/Q6 “All yes” confirms the revised shared understanding. The interview is complete; `$implement With Subagents (GPT 6 Luna X-Hight)` authorizes implementation, validation and local commit. Hardware results will be recorded separately from the design.
