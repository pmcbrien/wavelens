"""Wi-Fi setup from the phone ({"c":"net"}): what the bridge makes of nmcli's output, what it refuses before
nmcli is ever run, and that a password never reaches the journal or the phone. nmcli is faked; invented
network names; nothing on this machine's network is touched. Standard library only:
    python3 -m unittest companion/pi/tests/test_net.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

SCAN = "\n".join([
    "*:Example Home:78:WPA2",
    " :Example Home:41:WPA2",                 # a second access point of the same network, weaker
    " :Cafe\\: Guest:55:",                    # a colon in the name, open network
    " ::60:WPA2",                             # hidden: no name, left out
    " :Back\\\\Slash:30:WPA1 WPA2",
    " :Example-5G:90:WPA3",
])
PASSWORD = "correct-horse-battery"


def load_bridge():
    argv, sys.argv = sys.argv, ["bridge"]
    try:
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_net_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


class NetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load_bridge()

    def setUp(self):
        b = self.b
        self.sent, self.calls = [], []
        self.join_rc, self.join_out = 0, "Device 'wlan0' successfully activated."
        self.saved = (b.emit, b._nm, b.net_current, b.hello, b.shutil.which, b.NET_ON, b.NET_JOIN_GAP_S)
        b.emit = self.sent.append
        b.hello = lambda: None
        b.shutil.which = lambda name: "/usr/bin/" + name
        b.net_current = lambda: {"ssid": "Example Home", "ip": "192.0.2.10"}
        b.NET_ON, b.NET_JOIN_GAP_S = True, 0.0
        b.NET["busy"], b.NET["last_join"] = "", 0.0

        def fake_nm(args, timeout=20.0):
            self.calls.append(list(args))
            if "list" in args:
                return 0, SCAN
            if "connect" in args:
                return self.join_rc, self.join_out
            return 0, ""
        b._nm = fake_nm

    def tearDown(self):
        b = self.b
        b.emit, b._nm, b.net_current, b.hello, b.shutil.which, b.NET_ON, b.NET_JOIN_GAP_S = self.saved

    def run_cmd(self, cmd):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.b.net_command(cmd)
            for _ in range(200):
                if not self.b.NET["busy"]:
                    break
                time.sleep(0.01)
        self.assertEqual(self.b.NET["busy"], "", "the request never finished")
        return err.getvalue()

    def test_scan_is_one_row_per_network_strongest_first(self):
        rows = self.b.net_parse_scan(SCAN)
        self.assertEqual([r["s"] for r in rows], ["Example Home", "Example-5G", "Cafe: Guest", "Back\\Slash"])
        self.assertEqual(rows[0], {"s": "Example Home", "sig": 78, "sec": "WPA2", "on": True})
        self.assertEqual(rows[2]["sec"], "")                       # open
        self.assertEqual(rows[3]["sec"], "WPA1 WPA2")

    def test_scan_is_capped(self):
        many = "\n".join(f" :Net {i:02d}:{i}:WPA2" for i in range(40))
        self.assertEqual(len(self.b.net_parse_scan(many)), self.b.NET_MAX)

    def test_scan_command_reports_current_network_and_list(self):
        self.run_cmd({"c": "net", "v": "scan"})
        doc = self.sent[-1]
        self.assertEqual((doc["t"], doc["ssid"], doc["ip"]), ("net", "Example Home", "192.0.2.10"))
        self.assertEqual(len(doc["nets"]), 4)

    def test_join_passes_name_and_password_as_separate_arguments(self):
        self.run_cmd({"c": "net", "v": "join", "ssid": "Example Home", "psk": PASSWORD})
        join = next(c for c in self.calls if "connect" in c)
        self.assertEqual(join[join.index("connect") + 1:], ["Example Home", "password", PASSWORD])
        self.assertTrue(self.sent[-1]["ok"])
        self.assertIn("Joined Example Home", self.sent[-1]["msg"])

    def test_open_and_hidden_networks(self):
        self.run_cmd({"c": "net", "v": "join", "ssid": "Cafe: Guest", "psk": "", "hidden": True})
        join = next(c for c in self.calls if "connect" in c)
        self.assertEqual(join[join.index("connect") + 1:], ["Cafe: Guest", "hidden", "yes"])

    def test_password_never_reaches_the_journal_or_the_phone(self):
        self.join_rc = 4
        self.join_out = f"Error: 802-11-wireless-security.psk: '{PASSWORD}' is not valid. Secrets were required."
        journal = self.run_cmd({"c": "net", "v": "join", "ssid": "Example Home", "psk": PASSWORD})
        self.assertNotIn(PASSWORD, journal)
        self.assertNotIn(PASSWORD, repr(self.sent))
        self.assertFalse(self.sent[-1]["ok"])
        self.assertIn("check the password", self.sent[-1]["msg"])

    def test_network_out_of_range(self):
        self.join_rc, self.join_out = 10, "Error: No network with SSID 'Example Far' found."
        self.run_cmd({"c": "net", "v": "join", "ssid": "Example Far", "psk": PASSWORD})
        self.assertIn("can't see Example Far", self.sent[-1]["msg"])

    def test_bad_input_is_refused_before_nmcli_runs(self):
        for ssid, psk in (("", PASSWORD), ("x" * 33, PASSWORD), ("Example Home", "short"), ("Example Home", "p" * 65),
                          ("--ask", PASSWORD), ("Example\nHome", PASSWORD), (["a"], PASSWORD), ("Example Home", 12345678)):
            self.calls.clear()
            self.run_cmd({"c": "net", "v": "join", "ssid": ssid, "psk": psk})
            self.assertFalse(self.sent[-1]["ok"], (ssid, psk))
            self.assertFalse(any("connect" in c for c in self.calls), (ssid, psk))

    def test_one_request_at_a_time(self):
        self.b.NET["busy"] = "join"
        try:
            self.b.net_command({"c": "net", "v": "scan"})
            self.assertEqual(self.sent[-1]["busy"], "join")
            self.assertEqual(self.calls, [])
        finally:
            self.b.NET["busy"] = ""

    def test_switched_off(self):
        self.b.NET_ON = False
        self.run_cmd({"c": "net", "v": "join", "ssid": "Example Home", "psk": PASSWORD})
        self.assertFalse(self.sent[-1]["ok"])
        self.assertEqual(self.calls, [])
        self.assertFalse(self.b.net_available())

    def test_no_networkmanager(self):
        self.b.shutil.which = lambda name: None
        self.run_cmd({"c": "net", "v": "scan"})
        self.assertIn("NetworkManager", self.sent[-1]["msg"])
        self.assertEqual(self.calls, [])

    def test_unknown_request(self):
        self.run_cmd({"c": "net", "v": "forget", "ssid": "Example Home"})
        self.assertFalse(self.sent[-1]["ok"])
        self.assertEqual(self.calls, [])


class RxLinesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load_bridge()

    def test_one_write_one_command(self):
        rx = self.b.RxLines()
        self.assertEqual(rx.feed(b'{"c":"ping"}'), {"c": "ping"})          # no newline: older apps
        self.assertEqual(rx.feed(b'{"c":"ping"}\n'), {"c": "ping"})

    def test_a_command_split_across_writes(self):
        line = ('{"c":"net","v":"join","ssid":"' + "s" * 32 + '","psk":"' + "p" * 63 + '"}\n').encode()
        rx, got = self.b.RxLines(), []
        for i in range(0, len(line), 20):                                  # the smallest MTU a phone may be left with
            got.append(rx.feed(line[i:i + 20], now=100.0))
        self.assertTrue(all(g is None for g in got[:-1]))
        self.assertEqual(got[-1]["psk"], "p" * 63)

    def test_a_stale_half_does_not_spoil_the_next_command(self):
        rx = self.b.RxLines()
        self.assertIsNone(rx.feed(b'{"c":"net","v":"jo', now=10.0))
        self.assertEqual(rx.feed(b'{"c":"ping"}\n', now=20.0), {"c": "ping"})

    def test_rubbish_is_dropped(self):
        rx = self.b.RxLines()
        self.assertIsNone(rx.feed(b"not json\n"))
        self.assertIsNone(rx.feed(b"[1,2]\n"))
        self.assertIsNone(rx.feed(b"x" * 5000))
        self.assertEqual(rx.feed(b'{"c":"ping"}\n'), {"c": "ping"})


if __name__ == "__main__":
    unittest.main()
