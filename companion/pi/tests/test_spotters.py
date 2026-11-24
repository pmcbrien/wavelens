"""Pi-hosted Spotters against fake ESP32s on pseudo-terminals: hosting handshake, auto roles, relaying hits
tagged with the unit, owner-chosen roles that persist, the phone's Bluetooth Spotter taking Bluetooth,
pausing a port for the flasher, and unplugging. Standard library only:
    python3 -m unittest companion/pi/tests/test_spotters.py
"""
from __future__ import annotations

import json
import os
import pty
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import spotters  # noqa: E402


class FakeBoard:
    """Answers like the firmware: '@'-prefixed JSON lines once hosted, applies mode/ch, can emit hits."""

    def __init__(self, name: str):
        self.master, slave = pty.openpty()
        self.path = os.ttyname(slave)
        self.slave = slave                      # keep open so the pty survives the hub reopening it
        self.name, self.hosted, self.mode, self.ch = name, False, "both", "hop"
        self.face = ""
        self.got: "list[dict]" = []
        self.alive = True
        threading.Thread(target=self.run, daemon=True).start()

    def say(self, obj: dict) -> None:
        os.write(self.master, b"@" + json.dumps(obj).encode() + b"\n")

    def hello(self) -> None:
        h = {"t": "hello", "fw": "wavelens-esp32", "ver": "2026.11.23.1", "name": self.name, "chip": "esp32s3",
             "caps": ["wifi", "ble", "host", "df"]}
        if self.face:
            h["face"] = self.face
        self.say(h)

    def run(self) -> None:
        os.write(self.master, b"ets Jun  8 2016 boot text\r\n")
        buf = b""
        while self.alive:
            try:
                data = os.read(self.master, 1024)
            except OSError:
                return
            buf += data
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                try:
                    c = json.loads(line)
                except ValueError:
                    continue
                self.got.append(c)
                if c.get("c") == "host":
                    self.hosted = True
                    self.hello()
                elif c.get("c") == "face":
                    self.face = c["v"]
                    self.hello()
                elif c.get("c") == "tune":
                    self.say({"t": "tune", "ok": True, "ch": c["ch"], "ms": c["ms"]})
                elif c.get("c") == "mode":
                    self.mode = c["v"]
                elif c.get("c") == "ch":
                    self.ch = c["v"]


def wait(pred, secs=8.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class HubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        spotters.ROLES_FILE = os.path.join(self.tmp, "spotters.json")
        spotters.PAUSE_FILE = os.path.join(self.tmp, "pause")
        spotters.HOST_EVERY_S = 0.3
        self.a, self.b = FakeBoard("Wavelens-AAAA"), FakeBoard("Wavelens-BBBB")
        self.ports = {"platform-x-usb-0:1.1:1.0-port0": self.a.path, "platform-x-usb-0:1.2:1.0-port0": self.b.path}
        spotters.discover = lambda: dict(self.ports)
        self.sent: "list[dict]" = []
        self.logs: "list[str]" = []
        self.hub = spotters.Hub(self.sent.append, self.logs.append)

    def tearDown(self):
        for u in list(self.hub.units.values()):
            u.stop = True

    def test_host_auto_roles_and_relay(self):
        self.assertTrue(wait(lambda: self.a.hosted and self.b.hosted), "boards were not hosted")
        self.assertTrue(wait(lambda: self.a.mode == "ble" and self.b.mode == "wifi" and self.b.ch == [1, 6, 11]),
                        f"auto roles: a={self.a.mode} b={self.b.mode}/{self.b.ch}")
        self.b.say({"t": "wifi", "bssid": "02:00:00:00:00:01", "ssid": "Example", "r": -60, "ch": 6})
        self.assertTrue(wait(lambda: any(o.get("t") == "wifi" for o in self.sent)))
        hit = next(o for o in self.sent if o.get("t") == "wifi")
        self.assertEqual(hit["u"], "USB 1.2")
        info = {i["u"]: i for i in self.hub.info()}
        self.assertTrue(info["USB 1.1"]["hosted"] and info["USB 1.1"]["auto"])
        self.assertEqual(info["USB 1.2"]["role_label"], "Wi-Fi ch 1, 6, 11")

    def test_owner_role_persists_and_auto_returns(self):
        self.assertTrue(wait(lambda: self.a.hosted and self.b.hosted))
        msg = self.hub.set_role("USB 1.1", "wifi:hop")
        self.assertIn("all channels", msg)
        self.assertTrue(wait(lambda: self.a.mode == "wifi" and self.a.ch == "hop"))
        with open(spotters.ROLES_FILE) as f:
            self.assertEqual(json.load(f), {"platform-x-usb-0:1.1:1.0-port0": "wifi:hop"})
        self.assertIn("unknown job", self.hub.set_role("USB 1.1", "nonsense"))
        self.assertIn("no Spotter", self.hub.set_role("USB 9.9", "ble"))
        self.hub.set_role("USB 1.1", "auto")
        self.assertTrue(wait(lambda: self.a.mode == "ble"))

    def test_phone_spotter_hops_everything(self):
        self.assertTrue(wait(lambda: self.a.hosted and self.b.hosted))
        self.hub.set_phone_spotter(True)
        self.assertTrue(wait(lambda: self.a.mode == "ble" and self.b.mode == "wifi" and self.b.ch == [1, 6, 11]))
        self.assertEqual(self.hub.phone_role(), "both")

    def test_pause_for_flasher_then_resume(self):
        self.assertTrue(wait(lambda: self.a.hosted))
        with open(spotters.PAUSE_FILE, "w") as f:
            f.write(self.a.path + "\n")
        unit = next(u for u in self.hub.units.values() if u.dev == self.a.path)
        self.assertTrue(wait(lambda: unit.fd is None and unit.msg == "being flashed"))
        os.remove(spotters.PAUSE_FILE)
        self.a.hosted = False
        self.assertTrue(wait(lambda: self.a.hosted and unit.hosted, 12), "not re-hosted after the flash")

    def test_unplug(self):
        self.assertTrue(wait(lambda: len(self.hub.units) == 2))
        del self.ports["platform-x-usb-0:1.2:1.0-port0"]
        self.assertTrue(wait(lambda: len(self.hub.units) == 1, 12))
        self.assertTrue(any("unplugged" in l for l in self.logs))
        self.assertTrue(wait(lambda: self.a.mode == "both"), "a lone Spotter should go back to Wi-Fi + Bluetooth")


    def test_direction_finding(self):
        self.assertTrue(wait(lambda: self.a.hosted and self.b.hosted))
        self.assertIn("facing", self.hub.set_face("USB 1.1", "left"))
        self.assertIn("facing", self.hub.set_face("USB 1.2", "right"))
        self.assertIn("must be", self.hub.set_face("USB 1.2", "upwards"))
        self.assertTrue(wait(lambda: self.hub.faced_count() == 2), "faces not picked up from the hellos")
        # two sides set: the Wi-Fi unit keeps Bluetooth on beside its channels
        self.assertTrue(wait(lambda: self.b.mode == "both" and self.b.ch == [1, 6, 11]), f"b={self.b.mode}/{self.b.ch}")
        dev = "02:00:00:00:00:09"
        for _ in range(3):
            self.a.say({"t": "ble", "id": dev, "r": -80})
            self.b.say({"t": "ble", "id": dev, "r": -55})
        self.assertTrue(wait(lambda: any(o.get("brg") for o in self.sent if o.get("id") == dev)), "no bearing on the hits")
        brg = [o["brg"] for o in self.sent if o.get("id") == dev and o.get("brg")][-1]
        self.assertTrue(45 <= brg["deg"] <= 135, brg)                 # louder on the right
        self.assertEqual(brg["word"], "right")
        self.assertEqual(brg["n"], 2)
        info = {i["u"]: i for i in self.hub.info()}
        self.assertEqual((info["USB 1.1"]["face"], info["USB 1.2"]["face"]), ("left", "right"))
        # a converge: both units tune to the channel, once per few seconds per device
        self.assertIn("2 Spotters", self.hub.converge("02:00:00:00:00:0a", 6))
        self.assertIn("already", self.hub.converge("02:00:00:00:00:0a", 6))
        self.assertTrue(wait(lambda: any(c.get("c") == "tune" and c.get("ch") == 6 for c in self.a.got)
                             and any(c.get("c") == "tune" for c in self.b.got)))
        self.assertIn("needs two", self.hub.converge("02:00:00:00:00:0b", 149))   # no 5 GHz on these chips


class PlanTest(unittest.TestCase):
    def test_bearing_maths(self):
        b = spotters.bearing
        self.assertIsNone(b({"x": -60}, {"x": 0}))                    # one side: no direction
        self.assertIsNone(b({"x": -60, "y": -60}, {"x": 0, "y": 0}))  # two on the same side
        deg, agree, n = b({"f": -50, "r": -80}, {"f": 0, "r": 180})
        self.assertTrue(deg < 10 or deg > 350)
        deg, _, _ = b({"l": -70, "r": -70, "f": -50}, {"l": 270, "r": 90, "f": 0})
        self.assertTrue(deg < 10 or deg > 350)
        self.assertEqual(spotters.compass_word(100), "right")
        self.assertEqual(spotters.compass_word(330), "ahead-left")


    def test_auto_roles(self):
        r = spotters.auto_roles
        self.assertEqual(list(r(["a"], False).values()), ["both"])
        self.assertEqual(list(r(["a", "b", "c", "d"], False).values()), ["ble", "wifi:1", "wifi:6", "wifi:11"])
        self.assertEqual(list(r(["a", "b", "c", "d", "e"], False).values())[-1], "wifi:hop")
        self.assertEqual(list(r(["a"], True).values()), ["wifi:1,6,11"])
        self.assertEqual(list(r(["a", "b"], True).values()), ["ble", "wifi:1,6,11"])

    def test_port_label(self):
        self.assertEqual(spotters.port_label("platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0-port0"), "USB 1.3")

    def test_role_commands(self):
        self.assertEqual(spotters.role_commands("wifi:1,6,11")[1], {"c": "ch", "v": [1, 6, 11]})
        self.assertEqual(spotters.role_commands("ble"), [{"c": "mode", "v": "ble"}])


if __name__ == "__main__":
    unittest.main()
