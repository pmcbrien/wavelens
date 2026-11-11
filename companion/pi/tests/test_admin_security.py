"""Admin page, Settings tab, "Bluetooth and remote access": the four switches write the defaults file or
switch a systemd unit, only when they change, and the bridge reads the new keys. systemctl and bluetoothctl
are faked; nothing on this machine is touched. Standard library only:
    python3 -m unittest companion/pi/tests/test_admin_security.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import wavelens_admin as admin  # noqa: E402

DEFAULTS = """# Wi-Fi setup from the app.
WAVELENS_WIFI_SETUP=1

# Updates and the area from the app.
WAVELENS_APP_CONTROL=1
WAVELENS_ARGS="--name Wavelens-Test"
"""


class SecuritySettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "wavelens-bridge")
        with open(self.path, "w") as f:
            f.write(DEFAULTS)
        self.saved = (admin.DEFAULTS_FILE, admin.run)
        admin.DEFAULTS_FILE = self.path
        self.units = {"wavelens-bt-agent": "enabled", "ssh": "enabled", "sshd": "enabled", "ssh.socket": "disabled"}
        self.calls = []

        def fake_run(argv, timeout=20, stdin_text=None):
            self.calls.append(list(argv))
            if argv[:2] == ["systemctl", "is-enabled"]:
                st = self.units.get(argv[2])
                return (0, st) if st else (1, "Failed to get unit file state: No such file or directory")
            if argv[:2] == ["systemctl", "enable"] or argv[:2] == ["systemctl", "disable"]:
                self.units[argv[3]] = argv[1] + "d"
                return 0, ""
            return 0, ""
        admin.run = fake_run

    def tearDown(self):
        admin.DEFAULTS_FILE, admin.run = self.saved
        self.tmp.cleanup()

    def form(self, **kw):
        f = {"wifi_setup": "1", "app_control": "1", "bt_autopair": "1", "ssh": "1"}
        f.update(kw)
        return f

    def test_state_read(self):
        st = admin.security_state()
        self.assertEqual((st["wifi_setup"], st["app_control"], st["bt_autopair"], st["ssh"], st["ssh_unit"]),
                         (True, True, "on", "on", "ssh"))

    def test_nothing_changed_touches_nothing(self):
        before = open(self.path).read()
        self.calls.clear()
        msgs, restart = admin.security_apply(self.form(), admin.security_state())
        self.assertEqual(msgs, [])
        self.assertFalse(restart)
        self.assertEqual(open(self.path).read(), before)
        self.assertFalse([c for c in self.calls if c[1] in ("enable", "disable")])

    def test_turning_off_app_switches_writes_defaults_and_restarts(self):
        msgs, restart = admin.security_apply(self.form(wifi_setup="0", app_control="0"), admin.security_state())
        self.assertTrue(restart)
        d = admin.defaults_read()
        self.assertEqual((d["WAVELENS_WIFI_SETUP"], d["WAVELENS_APP_CONTROL"]), ("0", "0"))
        self.assertEqual(d["WAVELENS_ARGS"], "--name Wavelens-Test")          # other keys untouched
        self.assertIn("# Wi-Fi setup from the app.", open(self.path).read())   # comments kept
        self.assertEqual(len(msgs), 2)

    def test_missing_key_counts_as_on_and_is_appended(self):
        with open(self.path, "w") as f:
            f.write("WAVELENS_WIFI_SETUP=1\n")
        self.assertTrue(admin.security_state()["app_control"])
        admin.security_apply(self.form(app_control="0"), admin.security_state())
        self.assertEqual(admin.defaults_read()["WAVELENS_APP_CONTROL"], "0")

    def test_pairing_off_disables_agent_and_pairable(self):
        msgs, restart = admin.security_apply(self.form(bt_autopair="0"), admin.security_state())
        self.assertFalse(restart)                        # a unit, not a bridge setting
        self.assertIn(["systemctl", "disable", "--now", "wavelens-bt-agent"], self.calls)
        self.assertIn(["bluetoothctl", "pairable", "off"], self.calls)

    def test_ssh_off_and_back_on(self):
        self.units["ssh.socket"] = "enabled"
        admin.security_apply(self.form(ssh="0"), admin.security_state())
        self.assertIn(["systemctl", "disable", "--now", "ssh"], self.calls)
        self.assertIn(["systemctl", "disable", "--now", "ssh.socket"], self.calls)
        self.assertEqual(admin.security_state()["ssh"], "off")
        admin.security_apply(self.form(ssh="1"), admin.security_state())
        self.assertIn(["systemctl", "enable", "--now", "ssh"], self.calls)

    def test_missing_units_are_left_alone(self):
        self.units = {}
        st = admin.security_state()
        self.assertEqual((st["bt_autopair"], st["ssh"]), ("missing", "missing"))
        self.calls.clear()
        msgs, _ = admin.security_apply(self.form(bt_autopair="0", ssh="0"), st)
        self.assertEqual(msgs, [])
        self.assertFalse([c for c in self.calls if c[1] in ("enable", "disable")])

    def test_card_renders_and_escapes(self):
        html = admin.Handler.security_card(None, {"csrf": 'x"<'})
        self.assertIn('action="/security"', html)
        self.assertIn("x&quot;&lt;", html)
        for key in ("wifi_setup", "app_control", "bt_autopair", "ssh"):
            self.assertIn(f'name="{key}"', html)


class BridgeReadsTheKeysTest(unittest.TestCase):
    def load(self, env):
        argv, sys.argv = sys.argv, ["bridge"]
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            spec = importlib.util.spec_from_file_location("wavelens_bridge_for_admin_test",
                                                          os.path.join(HERE, "..", "wavelens_bridge.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        finally:
            sys.argv = argv
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_defaults_on(self):
        b = self.load({"WAVELENS_WIFI_SETUP": "1", "WAVELENS_APP_CONTROL": "1"})
        self.assertTrue(b.NET_ON)
        self.assertTrue(b.APP_CONTROL)

    def test_off(self):
        b = self.load({"WAVELENS_WIFI_SETUP": "0", "WAVELENS_APP_CONTROL": "off"})
        self.assertFalse(b.NET_ON)
        self.assertFalse(b.APP_CONTROL)


if __name__ == "__main__":
    unittest.main()
