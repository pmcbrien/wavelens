"""wavelens_bt_config: the main.conf edit that keeps iOS from asking to pair with the Pi."""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import wavelens_bt_config as btc  # noqa: E402

# Trimmed from Raspberry Pi OS Bookworm's /etc/bluetooth/main.conf (BlueZ 5.66)
BOOKWORM = """[General]

# Default adapter name
#Name = BlueZ

# How long to stay in pairable mode before going back to non-discoverable
# 0 = disable timer, i.e. stay pairable forever
#PairableTimeout = 0

# Specify the policy to the JUST-WORKS repairing initiated by peer
# Possible values: "never", "confirm", "always"
# Defaults to "never"
#JustWorksRepairing = never

[BR]
#PageScanType =

[GATT]
# GATT attribute cache.
#Cache = always

# Enable/Disable client role
# Defaults to true
#Client = true

[CSIS]
#SIRK = ""

[Policy]
#AutoEnable=true
"""


def section(text, name):
    """The key = value lines of one section."""
    out, cur = {}, None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            cur = s.strip("[]")
        elif cur == name and s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class BtConfigTest(unittest.TestCase):
    def test_bookworm_file(self):
        new = btc.apply(BOOKWORM)
        self.assertEqual(section(new, "GATT"), {"Client": "false"})
        # LE only: with BR/EDR off a silently paired device has no classic profile (HID) to use
        self.assertEqual(section(new, "General"), {"JustWorksRepairing": "always", "ControllerMode": "le"})
        self.assertIn("#Name = BlueZ", new)          # other settings and comments untouched
        self.assertIn("#Cache = always", new)
        self.assertIn("[Policy]", new)
        self.assertEqual(section(new, "BR"), {})

    def test_idempotent(self):
        once = btc.apply(BOOKWORM)
        self.assertEqual(btc.apply(once), once)

    def test_existing_values_replaced(self):
        new = btc.apply("[General]\nJustWorksRepairing = never\n[GATT]\nClient = true\n")
        self.assertEqual(section(new, "General")["JustWorksRepairing"], "always")
        self.assertEqual(section(new, "GATT"), {"Client": "false"})
        self.assertEqual(new.count("JustWorksRepairing"), 1)

    def test_empty_or_missing_sections(self):
        new = btc.apply("")
        self.assertEqual(section(new, "GATT"), {"Client": "false"})
        self.assertEqual(section(new, "General")["JustWorksRepairing"], "always")
        new = btc.apply("[Policy]\nAutoEnable=true\n")
        self.assertEqual(section(new, "Policy"), {"AutoEnable": "true"})
        self.assertEqual(section(new, "GATT"), {"Client": "false"})


if __name__ == "__main__":
    unittest.main()
