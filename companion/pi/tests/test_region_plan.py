"""A country pack's radio plan: which ranges the Pi sweeps, where it looks for trunked control channels, what
Follow may tune, and which country a companion starts in. Australia is the worked case. No radio is touched.
    python3 -m unittest companion/pi/tests/test_region_plan.py
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
import regions  # noqa: E402


def load_bridge():
    argv, sys.argv = sys.argv, ["bridge"]
    try:
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_region_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


class PackPlanTest(unittest.TestCase):
    def test_australia_pack_has_a_plan(self):
        plan = regions.radio_plan(regions.find("AU"))
        self.assertIn("403M:430M:12.5k", plan["sweeps"]["uhf"])
        self.assertEqual(plan["cc_bands"][0][1:], (403.0, 430.0, "400 MHz"))
        self.assertTrue(any(lo <= 476.9 <= hi for lo, hi, _ in plan["follow_bands"]))            # UHF CB
        self.assertFalse(any(lo <= 433.92 <= hi for lo, hi, _ in plan["follow_bands"]))          # key fobs are not voice
        self.assertFalse(any("806M" in s or "851M" in s for s in plan["sweeps"]["uhf"]))         # no US 800 MHz

    def test_no_pack_and_a_pack_without_a_plan(self):
        self.assertEqual(regions.radio_plan(None), {})
        self.assertEqual(regions.radio_plan({"companion": {"bands": []}}), {})

    def test_bad_entries_keep_the_built_in_lists(self):
        bad = {"companion": {"sweeps": {"uhf": ["403M:430M", "5000M:5100M:12.5k", "; rm -rf /"]},
                             "cc_bands": [{"lo": 430, "hi": 403}], "follow_bands": [{"lo": 1, "hi": 2, "mode": "wbfm"}]}}
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(regions.radio_plan(bad), {})


class BridgePlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load_bridge()

    def setUp(self):
        self.sent = []
        self.saved_emit = self.b.emit
        self.b.emit = self.sent.append
        self.tmp = tempfile.TemporaryDirectory()
        self.region("US")

    def tearDown(self):
        self.region("US")
        self.b.REGION["home"] = ""
        self.b.emit = self.saved_emit
        self.tmp.cleanup()

    def region(self, code):
        with contextlib.redirect_stderr(io.StringIO()):
            self.b.set_region(code, announce=False)

    def area(self, **doc):
        path = os.path.join(self.tmp.name, "area.json")
        with open(path, "w") as f:
            json.dump(doc, f)
        with contextlib.redirect_stderr(io.StringIO()):
            return self.b.start_region("", path), self.b.start_region("US", path)

    def test_plan_follows_the_region_and_comes_back(self):
        b = self.b
        us = (list(b.SWEEP_UHF), list(b.CC_BANDS), list(b.FOLLOW_BANDS))
        self.region("AU")
        self.assertEqual(b.SWEEP_UHF, ["403M:430M:12.5k", "450M:520M:12.5k"])
        self.assertEqual(b.CC_BANDS[0][3], "400 MHz")
        self.assertEqual(b.follow_mode(476.9), "auto")
        self.assertIsNone(b.follow_mode(855.0))
        plan = b.plan_radios([(0, "00000001", "Generic"), (1, "00000002", "Generic"), (2, "00000003", "Generic")], False)
        self.assertEqual(plan["sweeps"], ["144M:174M:12.5k@00000002", "403M:430M:12.5k@00000002", "450M:520M:12.5k@00000002"])
        self.assertEqual(regions.rtl433_args(b.REGION["pack"], b.RTL433_DEFAULT_ARGS), "-f 433.92M -f 915M -H 30 -g 40")
        self.region("US")
        self.assertEqual((b.SWEEP_UHF, b.CC_BANDS, b.FOLLOW_BANDS), us)
        self.assertEqual(b.follow_mode(855.0), None)
        self.assertEqual(b.follow_mode(45.0), "auto")

    def test_the_hunt_band_is_covered_by_the_sweep(self):
        b = self.b
        self.assertFalse(b.sweep_specs_cover(["806M:824M:12.5k@A"], b.CC_BANDS[0]))      # US: 851–869 gets added
        self.region("AU")
        self.assertTrue(b.sweep_specs_cover(b.SWEEP_VHF + b.SWEEP_UHF, b.CC_BANDS[0]))   # AU: already in the plan

    def test_start_region_comes_from_the_area(self):
        self.assertEqual(self.area(country="AU", postcode="2000"), ("AU", "US"))         # explicit --region still wins
        self.assertEqual(self.b.REGION["home"], "AU")
        self.assertEqual(self.area(country="US", postcode="49749")[0], "US")
        self.assertEqual(self.area(country="ZZ")[0], "US")                               # no pack for it: US tables
        self.assertEqual(self.b.REGION["home"], "")
        self.assertEqual(self.area()[0], "US")                                           # no area set

    def test_channel_snap_knows_the_australian_bands(self):
        plans = self.b.CHANNEL_PLANS
        for mhz in (410.0, 476.9, 505.0):
            self.assertTrue(any(lo <= mhz <= hi for lo, hi, _ in plans), mhz)


if __name__ == "__main__":
    unittest.main()
