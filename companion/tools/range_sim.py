#!/usr/bin/env python3
"""
Range and warning-time simulation for Wavelens, with and without a companion radio.

A link-budget model, not a measurement: log-distance path loss with a free-space
reference at 1 m, a path-loss exponent per scenario, stated antenna gains, receiver
sensitivities and losses. The point is to make the assumptions explicit and the
arithmetic reproducible, then let the Rule hits table in the app replace the numbers
with measured ones. Edit the tables below for your own antenna or speeds and rerun.

    python3 range_sim.py            # prints Markdown tables, writes range-sim.svg next to this file
    python3 range_sim.py --json     # machine-readable

Model:  d = 10 ** ((Ptx + Gtx + Grx - Lrx - PL0(f) - S) / (10 n))   metres, median, in the beam
        PL0(f) = 20 log10(f_MHz) - 27.55                            free-space loss at 1 m
        d <= 4120 * (sqrt(h_tx) + sqrt(h_rx))                       radio horizon, heights in m
        warning = d / v - latency                                   seconds before you reach it

The ranges are medians with line of sight in the antenna's beam. Reliable range (nine
passes in ten) is roughly half: budget 10 dB of fading and clutter. Trees, trucks and a
pole behind a building take more.
"""

import argparse
import json
import math
from pathlib import Path

# ---------------------------------------------------------------- receivers
# name: (gain dBi, extra loss dB, sensitivity dBm by radio kind)
# Phone: internal antenna behind glass and roof line, body/car loss folded into Lrx.
# ESP32 companion: 12 dBi Yagi on the roof, 1 dB of coax; ESP32 BLE ~ -97 dBm, Wi-Fi 1 Mbps beacon ~ -98 dBm.
# RTL-SDR + LPDA (10 dBi): NF ~ 5 dB with a cheap LNA, 12.5 kHz channel, 10 dB SNR to decode → ~ -118 dBm;
#   rtl_433 in practice decodes TPMS down to about -105 dBm, so that is used.
# HackRF sweep: NF ~ 15 dB, 1 MHz bins, 18 dB over the floor to report → about -81 dBm.
RECEIVERS = {
    "phone":   {"gain": -2.0, "loss": 6.0,  "h": 1.0, "sens": {"ble": -94, "wifi": -90}},
    "esp32":   {"gain": 12.0, "loss": 1.0,  "h": 1.6, "sens": {"ble": -97, "wifi": -96}},
    "rtlsdr":  {"gain": 10.0, "loss": 1.0,  "h": 1.6, "sens": {"subghz": -100, "licensed": -112}},
    "rx5808":  {"gain": 14.0, "loss": 0.5,  "h": 1.6, "sens": {"video": -90}},
    "hackrf":  {"gain": 10.0, "loss": 1.0,  "h": 1.6, "sens": {"sweep": -81}},
}

# ---------------------------------------------------------------- targets
# (label, radio kind, f MHz, Ptx dBm, Gtx dBi, path-loss exponent, transmitter height m, receivers to compare)
# Exponents: 2.0 is free space; 2.5–2.7 is a roadside with cars and trees; 3.0 is a transmitter near the ground.
TARGETS = [
    ("BLE beacon on a pole (battery pack, accessory radio)", "ble",      2440, 0,  0, 2.6, 5.0,  ["phone", "esp32"]),
    ("Wi-Fi hotspot / AP (body cam, setup network)",          "wifi",     2440, 20, 2, 2.7, 2.0,  ["phone", "esp32"]),
    ("TPMS sensor / key fob, 315-433 MHz",                    "subghz",   433,  -5, -3, 3.0, 0.4,  ["rtlsdr"]),
    ("Remote / sensor / LoRa node, 433-915 MHz",              "subghz",   915,  10, 0, 2.8, 2.0,  ["rtlsdr"]),
    ("5.8 GHz FPV video, 200 mW, drone in the air",           "video",    5800, 23, 2, 2.2, 80.0, ["rx5808"]),
    ("5.8 GHz FPV video, 200 mW (HackRF sweep)",              "sweep",    5800, 23, 2, 2.2, 80.0, ["hackrf"]),
    ("Licensed handheld, 5 W",                                "licensed", 460,  37, 0, 3.0, 1.5,  ["rtlsdr"]),
    ("Licensed mobile, 25 W",                                 "licensed", 460,  44, 3, 2.8, 2.0,  ["rtlsdr"]),
    ("Licensed repeater on a tower, 100 W",                   "licensed", 460,  50, 6, 2.3, 60.0, ["rtlsdr"]),
]

LATENCY_S = {"phone": 0.7, "esp32": 0.9, "rtlsdr": 0.8, "rx5808": 1.2, "hackrf": 2.0}   # detect → tone, roughly
SPEEDS_MPH = [45, 70]


def pl0(f_mhz: float) -> float:
    return 20 * math.log10(f_mhz) - 27.55


def horizon_m(h_tx: float, h_rx: float) -> float:
    return 4120.0 * (math.sqrt(h_tx) + math.sqrt(h_rx))


def range_m(ptx, gtx, f, n, h_tx, rx, kind):
    """(metres, capped_by_horizon)"""
    r = RECEIVERS[rx]
    margin = ptx + gtx + r["gain"] - r["loss"] - pl0(f) - r["sens"][kind]
    d = 10 ** (margin / (10 * n))
    cap = horizon_m(h_tx, r["h"])
    return (cap, True) if d > cap else (d, False)


def fmt_m(d: float) -> str:
    if d >= 10_000:
        return f"{d / 1000:.0f} km"
    if d >= 1000:
        return f"{d / 1000:.1f} km"
    return f"{d:.0f} m"


def fmt_s(t: float) -> str:
    if t <= 0:
        return "none"
    return f"{t:.0f} s" if t >= 10 else f"{t:.1f} s"


def simulate():
    rows = []
    for label, kind, f, ptx, gtx, n, h_tx, rxs in TARGETS:
        for rx in rxs:
            d, capped = range_m(ptx, gtx, f, n, h_tx, rx, kind)
            lat = LATENCY_S[rx]
            w = {}
            for mph in SPEEDS_MPH:
                v = mph * 0.44704
                w[f"approach_{mph}"] = d / v - lat
                w[f"oncoming_{mph}"] = d / (2 * v) - lat
            rows.append({"target": label, "receiver": rx, "kind": kind, "range_m": d, "horizon": capped, **w})
    return rows


def markdown(rows) -> str:
    out = ["| Target | Receiver | Median range | Warning, you at 45 mph | at 70 mph | Oncoming, both at 70 |", "|---|---|---|---|---|---|"]
    for r in rows:
        rng = fmt_m(r["range_m"]) + (" (horizon)" if r["horizon"] else "")
        if r["kind"] == "licensed":
            out.append(f"| {r['target']} | {r['receiver']} | {rng} | regional — not a warning | | |")
        else:
            out.append(f"| {r['target']} | {r['receiver']} | {rng} | {fmt_s(r['approach_45'])} | {fmt_s(r['approach_70'])} | {fmt_s(r['oncoming_70'])} |")
    out.append("")
    out.append("Medians with line of sight in the beam; reliable range is roughly half. Riding with you or following you: unlimited — the closing speed is zero. "
               "A fixed site you have passed before: 10–15 s from a 300 m history geofence at any speed, with no radio needed (planned).")
    return "\n".join(out)


def svg(rows, path: Path) -> None:
    """Horizontal bars: range per target/receiver, log scale, phone vs companion highlighted."""
    rows = sorted(rows, key=lambda r: r["range_m"])
    w, h = 980, 70 + 30 * len(rows) + 60
    lo, hi = 10, 100_000
    x0, x1 = 420, 880
    def x(d): return x0 + (x1 - x0) * (math.log10(max(lo, min(hi, d))) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
    col = {"phone": "#0f766e", "esp32": "#b45309", "rtlsdr": "#1d4ed8", "rx5808": "#7c3aed", "hackrf": "#6b7280"}
    p = [f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {w} {h}' width='{w}' height='{h}' font-family='Inter, Helvetica, Arial, sans-serif'>",
         f"<rect width='{w}' height='{h}' fill='#fff'/>",
         "<text x='24' y='34' font-size='18' font-weight='700' fill='#1f2933'>Simulated detection range — phone vs companion radios</text>",
         "<text x='24' y='54' font-size='11' fill='#6b7280'>Median range with line of sight in the beam; reliable range is about half. Link budget in range_sim.py, not measurements. Log scale.</text>"]
    for tick in (10, 100, 1000, 10_000, 100_000):
        tx = x(tick)
        p.append(f"<line x1='{tx:.0f}' y1='66' x2='{tx:.0f}' y2='{h - 50}' stroke='#e5e7eb'/>")
        p.append(f"<text x='{tx:.0f}' y='{h - 36}' font-size='10' fill='#6b7280' text-anchor='middle'>{fmt_m(tick)}</text>")
    for i, r in enumerate(rows):
        y = 80 + i * 30
        c = col[r["receiver"]]
        p.append(f"<text x='410' y='{y + 13}' font-size='11' fill='#1f2933' text-anchor='end'>{r['target']}</text>")
        p.append(f"<rect x='{x0}' y='{y}' width='{max(2, x(r['range_m']) - x0):.0f}' height='18' rx='3' fill='{c}' fill-opacity='0.85'/>")
        tag = fmt_m(r["range_m"]) + (" · horizon" if r["horizon"] else "")
        p.append(f"<text x='{x(r['range_m']) + 6:.0f}' y='{y + 13}' font-size='11' fill='{c}' font-weight='600'>{tag} · {r['receiver']}</text>")
    lx = 24
    for k, c in col.items():
        p.append(f"<rect x='{lx}' y='{h - 24}' width='12' height='12' rx='2' fill='{c}'/>")
        p.append(f"<text x='{lx + 16}' y='{h - 14}' font-size='11' fill='#1f2933'>{k}</text>")
        lx += 90
    p.append("</svg>")
    path.write_text("\n".join(p), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--svg", default=str(Path(__file__).with_name("range-sim.svg")))
    a = ap.parse_args()
    rows = simulate()
    if a.json:
        print(json.dumps(rows, indent=1))
    else:
        print(markdown(rows))
    svg(rows, Path(a.svg))


if __name__ == "__main__":
    main()
