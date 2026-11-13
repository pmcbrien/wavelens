"""The owner's frequency list (freqdb.py), the admin page's Frequencies tab, and the bridge reading it.
Invented channels only. Standard library only:
    python3 -m unittest companion/pi/tests/test_frequencies.py
"""
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import freqdb  # noqa: E402

CHIRP = ("Location,Name,Frequency,Duplex,Offset,Tone,rToneFreq,cToneFreq,DtcsCode,DtcsPolarity,Mode,TStep,Skip,Comment\r\n"
         "0,EXRPT,146.940000,-,0.600000,Tone,127.3,88.5,023,NN,FM,5.00,,Example repeater\r\n"
         "1,SIMPLX,446.000000,,0.000000,,88.5,88.5,023,NN,NFM,5.00,S,\r\n"
         "2,DCSCH,462.562500,,0.000000,DTCS,88.5,88.5,031,NR,NFM,5.00,,\r\n"
         "3,,0.000000,,0.000000,,88.5,88.5,023,NN,FM,5.00,,\r\n")
GQRX = ("# Tag name          ;  color\nExample tag         ; #c0c0c0\n\n"
        "# Frequency ; Name                     ; Modulation          ;  Bandwidth; Tags\n"
        "   162550000; Example weather          ; Narrow FM           ;      12500; Example tag,Second\n"
        "   118300000; Example tower            ; AM                  ;       8000; Untagged\n")
SDRPP = {"bookmarkDisplayMode": 1, "selectedList": "General", "lists": {"General": {"showOnWaterfall": True, "bookmarks": {
    "Example net": {"bandwidth": 12500.0, "frequency": 145500000.0, "mode": 0},
    "Example sideband": {"bandwidth": 2800.0, "frequency": 14250000.0, "mode": 4}}}}}
RR = ('"Frequency Output","Frequency Input","FCC Callsign",Agency/Category,Description,"Alpha Tag","PL Output Tone","PL Input Tone",Mode,"Class Station Code",Tag\n'
      '151.040000,159.19500,WQZZ009,"Example County","Road Commission","Example Roads","110.9 PL",,FMN,RM,"Public Works"\n'
      '155.475000,,WQZZ010,"Example County","Sheriff Dispatch","EXSO Disp","293 NAC",,P25,BM,"Law Dispatch"\n')
PLAIN = ("Name,Frequency,Tone,Group,Notes\n"
         "Example marina,156.8,,Marine,\n"
         "Example yard,\"464,5000\",D023,Work,=HYPERLINK(\"http://example.com\")\n"
         "Broken row,not a number,,,\n"
         "Example again,156.800,,Marine,same channel twice\n")


def chan(name="Example channel", freq="155.475", **kw):
    return freqdb.clean_channel(dict({"name": name, "freq_hz": freq}, **kw))


class Typing(unittest.TestCase):
    def test_frequencies_as_people_write_them(self):
        for typed, hz in (("155.475", 155475000), ("155,475 MHz", 155475000), ("462562.5 kHz", 462562500),
                          ("155475000", 155475000), ("1.2 GHz", 1200000000), (" 27.185 ", 27185000), (".5", 500000)):
            self.assertEqual(freqdb.parse_hz(typed), hz, typed)
        for bad in ("", "abc", "0", "99999 MHz", "1e3", "-146.52", None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                freqdb.parse_hz(bad)

    def test_hertz_survive_exactly(self):
        self.assertEqual(freqdb.mhz(462562500), "462.56250")
        self.assertEqual(freqdb.mhz(162400001), "162.400001")
        self.assertEqual(freqdb.parse_hz(freqdb.mhz(851012500)), 851012500)

    def test_itu_emission_designators(self):
        self.assertEqual(freqdb.emission_bandwidth("11K2F3E"), 11200)
        self.assertEqual(freqdb.emission_bandwidth("8K10F1E"), 8100)
        self.assertEqual(freqdb.emission_bandwidth("2M50G7W"), 2500000)
        self.assertEqual(chan(emission="11k2f3e")["bandwidth_hz"], 11200)
        self.assertEqual(chan(emission="11K2F3E", bandwidth_hz="12500")["bandwidth_hz"], 12500)   # a given width wins
        for bad in ("11.2F3E", "0K20F3E", "11K2Z3E", "FM"):
            with self.assertRaises(ValueError, msg=bad):
                chan(emission=bad)

    def test_tones_and_codes(self):
        for kind, typed, stored in (("ctcss", "127.3 Hz", "127.3"), ("ctcss", "100", "100.0"), ("dcs", "23", "023"),
                                    ("dcs", "D031I", "031I"), ("nac", "$293", "293"), ("nac", "f7e", "F7E"),
                                    ("cc", "01", "1"), ("ran", "63", "63")):
            self.assertEqual(freqdb.clean_tone(kind, typed), stored, (kind, typed))
        for kind, bad in (("ctcss", "12"), ("ctcss", "x"), ("dcs", "089"), ("nac", "1000"), ("cc", "16"), ("ran", "64"), ("ctcss", "")):
            with self.assertRaises(ValueError, msg=(kind, bad)):
                freqdb.clean_tone(kind, bad)

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(ValueError) as e:
            freqdb.clean_channel({"name": "", "freq_hz": "nope", "mode": "telepathy", "lat": "91", "lon": ""})
        for word in ("name", "frequency", "mode", "latitude"):
            self.assertIn(word, str(e.exception))

    def test_text_is_one_clean_line(self):
        c = chan(name="  Example\r\nname\x00 ", notes="x" * 900, tags="a, A, b,,c")
        self.assertEqual(c["name"], "Example name")
        self.assertEqual(len(c["notes"]), 500)
        self.assertEqual(c["tags"], ["a", "b", "c"])
        self.assertRegex(c["id"], r"^[0-9a-f]{12}$")
        self.assertRegex(c["updated"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_simplex_written_as_a_pair_has_no_input(self):
        self.assertIsNone(chan(input_hz="155.475")["input_hz"])


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "etc", "frequencies.json")
        self.store = freqdb.Store(self.path, os.path.join(self.tmp.name, "backups"))

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, *channels):
        self.store.edit(lambda doc: doc["channels"].extend(channels))


class StoreTest(Tmp):
    def test_no_file_is_an_empty_list(self):
        self.assertEqual(self.store.load()["channels"], [])

    def test_save_is_sorted_versioned_and_leaves_nothing_behind(self):
        self.add(chan("B", "460.1"), chan("A", "151.1"))
        doc = self.store.load()
        self.assertEqual([c["name"] for c in doc["channels"]], ["A", "B"])
        self.assertEqual((doc["format"], doc["revision"]), (freqdb.FORMAT, 1))
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path))), ["frequencies.json", "frequencies.json.lock"])

    def test_every_change_keeps_what_it_replaced_and_can_be_put_back(self):
        self.add(chan("First", "151.1"))
        self.add(chan("Second", "152.2"))
        self.store.edit(lambda doc: doc.update(channels=[]))
        backs = self.store.backups()
        self.assertEqual([b["channels"] for b in backs], [2, 1])
        self.assertEqual(self.store.restore(backs[0]["name"]), 2)
        self.assertEqual([c["name"] for c in self.store.load()["channels"]], ["First", "Second"])
        self.assertEqual(self.store.backups()[0]["channels"], 0)          # the emptied list is itself kept

    def test_only_the_newest_backups_are_kept(self):
        for i in range(freqdb.KEEP_BACKUPS + 4):
            self.add(chan(f"Ch {i}", f"{150 + i}.5"))
        self.assertEqual(len(self.store.backups()), freqdb.KEEP_BACKUPS)

    def test_a_backup_is_named_from_the_list_never_a_path(self):
        self.add(chan())
        for name in ("../etc/frequencies.json", "/etc/passwd", "", "frequencies-x.json"):
            with self.assertRaises(freqdb.StoreError):
                self.store.backup_path(name)

    def test_a_failed_change_changes_nothing(self):
        self.add(chan("Keep", "151.1"))

        def boom(doc):
            doc["channels"].clear()
            raise ValueError("no")
        with self.assertRaises(ValueError):
            self.store.edit(boom)
        self.assertEqual(len(self.store.load()["channels"]), 1)
        self.assertEqual(self.store.backups(), [])

    def test_a_damaged_file_is_refused_not_overwritten_until_asked(self):
        self.add(chan())
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{half a file")
        with self.assertRaises(freqdb.StoreError):
            self.store.load()
        with self.assertRaises(freqdb.StoreError):
            self.add(chan("Other", "152.2"))
        self.store.edit(lambda doc: doc.update(channels=[]), recover=True)
        self.assertEqual(self.store.load()["channels"], [])

    def test_the_list_has_a_ceiling(self):
        with self.assertRaises(freqdb.StoreError):
            self.store.edit(lambda doc: doc["channels"].extend([{"freq_hz": 1, "name": "x"}] * (freqdb.MAX_CHANNELS + 1)))

    def test_merge_modes(self):
        doc = {"channels": [chan("Mine", "151.1")]}
        theirs = [chan("Theirs", "151.1"), chan("New", "152.2")]
        self.assertEqual(freqdb.merge(doc, theirs, "add"), {"added": 1, "updated": 0, "skipped": 1})
        self.assertEqual(sorted(c["name"] for c in doc["channels"]), ["Mine", "New"])
        mine_id = next(c["id"] for c in doc["channels"] if c["name"] == "Mine")
        self.assertEqual(freqdb.merge(doc, theirs, "update")["updated"], 2)
        self.assertEqual(next(c["id"] for c in doc["channels"] if c["name"] == "Theirs"), mine_id)   # same entry, new content
        self.assertEqual(freqdb.merge(doc, [chan("Only", "153.3")], "replace")["added"], 1)
        self.assertEqual(len(doc["channels"]), 1)

    def test_stored_file_matches_the_published_schema(self):
        self.add(chan(input_hz="150.775", tone_type="ctcss", tone="127.3", lat="27.9", lon="-82.8"))
        doc, props = self.store.load(), freqdb.SCHEMA["properties"]["channels"]["items"]["properties"]
        self.assertEqual(doc["format"], freqdb.SCHEMA["properties"]["format"]["const"])
        for c in doc["channels"]:
            self.assertEqual(set(c) - set(props), set())
            self.assertIn(c["mode"], props["mode"]["enum"])
            self.assertRegex(c["id"], props["id"]["pattern"])


class Formats(unittest.TestCase):
    def read(self, text_or_obj):
        data = json.dumps(text_or_obj).encode() if isinstance(text_or_obj, dict) else text_or_obj.encode()
        return freqdb.parse_import(data)

    def test_chirp(self):
        got = self.read(CHIRP)
        self.assertEqual(got["format"], "chirp")
        by = {c["name"]: c for c in got["channels"]}
        self.assertEqual(sorted(by), ["DCSCH", "EXRPT", "SIMPLX"])                      # the empty memory slot is skipped
        self.assertEqual((by["EXRPT"]["freq_hz"], by["EXRPT"]["input_hz"]), (146940000, 146340000))
        self.assertEqual((by["EXRPT"]["tone_type"], by["EXRPT"]["tone"]), ("ctcss", "127.3"))
        self.assertEqual((by["DCSCH"]["tone_type"], by["DCSCH"]["tone"]), ("dcs", "031I"))
        self.assertTrue(by["SIMPLX"]["lockout"])
        self.assertEqual(by["SIMPLX"]["bandwidth_hz"], 12500)

    def test_gqrx(self):
        got = self.read(GQRX)
        self.assertEqual(got["format"], "gqrx")
        by = {c["name"]: c for c in got["channels"]}
        self.assertEqual((by["Example weather"]["freq_hz"], by["Example weather"]["group"], by["Example weather"]["tags"]),
                         (162550000, "Example tag", ["Second"]))
        self.assertEqual((by["Example tower"]["mode"], by["Example tower"]["group"]), ("am", ""))

    def test_sdrpp_whole_file_and_exported_list(self):
        got = self.read(SDRPP)
        self.assertEqual((got["format"], len(got["channels"])), ("sdrpp", 2))
        self.assertEqual({c["group"] for c in got["channels"]}, {"General"})
        self.assertEqual(next(c for c in got["channels"] if c["name"] == "Example sideband")["mode"], "usb")
        self.assertEqual(len(self.read({"bookmarks": SDRPP["lists"]["General"]["bookmarks"]})["channels"]), 2)

    def test_radioreference_export(self):
        got = self.read(RR)
        self.assertEqual(got["format"], "radioreference")
        by = {c["name"]: c for c in got["channels"]}
        roads = by["Example Roads"]
        self.assertEqual((roads["input_hz"], roads["tone"], roads["service"], roads["group"]),
                         (159195000, "110.9", "Local government", "Example County"))
        self.assertEqual((by["EXSO Disp"]["mode"], by["EXSO Disp"]["tone_type"], by["EXSO Disp"]["service"]),
                         ("p25", "nac", "Law enforcement"))

    def test_plain_csv_one_bad_row_never_loses_the_rest(self):
        got = self.read(PLAIN)
        self.assertEqual(got["format"], "csv")
        self.assertEqual(sorted(c["name"] for c in got["channels"]), ["Example marina", "Example yard"])
        self.assertEqual(got["repeats"], 1)
        self.assertEqual(len(got["problems"]), 1)
        self.assertIn("row 3 (Broken row)", got["problems"][0])
        yard = next(c for c in got["channels"] if c["name"] == "Example yard")
        self.assertEqual((yard["freq_hz"], yard["tone_type"], yard["tone"]), (464500000, "dcs", "023"))

    def test_files_that_are_not_frequency_lists_are_refused(self):
        for junk in (b"", b"hello", b"\x89PNG\r\n\x1a\n" + bytes(200), b'{"something": "else"}', b"{not json", b"a,b,c\n1,2,3\n"):
            with self.assertRaises(ValueError, msg=junk[:12]):
                freqdb.parse_import(junk)

    def test_every_export_reads_back(self):
        chans = [chan("Example pair", "146.94", input_hz="146.34", tone_type="ctcss", tone="127.3", mode="fm", group="Ham",
                      lockout=True, notes="a; note, with \"marks\""),
                 chan("Example digital", "460.0125", mode="dmr", tone_type="cc", tone="1", emission="7K60FXE", lat="27.9", lon="-82.8"),
                 chan("Example pair", "147.0", mode="am")]
        keeps = {"wavelens": ("input_hz", "tone", "mode", "group", "lockout", "notes", "emission", "lat"),
                 "csv": ("input_hz", "tone", "mode", "group", "lockout", "notes", "emission", "lat"),
                 "chirp": ("input_hz", "lockout", "notes"), "gqrx": ("group",), "sdrpp": ()}
        for fmt in freqdb.EXPORT_FORMATS:
            back = freqdb.parse_import(freqdb.export(chans, fmt).encode())
            self.assertEqual((back["format"], back["problems"]), (fmt, []), fmt)
            self.assertEqual(sorted(c["freq_hz"] for c in back["channels"]), sorted(c["freq_hz"] for c in chans), fmt)
            pair = next(c for c in back["channels"] if c["freq_hz"] == 146940000)
            for field in keeps[fmt]:
                self.assertEqual(pair[field], chans[0][field], (fmt, field))
        self.assertEqual(json.loads(freqdb.export(chans, "wavelens"))["format"], freqdb.FORMAT)
        self.assertEqual(len(json.loads(freqdb.export(chans, "sdrpp"))["bookmarks"]), 3)   # two share a name: both kept

    def test_a_spreadsheet_never_runs_a_cell_from_someone_elses_file(self):
        out = freqdb.export([chan("=1+1", notes="@SUM(A1)", group="+x")], "csv")
        self.assertIn("'=1+1", out)
        self.assertIn("'@SUM(A1)", out)
        self.assertIn("'+x", out)
        self.assertIn("'=1+1", freqdb.export([chan("=1+1")], "chirp"))


class BridgeSide(Tmp):
    def lookup(self):
        look = freqdb.Lookup(self.path)
        look.CHECK_EVERY = 0.0
        return look

    def test_names_output_and_input_and_reads_changes_without_a_restart(self):
        look = self.lookup()
        self.assertIsNone(look.label(146.94))
        self.add(chan("Example repeater", "146.94", input_hz="146.34", mode="fm", tone_type="ctcss", tone="127.3", service="Amateur radio"))
        name, cat, conf, x = look.label(146.9412)
        self.assertEqual((name, cat, conf), ("Example repeater", "UNKNOWN", "POSSIBLE"))
        self.assertEqual((x["listen"], x["lic_mhz"], x["agency"], x["own_tone"]), ("fm", "146.94000", "Amateur radio", "CTCSS 127.3"))
        name, _, conf, x = look.label(146.34)
        self.assertEqual((name, conf, x["lic_mhz"]), ("Example repeater · keying up nearby", "LIKELY", "146.34000"))
        self.assertIsNone(look.label(146.50))

    def test_a_wide_bin_with_several_channels_names_none(self):
        self.add(chan("One", "460.1"), chan("Two", "460.2"))
        look = self.lookup()
        name, _, conf, x = look.label(460.15, tol_khz=500)
        self.assertIn("2 of your channels", name)
        self.assertNotIn("lic_mhz", x)
        self.assertEqual(look.label(460.1)[0], "One")

    def test_lockout_is_per_channel_and_exact(self):
        self.add(chan("Pager", "152.84", lockout=True), chan("Fine", "152.90"))
        look = self.lookup()
        self.assertTrue(look.locked(152.842))
        self.assertFalse(look.locked(152.86))
        self.assertFalse(look.locked(152.90))

    def test_a_mode_the_bridge_cannot_tune_is_heard_as_auto(self):
        self.add(chan("Sideband", "28.4", mode="usb"))
        self.assertEqual(self.lookup().label(28.4)[3]["listen"], "auto")

    def test_a_damaged_list_is_an_empty_one_to_the_bridge(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[]")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(self.lookup().label(146.94))

    def test_the_bridge_asks_the_owners_list_first_and_follow_skips_lockouts(self):
        argv, sys.argv = sys.argv, ["wavelens_bridge"]
        try:
            spec = importlib.util.spec_from_file_location("wavelens_bridge_for_freq_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
            b = importlib.util.module_from_spec(spec)
            with contextlib.redirect_stderr(io.StringIO()):
                spec.loader.exec_module(b)
        finally:
            sys.argv = argv
        self.add(chan("Example dispatch", "155.475", mode="p25", lockout=True))
        b.OWN = self.lookup()
        name, _, _, x = b.licensed_label(155.475, 12.5)
        self.assertEqual((name, x["listen"]), ("Example dispatch", "p25"))
        self.assertTrue(b.locked_out(155.475))
        self.assertFalse(b.locked_out(155.60))
        b.OWN = None                                         # an install without the module behaves as before
        self.assertFalse(b.locked_out(155.475))
        self.assertIsNone(b.licensed_label(155.475, 12.5))


class AdminPages(Tmp):
    """The Frequencies tab end to end over HTTP, logged in, against a list in a temporary folder."""

    @classmethod
    def setUpClass(cls):
        argv, sys.argv = sys.argv, ["wavelens_admin"]
        try:
            spec = importlib.util.spec_from_file_location("wavelens_admin_for_freq_test", os.path.join(HERE, "..", "wavelens_admin.py"))
            cls.admin = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.admin)
        finally:
            sys.argv = argv

    def setUp(self):
        super().setUp()
        self.saved = (freqdb.STORE, freqdb.BACKUP_DIR, self.admin.ADMIN_JSON)
        freqdb.STORE, freqdb.BACKUP_DIR = self.path, self.store.backup_dir
        self.admin.ADMIN_JSON = os.path.join(self.tmp.name, "admin.json")
        with open(self.admin.ADMIN_JSON, "w", encoding="utf-8") as f:
            f.write("{}")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.admin.Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tok = self.admin.SESSIONS.new()
        self.csrf = self.admin.SESSIONS.get(self.tok)["csrf"]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.admin.SESSIONS.drop(self.tok)
        freqdb.STORE, freqdb.BACKUP_DIR, self.admin.ADMIN_JSON = self.saved
        super().tearDown()

    def ask(self, method, path, body=None, headers=None, cookie=True):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = f"wl_session={self.tok}"
        with contextlib.redirect_stderr(io.StringIO()):
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data.decode("utf-8", "replace")

    def post(self, csrf=None, **fields):
        fields["csrf"] = self.csrf if csrf is None else csrf
        return self.ask("POST", "/frequencies", urllib.parse.urlencode(fields), {"Content-Type": "application/x-www-form-urlencoded"})

    def follow(self, resp):
        self.assertEqual(resp[0], 303, resp[2][:300])
        return self.ask("GET", resp[1]["Location"])

    def channels(self):
        return self.store.load()["channels"]

    def test_needs_a_login_and_a_form_token(self):
        self.assertIn("Log in", self.ask("GET", "/frequencies", cookie=False)[2])
        self.assertEqual(self.ask("GET", "/frequencies/export?fmt=csv", cookie=False)[1].get("Content-Disposition"), None)
        self.assertEqual(self.post(csrf="wrong", do="save", name="X", freq="155.475")[0], 403)
        self.assertEqual(self.channels(), [])

    def test_add_edit_and_delete_a_channel(self):
        status, _, page = self.follow(self.post(do="save", name="Example <b>fire</b>", freq="154,280", mode="fm",
                                                tone_type="ctcss", tone="127.3", group="County fire", bandwidth="12.5"))
        self.assertEqual(status, 200)
        self.assertIn("Saved Example &lt;b&gt;fire&lt;/b&gt; on 154.28000 MHz", page)
        self.assertNotIn("<b>fire</b>", page)                                   # names are shown, never run
        (c,) = self.channels()
        self.assertEqual((c["freq_hz"], c["bandwidth_hz"], c["tone"]), (154280000, 12500, "127.3"))
        edit = self.ask("GET", f"/frequencies/edit?id={c['id']}")[2]
        self.assertIn('value="154.28000"', edit)
        stamp = re.search(r'name="stamp" value="([^"]*)"', edit).group(1)
        self.follow(self.post(do="save", id=c["id"], stamp=stamp, name="Example fire", freq="154.28", mode="fm", lockout="1"))
        (c2,) = self.channels()
        self.assertEqual((c2["id"], c2["name"], c2["lockout"], c2["created"]), (c["id"], "Example fire", True, c["created"]))
        self.assertIn("Tick the box", self.follow(self.post(do="delete", id=c["id"]))[2])
        self.assertEqual(len(self.channels()), 1)
        self.follow(self.post(do="delete", id=c["id"], sure="1"))
        self.assertEqual(self.channels(), [])

    def test_a_bad_form_is_not_saved_and_keeps_what_was_typed(self):
        status, _, page = self.post(do="save", name="Example typo", freq="15x.475", tone_type="dcs", tone="999")
        self.assertEqual(status, 200)
        self.assertIn("Not saved.", page)
        self.assertIn("frequency", page)
        self.assertIn("DCS code", page)
        self.assertIn('value="Example typo"', page)
        self.assertEqual(self.channels(), [])

    def test_a_repeat_and_a_stale_edit_are_caught(self):
        self.follow(self.post(do="save", name="First", freq="155.475"))
        self.assertIn("already in your list as “First”", self.post(do="save", name="Again", freq="155.4750")[2])
        (c,) = self.channels()
        self.assertIn("changed in another window", self.post(do="save", id=c["id"], stamp="2001-01-01T00:00:00Z", name="Stale", freq="155.475")[2])
        self.assertEqual(self.channels()[0]["name"], "First")

    def test_bulk_actions(self):
        self.add(chan("A", "151.1"), chan("B", "152.2"), chan("C", "153.3"))
        a, b, c = self.channels()
        self.follow(self.post(**{"do": "bulk", "act": "lock", "id_" + a["id"]: "1", "id_" + b["id"]: "1"}))
        self.assertEqual([x["lockout"] for x in self.channels()], [True, True, False])
        self.follow(self.post(**{"do": "bulk", "act": "group", "to_group": "Work", "id_" + c["id"]: "1"}))
        self.assertEqual(self.channels()[2]["group"], "Work")
        self.assertIn("Nothing deleted", self.follow(self.post(**{"do": "bulk", "act": "delete", "id_" + a["id"]: "1"}))[2])
        self.follow(self.post(**{"do": "bulk", "act": "delete", "sure": "1", "id_" + a["id"]: "1"}))
        self.assertEqual([x["name"] for x in self.channels()], ["B", "C"])

    def upload(self, text_):
        boundary = "----wavelenstest"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{self.csrf}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"do\"\r\n\r\nimport_upload\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"..\\\\..\\\\list.csv\"\r\n"
                f"Content-Type: text/csv\r\n\r\n{text_}\r\n--{boundary}--\r\n").encode()
        return self.ask("POST", "/frequencies", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def test_import_shows_what_it_read_before_changing_anything(self):
        self.add(chan("Mine", "146.94", tone_type="ctcss", tone="127.3"))
        _, _, page = self.follow(self.upload(CHIRP))
        self.assertIn("read as CHIRP CSV", page)
        self.assertIn("list.csv", page)
        self.assertNotIn("..", re.search(r"<th>File</th><td>([^<]*)", page).group(1))
        self.assertEqual(len(self.channels()), 1)                               # nothing yet
        _, _, page = self.follow(self.post(do="import_apply", mode="add", to_group="Imported"))
        self.assertIn("2 added, 0 overwritten, 1 already in your list", page)
        self.assertEqual(sorted(c["name"] for c in self.channels()), ["DCSCH", "Mine", "SIMPLX"])
        self.assertIn("expired", self.follow(self.post(do="import_apply", mode="add"))[2])   # one use

    def test_a_file_that_is_not_a_list_is_refused_plainly(self):
        self.assertIn("Not imported", self.follow(self.upload("just some words"))[2])

    def test_export_downloads_and_filters_by_group(self):
        self.add(chan("A", "151.1", group="One"), chan("B", "152.2", group="Two"))
        status, headers, text_ = self.ask("GET", "/frequencies/export?fmt=chirp")
        self.assertEqual(status, 200)
        self.assertIn('filename="wavelens-chirp.csv"', headers["Content-Disposition"])
        self.assertEqual(len(freqdb.parse_import(text_.encode())["channels"]), 2)
        one = self.ask("GET", "/frequencies/export?fmt=wavelens&group=One")[2]
        self.assertEqual([c["name"] for c in json.loads(one)["channels"]], ["A"])
        self.assertEqual(self.ask("GET", "/frequencies/export?fmt=exe")[0], 404)
        self.assertEqual(json.loads(self.ask("GET", "/frequencies/schema")[2])["title"], "Wavelens frequency list")

    def test_backups_page_puts_a_list_back_and_empties_on_request(self):
        self.add(chan("A", "151.1"))
        self.add(chan("B", "152.2"))
        name = self.store.backups()[0]["name"]
        self.assertIn("Put back", self.ask("GET", "/frequencies/backups")[2])
        self.assertEqual(len(json.loads(self.ask("GET", "/frequencies/backup?name=" + name)[2])["channels"]), 1)
        self.assertEqual(self.ask("GET", "/frequencies/backup?name=../admin.json")[0], 404)
        self.follow(self.post(do="restore", name=name))
        self.assertEqual([c["name"] for c in self.channels()], ["A"])
        self.assertIn("Not done", self.follow(self.post(do="restore", name="../../etc/passwd"))[2])
        self.assertIn("Nothing removed", self.follow(self.post(do="clear"))[2])
        self.follow(self.post(do="clear", sure="1"))
        self.assertEqual(self.channels(), [])

    def test_a_damaged_list_says_so_and_points_at_backups(self):
        self.add(chan())
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{half")
        page = self.ask("GET", "/frequencies")[2]
        self.assertIn("could not be read", page)
        self.assertIn("/frequencies/backups", page)
        self.assertEqual(self.ask("GET", "/frequencies/backups")[0], 200)

    def test_search_filter_and_paging(self):
        self.add(*[chan(f"Example {i:03d}", f"{150 + i * 0.0125:.4f}", group="Even" if i % 2 == 0 else "Odd") for i in range(130)])
        page = self.ask("GET", "/frequencies")[2]
        self.assertIn("page 1 of 2", page)
        self.assertEqual(page.count('name="id_'), 100)
        self.assertEqual(self.ask("GET", "/frequencies?page=2")[2].count('name="id_'), 30)
        self.assertEqual(self.ask("GET", "/frequencies?q=example+007")[2].count('name="id_'), 1)
        self.assertEqual(self.ask("GET", "/frequencies?group=Odd&page=9")[2].count('name="id_'), 65)
        self.assertIn("Nothing matches", self.ask("GET", "/frequencies?q=%3Cscript%3E")[2])


if __name__ == "__main__":
    unittest.main()
