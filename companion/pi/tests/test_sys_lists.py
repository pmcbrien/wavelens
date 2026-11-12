"""Favourite and ignored systems: favourites lead the ranking in their power order, an ignored system is
never the rotation's next stop, and a system is kept by its name so all its sites count as one.
Invented systems; no radio is touched.
    python3 -m unittest companion/pi/tests/test_sys_lists.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))


def load_bridge():
    argv, sys.argv = sys.argv, ["bridge"]
    try:
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_sys_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


def row(mhz, lvl, name=""):
    return {"mhz": mhz, "lvl": lvl, "modes": ["trunk"], "failed_at": 0.0, "ok": True, "name": name, "public": True}


class SysLists(unittest.TestCase):
    def setUp(self):
        self.b = load_bridge()
        self.ranked = [row(851.1, 40, "County Simulcast"), row(852.2, 30, "City P25"), row(853.3, 20)]

    def test_key_is_the_name_or_the_frequency(self):
        self.assertEqual(self.b.sys_key(self.ranked[0]), "county simulcast")
        self.assertEqual(self.b.sys_key(self.ranked[2]), "853.3000")

    def test_favourites_lead_in_power_order(self):
        self.b.TRUNK["sys_fav"] = {"853.3000", "city p25"}
        self.assertEqual([r["mhz"] for r in self.b.sys_order(self.ranked)], [852.2, 853.3, 851.1])

    def test_rotation_never_goes_to_an_ignored_system(self):
        b = self.b
        b.TRUNK["sys_avoid"] = {"city p25"}
        b.TRUNK["ranked"], b.TRUNK["ranked_at"], b.TRUNK["hold"] = self.ranked, time.time(), False
        b.HUNT["on"] = False
        b.LISTEN["mhz"], b.LISTEN["sq"] = 851.1, 0
        went = []
        b.trunk_prep = lambda mhz, mode: None
        b.listen_start = lambda mhz, mode, sq, tol=0.05: went.append(mhz)
        b.listen_status = lambda msg: None
        self.assertTrue(b.trunk_next_system(time.time(), force=True))
        self.assertEqual(went, [853.3])


if __name__ == "__main__":
    unittest.main()
