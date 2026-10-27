#!/usr/bin/env python3
"""Find a HackRF's own spurs so the bridge can mask them.

Run with NO ANTENNA on the HackRF (unscrew it; leave the SMA open). Anything still well over the
noise floor is the receiver itself, not the air. Prints a ready --hackrf-mask value.

    sudo systemctl stop wavelens-bridge        # free the HackRF
    python3 find_spurs.py                       # ~40 s
    sudo systemctl start wavelens-bridge

Then put the printed value in /etc/default/wavelens-bridge:
    WAVELENS_ARGS="--hackrf-sweep --hackrf-mask 4770:4805,3215:3240"
and restart the service.
"""
import argparse, statistics, subprocess, sys
from collections import defaultdict

ap = argparse.ArgumentParser()
ap.add_argument("--seconds", type=int, default=40, help="how long to sweep (default 40)")
ap.add_argument("--range", default="1:6000", help="MHz range (default 1:6000)")
ap.add_argument("--lna", type=int, default=16)
ap.add_argument("--vga", type=int, default=20)
ap.add_argument("--over", type=float, default=18.0, help="dB over the floor that counts as a spur (default 18, the bridge's threshold)")
ap.add_argument("--pad", type=float, default=1.0, help="MHz added each side of a spur (default 1)")
a = ap.parse_args()

cmd = ["timeout", str(a.seconds), "hackrf_sweep", "-f", a.range.replace(":", ":"), "-w", "100000", "-l", str(a.lna), "-g", str(a.vga)]
print("sweeping with no antenna for %d s ..." % a.seconds, file=sys.stderr)
p = subprocess.run(cmd, capture_output=True, text=True)
rows = [r for r in p.stdout.splitlines() if r.count(",") > 6]
if len(rows) < 50:
    sys.exit("hackrf_sweep produced almost nothing: is the HackRF free (service stopped) and in HackRF mode?\n" + p.stderr[-400:])

levels = defaultdict(list)       # bin Hz → dB readings
for r in rows:
    f = [x.strip() for x in r.split(",")]
    lo, step = float(f[2]), float(f[4])
    for i, v in enumerate(f[6:]):
        try:
            levels[lo + i * step].append(float(v))
        except ValueError:
            pass
med = {hz: statistics.median(v) for hz, v in levels.items() if len(v) >= 3}
floor = statistics.median(med.values())
hot = sorted(hz for hz, db in med.items() if db - floor >= a.over)
print(f"floor {floor:.0f} dB over {len(med)} bins; {len(hot)} spur bins", file=sys.stderr)

ranges = []
for hz in hot:
    mhz = hz / 1e6
    if ranges and mhz - ranges[-1][1] <= a.pad:
        ranges[-1][1] = mhz
    else:
        ranges.append([mhz, mhz])
for lo, hi in ranges:
    peak = max((med[hz], hz) for hz in hot if lo * 1e6 <= hz <= hi * 1e6)
    print(f"  {lo - a.pad:8.1f}–{hi + a.pad:8.1f} MHz   peak {peak[0]:.0f} dB at {peak[1]/1e6:.2f} MHz", file=sys.stderr)
if ranges:
    print("--hackrf-mask " + ",".join(f"{lo - a.pad:.0f}:{hi + a.pad + 0.999:.0f}" for lo, hi in ranges))
else:
    print("no spurs found", file=sys.stderr)
