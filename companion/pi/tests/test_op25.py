"""OP25 as the trunked P25 decoder: its trunk.tsv, and its HTTP terminal's answers read into the fields the
trunk loop uses (call, unit, encryption, control-channel activity). A fake terminal; no radio, no OP25.
Standard library only:
    python3 -m unittest companion/pi/tests/test_op25.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))


def load_bridge():
    argv, sys.argv = sys.argv, ["bridge"]
    try:
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_op25_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        with open(os.devnull, "w") as quiet:
            err, sys.stderr = sys.stderr, quiet
            try:
                spec.loader.exec_module(mod)
            finally:
                sys.stderr = err
        return mod
    finally:
        sys.argv = argv


B = load_bridge()
ANSWER = {"msgs": []}


class Terminal(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        cmds = json.loads(self.rfile.read(n))
        assert cmds[0]["command"] == "update"
        out = json.dumps(ANSWER["msgs"]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class FakeProc:
    def __init__(self):
        self.done = False
        self.sync, self.sync_at, self.call, self.call_at = "", 0.0, "", 0.0
        self.voice_at, self.enc_now, self.enc_at = 0.0, False, 0.0
        self.err_tail, self.err_log = deque(maxlen=3), deque(maxlen=15)

    def poll(self):
        return 0 if self.done else None


def trunk(tsbks, grp=0, src=0, enc=0, nac=0x800):
    return {"json_type": "trunk_update", "srcaddr": src, "grpaddr": grp, "encrypted": enc, "nac": nac,
            "2048": {"tsbks": tsbks, "top_line": "WACN 0x1 SYSID 0x1"}}


class OP25Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Terminal)
        B.OP25_HTTP_PORT = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_trunk_tsv(self):
        with tempfile.TemporaryDirectory() as d:
            B.OP25_DIR = d
            path = B.op25_files(856.9625, "trunkq", (10, 12))
            rows = open(path).read().splitlines()
            self.assertEqual(rows[1].split("\t")[1], '"856.96250"')
            self.assertEqual(rows[1].split("\t")[4], '"cqpsk"')
            self.assertEqual(open(os.path.join(d, "blacklist.tsv")).read(), "10\n12\n")
            path = B.op25_files(851.0125, "trunk", ())
            self.assertEqual(open(path).read().splitlines()[1].split("\t")[7], '""')   # no blacklist

    def test_poll_reads_control_channel_and_calls(self):
        B.LISTEN["gen"] = 7
        p = FakeProc()
        ANSWER["msgs"] = [trunk(100), {"json_type": "change_freq", "tgid": None, "freq": 856962500}]
        t = threading.Thread(target=B.op25_poll, args=(p, 7), daemon=True)
        t.start()
        time.sleep(1.2)
        ANSWER["msgs"] = [trunk(130), {"json_type": "change_freq", "tgid": None, "freq": 856962500}]
        time.sleep(1.2)
        self.assertEqual(p.sync, "P25p1")              # control channel messages counting up: locked
        self.assertEqual(p.nac, "800")
        self.assertEqual(p.call, "")
        ANSWER["msgs"] = [trunk(140, 1220, 4610122, 0), {"json_type": "change_freq", "tgid": 1220, "freq": 857962500}]
        time.sleep(1.2)
        self.assertEqual(p.call, "TG 1220 from 4610122")
        self.assertFalse(p.enc_now)
        self.assertIn(1220, p.active)
        self.assertAlmostEqual(p.voice_mhz, 857.9625)
        ANSWER["msgs"] = [trunk(150, 1306, 0, 1), {"json_type": "change_freq", "tgid": 1306, "freq": 858462500}]
        time.sleep(1.2)
        self.assertEqual(p.call, "TG 1306")
        self.assertTrue(p.enc_now)
        p.done = True
        t.join(timeout=3)

    def test_decoder_choice(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "rx.py"), "w").close()
            B.OP25_APPS = d
            os.environ.pop("WAVELENS_TRUNK_DECODER", None)
            B.OP25["benched_until"] = 0.0
            self.assertTrue(B.op25_wanted("trunkq"))
            self.assertTrue(B.op25_wanted("trunk"))
            self.assertFalse(B.op25_wanted("trunkd"))         # DMR stays on dsd-fme
            os.environ["WAVELENS_TRUNK_DECODER"] = "dsd-fme"
            self.assertFalse(B.op25_wanted("trunkq"))
            os.environ.pop("WAVELENS_TRUNK_DECODER")
            B.OP25["benched_until"] = time.time() + 60
            self.assertFalse(B.op25_wanted("trunkq"))         # benched after repeated failures
            B.OP25["benched_until"] = 0.0
        B.OP25_APPS = "/nonexistent"
        self.assertFalse(B.op25_wanted("trunkq"))             # not installed


if __name__ == "__main__":
    unittest.main()
