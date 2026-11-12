"""Follow plays speech. A channel RadioReference lists as data (mode Telm: telemetry, paging, alarms; or tag
Data) is not tuned, because as FM it is a buzz. Invented channels; no radio is touched.
    python3 -m unittest companion/pi/tests/test_follow_data.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))


def load_bridge():
    argv, sys.argv = sys.argv, ["bridge"]
    try:
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_follow_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


class FollowSkipsData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load_bridge()

    def setUp(self):
        b = self.b
        self.saved = (b.listen_start, b.LISTEN["pages"], b.pager_band_ok, dict(b.LISTEN["recent"]))
        self.trunk = (set(b.TRUNK["cats"]), b.TRUNK.get("unlisted", "mute"))
        b.TRUNK["cats"] = set()              # no category chosen: every voice hit is a candidate
        self.tuned = []
        b.listen_start = lambda mhz, mode, sq, tol=0.0: self.tuned.append((round(mhz, 4), mode))
        b.LISTEN["recent"].clear()
        b.LISTEN["played"].clear()
        b.LISTEN["req"] = None
        b.LISTEN["pages"] = False

    def tearDown(self):
        b = self.b
        b.listen_start, b.LISTEN["pages"], b.pager_band_ok, recent = self.saved
        b.TRUNK["cats"], b.TRUNK["unlisted"] = self.trunk
        b.LISTEN["recent"].clear()
        b.LISTEN["recent"].update(recent)

    def hit(self, mhz, **x):
        x.setdefault("exact_mhz", f"{mhz:.4f}")
        x.setdefault("lic_mhz", f"{mhz:.5f}")
        self.b.follow_hit({"f": int(mhz), "x": x})

    def test_a_chosen_category_filters_conventional_hits_too(self):
        b = self.b
        b.TRUNK["cats"], b.TRUNK["unlisted"] = {"police"}, "mute"
        self.hit(464.375, agency="Business", rr_mode="DMR")
        self.hit(155.250, agency="Fire / rescue", rr_mode="FMN")
        self.hit(461.300)                                    # nothing names it
        self.assertEqual(self.tuned, [])
        self.hit(453.725, agency="Law enforcement", rr_mode="FMN")
        self.assertEqual([m for m, _ in self.tuned], [453.725])
        b.TRUNK["unlisted"] = "play"                         # the option that lets unnamed things through
        b.LISTEN["req"] = None
        self.hit(461.300)
        self.assertIn(461.3, [m for m, _ in self.tuned])

    def test_what_counts_as_data(self):
        d = self.b.data_channel
        self.assertTrue(d({"rr_mode": "Telm", "rr_tag": "Data"}))
        self.assertTrue(d({"rr_mode": "telm"}))
        self.assertTrue(d({"rr_mode": "FMN", "rr_tag": "Data"}))
        self.assertFalse(d({"rr_mode": "FMN", "rr_tag": "Fire Dispatch"}))
        self.assertFalse(d({"rr_mode": "DMR", "rr_tag": "Business"}))
        self.assertFalse(d({}))

    def test_voice_is_tuned_and_data_is_not(self):
        self.hit(460.6, rr_mode="Telm", rr_tag="Data", listen="fm", rr_agency="Example County")
        self.assertEqual(self.tuned, [])
        self.assertEqual(len(self.b.LISTEN["recent"]), 0)
        self.hit(460.125, rr_mode="FMN", rr_tag="Fire Dispatch", listen="fm", rr_agency="Example County")
        self.assertEqual(self.tuned, [(460.125, "fm")])

    def test_a_client_that_reads_pages_still_gets_them(self):
        self.b.LISTEN["pages"] = True
        self.b.pager_band_ok = lambda mhz: True
        self.hit(462.75, rr_mode="Telm", rr_tag="Data", listen="fm", rr_agency="Example Paging")
        self.assertEqual(self.tuned, [(462.75, "fm")])
        self.tuned.clear()
        self.b.LISTEN["recent"].clear()
        self.b.pager_band_ok = lambda mhz: False
        self.hit(462.75, rr_mode="Telm", rr_tag="Data", listen="fm", rr_agency="Example Paging")
        self.assertEqual(self.tuned, [])


if __name__ == "__main__":
    unittest.main()
