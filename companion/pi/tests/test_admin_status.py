"""Admin page: the device list, diagnostics, sessions that survive a restart, and the update log header.
Standard library only:
    python3 -m unittest companion/pi/tests/test_admin_status.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))


def load_admin():
    spec = importlib.util.spec_from_file_location("wavelens_admin_for_status_test", os.path.join(HERE, "..", "wavelens_admin.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = load_admin()


class Status(unittest.TestCase):
    def test_roles_from_new_and_old_bridges(self):
        self.assertEqual(A.radio_roles({"hw": {"rtl": [{"serial": "1", "role": "sweep"}]}}), {"1": "sweep"})
        self.assertEqual(A.radio_roles({"hw_line": "2 RTL-SDR (00000001: sweep, 00000002: listen) · no HackRF"}),
                         {"00000001": "sweep", "00000002": "listen"})

    def test_snapshot_and_diagnostics_are_json(self):
        snap = A.devices_snapshot()
        json.dumps(snap)
        d = A.run_diagnostics(deep=False)
        json.dumps(d)
        levels = [c["level"] for c in d["checks"]]
        order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
        self.assertEqual(levels, sorted(levels, key=order.get))     # worst first
        self.assertTrue(A.support_report().startswith("Wavelens support report"))

    def test_sessions_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            s1 = A.Sessions(path)
            tok = s1.new()
            s2 = A.Sessions(path)
            self.assertIsNotNone(s2.get(tok))
            self.assertEqual(s2.get(tok)["csrf"], s1.get(tok)["csrf"])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            s2.drop(tok)
            self.assertIsNone(A.Sessions(path).get(tok))
            os.chmod(path, 0o644)                                   # a readable file is not trusted
            s1.new()
            os.chmod(path, 0o644)
            self.assertEqual(A.Sessions(path).table, {})

    def test_update_log_start_time(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "update.log")
            with open(log, "w") as f:
                f.write("update: apply started 2026-11-12 18:00:00\nupdate: checking GitHub for the latest version\n")
            old = A.UPDATE_LOG, A.update_running
            A.UPDATE_LOG, A.update_running = log, lambda: True
            try:
                snap = A.update_log_snapshot()
            finally:
                A.UPDATE_LOG, A.update_running = old
            self.assertEqual(snap["title"], "apply")
            self.assertEqual(snap["started"], time.mktime(time.strptime("2026-11-12 18:00:00", "%Y-%m-%d %H:%M:%S")))


if __name__ == "__main__":
    unittest.main()
