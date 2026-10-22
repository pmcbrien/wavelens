#!/usr/bin/env python3
"""Draws the companion hardware diagrams as SVG. Run from this folder: python3 make.py"""

from pathlib import Path

INK = "#1f2933"
MUTED = "#6b7280"
LINE = "#9aa5b1"
BG = "#ffffff"
PHONE = "#0f766e"
RF = "#b45309"
POWER = "#b91c1c"
DATA = "#1d4ed8"
BLE = "#7c3aed"
FONT = "font-family='Inter, Helvetica, Arial, sans-serif'"


class D:
    def __init__(self, w, h, title):
        self.w, self.h = w, h
        self.parts = [
            f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {w} {h}' width='{w}' height='{h}' {FONT}>",
            "<defs>"
            f"<marker id='arr' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'><path d='M0,0 L10,5 L0,10 z' fill='{LINE}'/></marker>"
            f"<marker id='arrb' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'><path d='M0,0 L10,5 L0,10 z' fill='{BLE}'/></marker>"
            f"<marker id='arrr' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'><path d='M0,0 L10,5 L0,10 z' fill='{RF}'/></marker>"
            f"<marker id='arrp' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'><path d='M0,0 L10,5 L0,10 z' fill='{POWER}'/></marker>"
            f"<marker id='arrd' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'><path d='M0,0 L10,5 L0,10 z' fill='{DATA}'/></marker>"
            "</defs>",
            f"<rect width='{w}' height='{h}' fill='{BG}'/>",
            f"<text x='24' y='34' font-size='20' font-weight='700' fill='{INK}'>{esc(title)}</text>",
        ]

    def text(self, x, y, s, size=12, fill=INK, weight="400", anchor="start", mono=False):
        fam = " font-family='ui-monospace, Menlo, Consolas, monospace'" if mono else ""
        self.parts.append(f"<text x='{x}' y='{y}' font-size='{size}' font-weight='{weight}' fill='{fill}' text-anchor='{anchor}'{fam}>{esc(s)}</text>")

    def lines(self, x, y, rows, size=11, fill=MUTED, lh=14, anchor="start"):
        for i, r in enumerate(rows):
            self.text(x, y + i * lh, r, size, fill, anchor=anchor)

    def box(self, x, y, w, h, title, rows=(), stroke=INK, fill="#f8fafc", title_fill=None, r=8):
        self.parts.append(f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='{r}' fill='{fill}' stroke='{stroke}' stroke-width='1.5'/>")
        self.text(x + 12, y + 22, title, 13, title_fill or stroke, "700")
        self.lines(x + 12, y + 40, rows)

    def arrow(self, x1, y1, x2, y2, color=LINE, label=None, dash=None, both=False, lx=None, ly=None, width=1.6):
        m = {LINE: "arr", BLE: "arrb", RF: "arrr", POWER: "arrp", DATA: "arrd"}.get(color, "arr")
        d = f" stroke-dasharray='{dash}'" if dash else ""
        s = f" marker-start='url(#{m})'" if both else ""
        self.parts.append(f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' stroke='{color}' stroke-width='{width}'{d} marker-end='url(#{m})'{s}/>")
        if label:
            self.text(lx if lx is not None else (x1 + x2) / 2, ly if ly is not None else (y1 + y2) / 2 - 6, label, 11, color, "600", "middle")

    def path(self, d, color=LINE, label=None, lx=0, ly=0, dash=None, width=1.6, arrow=True):
        m = {LINE: "arr", BLE: "arrb", RF: "arrr", POWER: "arrp", DATA: "arrd"}.get(color, "arr")
        dd = f" stroke-dasharray='{dash}'" if dash else ""
        me = f" marker-end='url(#{m})'" if arrow else ""
        self.parts.append(f"<path d='{d}' fill='none' stroke='{color}' stroke-width='{width}'{dd}{me}/>")
        if label:
            self.text(lx, ly, label, 11, color, "600", "middle")

    def yagi(self, x, y, label=None, color=RF, scale=1.0):
        # boom + elements, pointing right
        s = scale
        p = []
        p.append(f"<line x1='{x}' y1='{y}' x2='{x + 90 * s}' y2='{y}' stroke='{color}' stroke-width='3'/>")
        for i, (dx, hl) in enumerate([(0, 30), (16, 26), (32, 23), (48, 20), (64, 18), (80, 16)]):
            p.append(f"<line x1='{x + dx * s}' y1='{y - hl * s}' x2='{x + dx * s}' y2='{y + hl * s}' stroke='{color}' stroke-width='3'/>")
        self.parts += p
        if label:
            self.text(x + 45 * s, y + 46 * s, label, 11, color, "600", "middle")

    def panel(self, x, y, w, h, label=None, color=RF):
        self.parts.append(f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='4' fill='#fff7ed' stroke='{color}' stroke-width='2'/>")
        self.parts.append(f"<rect x='{x + w * 0.2}' y='{y + h * 0.2}' width='{w * 0.6}' height='{h * 0.6}' rx='3' fill='none' stroke='{color}' stroke-width='1.2'/>")
        if label:
            self.text(x + w / 2, y + h + 16, label, 11, color, "600", "middle")

    def beam(self, x, y, length, half_deg, color=RF):
        import math
        a = math.radians(half_deg)
        x2 = x + length
        dy = math.tan(a) * length
        self.parts.append(f"<path d='M{x},{y} L{x2},{y - dy} A{length},{length} 0 0 1 {x2},{y + dy} Z' fill='{color}' fill-opacity='0.12' stroke='{color}' stroke-opacity='0.5' stroke-dasharray='4,4'/>")

    def phone(self, x, y, label="Phone · Wavelens", rows=()):
        self.parts.append(f"<rect x='{x}' y='{y}' width='92' height='170' rx='12' fill='#ecfdf5' stroke='{PHONE}' stroke-width='2'/>")
        self.parts.append(f"<rect x='{x + 8}' y='{y + 14}' width='76' height='132' rx='4' fill='#ffffff' stroke='{PHONE}' stroke-width='1'/>")
        self.parts.append(f"<rect x='{x + 14}' y='{y + 22}' width='64' height='10' rx='2' fill='{PHONE}' fill-opacity='0.5'/>")
        for i in range(4):
            self.parts.append(f"<rect x='{x + 14}' y='{y + 40 + i * 24}' width='64' height='18' rx='2' fill='{PHONE}' fill-opacity='{0.12 + 0.05 * i}'/>")
        self.text(x + 46, y + 190, label, 12, PHONE, "700", "middle")
        self.lines(x + 46, y + 206, rows, 10, MUTED, 13, "middle")

    def note(self, x, y, w, rows, color=MUTED):
        h = 16 + 14 * len(rows)
        self.parts.append(f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='6' fill='#f3f4f6' stroke='{LINE}' stroke-width='1'/>")
        self.lines(x + 10, y + 18, rows, 11, color)

    def save(self, name):
        self.parts.append("</svg>")
        Path(name).write_text("\n".join(self.parts), encoding="utf-8")
        print("wrote", name)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&#39;")


# ---------------------------------------------------------------- 1. overview

def overview():
    d = D(980, 600, "Wavelens companion radios — how they connect")
    d.lines(24, 56, ["Every companion is a BLE peripheral speaking one line protocol. The phone is the central; what it hears goes",
                     "through the app's own signatures, alerts, history and map, and the phone can tell it where to listen."], 12, MUTED, 16)

    d.phone(60, 120, "Phone · Wavelens", ["central", "no network permission", "classifies, alerts, maps"])

    # BLE bus
    d.arrow(170, 205, 300, 205, BLE, "BLE GATT · notify ↑ / write ↓", both=True, lx=235, ly=192)
    d.lines(235, 222, ["one JSON line per hit", "band / channel commands back"], 10, BLE, 13, "middle")

    y = 90
    d.box(310, y, 300, 96, "ESP32 (S3 / C3 / classic, -U = u.FL antenna)",
          ["hears: 2.4 GHz Wi-Fi beacons, BLE adverts", "optional: RX5808 → 5.8 GHz video carriers", "honours: park on channels 1·6·11, mode wifi/ble"], RF)
    d.yagi(650, y + 48, "2.4 GHz Yagi / panel", RF, 0.9)
    d.arrow(610, y + 48, 648, y + 48, RF, width=2)

    y = 210
    d.box(310, y, 300, 96, "Raspberry Pi + RTL-SDR",
          ["hears: 315 / 433 / 915 MHz via rtl_433 (TPMS, remotes)", "+ monitor-mode USB Wi-Fi via Kismet", "relays what the tools decode; no parking"], DATA)
    d.yagi(650, y + 48, "log-periodic 400–1000 MHz", DATA, 0.9)
    d.arrow(610, y + 48, 648, y + 48, DATA, width=2)

    y = 330
    d.box(310, y, 300, 96, "Carputer (any Linux box, trunk or under seat)",
          ["same bridge script as the Pi", "as many SDRs and Wi-Fi adapters as USB ports", "12 V → DC-DC; BLE dongle if no built-in radio"], INK)
    d.panel(660, y + 20, 70, 56, "roof antenna farm", INK)
    d.arrow(610, y + 48, 658, y + 48, INK, width=2)

    y = 450
    d.box(310, y, 300, 96, "Arduino (not recommended)",
          ["needs an HM-10 for BLE and a radio module", "an ESP32 does both, for less, with Wi-Fi", "shown for completeness in arduino.svg"], MUTED, "#f9fafb")
    d.panel(660, y + 20, 70, 56, "5.8 GHz patch", MUTED)
    d.arrow(610, y + 48, 658, y + 48, MUTED, width=2)

    # BLE fan-out: a bus down to each companion
    d.path("M300,138 L300,498", BLE, arrow=False, width=1.2)
    for yy in (138, 258, 378, 498):
        d.path(f"M300,{yy} L310,{yy}", BLE, arrow=False, width=1.2)

    d.note(770, 100, 200, ["Which to build", "", "Wi-Fi hotspots + BLE: ESP32.", "Sub-GHz (TPMS, remotes): Pi + SDR.", "Everything at once: carputer.", "", "All of them: the antenna is", "the upgrade; the board is", "just a radio with a plug."])
    d.note(770, 290, 200, ["What none of them fix", "", "A directional antenna adds", "gain forward and loses it to", "the sides. It does not keep an", "oncoming car in range longer."], MUTED)
    d.text(24, 585, "companion/README.md has the protocol; the app pairs automatically under More → Companion radio.", 11, MUTED)
    d.save("overview.svg")


# ---------------------------------------------------------------- 2. ESP32 wiring

def esp32():
    d = D(980, 700, "ESP32 companion with an external directional antenna")
    d.lines(24, 56, ["Use a board with a u.FL / IPEX connector (ESP32-S3-WROOM-1U, ESP32-WROOM-32U, Seeed XIAO ESP32-C3).",
                     "Boards with only a PCB antenna cannot take an external one without surgery."], 12, MUTED, 16)

    # Board
    d.parts.append(f"<rect x='330' y='150' width='230' height='300' rx='10' fill='#f1f5f9' stroke='{INK}' stroke-width='2'/>")
    d.text(445, 176, "ESP32-S3-WROOM-1U dev board", 13, INK, "700", "middle")
    d.parts.append(f"<rect x='370' y='195' width='150' height='70' rx='4' fill='#cbd5e1' stroke='{INK}'/>")
    d.text(445, 225, "ESP32-S3-WROOM-1U", 11, INK, "600", "middle")
    d.text(445, 243, "module (shield can)", 10, MUTED, anchor="middle")
    d.parts.append(f"<circle cx='525' cy='205' r='5' fill='{RF}'/>")
    d.text(533, 223, "u.FL", 10, RF, "700")
    d.parts.append(f"<rect x='428' y='428' width='34' height='22' rx='3' fill='#cbd5e1' stroke='{INK}'/>")
    d.text(445, 466, "USB-C", 10, MUTED, anchor="middle")
    pins = [("GPIO23", "RX5808 CH1 data"), ("GPIO22", "RX5808 CH2 LE"), ("GPIO21", "RX5808 CH3 clock"), ("GPIO34", "RX5808 RSSI (ADC)"), ("3V3", "RX5808 V+"), ("GND", "RX5808 GND")]
    for i, (p, what) in enumerate(pins):
        yy = 290 + i * 22
        d.parts.append(f"<rect x='338' y='{yy - 8}' width='8' height='12' fill='{INK}'/>")
        d.text(352, yy + 2, p, 10, INK, "600", mono=True)
        d.text(300, yy + 2, what, 10, MUTED, anchor="end")

    # Antenna chain, laid out left to right along y = 160
    d.path("M525,205 C560,205 560,160 600,160", RF, width=2.2, arrow=False)
    d.parts.append(f"<rect x='600' y='150' width='22' height='20' rx='3' fill='#fff7ed' stroke='{RF}' stroke-width='1.5'/>")
    d.text(611, 190, "SMA bulkhead", 9, RF, "600", "middle")
    d.text(611, 203, "u.FL → SMA pigtail, 10–20 cm", 9, RF, anchor="middle")
    d.path("M622,160 L700,160", RF, width=3, arrow=False)
    d.text(661, 146, "short coax (LMR-240)", 10, RF, "600", "middle")
    d.yagi(715, 160, "", RF, 0.9)
    d.text(760, 218, "2.4 GHz directional:", 10, RF, "600", "middle")
    d.text(760, 231, "9–15 dBi Yagi or 8 dBi panel", 10, RF, anchor="middle")
    d.beam(797, 160, 120, 20)
    d.text(870, 128, "≈ 30–45° beam", 10, RF, "600", "middle")

    # Power
    d.path("M445,450 L445,520", POWER, width=2, arrow=False)
    d.box(380, 520, 130, 60, "USB 5 V", ["car adapter or the", "mount's USB port"], POWER, "#fef2f2")

    # Phone link
    d.phone(80, 200, "Phone · Wavelens", ["on the dash, charging"])
    d.arrow(330, 232, 172, 232, BLE, "BLE GATT", both=True, lx=251, ly=222)
    d.lines(251, 248, ["built-in radio; shares", "the external antenna"], 9, BLE, 12, "middle")

    # RX5808 option
    d.box(610, 300, 230, 110, "Optional: RX5808 5.8 GHz receiver", ["SPI-tuned analog video receiver,", "sweeps 5645–5945 MHz, reports carriers", "as DRONE · Possible. $10 module.", "RSSI pin 0–1.1 V → ADC (GPIO34)."], MUTED, "#f9fafb")
    d.panel(860, 318, 60, 50, "5.8 GHz patch", MUTED)
    d.arrow(840, 343, 858, 343, MUTED, width=2)
    for i, yy in enumerate((290, 312, 334, 356)):
        d.path(f"M560,{yy} C590,{yy} 590,{320 + i * 8} 610,{320 + i * 8}", MUTED, arrow=False, width=1, dash="3,3")

    d.note(24, 470, 320, [
        "Antenna notes",
        "",
        "• The ESP32 transmits on the same antenna it sniffs with,",
        "  so a high-gain Yagi also narrows the BLE link to the phone.",
        "  Keep the phone roughly in the beam, or use the second",
        "  u.FL on dual-antenna boards for BLE.",
        "• Power limit: esp_wifi_set_max_tx_power(8) (2 dBm) keeps",
        "  EIRP legal with a 15 dBi antenna.",
        "• Mag-mount on the roof pointing forward; coax through",
        "  the door seal; board inside, out of the sun.",
    ])
    d.save("esp32-directional.svg")


# ---------------------------------------------------------------- 3. Pi + SDR

def pi():
    d = D(980, 640, "Raspberry Pi + RTL-SDR with directional antennas")
    d.lines(24, 56, ["The Pi runs rtl_433 (sub-GHz decoders) and optionally Kismet (monitor-mode Wi-Fi);",
                     "wavelens_bridge.py relays both to the phone over the Pi's own Bluetooth."], 12, MUTED, 16)

    px, py = 310, 150
    d.parts.append(f"<rect x='{px}' y='{py}' width='230' height='170' rx='10' fill='#f1f5f9' stroke='{DATA}' stroke-width='2'/>")
    d.text(px + 105, py + 26, "Raspberry Pi 4 / 5", 13, DATA, "700", "middle")
    d.lines(px + 16, py + 48, ["Raspberry Pi OS (Bookworm)", "rtl_433 · Kismet · BlueZ 5.x", "python3-bluezero", "companion/pi/wavelens_bridge.py"], 11, INK, 15)
    for i in range(4):
        d.parts.append(f"<rect x='{px + 206}' y='{py + 18 + i * 30}' width='18' height='20' rx='2' fill='#cbd5e1' stroke='{INK}'/>")
    d.text(px + 215, py + 150, "USB", 9, MUTED, anchor="middle")
    d.parts.append(f"<rect x='{px + 95}' y='{py + 170}' width='40' height='14' rx='2' fill='#cbd5e1' stroke='{INK}'/>")

    # SDR chain (top USB port)
    d.arrow(px + 224, py + 28, 600, py + 28, DATA, width=2)
    d.box(600, py, 140, 56, "RTL-SDR Blog v4", ["24–1766 MHz, 2.4 MS/s"], DATA)
    d.path(f"M740,{py + 28} L780,{py + 28}", RF, width=3, arrow=False)
    d.box(780, py, 64, 56, "LNA", ["optional", "+20 dB"], MUTED, "#f9fafb")
    d.path(f"M844,{py + 28} L876,{py + 28}", RF, width=3, arrow=False)
    d.yagi(880, py + 28, "", RF, 0.55)
    d.text(905, py + 80, "log-periodic 400–1000 MHz", 10, RF, "600", "middle")
    d.text(905, py + 93, "315 / 433 / 915 MHz", 10, RF, anchor="middle")

    # Wi-Fi adapter chain (third USB port)
    d.arrow(px + 224, py + 88 + 30, 600, py + 118, DATA, width=2)
    d.box(600, py + 90, 140, 56, "USB Wi-Fi adapter", ["monitor mode (mt76 / rtl8812au)"], DATA)
    d.path(f"M740,{py + 118} L780,{py + 118}", RF, width=3, arrow=False)
    d.panel(782, py + 100, 46, 36, "2.4 / 5 GHz panel", RF)

    # BLE to phone
    d.phone(80, 150, "Phone · Wavelens", ["on the dash"])
    d.arrow(px, 235, 172, 235, BLE, "BLE (Pi built-in radio)", both=True, lx=241, ly=223)

    # Power
    d.path(f"M{px + 115},{py + 184} L{px + 115},400", POWER, width=2, arrow=False)
    d.text(px + 125, py + 205, "USB-C 5 V / 3 A", 10, POWER, "600")
    d.box(px + 35, 400, 160, 56, "12 V → 5 V 3 A buck", ["fused from the accessory circuit"], POWER, "#fef2f2")

    # Software flow
    d.box(40, 400, 260, 150, "Data flow on the Pi", [], INK, "#ffffff")
    rows = [
        ("rtl_433 -F json", "sub-GHz frames → JSON"),
        ("kismet → devices.json", "Wi-Fi APs, monitor adapter"),
        ("wavelens_bridge.py", "Wavelens lines + cat/conf"),
        ("BlueZ GATT peripheral", "notify, 180-byte chunks"),
    ]
    for i, (a_, b_) in enumerate(rows):
        yy = 440 + i * 24
        d.text(52, yy, a_, 10, DATA, "700", mono=True)
        d.text(170, yy, b_, 10, MUTED)
        if i < 3:
            d.arrow(90, yy + 5, 90, yy + 14, LINE, width=1)

    d.note(540, 330, 420, [
        "Why this build",
        "",
        "• Sub-GHz is where TPMS, key fobs, wireless sensors and LoRa live;",
        "  no phone hears any of it.",
        "• Kismet on a monitor-mode adapter sees every channel it is parked",
        "  on, including 5 GHz hidden-SSID hotspots, with no OS throttling.",
        "• A HackRF (1 MHz–6 GHz) on another port adds hackrf_sweep: energy",
        "  peaks in the 900 MHz, 1.2 GHz, 2.4 GHz and 5.8 GHz bands.",
        "• The bridge does not classify beyond a hint; the app's signature",
        "  pack still has the last word.",
        "• Antennas: a log-periodic covers 400–1000 MHz in one unit; a",
        "  2.4/5 GHz panel for the Wi-Fi adapter. Point both forward.",
    ])
    d.save("pi-sdr-directional.svg")


# ---------------------------------------------------------------- 4. carputer

def carputer():
    d = D(980, 680, "Carputer: a Linux box, an antenna farm, one bridge")
    d.lines(24, 56, ["Same software as the Pi build, on whatever runs Linux in the car — a mini PC under the seat,",
                     "an Intel NUC, a Pi 5 in a case. Scales by USB port."], 12, MUTED, 16)

    d.note(24, 92, 300, [
        "When a carputer makes sense",
        "",
        "• Two or more SDRs: one parked on 315/433 MHz for",
        "  TPMS and remotes, one on 915 MHz for LoRa / ISM.",
        "• A monitor-mode adapter parked on the 5 GHz channel",
        "  a body-cam hotspot uses — every beacon, no throttling.",
        "• A HackRF for hackrf_sweep: 1 MHz–6 GHz energy peaks.",
        "• Logging raw captures locally (never to the phone).",
        "",
        "Only want Wi-Fi + BLE with a better antenna? The ESP32",
        "build is a tenth of the effort.",
    ])
    d.note(656, 92, 300, [
        "Mounting and RF hygiene",
        "",
        "• Antennas forward, on the roof, with a ground plane",
        "  (the roof is one). Keep them 20 cm+ apart.",
        "• Short coax: every 3 m of RG-58 at 2.4 GHz costs ~3 dB,",
        "  which cancels a cheap Yagi's gain.",
        "• A 433 MHz LPDA is big; a 915 MHz one is about 30 cm.",
        "• Fuse the DC-DC. Ignition-switched so it never flattens",
        "  the battery.",
    ])

    # Roof antenna farm (above the cabin)
    d.parts.append(f"<rect x='300' y='316' width='220' height='14' rx='3' fill='#cbd5e1' stroke='{INK}'/>")
    d.text(410, 348, "mag-mount / NMO plate with bulkhead SMAs", 10, MUTED, anchor="middle")
    d.yagi(306, 290, "", RF, 0.5)
    d.text(328, 274, "2.4 GHz Yagi", 10, RF, "600", "middle")
    d.panel(380, 276, 34, 28, "", RF)
    d.text(397, 262, "5 GHz panel", 10, RF, "600", "middle")
    d.yagi(436, 292, "", DATA, 0.45)
    d.text(470, 274, "400–1000 MHz LPDA", 10, DATA, "600", "middle")
    d.parts.append(f"<line x1='505' y1='316' x2='505' y2='284' stroke='{INK}' stroke-width='3'/>")
    d.parts.append(f"<circle cx='505' cy='280' r='5' fill='{INK}'/>")
    d.text(520, 290, "GPS puck (optional)", 10, MUTED)

    # Car, side view, forward = right
    d.parts.append(f"<path d='M60,430 L120,430 L190,330 L560,330 L660,430 L880,430 L880,500 L60,500 Z' fill='#f3f4f6' stroke='{INK}' stroke-width='2'/>")
    for cx in (200, 760):
        d.parts.append(f"<circle cx='{cx}' cy='500' r='30' fill='#e5e7eb' stroke='{INK}' stroke-width='2'/>")
    d.text(900, 470, "→ forward", 11, MUTED)

    # Coax runs along the roof and down the C-pillar into the trunk
    for x, c in ((326, RF), (397, RF), (448, DATA)):
        d.path(f"M{x},330 L{x},338 L570,338 L650,436", c, width=1.8, arrow=False)
    d.text(690, 350, "coax bundle through", 10, MUTED)
    d.text(690, 362, "the hatch seal / C-pillar", 10, MUTED)

    # Phone on the dash, inside the cabin
    d.parts.append(f"<rect x='522' y='348' width='22' height='38' rx='4' fill='#ecfdf5' stroke='{PHONE}' stroke-width='1.5'/>")
    d.text(533, 404, "phone on the dash", 10, PHONE, "600", "middle")

    # Trunk box and the stack under the car
    d.box(660, 440, 210, 52, "Mini PC / NUC / Pi 5 (trunk)", ["Linux · rtl_433 · Kismet · bridge"], INK, "#ffffff")
    d.arrow(660, 466, 546, 372, BLE, dash="4,3", width=1.5)
    d.text(600, 440, "BLE", 10, BLE, "600", "middle")
    d.path("M765,492 L765,540", LINE, width=1.5, arrow=False)
    d.box(620, 540, 290, 60, "USB hub (powered)", ["RTL-SDR ×2 (433 + 915 MHz) · HackRF (optional)", "USB Wi-Fi monitor adapter · BLE 5 dongle"], DATA, "#eff6ff")
    d.box(620, 612, 290, 44, "12 V accessory → DC-DC, fused, ignition-switched", [], POWER, "#fef2f2")
    d.arrow(765, 600, 765, 612, POWER, width=1.5)
    d.save("carputer.svg")


# ---------------------------------------------------------------- 5. arduino

def arduino():
    d = D(980, 680, "Arduino variant — shown for completeness; build the ESP32 instead")
    d.lines(24, 56, ["An Arduino has no radio. To be a companion it needs a BLE module for the link and a radio module for the",
                     "listening; an ESP32 has both on one $8 board and also sniffs Wi-Fi."], 12, MUTED, 16)

    d.parts.append(f"<rect x='330' y='150' width='230' height='250' rx='10' fill='#f1f5f9' stroke='{MUTED}' stroke-width='2'/>")
    d.text(445, 176, "Arduino Nano / Uno", 13, INK, "700", "middle")
    d.text(445, 194, "(5 V logic: level-shift to 3.3 V modules)", 10, MUTED, anchor="middle")
    pins = [("D2 / D3", "HM-10 TX / RX (SoftwareSerial)"), ("D10", "RX5808 CH1 data"), ("D11", "RX5808 CH2 LE"), ("D12", "RX5808 CH3 clock"), ("A0", "RX5808 RSSI"), ("3V3 / GND", "both modules")]
    for i, (p, what) in enumerate(pins):
        yy = 230 + i * 24
        d.parts.append(f"<rect x='338' y='{yy - 8}' width='8' height='12' fill='{INK}'/>")
        d.text(352, yy + 2, p, 10, INK, "600", mono=True)
        d.text(315, yy + 2, what, 10, MUTED, anchor="end")

    # HM-10 link to the phone
    d.box(620, 160, 180, 80, "HM-10 BLE 4.0 (CC2541)", ["UART ↔ BLE bridge", "service UUID set via AT+UUID"], BLE)
    d.path("M560,230 L620,230", BLE, width=1.5, arrow=False)
    d.text(590, 220, "UART", 9, BLE, anchor="middle")
    d.phone(860, 150, "Phone", ["Wavelens"])
    d.arrow(800, 200, 858, 200, BLE, both=True)

    # RX5808 and its patch antenna
    d.box(620, 270, 180, 90, "RX5808 5.8 GHz", ["SPI-tuned video receiver,", "RSSI out; sweep 5645–5945 MHz", "→ 5.8 GHz video carriers only"], RF)
    for yy in (278, 302, 326, 350):
        d.path(f"M560,{yy} L620,{yy}", RF, width=1, arrow=False, dash="3,3")
    d.path("M710,360 L710,384", RF, width=3, arrow=False)
    d.panel(685, 384, 50, 40, "5.8 GHz patch · 14 dBi", RF)
    d.beam(735, 404, 70, 20)

    d.box(400, 430, 150, 50, "USB 5 V", ["car adapter"], POWER, "#fef2f2")
    d.path("M475,400 L475,430", POWER, width=2, arrow=False)

    d.note(24, 500, 600, [
        "Why it is the worst option",
        "",
        "• The HM-10 cannot run the GATT service the app expects without AT-command setup, and its",
        "  20-byte notifies make the link slow.",
        "• No Wi-Fi, no BLE scanning: the Arduino hears only what the extra module decodes (5.8 GHz carriers here).",
        "• An ESP32-S3-WROOM-1U does all of this plus Wi-Fi and BLE sniffing, with a u.FL for the antenna,",
        "  for less money and one sketch.",
        "",
        "If you already own the parts: wire as shown, speak the protocol in companion/README.md over the HM-10,",
        "and it pairs like any other companion.",
    ])
    d.save("arduino.svg")


# ---------------------------------------------------------------- 6. vehicle mount

def vehicle():
    d = D(980, 620, "Mounting a directional antenna on the vehicle")
    d.lines(24, 56, ["Top view. A directional antenna trades all-round coverage for reach in one direction.",
                     "Forward is almost always the right direction: it is where fixed hardware appears first."], 12, MUTED, 16)

    cx, cy = 300, 390
    # body and cabin
    d.parts.append(f"<rect x='{cx - 60}' y='{cy - 130}' width='120' height='260' rx='40' fill='#f3f4f6' stroke='{INK}' stroke-width='2'/>")
    d.parts.append(f"<rect x='{cx - 48}' y='{cy - 80}' width='96' height='130' rx='14' fill='#e5e7eb' stroke='{INK}' stroke-width='1'/>")
    d.text(cx + 90, cy - 120, "forward ▲", 11, MUTED)
    # Yagi on the roof front edge, pointing forward (up)
    d.parts.append(f"<line x1='{cx}' y1='{cy - 20}' x2='{cx}' y2='{cy - 100}' stroke='{RF}' stroke-width='3'/>")
    for i, hl in enumerate((22, 19, 16, 14, 12)):
        yy = cy - 30 - i * 16
        d.parts.append(f"<line x1='{cx - hl}' y1='{yy}' x2='{cx + hl}' y2='{yy}' stroke='{RF}' stroke-width='3'/>")
    d.path(f"M{cx + 24},{cy - 62} L{cx + 70},{cy - 62}", RF, width=1, arrow=False, dash="2,3")
    d.text(cx + 75, cy - 58, "Yagi, mag-mount, roof front", 10, RF, "600")
    # beam
    d.parts.append(f"<path d='M{cx},{cy - 100} L{cx - 55},{cy - 235} A140,140 0 0 1 {cx + 55},{cy - 235} Z' fill='{RF}' fill-opacity='0.12' stroke='{RF}' stroke-opacity='0.5' stroke-dasharray='4,4'/>")
    d.text(cx, cy - 250, "≈ 30–45° · 2–3× the range of the phone", 10, RF, "600", "middle")
    # sides and rear
    d.text(cx - 130, cy, "weak", 10, MUTED, anchor="middle")
    d.text(cx + 150, cy + 100, "weak", 10, MUTED, anchor="middle")
    d.text(cx, cy + 152, "behind: much weaker — a tail is the phone's job, not the Yagi's", 10, MUTED, anchor="middle")
    # phone, companion, coax
    d.parts.append(f"<rect x='{cx - 11}' y='{cy - 12}' width='22' height='16' rx='3' fill='#ecfdf5' stroke='{PHONE}' stroke-width='1.5'/>")
    d.path(f"M{cx + 12},{cy - 4} L{cx + 70},{cy - 4}", PHONE, width=1, arrow=False, dash="2,3")
    d.text(cx + 75, cy, "phone on the dash", 10, PHONE, "600")
    d.parts.append(f"<rect x='{cx - 12}' y='{cy + 28}' width='24' height='16' rx='2' fill='#fff7ed' stroke='{RF}'/>")
    d.path(f"M{cx + 12},{cy + 36} L{cx + 70},{cy + 36}", RF, width=1, arrow=False, dash="2,3")
    d.text(cx + 75, cy + 40, "companion under the seat", 10, RF, "600")
    d.path(f"M{cx},{cy - 20} C{cx + 30},{cy - 10} {cx + 30},{cy + 20} {cx + 2},{cy + 28}", RF, width=1.5, arrow=False, dash="3,3")
    d.text(cx + 75, cy + 20, "coax via the door seal", 10, MUTED)

    d.note(520, 120, 430, [
        "Choosing the antenna",
        "",
        "2.4 GHz (Wi-Fi + BLE)     9–15 dBi Yagi, or 8 dBi panel. Yagi is longer and narrower;",
        "                          the panel is flatter and forgiving of aim.",
        "5 GHz (body-cam hotspots) 14 dBi panel; Yagis are fiddly at 5 GHz.",
        "315 / 433 / 915 MHz        log-periodic (LPDA). 400–1000 MHz covers all three.",
        "5.8 GHz (video)            14 dBi patch on the RX5808; tiny.",
        "",
        "Gain is in dBi: every +3 dB roughly +40% range. 12 dBi ≈ 4× the",
        "power of the phone's antenna in the beam, and ≈ none out of it.",
    ])
    d.note(520, 310, 430, [
        "Rules of thumb",
        "",
        "• Roof, not dash: glass and the roof line eat 2.4 GHz. A mag-mount on",
        "  the steel roof gives the antenna its ground plane for free.",
        "• Short coax. RG-58 loses ~1 dB per metre at 2.4 GHz; LMR-240 half that.",
        "• Aim slightly up (5–10°): poles and cameras are 4–6 m high.",
        "• Keep the phone in the beam if the ESP32 shares the antenna for BLE,",
        "  or give BLE its own stub antenna on dual-u.FL boards.",
        "• Transmit power down (esp_wifi_set_max_tx_power) with high-gain",
        "  antennas: the EIRP limit still applies to a sniffer that advertises.",
    ])
    d.save("vehicle-mount.svg")


if __name__ == "__main__":
    overview(); esp32(); pi(); carputer(); arduino(); vehicle()
