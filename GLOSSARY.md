# Rise Board Ethernet Test

An Ethernet test between the Host PC and the Rise Board's SFP2 connection.

## Language

**Frame loopback test**:
A test in which the Host PC sends Ethernet frames to the board and verifies the frames returned by the board.
_Avoid_: Eth loop test, ping test

**Test frame**:
An Ethernet frame belonging to this loopback test, identified by its test protocol and marker. Its payload identifies the test run and the frame's sequence within that run.

**Correctness milestone**:
The first test milestone, which establishes a reliable pass/fail result at low traffic before sustained-load testing.

**Sustained correctness run**:
A frame loopback test that continuously sends and verifies frames for a chosen interval. It checks reliability over time and records the observed rate of the test path.
_Avoid_: Maximum-throughput test, line-rate test
