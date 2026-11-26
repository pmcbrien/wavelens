#!/usr/bin/env python3
"""Exit 1 if an app image doesn't fit its OTA slot with a margin to spare.

usage: check_size.py firmware.bin partitions.bin [margin_bytes]

Reads the ota_0 size from the built partition table (32-byte entries, magic bytes AA 50). PlatformIO's
"Flash: 98%" line counts sections, not the .bin, so it never fails when the file outgrows the slot.
"""
import struct, sys, os

img, table = sys.argv[1], sys.argv[2]
margin = int(sys.argv[3]) if len(sys.argv) > 3 else 16384
slot = None
data = open(table, "rb").read()
for i in range(0, len(data) - 31, 32):
    magic, ptype, sub, off, size = struct.unpack_from("<HBBII", data, i)
    if magic != 0x50AA:  # bytes AA 50, little-endian
        break
    if ptype == 0 and sub == 0x10:  # app, ota_0
        slot = size
        break
if slot is None:
    print(f"::error::no ota_0 partition in {table}")
    sys.exit(1)
n = os.path.getsize(img)
free = slot - n
print(f"{img}: {n} bytes, slot {slot}, {free} free (margin {margin})")
sys.exit(0 if free >= margin else 1)
