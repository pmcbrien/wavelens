"""A channel tapped while scanning is a one-off visit: the app's once-a-minute renewal of that tap must not
pull the scanner back to it. Standard library only:
    python3 -m unittest companion/pi/tests/test_follow_renewal.py
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
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_renewal_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
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


class Renewal(unittest.TestCase):
    def setUp(self):
        B.LISTEN.pop("manual_cmd", None)
        B.LISTEN.update(follow=True, req=("trunkq", 856.9625), mhz=856.9625, follow_until=0.0, until=0.0)
        self.err, sys.stderr = sys.stderr, open(os.devnull, "w")

    def tearDown(self):
        sys.stderr.close()
        sys.stderr = self.err

    def test_first_tap_tunes(self):
        self.assertFalse(B.manual_listen_is_renewal(854.6052, "auto", 1000.0))

    def test_same_tap_a_minute_later_while_scanning_elsewhere_is_a_renewal(self):
        B.manual_listen_is_renewal(854.6052, "auto", 1000.0)
        self.assertTrue(B.manual_listen_is_renewal(854.6052, "auto", 1060.0))
        self.assertGreater(B.LISTEN["follow_until"], 1060.0)          # Follow kept alive

    def test_renewal_while_still_on_the_channel_is_ordinary(self):
        B.manual_listen_is_renewal(854.6052, "auto", 1000.0)
        B.LISTEN.update(req=("auto", 854.6), mhz=854.6)
        self.assertFalse(B.manual_listen_is_renewal(854.6052, "auto", 1060.0))

    def test_not_scanning_a_repeat_tunes(self):
        B.LISTEN["follow"] = False
        B.manual_listen_is_renewal(854.6052, "auto", 1000.0)
        self.assertFalse(B.manual_listen_is_renewal(854.6052, "auto", 1060.0))

    def test_a_new_tap_or_a_quick_second_tap_tunes(self):
        B.manual_listen_is_renewal(854.6052, "auto", 1000.0)
        self.assertFalse(B.manual_listen_is_renewal(460.1, "auto", 1060.0))   # different channel
        self.assertFalse(B.manual_listen_is_renewal(460.1, "auto", 1065.0))   # tapped again 5 s later


if __name__ == "__main__":
    unittest.main()
