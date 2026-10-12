# Rise Board Ethernet Test

Ethernet tests of the Rise Board's SFP connections and its host-facing Ethernet port.

## Language

**Frame loopback test**:
A test in which the Host PC sends Ethernet frames to the board and verifies the frames returned by the board.
_Avoid_: Eth loop test, ping test

**Cross-port forwarding test**:
A test in which one Host PC Ethernet adapter sends frames into one board SFP port and another adapter verifies those frames after they leave the other SFP port, with Ethernet addresses and payload preserved.
_Avoid_: Frame loopback test, ping test

**SFP cable loop**:
A physical cable connection between the board's SFP1 and SFP2 ports, allowing frames transmitted by one port to be received by the other.
_Avoid_: Host frame loopback, internal PHY loopback

**Board Ethernet port**:
The board's RJ45 Ethernet connection served by its onboard RTL8211F PHY, distinct from the RJ45 connections on copper SFP modules.

**Test frame**:
An Ethernet frame belonging to this loopback test, identified by its test protocol and marker. Its payload identifies the test run and the frame's sequence within that run.

**Correctness milestone**:
The first test milestone, which establishes a reliable pass/fail result at low traffic before sustained-load testing.

**Sustained correctness run**:
An Ethernet frame test that continuously sends and verifies frames for a chosen interval. It checks reliability over time and records the observed rate of the test path.
_Avoid_: Maximum-throughput test, line-rate test
