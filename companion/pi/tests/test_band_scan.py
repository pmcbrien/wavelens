"""Band scan (the app's Weather / Marine / Aircraft buttons): step through the band's channels, leave a quiet one
after SCAN_DWELL_S, stay on a voice until it has been quiet SCAN_HANG_S. Standard library only:
    python3 -m unittest companion/pi/tests/test_band_scan.py
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
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_band_scan_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
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
MARINE = [156.8, 156.3, 156.45]


class BandScan(unittest.TestCase):
    def setUp(self):
        self.tuned = []
        self.now = 1000.0

        def fake_start(mhz, mode, sq, tol=0.05):
            self.tuned.append((mhz, mode, sq))
            # what the listener does once rtl_fm is up: a fresh channel, no audio yet
            B.LISTEN.update(req=(mode, mhz), proc=object(), mhz=mhz, mode=mode, asked=mode, sq=sq, starting=False,
                            tuned_at=self.now, last_audio=self.now, chan_at=self.now, until=self.now + 300)

        self._start, B.listen_start = B.listen_start, fake_start
        self._status, B.listen_status = B.listen_status, lambda msg: None
        self._stop, B.listen_stop = B.listen_stop, lambda *a, **k: B.LISTEN.update(req=None, proc=None)
        B.LISTEN.update(follow=False, req=None, proc=None, sq=0, lockout=[])
        B.scan_stop()

    def tearDown(self):
        B.listen_start, B.listen_status, B.listen_stop = self._start, self._status, self._stop
        B.scan_stop()

    def tick(self, dt):
        self.now += dt
        B._scan_tick(self.now)

    def test_parse(self):
        chans, mode, name = B.scan_parse({"mhz": [156.8, "156.3", 156.8001, 5.0, "x"], "mode": "fm", "name": "Marine"})
        self.assertEqual(chans, [156.8, 156.3])
        self.assertEqual((mode, name), ("fm", "Marine"))
        self.assertIsNone(B.scan_parse({"mhz": []}))

    def test_quiet_channels_are_skipped(self):
        B.scan_start(MARINE, "fm", "Marine", 0)
        self.assertEqual(self.tuned[-1][:2], (156.8, "fm"))
        self.assertGreaterEqual(self.tuned[-1][2], B.SCAN_MIN_SQ)       # never scans with the squelch wide open
        self.tick(1.0)
        self.assertEqual(len(self.tuned), 1)                            # still giving the squelch time
        self.tick(B.SCAN_DWELL_S)
        self.assertEqual(self.tuned[-1][0], 156.3)                      # nothing heard: next channel
        self.tick(B.SCAN_DWELL_S + 0.1)
        self.assertEqual(self.tuned[-1][0], 156.45)
        self.tick(B.SCAN_DWELL_S + 0.1)
        self.assertEqual(self.tuned[-1][0], 156.8)                      # and round again

    def test_a_voice_holds_the_channel_until_quiet(self):
        B.scan_start(MARINE, "fm", "Marine", 30)
        self.tick(0.5)
        B.LISTEN["last_audio"] = self.now                               # squelch opened: someone talking
        for _ in range(20):
            self.tick(0.5)
            B.LISTEN["last_audio"] = self.now                           # ... for ten seconds
        self.assertEqual(len(self.tuned), 1)
        self.tick(B.SCAN_HANG_S - 0.5)
        self.assertEqual(len(self.tuned), 1)                            # hang time after the voice
        self.tick(1.0)
        self.assertEqual(self.tuned[-1][0], 156.3)

    def test_ranges_from_band_edges(self):
        # military air, 225-400 MHz: cut into pieces no wider than an on-request sweep window
        r = B.scan_ranges({"ranges": [[225, 400]]}, [243.0, 282.8])
        self.assertEqual(r[0], "225.0000M:265.0000M:12.5k")
        self.assertEqual(r[-1], "385.0000M:400.0000M:12.5k")
        self.assertEqual(len(r), 5)
        # ham: two edges given, two ranges; nonsense entries skipped
        self.assertEqual(B.scan_ranges({"ranges": [[144, 148], [420, 450], ["x"], [1, 2]]}, [146.52]),
                         ["144.0000M:148.0000M:12.5k", "420.0000M:450.0000M:12.5k"])

    def test_ranges_from_channels_when_no_edges(self):
        r = B.scan_ranges({}, [156.8, 156.3, 157.1, 446.0])
        self.assertEqual(r, ["155.8000M:157.6000M:12.5k", "445.5000M:446.5000M:12.5k"])

    def test_sweep_keeps_to_the_band_scan(self):
        usual = ["150M:174M:12.5k", "851M:869M:12.5k"]
        self.assertEqual(B.sweep_ranges(usual), usual)
        B.scan_start(MARINE, "fm", "Marine", 30, ["155.8000M:162.1000M:12.5k"])
        self.assertEqual(B.sweep_ranges(usual), ["155.8000M:162.1000M:12.5k"])
        B.scan_stop()
        self.assertEqual(B.sweep_ranges(usual), usual)            # back to the usual ranges
        B.scan_start(MARINE, "fm", "Marine", 30)                   # an older app sends no ranges: unchanged
        self.assertEqual(B.sweep_ranges(usual), usual)

    def test_renewal_does_not_restart_the_scan(self):
        B.scan_start(MARINE, "fm", "Marine", 30)
        self.tick(B.SCAN_DWELL_S + 0.1)
        self.assertEqual(self.tuned[-1][0], 156.3)
        B.scan_start(MARINE, "fm", "Marine", 30)                        # the app's once-a-minute resend
        self.assertEqual(self.tuned[-1][0], 156.3)
        self.assertEqual(len(self.tuned), 2)

    def test_single_channel_stays(self):
        B.scan_start([162.55], "fm", "Weather", 30)
        for _ in range(10):
            self.tick(B.SCAN_DWELL_S)
        self.assertEqual(len(self.tuned), 1)

    def test_stop(self):
        B.scan_start(MARINE, "fm", "Marine", 30)
        B.scan_stop()
        self.tick(B.SCAN_DWELL_S + 1)
        self.assertEqual(len(self.tuned), 1)

    def test_status_carries_the_scan(self):
        B.listen_status = self._status
        sent = []
        emit, B.emit = B.emit, sent.append
        try:
            B.scan_start(MARINE, "fm", "Marine", 30)
            B.listen_status("x")
        finally:
            B.emit = emit
        self.assertEqual(sent[-1]["listen"]["scan"]["name"], "Marine")
        self.assertEqual(sent[-1]["listen"]["scan"]["n"], 3)


if __name__ == "__main__":
    unittest.main()
