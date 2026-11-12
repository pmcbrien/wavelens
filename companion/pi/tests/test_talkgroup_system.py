"""Talkgroup names and the POLICE / FIRE / EMS / OTHER filter follow the system being followed.

Talkgroup numbers are only unique inside one system, so the bridge identifies the system first: by its
RadioReference SID, then by name (which may carry a site in brackets, or be abbreviated). A name is never
borrowed from another system that happens to use the same number. Invented systems; no radio is touched.
    python3 -m unittest companion/pi/tests/test_talkgroup_system.py
"""
from __future__ import annotations

import csv
import importlib.util
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

HEAD = "System,SID,Counties,Decimal,Hex,Alpha Tag,Mode,Description,Tag,Category\n"
ROWS = (
    '"Big State System (EDACS)",11,"A",1538,602,"Other SO","D","Other","Law Dispatch","x"\n'
    '"Home County",22,"A",1538,602,"Home SO 2","D","Patrol 2","Law Dispatch","x"\n'
    '"Home County",22,"A",1536,600,"Home SO 1","D","Patrol 1","Law Dispatch","x"\n'
    '"Home County",22,"A",536,218,"Home FD","D","Fire Dispatch","Fire Dispatch","x"\n'
    '"Home County",22,"A",1040,410,"SE-1","D","Special Events 1","Interop","x"\n'
    '"Home County",22,"A",1600,640,"Home Tac","DE","Tac","Law Tac","x"\n'
    '"Air Base",33,"A",1700,6a4,"Flight Ops","D","Ops","Military","x"\n'
    '"Metro",55,"A",9,9,"Metro plain","D","d","Law Dispatch","x"\n'
    '"Metro (P25)",56,"A",9,9,"Metro P25","D","d","Law Dispatch","x"\n'
)


def load_bridge():
    argv, sys.argv = sys.argv, ["bridge"]
    try:
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_tg_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


class TalkgroupSystem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load_bridge()

    def setUp(self):
        b = self.b
        self.saved = (b.RR, dict(b.TRUNK), dict(b.LISTEN))
        p = b.RrPack.__new__(b.RrPack)
        p.talkgroups, p.tg_sids, p.tg_systems = {}, set(), set()
        p.controls, p.sites, p.rows, p.keys = [], [], [], []
        p._load_talkgroups(csv.DictReader(io.StringIO(HEAD + ROWS)))
        self.p = b.RR = p
        b.TRUNK.update(cats={"police"}, unlisted="mute", interop="other", tg_allow=set(), system="", sid_at=None)

    def tearDown(self):
        b = self.b
        b.RR = self.saved[0]
        b.TRUNK.clear(); b.TRUNK.update(self.saved[1])
        b.LISTEN.clear(); b.LISTEN.update(self.saved[2])

    def follow(self, name: str, sid: str = ""):
        """As if the bridge had locked onto this system (the SID comes from its site row)."""
        b = self.b
        b.LISTEN["mhz"] = 851.0125
        b.TRUNK["system"] = name
        b.TRUNK["sid_at"] = (851.0125, name, sid)

    def alpha(self, dec, system="", sid=""):
        hit = self.p.talkgroup(dec, system, sid)
        return hit[0] if hit else None

    def test_sid_wins_whatever_the_name_says(self):
        self.assertEqual(self.alpha(1538, "anything at all", "22"), "Home SO 2")

    def test_name_with_a_site_in_brackets(self):
        self.assertEqual(self.alpha(1538, "Home County (Countywide Simulcast)"), "Home SO 2")
        self.assertEqual(self.alpha(1538, "Home County"), "Home SO 2")

    def test_most_specific_name_wins(self):
        self.assertEqual(self.alpha(9, "Metro (P25) (Site 1)"), "Metro P25")
        self.assertEqual(self.alpha(9, "Metro (Site 1)"), "Metro plain")

    def test_never_borrowed_from_another_system(self):
        self.assertIsNone(self.alpha(1700, "Home County (X)", "22"))
        self.assertIsNone(self.alpha(1700, "Home County (X)"))

    def test_a_system_the_export_does_not_cover(self):
        self.assertEqual(self.alpha(1700, "unlisted P25 system (NAC 123)"), "Flight Ops")   # unique number
        self.assertIsNone(self.alpha(1538, "unlisted P25 system (NAC 123)"))               # shared number

    def test_filter_on_a_covered_system(self):
        b = self.b
        self.follow("Home County (Countywide Simulcast)", "22")
        verdict = lambda tg: b.tg_muted_by_category(tg, b.tg_category(tg))
        self.assertFalse(verdict(1538))                  # police: heard
        self.assertTrue(verdict(536))                    # fire: muted
        self.assertTrue(verdict(1040))                   # interop counts as other
        self.assertTrue(verdict(99999))                  # unlisted on a covered system
        b.TRUNK["unlisted"], b.TRUNK["interop"] = "play", "police"
        self.assertFalse(verdict(1040))
        self.assertFalse(verdict(99999))
        b.TRUNK["unlisted"], b.TRUNK["interop"] = "mute", "other"
        b.TRUNK["tg_allow"] = {536}
        self.assertFalse(verdict(536))                   # Unmute beats the category
        b.TRUNK["cats"] = set()
        self.assertFalse(verdict(1040))                  # no filter: everything plays

    def test_filter_on_a_system_the_export_does_not_cover(self):
        b = self.b
        self.follow("unlisted P25 system (NAC 123)")
        self.assertFalse(b.tg_muted_by_category(99999, b.tg_category(99999)))

    def test_calls_outside_the_filter_are_not_followed(self):
        b = self.b
        self.follow("Home County (Countywide Simulcast)", "22")
        self.assertEqual(b.trunk_blocked(851.0125), (536, 1040, 1600))  # fire, interop, always encrypted; never another system's 1700
        b.TRUNK["tg_allow"], b.TRUNK["tg_avoid"] = {536}, {1538}
        self.assertEqual(b.trunk_blocked(851.0125), (1538, 1040, 1600))  # Unmute is followed; Ignore is not
        b.TRUNK["cats"] = set()
        self.assertEqual(b.trunk_blocked(851.0125), (1538, 1600))       # no filter: what is ignored, and what cannot be heard
        b.TRUNK.update(cats={"police"}, tg_avoid=set(), tg_lock=1536)
        self.assertEqual(b.trunk_blocked(851.0125), ())               # Hold decides on its own
        b.TRUNK["tg_lock"] = 0
        self.follow("unlisted P25 system (NAC 123)")
        self.assertEqual(b.trunk_blocked(851.0125), ())               # a system the export does not cover
        self.assertTrue(b.GRANT_RE.search("SVC [04] CHAN [0485] Group [1400] [skip] Source [706623]"))
        self.assertTrue(b.GRANT_RE.search("SVC [04] CHAN [0485] Group [1400] Source [706623]"))


if __name__ == "__main__":
    unittest.main()
