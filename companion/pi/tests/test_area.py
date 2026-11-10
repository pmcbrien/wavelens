"""The area setting (area.py) against a tiny published "area" release built from synthetic data: a postcode
becomes a position and its districts, the licence pack is cut down to them, a country with no licence data
still gets an area, and the bridge finds what to load in the area folder. Nothing here is a real record.
Standard library only:
    python3 -m unittest companion/pi/tests/test_area.py
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
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools"))
import area  # noqa: E402
import area_packs  # noqa: E402
import fcc_local_pack  # noqa: E402

RR_COUNTY = '"Frequency Output","Frequency Input","FCC Callsign",Agency/Category,Description,"Alpha Tag","PL Output Tone","PL Input Tone",Mode,"Class Station Code",Tag\n151.040000,159.19500,WQZZ009,"Example County","Road Commission","Example Roads",,,FM,RM,"Public Works"\n'
RR_STATE = RR_COUNTY.replace("Agency/Category,Description", "Agency/Category,County,Description").replace('"Example County","Road', '"Example County",Example,"Road')
RR_SITES = 'System,SID,Counties,RFSS,Site Dec,Site Hex,Site NAC,Description,County Name,Lat,Lon,Range,Frequencies\n"State of Example (P25)",1,"Statewide",1,001,1,123,"Exampleville","Example",27.9,-82.8,20,851.012500,852.012500c\n'
RR_TGS = 'System,Decimal,Hex,Alpha Tag,Mode,Description,Tag,Category\n"State of Example (P25)",101,065,"EX DISP","D","Example Dispatch","Law Dispatch","Example County"\n'


def put(path, text):
    with open(path, "wb") as f:
        f.write(text if isinstance(text, bytes) else text.encode("utf-8"))


class AreaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        gn = os.path.join(d, "geo.zip")
        with zipfile.ZipFile(gn, "w") as zf:
            zf.writestr("allCountries.txt", area_packs.SYNTH_GEONAMES)
        uls = os.path.join(d, "uls.zip")
        with open(uls, "wb") as f:
            f.write(fcc_local_pack._synthetic_zip())
        cls.release = os.path.join(d, "release")
        with contextlib.redirect_stderr(io.StringIO()):
            cls.index = area_packs.build_all([uls], gn, cls.release, group_size=2, states=("FL", "TX"))
        cls.base = "file://" + cls.release

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.work.name, "area")
        self.file = os.path.join(self.work.name, "etc", "area.json")
        self._saved = (area.BASE_URL, area.AREA_DIR, area.AREA_FILE)
        area.BASE_URL, area.AREA_DIR, area.AREA_FILE = self.base, self.dir, self.file

    def tearDown(self):
        area.BASE_URL, area.AREA_DIR, area.AREA_FILE = self._saved
        self.work.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = area.main(list(argv))
        return rc, out.getvalue()

    def pack(self):
        with open(os.path.join(self.dir, "fcc-local.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_postcode_becomes_position_and_districts(self):
        a = area.resolve("us", "00001", radius_km=50, base=self.base)
        self.assertEqual((a["country"], a["postcode"], a["region"], a["district"]), ("US", "00001", "FL", "Example"))
        self.assertEqual((a["lat"], a["lon"]), (27.91, -82.81))
        self.assertEqual(a["regions"], {"FL": ["Example", "Sample"]})      # the Texas postcode is out of range

    def test_set_writes_area_and_a_cut_down_licence_pack(self):
        rc, out = self.run_cli("set", "US", "00001", "--no-restart")
        self.assertEqual(rc, 0, out)
        stored = area.read_area()
        self.assertEqual((stored["postcode"], stored["licences"], stored["data_built"]), ("00001", 3, self.index["built"]))
        p = self.pack()
        self.assertEqual(p["format"], "wavelens-fcc-local/1")
        self.assertEqual(sorted({e["call"] for e in p["entries"]}), ["WQZZ001", "WQZZ002"])
        self.assertTrue(all("areas" not in e for e in p["entries"]))
        self.assertEqual(os.listdir(self.dir).count("fcc-local.json"), 1)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".")], [])   # no download left behind

    def test_radius_and_district_decide_what_is_kept(self):
        # from the next district over, 5 km takes neither that licensee's county nor its site 24 km away
        self.assertEqual(self.run_cli("set", "US", "00002", "--radius-km", "5", "--no-restart")[0], 0)
        self.assertEqual(self.pack()["entries"], [])
        # 40 km reaches the district, so the mobile-only licence (no site) comes with it
        self.assertEqual(self.run_cli("set", "00002", "--radius-km", "40", "--no-restart")[0], 0)   # country kept
        self.assertEqual(len(self.pack()["entries"]), 3)

    def test_site_in_range_counts_without_a_district_match(self):
        entries = [{"call": "A", "mhz": 1.0, "areas": ["FL|ELSEWHERE"], "sites": [[27.9, -82.8]]},
                   {"call": "B", "mhz": 2.0, "areas": ["FL|ELSEWHERE"], "sites": [[31.0, -100.0]]},
                   {"call": "C", "mhz": 3.0, "areas": ["FL|ST. EXAMPLE"], "sites": []}]
        got = area.filter_entries(entries, "FL", ["Saint Example"], 27.91, -82.81, 20)
        self.assertEqual([e["call"] for e in got], ["A", "C"])

    def test_country_without_licence_data(self):
        self.assertEqual(self.run_cli("set", "US", "00001", "--no-restart")[0], 0)
        rc, out = self.run_cli("set", "xx", "ab1 2cd", "--no-restart")      # only the leading part is in the table
        self.assertEqual(rc, 0, out)
        stored = area.read_area()
        self.assertEqual((stored["country"], stored["postcode"], stored["region"], stored["licences"]), ("XX", "AB12", "Testshire", 0))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "fcc-local.json")))   # Florida's pack is gone
        self.assertIn("no public licence data", out)

    def test_position_without_a_postcode_table(self):
        rc, out = self.run_cli("set", "--country", "ZZ", "--lat", "10.5", "--lon", "20.25", "--no-restart")
        self.assertEqual(rc, 0, out)
        self.assertEqual((area.read_area()["lat"], area.read_area()["lon"], area.read_area()["regions"]), (10.5, 20.25, {}))
        rc, out = self.run_cli("set", "DE", "--lat", "52.5", "--lon", "13.4", "--no-restart")
        self.assertEqual((rc, area.read_area()["country"]), (0, "DE"))

    def test_mistakes_are_one_line_errors(self):
        for argv in (["set", "US", "99999"], ["set", "USA", "00001"], ["set", "ZZ", "12345"], ["set", "US"],
                     ["set", "US", "00001", "--radius-km", "5000"]):
            rc, out = self.run_cli(*argv, "--no-restart")
            self.assertEqual(rc, 1, argv)
            self.assertTrue(out.strip().splitlines()[-1].startswith("error: "), out)
        self.assertEqual(area.read_area(), {})

    def test_unreachable_release_leaves_the_old_area(self):
        self.assertEqual(self.run_cli("set", "US", "00001", "--no-restart")[0], 0)
        area.BASE_URL = "file://" + os.path.join(self.work.name, "nowhere")
        rc, out = self.run_cli("set", "US", "00002", "--no-restart")
        self.assertEqual(rc, 1)
        self.assertEqual(area.read_area()["postcode"], "00001")
        self.assertEqual(len(self.pack()["entries"]), 3)

    def test_refresh_only_when_newer(self):
        self.assertEqual(self.run_cli("refresh", "--no-restart")[1].strip(), "no area set: nothing to refresh")
        self.run_cli("set", "US", "00001", "--no-restart")
        self.assertIn("is current", self.run_cli("refresh", "--no-restart")[1])
        os.unlink(os.path.join(self.dir, "fcc-local.json"))
        self.assertEqual(self.run_cli("refresh", "--force", "--no-restart")[0], 0)
        self.assertEqual(len(self.pack()["entries"]), 3)

    def test_clear(self):
        self.run_cli("set", "US", "00001", "--no-restart")
        os.makedirs(os.path.join(self.dir, "rr"), exist_ok=True)
        put(os.path.join(self.dir, "rr", "county.csv"), RR_COUNTY)
        self.assertEqual(self.run_cli("clear", "--no-restart")[0], 0)
        self.assertEqual(area.read_area(), {})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "fcc-local.json")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "rr", "county.csv")))    # the owner's files stay

    def test_bridge_inputs(self):
        self.assertEqual(area.inputs_for_bridge(), {"fcc": "", "rr": [], "counties": [], "area": {}})   # nothing set
        self.run_cli("set", "US", "00002", "--radius-km", "40", "--no-restart")
        rr = os.path.join(self.dir, "rr")
        for name, text in (("z-nation.csv", RR_COUNTY.replace('"Frequency Output"', '"Nationwide Agency","Frequency Output"')),
                           ("state.csv", RR_STATE), ("county.csv", RR_COUNTY), ("tgs.csv", RR_TGS), ("sites.csv", RR_SITES),
                           ("notes.csv", "a,b\n1,2\n")):
            put(os.path.join(rr, name), text)
        put(os.path.join(rr, "binary.csv"), b"\xff\xfe\x00junk")
        got = area.inputs_for_bridge()
        self.assertEqual(got["fcc"], os.path.join(self.dir, "fcc-local.json"))
        self.assertEqual([os.path.basename(p) for p in got["rr"]], ["sites.csv", "tgs.csv", "county.csv", "state.csv", "z-nation.csv"])
        self.assertEqual(got["counties"], ["Sample", "Example"])            # the home district first

    def test_bridge_loads_what_the_area_tool_wrote(self):
        self.run_cli("set", "US", "00001", "--no-restart")
        rr = os.path.join(self.dir, "rr")
        for name, text in (("county.csv", RR_COUNTY), ("tgs.csv", RR_TGS), ("sites.csv", RR_SITES)):
            put(os.path.join(rr, name), text)
        argv, sys.argv = sys.argv, ["bridge"]
        try:
            spec = importlib.util.spec_from_file_location("wavelens_bridge_for_area_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
        finally:
            sys.argv = argv
        got = area.inputs_for_bridge()
        fcc = bridge.FccPack(got["fcc"])
        self.assertEqual(len(fcc.entries), 3)
        rrp = bridge.RrPack(got["rr"], got["counties"])
        self.assertEqual((len(rrp.sites), len(rrp.talkgroups)), (1, 1))
        self.assertTrue(rrp.rows)


if __name__ == "__main__":
    unittest.main()
