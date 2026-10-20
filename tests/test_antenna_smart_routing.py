"""Hardware-free tests for scripts/tools/antenna_smart_routing.py.

Run from the repo root: python -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tools" / "antenna_smart_routing.py"
_spec = importlib.util.spec_from_file_location("antenna_smart_routing", SCRIPT)
asr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(asr)

# run() starts with last_switch_time = 0, so a clock near zero would look like a cooldown.
T0 = 1_000_000.0
INTERVALS = (10, 6, 2)  # happy, curious, desperate: the CLI defaults
H = asr.SWITCH_HYSTERESIS


class FakeClock:
    def __init__(self, start=T0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def strftime(self, fmt):
        return "00:00:00"


class FakeSwitch:
    def __init__(self, clock=None, fail_on_toggle=None):
        self.clock = clock or FakeClock()
        self.current = 0
        self.calls = []
        self.toggle_times = []
        self.cleanups = 0
        self._fail_on_toggle = fail_on_toggle

    def select(self, antenna):
        self.calls.append(("select", antenna))
        self.current = antenna

    def toggle(self):
        if self._fail_on_toggle:
            raise self._fail_on_toggle
        self.calls.append(("toggle",))
        self.toggle_times.append(self.clock.now)
        self.current = 1 - self.current
        return self.current

    def cleanup(self):
        self.cleanups += 1


class SubprocessTripwire(BaseException):
    """Not an Exception, so read_snr's `except Exception` cannot swallow it."""


def run_scenario(readings, intervals=INTERVALS, switch=None):
    """Run the control loop on scripted SNR readings (None means no data).

    The loop ends with KeyboardInterrupt when the script runs out.
    """
    switch = switch or FakeSwitch()
    script = iter(readings)

    def fake_read_snr(interface="wlan0"):
        try:
            snr = next(script)
        except StopIteration:
            raise KeyboardInterrupt
        return None if snr is None else (snr, -60, -60 - snr)

    with mock.patch.object(asr, "read_snr", fake_read_snr), \
            mock.patch.object(asr, "read_batman_neighbors", return_value=[]), \
            mock.patch.object(asr.subprocess, "run", side_effect=SubprocessTripwire), \
            mock.patch.object(asr, "time", switch.clock), \
            contextlib.redirect_stdout(io.StringIO()):
        asr.run(switch, ["RF1", "RF2"], "wlan0", *intervals, monitor=False)
    return switch


def gaps(times):
    return [b - a for a, b in zip(times, times[1:])]


class ProbeDecisionTests(unittest.TestCase):
    def test_switches_when_improvement_equals_hysteresis(self):
        switch = run_scenario([20, 20 + H])
        self.assertEqual(switch.calls, [("toggle",)])
        self.assertEqual(switch.current, 1)

    def test_stays_when_improvement_is_just_below_hysteresis(self):
        switch = run_scenario([20, 20 + H - 1])
        self.assertEqual(switch.calls, [("toggle",), ("select", 0)])
        self.assertEqual(switch.current, 0)

    def test_restores_original_antenna_when_candidate_has_no_data(self):
        switch = run_scenario([20, None])
        self.assertEqual(switch.calls, [("toggle",), ("select", 0)])
        self.assertEqual(switch.current, 0)

    def test_does_nothing_while_there_is_no_link_data(self):
        switch = run_scenario([None, None])
        self.assertEqual(switch.calls, [])
        self.assertEqual(switch.clock.now - T0, 2 * INTERVALS[1])

    def test_sub_hysteresis_noise_never_flaps(self):
        noise = [20, 20 + H - 1, 20, 20 - H + 1] * 5
        switch = run_scenario(noise)
        self.assertEqual(switch.calls, [("toggle",), ("select", 0)] * 10)
        self.assertEqual(switch.current, 0)

    def test_noise_above_hysteresis_switches_on_every_probe(self):
        """Characterization: probe cadence is the only brake on flapping."""
        switch = run_scenario([20, 20 + H] * 3)
        self.assertEqual(switch.calls, [("toggle",)] * 3)
        self.assertEqual(gaps(switch.toggle_times), [asr.SAMPLE_WINDOW + INTERVALS[0]] * 2)


class ModeBoundaryTests(unittest.TestCase):
    def test_poll_interval_follows_snr_at_each_boundary(self):
        happy, curious, desperate = INTERVALS
        cases = (
            (asr.SNR_HAPPY, happy),
            (asr.SNR_HAPPY - 1, curious),
            (asr.SNR_CURIOUS, curious),
            (asr.SNR_CURIOUS - 1, desperate),
        )
        for snr, interval in cases:
            with self.subTest(snr=snr):
                switch = run_scenario([snr, snr])
                self.assertEqual(switch.clock.now - T0, asr.SAMPLE_WINDOW + interval)


class CooldownTests(unittest.TestCase):
    def test_cooldown_delays_probes_when_cycles_are_short(self):
        """Desperate mode: sample window plus interval is shorter than COOLDOWN."""
        switch = run_scenario([5] * 6)
        (gap,) = gaps(switch.toggle_times)
        self.assertGreaterEqual(gap, asr.COOLDOWN)
        self.assertLess(gap, asr.COOLDOWN + INTERVALS[2])

    def test_cooldown_adds_no_delay_at_default_happy_and_curious_intervals(self):
        for snr, interval in ((20, INTERVALS[0]), (10, INTERVALS[1])):
            with self.subTest(snr=snr):
                switch = run_scenario([snr] * 6)
                self.assertEqual(gaps(switch.toggle_times), [asr.SAMPLE_WINDOW + interval] * 2)


class ShutdownTests(unittest.TestCase):
    def test_cleanup_runs_on_ctrl_c(self):
        self.assertEqual(run_scenario([20, 20]).cleanups, 1)

    def test_cleanup_runs_when_the_switch_raises(self):
        switch = FakeSwitch(fail_on_toggle=OSError("adapter unplugged"))
        with self.assertRaises(OSError):
            run_scenario([20, 20], switch=switch)
        self.assertEqual(switch.cleanups, 1)


ASSOCLIST = (
    "AA:BB:CC:DD:EE:01  -46 dBm / -85 dBm (SNR 39)  100 ms ago\n"
    "\tRX: 6.5 MBit/s\n"
    "AA:BB:CC:DD:EE:02  -70 dBm / -85 dBm (SNR 15)  200 ms ago\n"
)
INFO = 'wlan1  ESSID: "haven"\n  Signal: -60 dBm  Noise: -90 dBm\n'


def fake_iwinfo(outputs):
    """Fake subprocess.run; outputs maps the iwinfo subcommand to stdout or an exception."""
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        out = outputs[cmd[2]]
        if isinstance(out, Exception):
            raise out
        return types.SimpleNamespace(stdout=out, returncode=0)

    return run, commands


class ReadSnrTests(unittest.TestCase):
    def read(self, outputs):
        run, commands = fake_iwinfo(outputs)
        with mock.patch.object(asr.subprocess, "run", run):
            return asr.read_snr("wlan1"), commands

    def test_picks_the_peer_with_the_best_snr(self):
        result, commands = self.read({"assoclist": ASSOCLIST})
        self.assertEqual(result, (39, -46, -85))
        self.assertEqual(commands, [["iwinfo", "wlan1", "assoclist"]])

    def test_falls_back_to_interface_info_without_peers(self):
        result, commands = self.read({"assoclist": "", "info": INFO})
        self.assertEqual(result, (30, -60, -90))
        self.assertEqual(commands, [["iwinfo", "wlan1", "assoclist"], ["iwinfo", "wlan1", "info"]])

    def test_unparseable_output_gives_none(self):
        result, _ = self.read({"assoclist": "garbage", "info": "garbage"})
        self.assertIsNone(result)

    def test_missing_iwinfo_gives_none(self):
        missing = FileNotFoundError("iwinfo")
        result, _ = self.read({"assoclist": missing, "info": missing})
        self.assertIsNone(result)


class StubSerial:
    def __init__(self, port, baudrate, timeout):
        self.dtr = self.rts = self.break_condition = None
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class SerialSwitchTests(unittest.TestCase):
    def make(self, pin, invert):
        stub = {"serial": types.SimpleNamespace(Serial=StubSerial)}
        with mock.patch.dict(sys.modules, stub):
            return asr.SerialSwitch("/dev/null", pin=pin, invert=invert)

    def test_line_level_for_every_pin_antenna_and_invert(self):
        for pin in ("dtr", "rts", "txd"):
            for invert in (False, True):
                for antenna in (0, 1):
                    with self.subTest(pin=pin, invert=invert, antenna=antenna):
                        switch = self.make(pin, invert)
                        switch.select(antenna)
                        lines = {
                            "dtr": switch.ser.dtr,
                            "rts": switch.ser.rts,
                            "txd": switch.ser.break_condition,
                        }
                        driven_low = (antenna ^ invert) == 0
                        self.assertEqual(lines[pin], driven_low)
                        if pin == "txd" and not driven_low:
                            self.assertEqual(switch.ser.writes[-1], b"\xff" * 4)

    def test_cleanup_releases_break_and_closes_the_port(self):
        switch = self.make("txd", False)
        switch.select(0)
        switch.cleanup()
        self.assertIs(switch.ser.break_condition, False)
        self.assertTrue(switch.ser.closed)


if __name__ == "__main__":
    unittest.main()
