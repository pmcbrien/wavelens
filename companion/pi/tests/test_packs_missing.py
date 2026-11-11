"""A local data file that will not load never stops the bridge: it is left out and named. And the admin
page tells a missing data file from missing software. Invented data. Standard library only:
    python3 -m unittest companion/pi/tests/test_packs_missing.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

RR_COUNTY = ('"Frequency Output","Frequency Input","FCC Callsign",Agency/Category,Description,"Alpha Tag","PL Output Tone","PL Input Tone",Mode,"Class Station Code",Tag\n'
             '151.040000,159.19500,WQZZ009,"Example County","Road Commission","Example Roads",,,FM,RM,"Public Works"\n')
PACK = {"format": "wavelens-fcc-local/1", "area": {}, "entries": [
    {"mhz": 458.0125, "mhz_hi": None, "bw_khz": 12.5, "call": "VKZ001", "name": "Example Regional Council", "service": "Land Mobile",
     "service_label": "Land Mobile", "class": "FB", "cat": "UNKNOWN", "agency": "Local government", "place": "Example Hill", "sites": [[-26.0, 153.0]]}]}


def load(name):
    argv, sys.argv = sys.argv, [name]
    try:
        spec = importlib.util.spec_from_file_location(name + "_for_missing_pack_test", os.path.join(HERE, "..", name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stderr(io.StringIO()):
            spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


class MissingPackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load("wavelens_bridge")
        cls.admin = load("wavelens_admin")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.good_fcc = os.path.join(d, "licences.json")
        with open(self.good_fcc, "w", encoding="utf-8") as f:
            json.dump(PACK, f)
        self.good_rr = os.path.join(d, "county.csv")
        with open(self.good_rr, "w", encoding="utf-8") as f:
            f.write(RR_COUNTY)
        self.junk = os.path.join(d, "junk.json")
        with open(self.junk, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.gone = os.path.join(d, "not-copied-yet.json")

    def tearDown(self):
        self.tmp.cleanup()

    def packs(self, fcc="", rr=(), found=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = self.b.load_local_packs(fcc, list(rr), [], found or {"fcc": "", "rr": [], "counties": []})
        return out + (err.getvalue(),)

    def test_named_licence_pack_that_is_missing_is_left_out(self):
        fcc, rr, problems, log = self.packs(fcc=self.gone)
        self.assertIsNone(fcc)
        self.assertEqual(len(problems), 1)
        self.assertIn("is not on this Pi", problems[0])
        self.assertIn("--fcc-pack", problems[0])
        self.assertIn("left out", log)

    def test_named_pack_missing_falls_back_to_the_area_folder(self):
        fcc, _, problems, _ = self.packs(fcc=self.gone, found={"fcc": self.good_fcc, "rr": [], "counties": []})
        self.assertEqual(len(fcc.entries), 1)
        self.assertEqual(len(problems), 1)

    def test_unreadable_pack_is_left_out_too(self):
        fcc, _, problems, _ = self.packs(fcc=self.junk)
        self.assertIsNone(fcc)
        self.assertIn("could not be read", problems[0])

    def test_one_bad_radioreference_file_costs_only_itself(self):
        _, rr, problems, _ = self.packs(rr=[os.path.join(self.tmp.name, "nope.csv"), self.good_rr])
        self.assertEqual(len(rr.rows), 2)                       # the good file's one channel, output and input side
        self.assertEqual(len(problems), 1)
        self.assertIn("--rr-pack", problems[0])

    def test_all_good_means_no_problems(self):
        fcc, rr, problems, _ = self.packs(fcc=self.good_fcc, rr=[self.good_rr])
        self.assertEqual((len(fcc.entries), problems), (1, []))
        self.assertIsNotNone(rr)
        self.assertEqual(self.packs()[:3], (None, None, []))

    def test_admin_tells_a_missing_data_file_from_missing_software(self):
        def verdict(line):
            return next((fix for rx, _, fix in self.admin.KNOWN_FAULTS if rx.search(line)), None)
        self.assertEqual(verdict("FileNotFoundError: [Errno 2] No such file or directory: '/usr/local/lib/wavelens/licences-au.json'"), "settings")
        self.assertEqual(verdict("FileNotFoundError: [Errno 2] No such file or directory: '/usr/local/lib/wavelens/rr/county.csv'"), "settings")
        self.assertEqual(verdict("python3: can't open file '/usr/local/lib/wavelens/wavelens_bridge.py': [Errno 2] No such file or directory"), "reinstall")
        self.assertEqual(verdict("No such file or directory: '/usr/local/lib/wavelens/regions.py'"), "reinstall")
        self.assertIn("settings", self.admin.FIXES)


if __name__ == "__main__":
    unittest.main()
