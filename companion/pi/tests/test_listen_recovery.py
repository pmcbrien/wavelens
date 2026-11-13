"""A listening dongle that drops off USB and comes back is noticed, and an OP25 that stops decoding is
restarted. Synthetic values only; no radio is touched.
    python3 -m unittest companion/pi/tests/test_listen_recovery.py
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
        spec = importlib.util.spec_from_file_location("wavelens_bridge_for_recovery_test", os.path.join(HERE, "..", "wavelens_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


class ListenRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load_bridge()

    def test_reenumerated(self):
        f = self.b.listen_dongle_reenumerated
        self.assertTrue(f({"1111": "3-34", "2222": "1-2"}, {"1111": "3-35", "2222": "1-2"}, "1111"))
        self.assertFalse(f({"1111": "3-34"}, {"1111": "3-34"}, "1111"))          # unchanged
        self.assertFalse(f({"1111": "3-34", "2222": "1-2"}, {"1111": "3-34", "2222": "1-9"}, "1111"))  # the other one
        self.assertFalse(f({"1111": "3-34"}, {}, "1111"))                         # gone: hot-plug handles it
        self.assertFalse(f({}, {"1111": "3-35"}, "1111"))                         # new: hot-plug handles it
        self.assertFalse(f({"1111": "3-34"}, {"1111": "3-35"}, ""))               # nothing listening

    def test_op25_stalled(self):
        f, s = self.b.op25_stalled, self.b.OP25_STALL_S
        self.assertFalse(f(0.0, 0.0, 1000.0))                    # never decoded: start-up timeout applies
        self.assertFalse(f(990.0, 0.0, 1000.0))                  # control channel 10 s ago
        self.assertTrue(f(1000.0 - s - 1, 0.0, 1000.0))          # nothing for longer than the limit
        self.assertFalse(f(1000.0 - s - 1, 995.0, 1000.0))       # a call 5 s ago keeps it alive


if __name__ == "__main__":
    unittest.main()
