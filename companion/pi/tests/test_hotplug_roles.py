"""Hotplug roles: a listening dongle that drops off USB and comes back gets listening back, and with
two dongles a returning one is never handed to rtl_433. Made-up serials; no radio is touched.
    python3 -m unittest companion/pi/tests/test_hotplug_roles.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))


def load_bridge():
    argv, sys.argv = sys.argv, ["bridge"]
    try:
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_hotplug_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


LISTEN_SN, SWEEP_SN = "00000001", "00000002"


class HotplugRoles(unittest.TestCase):
    def setUp(self):
        b = self.b = load_bridge()
        b.emit = lambda *a, **k: None
        b.hello = lambda *a, **k: None
        self.resolved = []
        b.listen_resolve = lambda mode, why, mhz=None: self.resolved.append(why)
        b.AUTO.update({"on": True, "known": {LISTEN_SN, SWEEP_SN}, "hackrf": False, "args": None, "rtl433": None, "listen_home": None})
        b.LISTEN["dev"] = LISTEN_SN
        b.LISTEN["req"] = (856.9625, "p25", 0)
        b.LISTEN["mode"] = "p25"

    def test_listening_returns_to_its_dongle(self):
        b = self.b
        b.hotplug_step({SWEEP_SN}, False)
        self.assertEqual(b.LISTEN["dev"], SWEEP_SN)          # moved while it was gone
        b.hotplug_step({SWEEP_SN, LISTEN_SN}, False)
        self.assertEqual(b.LISTEN["dev"], LISTEN_SN)         # and back when it returns
        self.assertEqual(self.resolved, ["the listening dongle is back"])
        self.assertIsNone(b.AUTO.get("rtl433"))

    def test_two_dongles_never_start_sensors(self):
        b = self.b
        b.LISTEN["dev"] = LISTEN_SN
        b.AUTO["known"] = {LISTEN_SN}
        b.hotplug_step({LISTEN_SN, SWEEP_SN}, False)        # a second dongle appears while one listens
        self.assertIsNone(b.AUTO.get("rtl433"))

    def test_nothing_left_then_back(self):
        b = self.b
        b.AUTO["known"] = {LISTEN_SN}
        b.hotplug_step(set(), False)
        b.hotplug_step({LISTEN_SN}, False)
        self.assertEqual(b.LISTEN["dev"], LISTEN_SN)
        self.assertIsNone(b.AUTO.get("listen_home"))


HUNG = """Current status for hub 3 [1d6b:0002 xHCI Host Controller, USB 2.00, 2 ports, ppps]
  Port 1: 0503 power highspeed enable connect [0bda:2838 Radio 00000002]
  Port 2: 0100 power
Current status for hub 1 [1d6b:0002 xHCI Host Controller, USB 2.00, 2 ports, ppps]
  Port 1: 0503 power highspeed enable connect []
  Port 2: 0100 power
"""
UNPLUGGED = HUNG.replace("0503 power highspeed enable connect []", "0100 power")


class UsbRescue(unittest.TestCase):
    def setUp(self):
        b = self.b = load_bridge()
        b.emit = lambda *a, **k: None
        for target, name, fake in ((b.time, "sleep", lambda s: None), (b.shutil, "which", lambda name: "/usr/sbin/" + name)):
            p = mock.patch.object(target, name, fake)   # shared modules: undo after each test
            p.start(); self.addCleanup(p.stop)
        b.USB_RESCUE.update({"seen_at": 0.0, "tries": [], "gave_up": False})
        self.cmds = []

    def run_(self, cmd):
        self.cmds.append(cmd)
        return ""

    def test_hung_port_found_unplugged_ignored(self):
        self.assertEqual(self.b.usb_stuck_hubs(HUNG), ["1"])
        self.assertEqual(self.b.usb_stuck_hubs(UNPLUGGED), [])

    def test_cycles_after_ten_seconds_then_rate_limits(self):
        b = self.b
        self.assertFalse(b.usb_rescue(1000.0, HUNG, self.run_))          # first sight: wait
        self.assertFalse(b.usb_rescue(1005.0, HUNG, self.run_))          # not 10 s yet
        self.assertTrue(b.usb_rescue(1011.0, HUNG, self.run_))           # power-cycle every hub
        self.assertEqual(self.cmds, ["uhubctl -l 3 -a off", "uhubctl -l 1 -a off", "uhubctl -l 3 -a on", "uhubctl -l 1 -a on"])
        b.usb_rescue(1020.0, HUNG, self.run_)
        self.assertFalse(b.usb_rescue(1040.0, HUNG, self.run_))          # within the two-minute gap
        self.assertTrue(b.usb_rescue(1200.0, HUNG, self.run_))

    def test_gives_up_after_three_an_hour(self):
        b = self.b
        t = 1000.0
        for _ in range(3):
            b.usb_rescue(t, HUNG, self.run_); t += 11
            self.assertTrue(b.usb_rescue(t, HUNG, self.run_)); t += 200
        b.usb_rescue(t, HUNG, self.run_); t += 11
        self.assertFalse(b.usb_rescue(t, HUNG, self.run_))
        self.assertTrue(b.USB_RESCUE["gave_up"])

    def test_unplugged_radio_never_cycles(self):
        b = self.b
        for t in (1000.0, 1020.0, 1400.0):
            self.assertFalse(b.usb_rescue(t, UNPLUGGED, self.run_))
        self.assertEqual(self.cmds, [])


if __name__ == "__main__":
    unittest.main()
