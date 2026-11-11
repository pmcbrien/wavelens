"""Where the sweep says a carrier is. rtl_power's rows give each bin's centre, hackrf_sweep's its lower
edge; reading rtl_power the hackrf way put every carrier half a bin high and the scanner tuned beside the
channel (a P25 control channel on 856.9625 was tried on 856.9688 and barely decoded). The rows below have the
shape rtl_power printed on a bench Scanner; the levels are invented. No radio is touched.
    python3 -m unittest companion/pi/tests/test_sweep_frequency.py
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
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_sweep_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


def rtl_power_rows(lo_hz, hi_hz, step, carriers, n=4, floor=-20.0):
    """n passes of one rtl_power row: (hi - lo) / step + 1 bins, bin i centred on lo + i * step. A carrier
    at f puts its power in the bin nearest f and leaks into the two beside it, more into the nearer one."""
    bins = int(round((hi_hz - lo_hz) / step)) + 1
    vals = [floor] * bins
    for f, level in carriers:
        x = (f - lo_hz) / step
        i = int(round(x))
        frac = x - i                                   # -0.5 .. 0.5: where in the bin the carrier sits
        vals[i] = level
        vals[i - 1] = level - 8.0 - 12.0 * frac        # nearer neighbour is louder
        vals[i + 1] = level - 8.0 + 12.0 * frac
    line = f"2026-11-11, 11:00:00, {lo_hz:.0f}, {hi_hz:.0f}, {step:.2f}, 10, " + ", ".join(f"{v:.2f}" for v in vals)
    return [line] * n


class FakeRR:
    def __init__(self, controls):
        self.controls = controls

    def control_at(self, mhz, tol_mhz=0.0065):
        near = [f for f in self.controls if abs(f - mhz) <= tol_mhz]
        return (min(near, key=lambda f: abs(f - mhz)), True, {}) if near else None


class BinCentre(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load_bridge()

    def setUp(self):
        self.saved_rr = self.b.RR
        self.b.RR = None

    def tearDown(self):
        self.b.RR = self.saved_rr

    def test_each_tool_by_its_own_convention(self):
        self.assertEqual(self.b.bin_centre_hz("rtlpower", 856959375.0, 9375.0), 856959375.0)
        self.assertEqual(self.b.bin_centre_hz("hackrf", 856000000.0, 100000.0), 856050000.0)

    def test_control_channels_land_on_their_channel(self):
        # 800 MHz channels are 851.0125 + n x 12.5 kHz: 856.9625 and 857.4625, as measured off air
        rows = rtl_power_rows(856.5e6, 857.7e6, 9375.0, [(856.9625e6, 8.0), (857.4625e6, 5.0)])
        got = sorted(f for f, _ in self.b.cc_steady(rows))
        self.assertEqual(got, [856.9625, 857.4625])

    def test_never_half_a_bin_high(self):
        for f in (851.0125e6, 853.4375e6, 859.7375e6, 460.1e6, 453.2125e6, 154.875e6):
            lo = f - 300e3
            rows = rtl_power_rows(lo, lo + 600e3, 9375.0, [(f, 6.0)])
            got = [g for g, _ in self.b.cc_steady(rows)]
            self.assertEqual(len(got), 1, f)
            self.assertLess(abs(got[0] * 1e6 - f), 3200, f"{f/1e6} read as {got[0]}")   # inside the 6.25 kHz raster step

    def test_live_sweep_hunt_agrees(self):
        sw = self.b.Sweep("rtlpower")
        try:
            now = 1000.0
            lo, step = 856.5e6, 9375.0
            line = rtl_power_rows(lo, 857.7e6, step, [(856.9625e6, 8.0)], n=1)[0]
            vals = [float(v) for v in line.split(",")[6:]]
            for i, db in enumerate(vals):
                sw.hist[lo + i * step] = self.b.deque([(now - 3, db), (now - 2, db), (now - 1, db)], maxlen=8)
            got, nread = self.b.sweep_steady(856.5, 857.7, now)
            self.assertEqual(nread, 3)
            self.assertEqual([f for f, _ in got], [856.9625])
        finally:
            self.b.SWEEPS.remove(sw)

    def test_weather_radio_is_not_a_control_channel(self):
        rows = rtl_power_rows(162.3e6, 162.6e6, 9375.0, [(162.45e6, 9.0)])
        self.assertEqual(self.b.cc_steady(rows), [])
        self.assertIsNone(self.b.cc_place(162.55))
        self.assertIsNotNone(self.b.cc_place(162.25))

    def test_a_listed_control_channel_wins_over_the_raster(self):
        self.b.RR = FakeRR([856.9625, 858.2375])
        self.assertEqual(self.b.cc_place(856.9655), 856.9625)       # what the bench Scanner read
        self.assertEqual(self.b.cc_place(856.9680), 856.9625)       # even a poor reading, within half a channel
        self.assertEqual(self.b.cc_place(855.3440), 855.34375)      # nothing listed there: the raster


class DecodeQuality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load_bridge()

    def test_counts_dsd_fme_lines(self):
        b = self.b
        lines = ["11:25:57 Sync: +P25p1 WACN: BEE00; SYS: 802; NAC/CC: 800;  LDU1",
                 " LCW FEC ERR",
                 "11:25:57 Sync: +P25p1 WACN: BEE00;  duid:EE",
                 " Ignoring LDU2 not preceeded by LDU1",
                 "11:25:58 Sync: +P25p1 NAC/CC: 800;  TSBK",
                 "11:25:59 Sync: +P25p1 NAC/CC: 800;  LDU2"]
        bad = sum(len(b.BAD_FRAME_RE.findall(x)) for x in lines)
        voice = sum(len(b.VOICE_FRAME_RE.findall(x)) for x in lines if not b.BAD_FRAME_RE.search(x))
        ctrl = sum(1 for x in lines if b.CTRL_FRAME_RE.search(x))
        self.assertEqual((voice, bad, ctrl), (2, 3, 1))     # a line reporting a failure names frames it did not play

    def test_words(self):
        q = self.b.decode_quality
        self.assertEqual(q({"voice": 5, "bad": 0, "ctrl": 3}), "")                 # too little to say
        self.assertIn("clean", q({"voice": 200, "bad": 2, "ctrl": 50}))
        self.assertIn("fair", q({"voice": 200, "bad": 20, "ctrl": 0}))
        self.assertIn("poor", q({"voice": 100, "bad": 60, "ctrl": 0}))


if __name__ == "__main__":
    unittest.main()
