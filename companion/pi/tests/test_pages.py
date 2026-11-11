"""Pager decodes (Lab): the decoder runs only where paging is, and a decode has to look like a page somebody
sent before it is shown or the channel is muted as pager data. Invented messages; nothing here was received.
Standard library only:
    python3 -m unittest companion/pi/tests/test_pages.py
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
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_page_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        with open(os.devnull, "w") as quiet:          # the import says which region it starts in
            err, sys.stderr = sys.stderr, quiet
            try:
                spec.loader.exec_module(mod)
            finally:
                sys.stderr = err
        return mod
    finally:
        sys.argv = argv


class PagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load_bridge()

    def setUp(self):
        self._saved = (dict(self.b.REGION), self.b.PAGER_ANY_BAND)

    def tearDown(self):
        self.b.REGION.clear(); self.b.REGION.update(self._saved[0])
        self.b.PAGER_ANY_BAND = self._saved[1]

    def test_decoder_runs_only_on_paging_bands_in_the_us(self):
        ok = self.b.pager_band_ok
        for mhz in (152.24, 157.74, 454.325, 462.775, 929.6125, 931.9375):
            self.assertTrue(ok(mhz), mhz)
        for mhz in (460.6, 453.2125, 155.475, 851.0125, 146.52, 462.5625):     # public safety, ham, FRS
            self.assertFalse(ok(mhz), mhz)

    def test_pack_bands_replace_the_us_list_and_other_countries_are_not_gated(self):
        b = self.b
        b.REGION.update(code="AU", pack={"companion": {"pager_bands": [[148.0, 149.0]]}})
        self.assertTrue(b.pager_band_ok(148.3375))
        self.assertFalse(b.pager_band_ok(152.24))
        b.REGION.update(code="AU", pack={"companion": {}})
        self.assertTrue(b.pager_band_ok(460.6))            # no list for this country: the plausibility test decides
        b.REGION.update(code="AU", pack={"companion": {"pager_bands": "nonsense"}})
        self.assertTrue(b.pager_band_ok(460.6))
        b.REGION.update(code="US", pack=None)
        b.PAGER_ANY_BAND = True
        self.assertTrue(b.pager_band_ok(460.6))

    def test_kind_comes_from_the_decoder_label(self):
        k = self.b.page_kind
        self.assertEqual(k("Address:  123456  Function: 0  Numeric: 555-0100"), "numeric")
        self.assertEqual(k("Address:  123456  Function: 3  Alpha:   ENGINE 4 RESPOND"), "alpha")
        self.assertEqual(k("[123456] ALN  ENGINE 4 RESPOND"), "alpha")
        self.assertEqual(k("Address:  123456  Function: 1"), "other")

    def test_junk_read_as_numeric_pages_is_dropped(self):
        p = self.b.page_plausible
        for junk in ("7-3.]]1[204-[-915[U3]8802-4417-6620]551907] 18]662U 7741.30086125-0914.2 6.1[7300",
                     "3-0]11920336718254061.7.2-40913355.8102 6648-..3U7712904.3301705510437720]09 2",
                     "U[]--..U", "12", "[4]U9", "1-2-3-4-5-6-7-8-9-0-1-2-3-4-5-6"):
            self.assertFalse(p("POCSAG512", "numeric", junk), junk)

    def test_real_looking_pages_are_kept(self):
        p = self.b.page_plausible
        for number in ("555-0100", "5550100", "555 0100 911", "1234"):
            self.assertTrue(p("POCSAG1200", "numeric", number), number)
        for text in ("ENGINE 4 RESPOND TO 100 EXAMPLE ST FOR ALARM", "Call the desk at ext 4410", "ROOM 212: BED READY", "TEST 123"):
            self.assertTrue(p("POCSAG1200", "alpha", text), text)
        self.assertTrue(p("FLEX", "alpha", "STATION 7 MEDICAL AID"))

    def test_unreadable_alpha_is_dropped(self):
        p = self.b.page_plausible
        for junk in ("}{~^|`\\$%", "a", "x%", "~~q^^^|||}}}{{{zz", "", "   "):
            self.assertFalse(p("POCSAG512", "alpha", junk), repr(junk))
        self.assertFalse(p("FLEX", "alpha", "~ ^"))


if __name__ == "__main__":
    unittest.main()
