# Antenna Smart Routing: Contribution Roadmap

Status: draft planning document. This roadmap proposes small, independently testable improvements to `scripts/tools/antenna_smart_routing.py`. It does not change runtime behavior.

## Control-Loop Test Foundation

Establish a focused, hardware-free test foundation for the antenna controller, alongside the related script and guide corrections. Keep testability seams in the tests unless results show a small production change is necessary.

1. Add `tests/test_antenna_smart_routing.py` using Python's standard `unittest` library. Use fakes or `unittest.mock` patches for scripted measurements, switch calls, and time; do not require hardware, `pyserial`, or external tools. Keep testability seams in the test module unless a small production change is clearly necessary.
2. Fix the confirmed `--interface` propagation bug and cover it with one regression test.
3. Keep each antenna's SNR, signal, and noise from the same measurement; test this only if the comparison data path changes.
4. Align the guide's mode boundaries with the existing threshold constants. Do not change threshold values as part of a documentation correction.
5. Cover the basic control contract with a few scripted sequences: switch only when the candidate clears hysteresis; do not switch for a marginal improvement below hysteresis; oscillating sub-threshold readings do not cause antenna flapping; an unavailable candidate measurement restores the original antenna; and cleanup runs when the loop stops.
6. Verify the existing `COOLDOWN` prevents another probe and switch attempt inside its interval. It already provides the minimum time between probe attempts, so no separate dwell-time mechanism is needed.

**Test command:** `python -m unittest discover -s tests`

**Acceptance:** The focused tests run on a development host with no hardware or third-party packages. They verify key switch-call sequences, hysteresis/no-flap behavior, cooldown, fallback, and cleanup without adding a general framework, runtime dependency, or broad refactor.

## Defer Unless Needed

- Display coherence: retain and display SNR, signal, and noise from the same measurement for each antenna; add a focused test when this is implemented.
- SSH hardening for `scripts/tools/halow_range_test.py`: avoid exposing passwords in process arguments and verify host keys; update its usage documentation consistently.
- GPIO inversion, GPIO pin policy, and hardware-specific startup/shutdown changes.
- Retry/backoff behavior, expanded logging, and a new telemetry architecture.
- Exhaustive CLI and threshold-combination tests.
- State-machine extraction or dependency-injection abstractions.
- CI workflow changes; consider CI if automated tests are adopted more broadly.

Keep these as separate follow-ups if field use or a focused test demonstrates the need.

## Validation

**Host:** Run the focused unit suite. This is sufficient for the initial software-only contribution.

**Optional hardware check:** When hardware is available, use one Haven node, a second HaLow peer, a compatible 900 MHz SPDT switch, and two correctly connected 50-ohm antennas. Verify both RF port mappings and observe one controlled switch/fallback sequence under stable conditions. Check component datasheets before wiring, use appropriate 3.3 V logic, and do not hot-swap antenna cables while transmitting. Shielded equipment and attenuators are optional, not prerequisites.

The [antenna smart routing guide](antenna-smart-routing.md) documents the switch and control options; verify details against the exact hardware in use.