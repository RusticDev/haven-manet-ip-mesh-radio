"""Tests for scripts/tools/antenna_smart_routing.py (no hardware required)."""

import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tools" / "antenna_smart_routing.py"
spec = importlib.util.spec_from_file_location("antenna_smart_routing", SCRIPT)
asr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(asr)


class FakeSwitch:
    def __init__(self):
        self.current = 0

    def select(self, antenna):
        self.current = antenna

    def toggle(self):
        self.select(1 - self.current)
        return self.current

    def cleanup(self):
        pass


class InterfaceOptionTest(unittest.TestCase):
    def test_run_reads_snr_from_requested_interface(self):
        interfaces = []

        def fake_read_snr(interface="wlan0"):
            interfaces.append(interface)
            return (20, -60, -80)

        # First sleep is the post-toggle sample window; the second ends the loop.
        sleeps = [None, KeyboardInterrupt]

        with mock.patch.object(asr, "read_snr", fake_read_snr), \
                mock.patch.object(asr, "read_batman_neighbors", return_value=[]), \
                mock.patch.object(asr.time, "sleep", side_effect=sleeps), \
                contextlib.redirect_stdout(io.StringIO()):
            asr.run(FakeSwitch(), ["RF1", "RF2"], "wlan1", 10, 6, 2, monitor=False)

        # One read for the current antenna and one for the probed antenna.
        self.assertGreaterEqual(len(interfaces), 2)
        self.assertEqual(set(interfaces), {"wlan1"})

    def test_default_interface_is_wlan0(self):
        interfaces = []

        def fake_read_snr(interface="wlan0"):
            interfaces.append(interface)
            return (20, -60, -80)

        with mock.patch.object(asr, "read_snr", fake_read_snr), \
                mock.patch.object(asr, "read_batman_neighbors", return_value=[]):
            asr.get_link_quality()

        self.assertEqual(interfaces, ["wlan0"])


if __name__ == "__main__":
    unittest.main()
