#!/usr/bin/env python3
"""Draws the RF spectrum map for the home page: the bands, what transmits in them, and which radio hears which part.
Run from this folder: python3 spectrum.py  (writes ../../site/assets/companion/spectrum.svg)"""

import math
from pathlib import Path

W = 980
X0, X1 = 70, 950          # plot area
F0, F1 = 1.0, 6000.0      # MHz, log scale
INK, MUTED, LINE, BG = "#1f2933", "#6b7280", "#d4d9df", "#ffffff"
FONT = "font-family='Inter, Helvetica, Arial, sans-serif'"

# what transmits there -> colour
KIND = {
    "safety": ("#1d4ed8", "Public safety & government radio"),
    "air": ("#7c3aed", "Aviation"),
    "device": ("#b45309", "Devices: sensors, fobs, trackers, Wi-Fi, drones"),
    "sat": ("#0f766e", "Satellite & navigation"),
    "cell": ("#be185d", "Cellular"),
    "bcast": ("#6b7280", "Broadcast & amateur"),
}

# (low MHz, high MHz, label, kind). US allocations, rounded.
BANDS = [
    (3, 30, "Shortwave / HF ham", "bcast"),
    (26.96, 27.41, "CB 27", "bcast"),
    (30, 50, "VHF low: public safety", "safety"),
    (88, 108, "FM radio", "bcast"),
    (108, 137, "Air band", "air"),
    (137, 138, "Weather sats 137", "sat"),
    (144, 148, "2 m ham", "bcast"),
    (150, 174, "VHF high: public safety, marine", "safety"),
    (225, 400, "Military air (UHF)", "air"),
    (314, 316, "315: tire sensors, fobs", "device"),
    (433.0, 434.8, "433.92: sensors, fobs", "device"),
    (450, 470, "UHF public safety", "safety"),
    (600, 960, "Cellular (low band)", "cell"),
    (764, 869, "700/800: P25 trunked", "safety"),
    (902, 928, "915 ISM: LoRa, meters", "device"),
    (1089, 1091, "ADS-B 1090: aircraft", "air"),
    (1227, 1228, "GPS L2", "sat"),
    (1240, 1300, "1.2 GHz video links", "device"),
    (1525, 1559, "Inmarsat", "sat"),
    (1575, 1576, "GPS L1", "sat"),
    (1616, 1626.5, "Iridium", "sat"),
    (1710, 2200, "Cellular (mid band)", "cell"),
    (2400, 2483.5, "2.4 GHz: Wi-Fi, Bluetooth, drones, trackers", "device"),
    (5150, 5895, "5 GHz Wi-Fi", "device"),
    (5725, 5875, "5.8 GHz: drone video", "device"),
]

# (name, sub, colour, segments, note, placement: "in" = inside the bar, "left" = before the first bar)
# Ranges from companion.html "What each radio covers".
RADIOS = [
    ("Phone", "built in", "#0f766e", [(2400, 2483.5), (5150, 5895)],
     "Bluetooth, 2.4 and 5 GHz Wi-Fi; the OS limits how often Wi-Fi is scanned", "left"),
    ("Spotter (ESP32)", "$49 or DIY", "#b45309", [(2400, 2483.5), (5150, 5895)],
     "2.4 GHz Wi-Fi + Bluetooth, parked on the channels you pick; 5 GHz on an ESP32-C5", "left"),
    ("ESP32 add-on modules", "DIY", "#b45309", [(300, 348), (387, 464), (779, 928), (5645, 5945)],
     "CC1101 / SX1276 sub-GHz sweep; RX5808 5.8 GHz video", "left"),
    ("RTL-SDR", "in Scanner, $199", "#1d4ed8", [(24, 1766)],
     "24 MHz to 1.77 GHz: decodes sensors and fobs, sweeps, listens", "in"),
    ("HackRF One", "build only", "#7c3aed", [(1, 6000)],
     "1 MHz to 6 GHz: energy sweep, about once a second", "in"),
]


def x(f):
    return X0 + (math.log10(f) - math.log10(F0)) / (math.log10(F1) - math.log10(F0)) * (X1 - X0)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tw(s, size):
    return len(s) * size * 0.56


body = []


def t(xx, yy, s, size=11, fill=INK, weight="400", anchor="start"):
    body.append(f"<text x='{xx:.1f}' y='{yy:.1f}' font-size='{size}' font-weight='{weight}' fill='{fill}' text-anchor='{anchor}'>{esc(s)}</text>")


t(24, 34, "The radio spectrum, and which radio hears which part", 20, INK, "700")
t(24, 54, "Log scale, 1 MHz to 6 GHz. US allocations; a local pack renames them where you drive.", 12, MUTED)

# ITU band strip and frequency labels
AX = 78
for lo, hi, name in [(3, 30, "HF"), (30, 300, "VHF"), (300, 3000, "UHF"), (3000, 6000, "SHF")]:
    body.append(f"<rect x='{x(lo):.1f}' y='{AX}' width='{x(hi) - x(lo):.1f}' height='18' fill='#f3f4f6' stroke='{LINE}'/>")
    t((x(lo) + x(hi)) / 2, AX + 13, name, 11, MUTED, "600", "middle")
TICKS = [(1, "1 MHz"), (10, "10"), (100, "100 MHz"), (1000, "1 GHz"), (6000, "6 GHz")]
for f, lab in TICKS:
    t(x(f), AX + 34, lab, 10, MUTED, anchor="middle")

# bands: each goes in the first row where its bar and label fit; labels near the right edge go on the left
ROW0, RH = AX + 48, 19
rows = []
for lo, hi, label, kind in sorted(BANDS, key=lambda b: b[0]):
    bx0, bx1 = x(lo), max(x(hi), x(lo) + 3)
    w = tw(label, 10.5)
    right = bx1 + 4 + w <= W - 8
    ext = (bx0, bx1 + 4 + w) if right else (bx0 - 4 - w, bx1)
    r = 0
    while r < len(rows) and any(not (ext[1] + 6 < a or ext[0] - 6 > b) for a, b in rows[r]):
        r += 1
    if r == len(rows):
        rows.append([])
    rows[r].append(ext)
    yy = ROW0 + r * RH
    body.append(f"<rect x='{bx0:.1f}' y='{yy}' width='{bx1 - bx0:.1f}' height='12' rx='2' fill='{KIND[kind][0]}' opacity='0.85'/>")
    if right:
        t(bx1 + 4, yy + 10, label, 10.5, INK)
    else:
        t(bx0 - 4, yy + 10, label, 10.5, INK, anchor="end")
band_bottom = ROW0 + len(rows) * RH

# radio lanes
LY = band_bottom + 34
t(24, LY - 6, "What hears it", 13, INK, "700")
for i, (name, sub, col, segs, note, where) in enumerate(RADIOS):
    yy = LY + 14 + i * 44
    body.append(f"<line x1='{X0}' y1='{yy + 16}' x2='{X1}' y2='{yy + 16}' stroke='{LINE}'/>")
    t(24, yy + 2, name, 12, INK, "700")
    t(24 + tw(name, 12) + 8, yy + 2, sub, 10.5, MUTED)
    for lo, hi in segs:
        body.append(f"<rect x='{x(lo):.1f}' y='{yy + 8}' width='{max(x(hi) - x(lo), 3):.1f}' height='16' rx='3' fill='{col}' opacity='0.9'/>")
    if where == "in":
        t(x(segs[0][0]) + 6, yy + 20, note, 10, "#ffffff", "600")
    else:
        t(x(segs[0][0]) - 8, yy + 20, note, 10, MUTED, anchor="end")
radios_bottom = LY + 14 + len(RADIOS) * 44

# legend, wrapped
lx, ly = 24, radios_bottom + 14
for key in ["safety", "air", "device", "sat", "cell", "bcast"]:
    col, lab = KIND[key]
    wdt = 17 + tw(lab, 10.5) + 18
    if lx + wdt > W - 16:
        lx, ly = 24, ly + 20
    body.append(f"<rect x='{lx}' y='{ly}' width='12' height='12' rx='2' fill='{col}'/>")
    t(lx + 17, ly + 10, lab, 10.5, INK)
    lx += wdt
t(24, ly + 38, "Receive-only. A sweep reports energy (“something is transmitting here”); a name only where the radio decodes the signal. "
  "Encrypted calls are never decoded.", 10, MUTED)
H = ly + 52

grid = [f"<line x1='{x(f):.1f}' y1='{AX + 22}' x2='{x(f):.1f}' y2='{radios_bottom}' stroke='{LINE}' stroke-dasharray='3 4'/>" for f, _ in TICKS]
svg = [f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {W} {H}' width='{W}' height='{H}' {FONT}>",
       f"<rect width='{W}' height='{H}' fill='{BG}'/>"] + grid + body + ["</svg>"]
dest = Path(__file__).resolve().parents[2] / "site/assets/companion/spectrum.svg"
dest.write_text("\n".join(svg) + "\n")
print(dest, len(rows), "band rows, height", H)
