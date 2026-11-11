"""Which RTL-SDRs the bridge plans for at start: rtl_test's list, plus anything the USB bus has that
rtl_test left out. Invented serials; no radio is touched. Standard library only:
    python3 -m unittest companion/pi/tests/test_radios.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))


def load_bridge():
    argv, sys.argv = sys.argv, ["bridge"]
    try:
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_radio_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


class ProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load_bridge()

    def probe(self, rtl_test_output, bus):
        b = self.b
        saved = (b.subprocess.run, b.usb_rtl_serials, b.ensure_hackrf_mode, b.shutil.which)
        b.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout="", stderr=rtl_test_output, returncode=0)
        b.usb_rtl_serials = lambda: list(bus)
        b.ensure_hackrf_mode = lambda: None
        b.shutil.which = lambda name: None
        try:
            return b.probe_radios()[0]
        finally:
            b.subprocess.run, b.usb_rtl_serials, b.ensure_hackrf_mode, b.shutil.which = saved

    def test_both_listed(self):
        out = "Found 2 device(s):\n  0:  Realtek, RTL2838UHIDIR, SN: 11110001\n  1:  Realtek, RTL2838UHIDIR, SN: 11110002\n"
        self.assertEqual([r[1] for r in self.probe(out, ["11110001", "11110002"])], ["11110001", "11110002"])

    def test_dongle_on_the_bus_but_not_listed_is_planned(self):
        out = "Found 1 device(s):\n  0:  Realtek, RTL2838UHIDIR, SN: 11110001\n"
        rtls = self.probe(out, ["11110001", "11110002"])
        self.assertEqual([r[1] for r in rtls], ["11110001", "11110002"])
        plan = self.b.plan_radios(rtls, False)
        self.assertEqual(plan["by"], "serial")
        self.assertTrue(plan["sweeps"] and plan["listen"], plan)       # two dongles: the scanner has one to use

    def test_empty_maker_and_product_still_count(self):
        out = "Found 2 device(s):\n  0:  Realtek, RTL2838UHIDIR, SN: 11110001\n  1:  , , SN: 11110002\n"
        self.assertEqual([r[1] for r in self.probe(out, [])], ["11110001", "11110002"])

    def test_same_serial_twice_is_not_added_again(self):
        out = "Found 2 device(s):\n  0:  Generic, RTL2832U, SN: 00000001\n  1:  Generic, RTL2832U, SN: 00000001\n"
        self.assertEqual(len(self.probe(out, ["00000001", "00000001"])), 2)

    def test_rtl_test_missing_altogether(self):
        self.assertEqual([r[1] for r in self.probe("", ["11110001"])], ["11110001"])


if __name__ == "__main__":
    unittest.main()
