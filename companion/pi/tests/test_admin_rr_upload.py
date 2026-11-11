"""Admin page, Area tab: uploading, listing and deleting the owner's RadioReference exports. The files go
through the same checks the bridge's loader applies, a hostile file name cannot leave the folder, and the
whole thing is driven once over HTTP. Invented data only; systemctl is faked. Standard library only:
    python3 -m unittest companion/pi/tests/test_admin_rr_upload.py
"""
from __future__ import annotations

import http.client
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import area  # noqa: E402
import wavelens_admin as admin  # noqa: E402

COUNTY = (b'"Frequency Output","Frequency Input","FCC Callsign",Agency/Category,Description,"Alpha Tag","PL Output Tone","PL Input Tone",Mode,"Class Station Code",Tag\n'
          b'151.040000,159.19500,WQZZ009,"Example County","Road Commission","Example Roads",,,FM,RM,"Public Works"\n')
STATE = COUNTY.replace(b"Agency/Category,", b"Agency/Category,County,").replace(b'"Example County",', b'"Example County",Example,')
NATION = COUNTY.replace(b'"Frequency Output"', b'"Nationwide Agency","Frequency Output"').replace(b"151.040000", b'"Example Agency",151.040000')
SITES = (b'System,SID,Counties,RFSS,Site Dec,Site Hex,Site NAC,Description,County Name,Lat,Lon,Range,Frequencies\n'
         b'"State of Example (P25)",1,"Statewide",1,001,1,123,"Exampleville","Example",27.9,-82.8,20,851.012500,852.012500c\n')
TGS = (b'System,Decimal,Hex,Alpha Tag,Mode,Description,Tag,Category\n'
       b'"State of Example (P25)",101,065,"EX DISP","D","Example Dispatch","Law Dispatch","Example County"\n')
NOT_RR = b"name,phone\nExample Person,555-0100\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (admin.RR_DIR, admin.run)
        admin.RR_DIR = os.path.join(self.tmp.name, "area", "rr")
        self.calls = []

        def fake_run(argv, timeout=20, stdin_text=None):
            self.calls.append(list(argv))
            return 0, ""
        admin.run = fake_run

    def tearDown(self):
        admin.RR_DIR, admin.run = self.saved
        self.tmp.cleanup()

    def stored(self):
        return sorted(os.listdir(admin.RR_DIR)) if os.path.isdir(admin.RR_DIR) else []


class Rules(Base):
    def test_kinds_agree_with_the_bridges_loader(self):
        for data in (COUNTY, STATE, NATION, SITES, TGS, NOT_RR, b"", b"\xff\xfe\x00junk", b"\xef\xbb\xbf" + COUNTY):
            path = os.path.join(self.tmp.name, "probe.csv")
            with open(path, "wb") as f:
                f.write(data)
            self.assertEqual(admin.rr_kind(data), area.rr_kind(path), data[:40])
        self.assertEqual([admin.rr_kind(d) for d in (SITES, TGS, COUNTY, STATE, NATION, NOT_RR)], [0, 0, 1, 2, 3, 9])

    def test_good_files_are_stored_and_listed(self):
        for name, data in (("county.csv", COUNTY), ("trs sites (1).CSV", SITES), ("tg.csv", TGS)):
            ok, msg = admin.rr_save(name, data)
            self.assertTrue(ok, msg)
        self.assertEqual(self.stored(), ["county.csv", "tg.csv", "trs sites (1).CSV"])
        rows = {e["name"]: (e["kind"], e["rows"]) for e in admin.rr_list()}
        self.assertEqual(rows, {"county.csv": (1, 1), "tg.csv": (0, 1), "trs sites (1).CSV": (0, 1)})
        self.assertEqual(os.stat(os.path.join(admin.RR_DIR, "county.csv")).st_mode & 0o777, 0o644)
        with open(os.path.join(admin.RR_DIR, "county.csv"), "rb") as f:
            self.assertEqual(f.read(), COUNTY)

    def test_what_is_stored_is_what_the_bridge_would_load(self):
        admin.rr_save("county.csv", COUNTY)
        admin.rr_save("sites.csv", SITES)
        got = area.inputs_for_bridge(os.path.dirname(admin.RR_DIR), os.path.join(self.tmp.name, "no-area.json"))
        self.assertEqual([os.path.basename(p) for p in got["rr"]], ["sites.csv", "county.csv"])

    def test_refused(self):
        for name, data, why in (("notes.txt", COUNTY, "not a .csv"), ("people.csv", NOT_RR, "not a RadioReference export"),
                                ("empty.csv", b"  \n", "empty"), ("bin.csv", b"\xff\xfe\x00\x01" * 50, "not a text file"),
                                ("", COUNTY, "not a .csv"), (".csv", COUNTY, "not a .csv"),
                                ("big.csv", COUNTY + b"x" * admin.MAX_RR_FILE, "larger than")):
            ok, msg = admin.rr_save(name, data)
            self.assertFalse(ok, name)
            self.assertIn(why, msg)
        self.assertEqual(self.stored(), [])

    def test_a_name_cannot_leave_the_folder(self):
        for name in ("../../etc/cron.d/x.csv", "/etc/passwd.csv", "..\\..\\boot\\cmdline.csv", "a/../../b.csv", "..csv", "x\x00.csv", "né<script>.csv"):
            ok, _ = admin.rr_save(name, COUNTY)
            for n in self.stored():
                self.assertNotIn("/", n)
                self.assertFalse(n.startswith("."))
        outside = [n for n in os.listdir(self.tmp.name) if n != "area"]
        self.assertEqual(outside, [])
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "area")), ["rr"])
        self.assertEqual(admin.rr_safe_name("C:\\Users\\me\\Downloads\\county (2).csv"), "county (2).csv")

    def test_same_name_replaces_and_nothing_half_written_is_left(self):
        admin.rr_save("county.csv", COUNTY)
        ok, msg = admin.rr_save("county.csv", STATE)
        self.assertTrue(ok)
        self.assertIn("replaced", msg)
        self.assertEqual(self.stored(), ["county.csv"])
        self.assertEqual(admin.rr_list()[0]["kind"], 2)

    def test_folder_must_not_be_a_symlink(self):
        os.makedirs(os.path.dirname(admin.RR_DIR))
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.mkdir(elsewhere)
        os.symlink(elsewhere, admin.RR_DIR)
        with self.assertRaises(OSError):
            admin.rr_save("county.csv", COUNTY)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_limit_on_how_many(self):
        saved, admin.MAX_RR_FILES = admin.MAX_RR_FILES, 2
        try:
            self.assertTrue(admin.rr_save("a.csv", COUNTY)[0])
            self.assertTrue(admin.rr_save("b.csv", COUNTY)[0])
            self.assertFalse(admin.rr_save("c.csv", COUNTY)[0])
            self.assertTrue(admin.rr_save("b.csv", STATE)[0])          # replacing is not adding
        finally:
            admin.MAX_RR_FILES = saved

    def test_delete_takes_only_a_listed_name(self):
        admin.rr_save("county.csv", COUNTY)
        keep = os.path.join(self.tmp.name, "keep.csv")
        with open(keep, "wb") as f:
            f.write(COUNTY)
        for name in ("../../keep.csv", keep, "", "nope.csv"):
            self.assertFalse(admin.rr_delete(name)[0])
        self.assertTrue(os.path.exists(keep))
        self.assertTrue(admin.rr_delete("county.csv")[0])
        self.assertEqual(self.stored(), [])


def multipart(fields, files):
    b = "wavelenstestboundary"
    out = []
    for k, v in fields.items():
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for field, name, data in files:
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\nContent-Type: text/csv\r\n\r\n'.encode() + data + b"\r\n")
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"


class OverHttp(Base):
    """The real handler on a loopback port, logged in with a session made directly."""

    def setUp(self):
        super().setUp()
        self.saved_admin = (admin.admin_exists, admin.host_ok)
        admin.admin_exists = lambda: True
        admin.host_ok = lambda h: True
        self.srv = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        admin.admin_exists, admin.host_ok = self.saved_admin
        super().tearDown()

    def login(self):
        tok = admin.SESSIONS.new()
        return f"wl_session={tok}", admin.SESSIONS.get(tok)

    def post(self, body, ctype, cookie):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/area", body=body, headers={"Content-Type": ctype, "Cookie": cookie, "Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def get_area(self, cookie):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/area", headers={"Cookie": cookie, "Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        page = r.read().decode()
        c.close()
        return r.status, page

    def test_upload_several_then_delete(self):
        cookie, sess = self.login()
        body, ctype = multipart({"csrf": sess["csrf"], "do": "rr_upload"},
                                [("rr", "county.csv", COUNTY), ("rr", "sites.csv", SITES), ("rr", "people.csv", NOT_RR)])
        self.assertEqual(self.post(body, ctype, cookie), 303)
        self.assertEqual(self.stored(), ["county.csv", "sites.csv"])
        self.assertIn(["systemctl", "restart", admin.BRIDGE_UNIT], self.calls)
        self.assertIn("people.csv: not a RadioReference export", sess["flash"])
        self.assertIn("Bridge restarted", sess["flash"])
        status, page = self.get_area(cookie)
        self.assertEqual(status, 200)
        for want in ("county.csv", "sites.csv", "county frequencies", "trunked sites / talkgroups", 'enctype="multipart/form-data"'):
            self.assertIn(want, page)
        del self.calls[:]
        body = f"csrf={sess['csrf']}&do=rr_delete&name=county.csv".encode()
        self.assertEqual(self.post(body, "application/x-www-form-urlencoded", cookie), 303)
        self.assertEqual(self.stored(), ["sites.csv"])
        self.assertIn(["systemctl", "restart", admin.BRIDGE_UNIT], self.calls)

    def test_nothing_good_means_no_restart(self):
        cookie, sess = self.login()
        body, ctype = multipart({"csrf": sess["csrf"], "do": "rr_upload"}, [("rr", "people.csv", NOT_RR)])
        self.assertEqual(self.post(body, ctype, cookie), 303)
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.calls, [])

    def test_needs_login_and_form_token(self):
        body, ctype = multipart({"csrf": "x", "do": "rr_upload"}, [("rr", "county.csv", COUNTY)])
        self.assertEqual(self.post(body, ctype, ""), 303)                      # to /login
        cookie, sess = self.login()
        self.assertEqual(self.post(body, ctype, cookie), 403)                  # wrong token
        self.assertEqual(self.stored(), [])

    def test_oversized_upload_is_refused(self):
        cookie, sess = self.login()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/area")
        c.putheader("Content-Type", "multipart/form-data; boundary=x")
        c.putheader("Cookie", cookie)
        c.putheader("Content-Length", str(admin.MAX_RR_BODY + 1))
        c.endheaders()
        self.assertEqual(c.getresponse().status, 413)
        c.close()

    def test_spotter_upload_limit_is_unchanged(self):
        self.assertEqual(admin.MAX_UPLOAD_BODY, 4 * 1024 * 1024 + 64 * 1024)


if __name__ == "__main__":
    unittest.main()
