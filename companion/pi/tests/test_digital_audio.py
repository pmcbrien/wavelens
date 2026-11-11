"""The layout of dsd-fme's audio is set by its decoder flag, and the bridge has to read it the same way: a
stereo preset read as mono plays every call at half speed, an octave low. The table is checked against
dsd-fme's presets, and DigiLayout — which corrects a wrong table from the audio itself — is fed the
datagrams dsd-fme sends (one 160-sample voice frame each). No radio is touched.
    python3 -m unittest companion/pi/tests/test_digital_audio.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import math
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

# dsd-fme's pulse_digi_out_channels for each -f preset (dsd_main.c).
DSD_CHANNELS = {"-fa": 2, "-fs": 2, "-ft": 2, "-f1": 1, "-fi": 1, "-fd": 1, "-fE": 1}


def load_bridge():
    argv, sys.argv = sys.argv, ["bridge"]
    try:
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_audio_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


class DigitalAudioLayout(unittest.TestCase):
    def test_channel_count_matches_the_decoder_flag(self):
        b = load_bridge()
        for mode, (flag, chans) in b.DIGITAL_MODES.items():
            if not flag:
                continue                     # trunkm: the bridge's own decoder, not dsd-fme
            preset = flag.split()[0]
            self.assertIn(preset, DSD_CHANNELS, f"{mode}: unknown dsd-fme preset {preset}")
            self.assertEqual(chans, DSD_CHANNELS[preset], f"{mode} ({preset})")

    def test_auto_is_stereo(self):
        # AUTO is how YSF, D-STAR and conventional P25 / DMR repeaters are heard
        self.assertEqual(load_bridge().DIGITAL_MODES["auto"], ("-fa", 2))


def voice(frame: int, n: int = 160, amp: int = 9000) -> "list[int]":
    """One 20 ms frame of something speech-like: two tones, never the same sample twice in a row."""
    return [int(amp * (math.sin((frame * n + i) * 0.31) + 0.5 * math.sin((frame * n + i) * 1.17)) / 1.5) or 1 for i in range(n)]


def mono_dgram(frame: int) -> bytes:
    return struct.pack("<160h", *voice(frame))


def stereo_dgram(left: "list[int]", right: "list[int]") -> bytes:
    return struct.pack("<320h", *[v for pair in zip(left, right) for v in pair])


class LayoutGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load_bridge()

    def run_stream(self, chans, dgrams):
        lay = self.b.DigiLayout(chans, "test")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = [lay.feed(d) for d in dgrams]
        return lay, out, err.getvalue()

    def test_stereo_copy_read_as_mono_is_corrected(self):
        # the YSF / P25 phase 1 case: dsd-fme copies the one voice to both sides
        frames = [voice(k) for k in range(50)]
        lay, out, said = self.run_stream(1, [stereo_dgram(f, f) for f in frames])
        self.assertEqual(lay.chans, 2)
        self.assertTrue(lay.corrected)
        self.assertEqual(said.count("corrected"), 1)             # said once, not per frame
        self.assertIn("half speed", said)
        wrong = [k for k, o in enumerate(out) if o != struct.pack("<160h", *frames[k])]
        self.assertLess(len(wrong), lay.PROOFS)                  # under 60 ms at the wrong speed
        self.assertEqual(len(b"".join(out[lay.PROOFS:])), (50 - lay.PROOFS) * 320)   # real time from then on

    def test_one_slot_stereo_read_as_mono_is_corrected(self):
        # DMR with one slot talking: the other side is silent
        quiet = [0] * 160
        lay, out, _ = self.run_stream(1, [stereo_dgram(voice(k), quiet) for k in range(20)])
        self.assertEqual(lay.chans, 2)
        lay, out, _ = self.run_stream(1, [stereo_dgram(quiet, voice(k)) for k in range(20)])
        self.assertEqual(lay.chans, 2)

    def test_mono_read_as_stereo_is_corrected(self):
        lay, out, said = self.run_stream(2, [mono_dgram(k) for k in range(20)])
        self.assertEqual(lay.chans, 1)
        self.assertIn("double speed", said)
        self.assertEqual(out[-1], mono_dgram(19))

    def test_a_right_table_is_left_alone(self):
        frames = [voice(k) for k in range(30)]
        lay, out, said = self.run_stream(2, [stereo_dgram(f, f) for f in frames])
        self.assertEqual((lay.chans, lay.corrected, said), (2, False, ""))
        self.assertEqual(out[7], struct.pack("<160h", *frames[7]))   # equal sides mix back to the voice, full level
        lay, out, said = self.run_stream(1, [mono_dgram(k) for k in range(30)])
        self.assertEqual((lay.chans, lay.corrected, said), (1, False, ""))
        self.assertEqual(out[7], mono_dgram(7))

    def test_two_slots_talking_stays_stereo(self):
        lay, _, said = self.run_stream(2, [stereo_dgram(voice(k), voice(k + 500, amp=5000)) for k in range(40)])
        self.assertEqual((lay.chans, said), (2, ""))

    def test_silence_and_odd_sizes_prove_nothing(self):
        lay, _, said = self.run_stream(1, [bytes(640)] * 20 + [b"\x01\x02\x03"] * 5 + [bytes(100)] * 5)
        self.assertEqual((lay.chans, said), (1, ""))

    def test_one_stray_datagram_does_not_flip_it(self):
        f = voice(3)
        dgrams = [mono_dgram(k) for k in range(10)] + [stereo_dgram(f, f)] + [mono_dgram(k) for k in range(10)]
        lay, _, said = self.run_stream(1, dgrams)
        self.assertEqual((lay.chans, said), (1, ""))


if __name__ == "__main__":
    unittest.main()
