"""Renaming a Scanner from the app ({"c":"name"}): the settings file keeps everything but the name, bad names
are refused, and an empty name goes back to the default. Standard library only:
    python3 -m unittest companion/pi/tests/test_rename.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import wavelens_bridge as wb  # noqa: E402

SETTINGS = '''# /etc/default/wavelens-bridge
WAVELENS_AUTO=1
# WAVELENS_ARGS="--name Commented-Out"
WAVELENS_RTL433="-f 433.92M -g 40"
WAVELENS_ARGS="--region us --rr-county 'Example County'"
WAVELENS_AUTO_UPDATE=0
'''


class Rename(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "wavelens-bridge")
        with open(self.path, "w") as f:
            f.write(SETTINGS)

    def tearDown(self):
        self.dir.cleanup()

    def lines(self):
        with open(self.path) as f:
            return f.read().splitlines()

    def test_args_with_name(self):
        self.assertEqual(wb.args_with_name("--region us", "Dads-Truck"), "--region us --name Dads-Truck")
        self.assertEqual(wb.args_with_name("--name Old --region us", "New_1"), "--region us --name New_1")
        self.assertEqual(wb.args_with_name("--name=Old --region us", ""), "--region us")
        self.assertEqual(wb.args_with_name("", "X"), "--name X")

    def test_save_keeps_everything_else(self):
        wb.save_name("Dads-Truck", self.path)
        got = self.lines()
        self.assertIn('''WAVELENS_ARGS="--region us --rr-county 'Example County' --name Dads-Truck"''', got)
        want = SETTINGS.splitlines()
        self.assertEqual([l for l in got if not l.startswith("WAVELENS_ARGS=")],
                         [l for l in want if not l.startswith("WAVELENS_ARGS=")])

    def test_rename_twice_then_reset(self):
        wb.save_name("One", self.path)
        wb.save_name("Two", self.path)
        self.assertIn('''WAVELENS_ARGS="--region us --rr-county 'Example County' --name Two"''', self.lines())
        wb.save_name("", self.path)
        self.assertIn('''WAVELENS_ARGS="--region us --rr-county 'Example County'"''', self.lines())

    def test_no_args_line_yet(self):
        with open(self.path, "w") as f:
            f.write("WAVELENS_AUTO=1\n")
        wb.save_name("Car", self.path)
        self.assertEqual(self.lines(), ["WAVELENS_AUTO=1", "", 'WAVELENS_ARGS="--name Car"'])

    def test_name_rule(self):
        for ok in ("A", "Dads-Truck", "car_2", "x" * 20):
            self.assertTrue(wb.NAME_RE.match(ok), ok)
        for bad in ("", "x" * 21, "two words", 'a"b', "café", "a;b", "$(id)"):
            self.assertFalse(wb.NAME_RE.match(bad), bad)

    def test_refused_names_change_nothing(self):
        said = []
        old_emit, old_file = wb.emit, wb.DEFAULTS_FILE
        wb.emit, wb.DEFAULTS_FILE = said.append, self.path
        os.environ["INVOCATION_ID"] = "test"
        try:
            for bad in ("two words", "x" * 21, 7, ["a"]):
                wb.rename(bad)
        finally:
            wb.emit, wb.DEFAULTS_FILE = old_emit, old_file
            del os.environ["INVOCATION_ID"]
        self.assertEqual(len(said), 4)
        self.assertTrue(all("name must be" in m["msg"] for m in said))
        self.assertEqual(self.lines(), SETTINGS.splitlines())

    def test_started_by_hand_cannot_rename(self):
        said = []
        old_emit, old_file = wb.emit, wb.DEFAULTS_FILE
        wb.emit, wb.DEFAULTS_FILE = said.append, self.path
        os.environ.pop("INVOCATION_ID", None)
        try:
            wb.rename("Car")
        finally:
            wb.emit, wb.DEFAULTS_FILE = old_emit, old_file
        self.assertIn("--name", said[0]["msg"])
        self.assertEqual(self.lines(), SETTINGS.splitlines())


if __name__ == "__main__":
    unittest.main()
