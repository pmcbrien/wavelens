"""Noise squelch on analog listens: static and dead carriers are not heard, voice is. Standard library only:
    python3 -m unittest companion/pi/tests/test_noise_squelch.py
"""
from __future__ import annotations

import math
import os
import random
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_band_scan import B  # noqa: E402

N = 320


def frame(xs):
    return struct.pack(f"<{N}h", *[max(-32768, min(32767, int(v))) for v in xs])


def static(rnd):
    return frame(rnd.gauss(0, 6500) for _ in range(N))


def voice(k):
    t0 = k * N
    return frame(5000 * math.sin(2 * math.pi * 300 * (t0 + i) / 8000) + 2500 * math.sin(2 * math.pi * 900 * (t0 + i) / 8000)
                 for i in range(N))


class NoiseSquelch(unittest.TestCase):
    def test_static_is_noise(self):
        rnd = random.Random(1)
        self.assertTrue(all(B.nsq_noise(static(rnd)) for _ in range(50)))

    def test_voice_is_not(self):
        self.assertFalse(any(B.nsq_noise(voice(k)) for k in range(50)))

    def test_dead_carrier_is_noise(self):
        self.assertTrue(B.nsq_noise(frame(30 * math.sin(i / 3) for i in range(N))))

    def test_hysteresis(self):
        rnd, sq = random.Random(2), B.NoiseSquelch()
        self.assertFalse(any(sq.frame(static(rnd)) for _ in range(40)))      # static never opens it
        self.assertFalse(sq.frame(voice(0)))                                    # one frame is not enough
        self.assertTrue(sq.frame(voice(1)))
        for _ in range(B.NSQ_CLOSE - 1):                                        # a short burst does not chop a word
            self.assertTrue(sq.frame(static(rnd)))
        self.assertFalse(sq.frame(static(rnd)))


if __name__ == "__main__":
    unittest.main()
