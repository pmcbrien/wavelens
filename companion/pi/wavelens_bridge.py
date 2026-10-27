#!/usr/bin/env python3
"""
Wavelens companion bridge — Raspberry Pi / carputer / any Linux box with BlueZ.

Turns the machine into the BLE peripheral the Wavelens app looks for, and relays
whatever the radios attached to it produce. Feeds can be combined; add your own by
writing Wavelens lines (see ../README.md) to stdin.

  rtl_433 -F json | ./wavelens_bridge.py --rtl433        # RTL-SDR: 433/315/915 MHz decoders (TPMS, remotes, sensors)
  ./wavelens_bridge.py --hackrf-sweep                    # HackRF: whole 1 MHz–6 GHz range, every carrier labelled by band
  ./wavelens_bridge.py --rtl-power 300M:1000M:50k        # RTL-SDR: the same idea below 1.7 GHz
  ./wavelens_bridge.py --rtl-power 150M:174M:12.5k --rtl-power 806M:815M:12.5k --rtl-power-device 00001090
  ./wavelens_bridge.py --rtl-power 24M:895M:250k@00000978 --rtl-power 895M:1766M:250k@00001090
                                                         # two dongles splitting the whole RTL-SDR range
                                                         # several ranges take turns on one dongle; pin it by serial
  ./wavelens_bridge.py --dump1090 localhost:30003        # ADS-B aircraft from dump1090 / readsb (SBS port)
  ./wavelens_bridge.py --kismet-json /tmp/kismet.json    # Kismet export from a monitor-mode Wi-Fi adapter
  ./wavelens_bridge.py --fcc-pack fcc-local.json ...     # label licensed carriers by name (see ../tools/fcc_local_pack.py)
  ./wavelens_bridge.py                                   # raw: stdin already in Wavelens line format

Several at once:  rtl_433 -F json | ./wavelens_bridge.py --rtl433 --hackrf-sweep --dump1090 localhost:30003

Needs: python3-bluezero (pip install bluezero), BlueZ 5.50+, the adapter powered, and
whichever of rtl_433 / hackrf_sweep / rtl_power / dump1090 you use on PATH. Receive only:
nothing here ever transmits. Status: written against the documented APIs and CSV formats;
not yet run on a Pi by the author.
"""

import argparse
from collections import deque
import bisect
import json
import math
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from queue import Queue, Empty

SVC = "57617665-6c65-6e73-0100-000000000001"
TX = "57617665-6c65-6e73-0100-000000000002"
RX = "57617665-6c65-6e73-0100-000000000003"
FW = "wavelens-bridge 0.1"

out: "Queue[str]" = Queue(maxsize=256)
state = {"linked": False, "band": "all", "mode": "rf", "ch": "hop", "park": "off", "lat": None, "lon": None, "pos_at": 0.0}


ECHO = False      # --echo: also print every line sent to the phone (watch with journalctl -f)


def emit(obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    if ECHO:
        print("sent " + line.rstrip(), file=sys.stderr)
    try:
        out.put_nowait(line)
    except Exception:
        pass  # full: drop, the next packet will come


CAPS = ["rf"]


def hello() -> None:
    emit({"t": "hello", "fw": FW, "name": state.get("name", "Wavelens-Pi"), "code": state.get("code", ""), "caps": sorted(set(CAPS))})


# ---------------------------------------------------------------- local FCC license pack

class FccPack:
    """Frequency → licensee lookup built by ../tools/fcc_local_pack.py.

    A hit at 453.21 MHz becomes "Licensed: Example County Sheriff's Office · WQZZ001 · public
    safety" with a category hint from the licensee's name and radio service. When the phone
    has shared its position (opt-in), the nearest licensed site's distance is included and
    the nearest licensee wins when several share a frequency.
    """

    def __init__(self, path: str):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.entries = d.get("entries", [])
        self.keys = [e["mhz"] for e in self.entries]           # sorted by the builder
        self.area = d.get("area", {})

    def lookup(self, mhz: float, tol_khz: float = 12.5):
        """Entries whose assigned frequency (or band) covers mhz ± tol. Nearest-first when a position is known."""
        lo = bisect.bisect_left(self.keys, mhz - 5.0)
        hits = []
        for e in self.entries[lo:]:
            if e["mhz"] > mhz + 5.0:
                break
            half = max(e.get("bw_khz") or 12.5, tol_khz) / 2000.0
            if e.get("mhz_hi"):
                ok = e["mhz"] - half <= mhz <= e["mhz_hi"] + half
            else:
                ok = abs(e["mhz"] - mhz) <= half
            if ok:
                hits.append(e)
        lat, lon = state.get("lat"), state.get("lon")
        if lat is not None and lon is not None and time.time() - state.get("pos_at", 0) < 120:
            def dist(e):
                ds = [haversine_km(lat, lon, la, lo_) for la, lo_ in e.get("sites", [])]
                return min(ds) if ds else 1e9
            hits.sort(key=dist)
            return [(e, dist(e)) for e in hits]
        return [(e, None) for e in hits]

    def label(self, mhz: float, tol_khz: float = 12.5):
        """(name, cat, conf, extras) for a carrier, or None when nothing is licensed there."""
        hits = self.lookup(mhz, tol_khz)
        if not hits:
            return None
        # A mobile/handheld (station class MO…) frequency is a repeater input: radios transmit there
        # toward the tower, and a low-power handheld is only audible from close by. Prefer that reading.
        inputs = [(h, dd) for h, dd in hits if is_input(h)]
        e, d = (inputs or hits)[0]
        who = e['name'] or e['call']
        if is_input(e):
            name = f"Radio transmitting nearby · {who} repeater input · {e['call']}"
        else:
            name = f"Licensed: {who} · {e['call']} · {e['service_label']}"
        x = {"licensee": e["name"], "call_sign": e["call"], "service": e["service_label"], "place": e.get("place", "")}
        if is_input(e):
            x["repeater_input"] = "a mobile or handheld transmitting up to its repeater — heard only from close by"
        if d is not None and d < 1e8:
            x["site_km"] = f"{d:.1f}"
        if len(hits) > 1:
            x["other_licensees"] = "; ".join(f"{h['name'] or h['call']} ({h['call']})" for h, _ in hits[1:4])
        return name, e.get("cat") or "UNKNOWN", "LIKELY", x


def is_input(e: dict) -> bool:
    """FCC station class MO, MO3 … = mobile / portable units; on a repeater pair that is the input side."""
    return str(e.get("class", "")).upper().startswith("MO")


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


FCC: "FccPack | None" = None
HACKRF_ON = False


# ---------------------------------------------------------------- spectrum sweeps (HackRF, rtl_power)

# What a carrier in each part of 1 MHz–6 GHz most likely is, from the US frequency allocation
# table. Energy only: a sweep sees that something is on the air, never what it says, so every
# label is a hint (POSSIBLE) and nothing is demodulated. The phone treats these as hints only.
#
# First match wins, so narrow entries come before the wide bands they sit in.
#   kind ""         report it (1 MHz buckets, at most every 2 s each)
#   kind "persist"  report only once it is over the floor on two passes in a row (video links are
#                   continuous; a rotating radar or a glitch shows once)
#   kind "bg"       background: broadcast, tower and satellite downlinks, fixed navigation beacons and
#                   radars. Everywhere, so one entry per band at most once a minute
#   steady=(...)    the same band means two different things: a carrier still there after STEADY_N passes
#                   in a row gets the steady label, one that comes and goes gets the burst label
#   name None       skipped (2.4 / 5 GHz Wi-Fi and Bluetooth: everything is there)
def B(lo, hi, name, cat="UNKNOWN", conf="POSSIBLE", kind="", steady=None):
    return {"lo": lo, "hi": hi, "name": name, "cat": cat, "conf": conf, "kind": kind, "steady": steady,
            "key": f"{lo:g}-{hi:g}"}


# The website and README list these; after changing BANDS run: python3 companion/tools/band_table.py
BANDS = [
    # ---- skipped outright
    B(2400, 2500, None),                              # 2.4 GHz Wi-Fi / BLE / microwave ovens
    B(5150, 5644, None),                              # 5 GHz Wi-Fi (also airport weather radar 5600–5650)
    B(5926, 6000, None),                              # Wi-Fi 6E
    B(1215, 1240, None),                              # GPS L2 + long-range air-traffic radar bursts
    B(1300, 1390, None),                              # long-range air-traffic and military radar
    B(1400, 1427, None),                              # radio astronomy: protected, nothing should transmit

    # ---- narrow, specific
    B(121.4, 121.6, "Aviation emergency frequency 121.5 MHz (ELT beacon or guard call)"),
    B(242.9, 243.1, "Military aviation emergency frequency 243.0 MHz (beacon or guard call)"),
    B(406.0, 406.1, "406 MHz distress beacon (EPIRB / PLB / ELT)"),
    B(161.95, 162.05, "AIS: ship or boat position transponder nearby"),
    B(144.37, 144.41, "APRS 144.39 MHz: amateur position beacon"),
    B(462.54, 462.74, "Walkie-talkie nearby (FRS / GMRS)"),
    B(467.54, 467.74, "Walkie-talkie nearby (FRS / GMRS)"),
    B(978, 978.5, "Aircraft ADS-B (978 MHz UAT): small plane nearby"),
    B(1029.5, 1030.5, "Secondary radar interrogation (1030 MHz): airport or military radar nearby"),
    B(1089.5, 1090.5, "Aircraft transponder (1090 MHz, Mode S / ADS-B) nearby"),
    B(26.96, 27.41, "CB radio transmitting nearby (trucks, CB users)", "VEHICLE"),

    # ---- unlicensed Part 15 / ISM devices
    B(314, 316, "315 MHz burst (TPMS, key fob)", "VEHICLE"),
    B(299, 319, "Key fob or garage remote (300–318 MHz)", "VEHICLE"),
    B(389.5, 390.5, "Garage remote (390 MHz)", "VEHICLE"),
    B(433.05, 434.79, "433 MHz ISM burst (remotes, sensors, TPMS)", "IOT"),
    B(902, 928, "900 MHz ISM burst (LoRa, telemetry, meters)", "IOT",
      steady=("Steady 900 MHz carrier: toll gantry or RFID / gate reader", "TRAFFIC_SENSOR", "POSSIBLE")),
    B(863, 870, "868 MHz ISM burst (EU LoRa, sensors)", "IOT"),
    B(49.8, 49.9, "49 MHz baby monitor, toy or old cordless phone", "IOT"),
    B(72, 76, "72 MHz radio-control model (RC aircraft)", "DRONE"),

    # ---- video and drone links
    B(5645, 5849, "5.8 GHz carrier (FPV video, or Wi-Fi ch 149+)", "DRONE", kind="persist"),
    B(5850, 5925, "5.9 GHz burst: connected-vehicle radio (C-V2X roadside unit or vehicle)", "VEHICLE",
      steady=("5.9 GHz steady carrier: FPV video on a high channel", "DRONE", "POSSIBLE")),
    B(5030, 5091, "Drone command-link band (5030–5091 MHz, aviation UAS control)", "DRONE", kind="persist"),
    B(5091, 5150, "Airport surface network (AeroMACS)", "NETWORK"),
    B(1240, 1300, "1.2 GHz carrier: video or telemetry link (also ham 23 cm)", "DRONE", kind="persist"),
    B(2025, 2110, "News / broadcast video link (2 GHz: ENG trucks, news helicopters)", "CAMERA", kind="persist"),
    B(4940, 4990, "4.9 GHz public-safety link (camera / video backhaul, mesh)", "NETWORK", kind="persist"),

    # ---- aircraft
    B(4200, 4400, "Radio altimeter: aircraft flying low overhead (helicopter, plane on approach, large drone)"),
    B(118, 137, "Aircraft or airport radio (VHF air band)"),
    B(108, 118, "Aviation navigation beacon (VOR / ILS)", kind="bg"),
    B(960, 1215, "Aviation navigation (DME / TACAN beacons)", kind="bg"),
    B(225, 400, "Military / government UHF: military aviation, tactical radio, SATCOM"),
    B(1435, 1525, "Flight-test / aeronautical telemetry (L-band)"),
    B(2360, 2395, "Flight-test / aeronautical telemetry (S-band)"),
    B(2200, 2290, "Federal telemetry: space operations, flight test, military UAV links"),

    # ---- phones and satellite terminals transmitting (uplinks: a strong burst is a transmitter nearby,
    #      very often your own phone)
    B(663, 698, "Phone transmitting nearby (600 MHz LTE uplink)", "PHONE"),
    B(699, 716, "Phone transmitting nearby (700 MHz LTE uplink)", "PHONE"),
    B(777, 787, "Phone transmitting nearby (700 MHz LTE uplink)", "PHONE"),
    B(788, 798, "Phone or public-safety LTE (FirstNet) transmitting nearby", "PHONE"),
    B(799, 805, "Radio keying up nearby (700 MHz public-safety mobile)"),
    B(806, 815, "Radio keying up nearby (800 MHz repeater input)"),
    B(815, 824, "Phone transmitting nearby (LTE band 26 uplink)", "PHONE"),
    B(824, 849, "Phone transmitting nearby (850 MHz cellular uplink)", "PHONE"),
    B(1710, 1780, "Phone transmitting nearby (AWS LTE uplink)", "PHONE"),
    B(1850, 1920, "Phone transmitting nearby (PCS uplink)", "PHONE"),
    B(2000, 2020, "Phone transmitting nearby (AWS-4 uplink)", "PHONE"),
    B(2305, 2315, "Phone transmitting nearby (WCS uplink)", "PHONE"),
    B(1610, 1626.5, "Satellite phone or tracker transmitting nearby (Globalstar / Iridium)", "PHONE"),
    B(1626.5, 1660.5, "Satellite terminal transmitting nearby (Inmarsat: maritime, aviation)", "PHONE"),

    # ---- two-way radio, by band
    B(156, 162.05, "Marine VHF radio (boats, harbors)"),
    B(160.2, 161.6, "Railroad radio"),
    B(162.4, 162.55, "NOAA Weather Radio", kind="bg"),
    B(162.05, 174, "Federal government VHF"),
    B(138, 144, "Federal / military VHF"),
    B(148, 150.8, "Federal / military VHF"),
    B(150.8, 174, "VHF two-way radio (public safety, business)"),
    B(144, 148, "Amateur radio (2 m)"),
    B(219, 225, "Amateur radio (1.25 m) / telemetry"),
    B(30, 50, "VHF low-band two-way radio"),
    B(50, 54, "Amateur radio (6 m)"),
    B(400, 406, "Weather balloon (radiosonde) or federal telemetry"),
    B(406.1, 420, "Federal government UHF"),
    B(420, 450, "Amateur radio (70 cm) / military radar"),
    B(450, 470, "UHF two-way radio (public safety, business)"),
    B(608, 614, "Hospital medical telemetry (WMTS)", "IOT"),
    B(769, 775, "700 MHz public-safety radio (base / repeater)", kind="bg"),
    B(896, 902, "900 MHz business radio / private LTE"),
    B(935, 941, "900 MHz business radio / private LTE", kind="bg"),
    B(1390, 1400, "Hospital medical telemetry (WMTS)", "IOT"),
    B(1427, 1435, "Hospital medical telemetry (WMTS)", "IOT"),
    B(1780, 1850, "Federal / military (1780–1850 MHz: tactical links, satellite control)"),
    B(4400, 4940, "Federal / military microwave (4.4–4.94 GHz)"),

    # ---- radars
    B(2700, 2900, "Airport or weather radar (S-band)", kind="bg"),
    B(2900, 3100, "Ship radar (S-band)"),
    B(3100, 3450, "Military radar (S-band)"),

    # ---- background: broadcast, towers, satellites — everywhere, reported once a minute per band
    B(1, 1.7, "AM broadcast", kind="bg"),
    B(1.7, 30, "HF: shortwave, amateur, maritime, aviation", kind="bg"),
    B(54, 88, "TV broadcast (VHF low)", kind="bg"),
    B(88, 108, "FM broadcast", kind="bg"),
    B(137, 138, "Weather / low-orbit satellite downlink", kind="bg"),
    B(174, 216, "TV broadcast (VHF high)", kind="bg"),
    B(470, 608, "TV broadcast (UHF)", kind="bg"),
    B(614, 652, "Cell tower (600 MHz downlink)", "CELL_SITE", kind="bg"),
    B(716, 768, "Cell tower (700 MHz downlink)", "CELL_SITE", kind="bg"),
    B(851, 869, "800 MHz tower output (regional, not nearby)", kind="bg"),
    B(869, 894, "Cell tower (850 MHz downlink)", "CELL_SITE", kind="bg"),
    B(929, 932, "Paging transmitter", kind="bg"),
    B(941, 960, "Fixed point-to-point links / paging", kind="bg"),
    B(1525, 1559, "Mobile satellite downlink (Inmarsat, Ligado)", kind="bg"),
    B(1660.5, 1710, "Weather satellite / radiosonde downlink", kind="bg"),
    B(1920, 2000, "Cell tower (PCS downlink)", "CELL_SITE", kind="bg"),
    B(2110, 2200, "Cell tower (AWS downlink)", "CELL_SITE", kind="bg"),
    B(2315, 2345, "Satellite radio (SiriusXM) and its street repeaters", kind="bg"),
    B(2345, 2360, "Cell tower (WCS downlink)", "CELL_SITE", kind="bg"),
    B(2496, 2690, "5G (2.5 GHz band 41): tower or phone", "CELL_SITE", kind="bg"),
    B(3450, 3550, "Cell tower (3.45 GHz 5G)", "CELL_SITE", kind="bg"),
    B(3550, 3700, "CBRS: private LTE / 5G, fixed wireless", "NETWORK", kind="bg"),
    B(3700, 3980, "Cell tower (C-band 5G)", "CELL_SITE", kind="bg"),
    B(3980, 4200, "Satellite C-band downlink", kind="bg"),
]


# GNSS: satellite signals arrive far below the noise floor, so a wide signal that rises clearly above
# it is local — most often a GPS jammer, sometimes a GPS repeater (hangars, garages) or interference.
# (band lo, band hi, judged lo, judged hi, label). The whole band is swept for the floor; only the part
# around the centre frequency (± 12 MHz, which every common jammer covers) is judged, which also keeps
# clear of the sweep's edges. GLONASS (1598–1606) and the Galileo/BeiDou edges are floor only.
GNSS = [
    (1559e6, 1610e6, 1563e6, 1587e6, "GPS band (L1)"),
    (1164e6, 1189e6, 1166e6, 1187e6, "GPS band (L5)"),
]
SETTLE_S = 2.0       # ignore rtl_power's first rows after it opens the dongle: gain and PLL still settling

PERSIST = {"DRONE"}  # these categories must be over the floor on two passes in a row before they're reported
STEADY_N = 3         # passes in a row over the floor before a carrier counts as steady
CHAIN_S = 30.0       # a bin's run of hot passes breaks if it isn't revisited within this long
BG_RATE_S = 60.0     # background bands: at most one report per band per minute
FLOOR_S = 40.0       # rolling floor time constant (a carrier that stays this long becomes the floor)
FLOOR_UP_GNSS_S = 1200.0   # ... rising in the GNSS bands, so a jammer riding with you stays visible

# Parked: the HackRF stops sweeping 1 MHz–6 GHz and stays on one 20 MHz slice, so a short transmission
# there is caught on the pass it happens instead of within a second. "le" is the body-cam-and-radio
# preset the phone sends: 796–816 MHz, where public-safety handhelds and car radios transmit (700 MHz
# public-safety mobile 799–805, 800 MHz repeater inputs 806–815). Energy only: "a radio keyed up
# nearby", never who or what was said. Set another slice with --hackrf-park-range.
PARK_RANGES = {"le": "796:816"}


_band_cache = {}


def band_of(mhz: float):
    """The BANDS entry for a frequency, or a generic one for anything not in the table."""
    k = round(mhz * 1000)
    b = _band_cache.get(k)
    if b is None:
        for e in BANDS:
            if e["lo"] <= mhz <= e["hi"]:
                b = e
                break
        else:
            b = {"lo": mhz, "hi": mhz, "name": f"{mhz:.1f} MHz carrier", "cat": "UNKNOWN", "conf": "POSSIBLE",
                 "kind": "", "steady": None, "key": "other"}
        _band_cache[k] = b
    return b


def gnss_window(hz: float):
    for w in GNSS:
        if w[0] <= hz <= w[1]:
            return w
    return None


class Sweep:
    """Parses hackrf_sweep / rtl_power CSV rows and reports bins that rise above a rolling floor.

    Row format (both tools): date, time, hz_low, hz_high, hz_bin_width, samples, dB, dB, dB, ...
    """

    def __init__(self, src: str, threshold_db: float = 18.0, rate_s: float = 2.0, bucket_mhz: float = 1.0):
        self.src, self.th, self.rate, self.bucket = src, threshold_db, rate_s, bucket_mhz
        self.floor = {}       # bin start Hz → EMA of dB
        self.last = {}        # bucket → last report time
        self.gps_prev = {}    # GNSS row start (MHz) → last time that row looked jammed
        self.settle_until = 0.0
        self.seen = {}        # bin Hz → time of the previous visit
        self.runs = {}        # bin Hz → [passes in a row over the floor, last dB, bin width]

    def row(self, line: str) -> None:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            return
        try:
            lo, hi, step = float(parts[2]), float(parts[3]), float(parts[4])
            vals = [float(v) for v in parts[6:] if v]
        except ValueError:
            return
        now = time.time()
        if now < self.settle_until:
            return
        gnss_rows = {}      # GNSS window → [bins judged, hot bins]
        for i, db in enumerate(vals):
            hz = lo + i * step
            prev_visit = self.seen.get(hz)
            self.seen[hz] = now
            f = self.floor.get(hz)
            if f is None:
                self.floor[hz] = db
                continue
            # slow floor: a carrier that stays for minutes becomes the floor, which is what we want for a drive.
            # Not in the GNSS bands: there the true floor is the empty band, and a jammer riding with you must
            # not fade into it, so the floor rises ~30x more slowly there (and falls at the normal rate).
            # Time-based, so the floor moves at the same speed whether a pass takes a second (full sweep)
            # or a few milliseconds (parked): about 40 s to follow a change, ~20 min upward in GNSS.
            w = gnss_window(hz)
            dt = min(max((now - prev_visit) if prev_visit is not None else 1.2, 0.001), 30.0)
            k = min(1.0, dt / (FLOOR_UP_GNSS_S if (w and db > f) else FLOOR_S))
            self.floor[hz] = f * (1 - k) + db * k
            if w:
                if not w[2] <= hz <= w[3]:
                    continue                 # GNSS but outside the judged window: floor only, never reported
                # Judged per row below: a jammer is wide and steady, so one hot bin is not enough.
                g = gnss_rows.setdefault(w, [0, []])
                g[0] += 1
                if db - f >= self.th:
                    g[1].append((hz, db, db - f))
                continue
            mhz = hz / 1e6
            if db - f < self.th:
                run = self.runs.pop(hz, None)       # quiet on this pass: the run of hot passes is over
                if run and run[0] < STEADY_N:
                    b = band_of(mhz)
                    if b["steady"] and not (FCC and FCC.label(mhz, tol_khz=max(step / 1000.0, 12.5))):
                        # it came and went: report it now with the burst label
                        self.report(mhz, run[1], step, run[1] - f, b["name"], b["cat"], b["conf"], b, None, now, {})
                continue
            run = self.runs.get(hz)
            if run and prev_visit is not None and 0.2 < now - prev_visit <= CHAIN_S:
                run[0] += 1
                run[1] = db
            elif not run or prev_visit is None or now - prev_visit > CHAIN_S:
                run = self.runs[hz] = [1, db, step]
            n = run[0]
            x = {"over_floor_db": f"{db - f:.0f}", "bin_hz": f"{step:.0f}"}
            lic = FCC.label(mhz, tol_khz=max(step / 1000.0, 12.5)) if FCC else None
            if lic:
                name, cat, conf, lx = lic
                x.update(lx)
                if cat in PERSIST and n < 2:
                    continue
                self.report(mhz, db, step, db - f, name, cat, conf, None, f"fcc/{lx['call_sign']}/{mhz:.3f}", now, x)
                continue
            b = band_of(mhz)
            if b["name"] is None:
                continue
            if b["steady"]:
                if n < STEADY_N:
                    continue                 # wait: steady label at STEADY_N passes, burst label when it drops
                name, cat, conf = b["steady"]
                x["steady"] = f"over the floor on {n} passes in a row"
            else:
                name, cat, conf = b["name"], b["cat"], b["conf"]
                if (b["kind"] == "persist" or cat in PERSIST) and n < 2:
                    continue                 # a video link transmits continuously; a radar sweep or a glitch shows once
            self.report(mhz, db, step, db - f, name, cat, conf, b, None, now, x)
        for w, (n, hot) in gnss_rows.items():
            self.gps_row(lo, step, n, hot, now, w[4])

    def report(self, mhz, db, step, over, name, cat, conf, b, ident, now, x) -> None:
        if not x:
            x = {"over_floor_db": f"{over:.0f}", "bin_hz": f"{step:.0f}"}
        if b is not None and b["kind"] == "bg":
            key, rate = "bg/" + b["key"], BG_RATE_S
            ident = ident or f"{self.src}/bg/{b['key']}"
            x["background"] = "broadcast, tower or beacon: everywhere, shown for completeness"
        else:
            bucket = round(mhz / self.bucket)
            key, rate = (bucket, cat if b is not None and b["steady"] and cat == b["steady"][1] else ""), self.rate
            ident = ident or f"{self.src}/{bucket * self.bucket:.0f}"
        if now - self.last.get(key, 0) < rate:
            return
        self.last[key] = now
        if b is not None:
            x.setdefault("band", f"{b['lo']:g}–{b['hi']:g} MHz")
        emit({"t": "rf", "id": ident, "n": name, "f": int(round(mhz)), "r": int(round(db)),
              "cat": cat, "conf": conf, "src": self.src, "x": x})

    def gps_row(self, lo: float, step: float, n: int, hot: list, now: float, label: str = "GPS band (L1)") -> None:
        """Possible GPS jammer only when a wide part of this row is raised (at least a quarter of its
        bins, and 3+) and the same row looked that way on the previous pass too. A single narrow
        spike — interference, a retune glitch — is ignored."""
        key = round(lo / 1e6)
        wide = len(hot) >= max(3, n // 4)
        if not wide:
            self.gps_prev.pop(key, None)
            return
        prev = self.gps_prev.get(key, 0.0)
        self.gps_prev[key] = now
        if not prev or now - prev > 90:
            return                                  # first pass that looks jammed: wait for a second
        if now - self.last.get("gps/" + label, 0) < self.rate:
            return
        self.last["gps/" + label] = now
        overs = sorted(o for _, _, o in hot)
        mid = sum(h for h, _, _ in hot) / len(hot) / 1e6
        emit({"t": "rf", "id": f"{self.src}/gps" + ("" if "L1" in label else "-l5"), "n": f"{label}: strong local signal — possible GPS jammer nearby",
              "f": int(round(mid)), "r": int(round(max(d for _, d, _ in hot))), "cat": "TOOL", "conf": "POSSIBLE",
              "src": self.src, "x": {
                  "over_floor_db": f"{overs[len(overs) // 2]:.0f}",
                  "width_mhz": f"{len(hot) * step / 1e6:.1f}",
                  "raised_bins": f"{len(hot)} of {n}, on two passes in a row",
                  "gps": ("satellites are far below the noise floor, so a wide signal this strong is local — "
                          "usually a jammer, sometimes a GPS repeater. Check whether the phone's location "
                          "is drifting or dropping out.")}})


def spawn(cmd: str):
    """Start a radio tool with stdout piped. Its stderr is drained on a thread (hackrf_sweep and
    rtl_power write progress there constantly) and the last few lines kept, so that when the tool
    exits early the journal says why. Returns the process, or None if the tool isn't installed."""
    try:
        p = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        print(f"bridge: {cmd.split()[0]} not found on PATH", file=sys.stderr)
        return None
    tail = deque(maxlen=4)

    def drain():
        for ln in p.stderr:
            if ln.strip():
                tail.append(ln.strip())
    threading.Thread(target=drain, daemon=True).start()
    p.started_at, p.err_tail = time.time(), tail
    return p


def reap(p, cmd: str) -> None:
    """Wait for a tool; if it lasted under 10 s, log what it said, since that is a failure, not a sweep."""
    p.wait()
    if time.time() - p.started_at < 10:
        time.sleep(0.2)                         # let the drain thread catch the last lines
        why = " | ".join(p.err_tail) or "no message"
        print(f"bridge: {cmd.split()[0]} exited after {time.time() - p.started_at:.1f} s (code {p.returncode}): {why}",
              file=sys.stderr)


def run_once(cmd: str, handler) -> bool:
    """Run a tool until it exits, handing it each stdout line. False if it isn't installed."""
    p = spawn(cmd)
    if p is None:
        return False
    for line in p.stdout:
        handler(line)
    reap(p, cmd)
    return True


def feed_process(cmd: str, handler) -> None:
    """Run a sweep tool and hand it each stdout line; restart it if it exits."""
    while run_once(cmd, handler):
        time.sleep(2)


def feed_hackrf(range_mhz: str, bin_hz: int) -> None:
    """hackrf_sweep over range_mhz, or over the parked slice while the phone has asked to park.
    A park change stops the running sweep and starts the other one."""
    sw = Sweep("hackrf")
    while True:
        park = state.get("park", "off")
        rng = PARK_RANGES.get(park, range_mhz)
        cmd = f"hackrf_sweep -f {rng} -w {bin_hz} -l 32 -g 20"
        p = spawn(cmd)
        if p is None:
            return
        print(f"bridge: hackrf_sweep {rng} MHz" + (f" (parked: {park})" if park in PARK_RANGES else ""), file=sys.stderr)

        def watch(proc=p, was=park):
            while proc.poll() is None:
                if state.get("park", "off") != was:
                    proc.terminate()
                    return
                time.sleep(0.2)

        threading.Thread(target=watch, daemon=True).start()
        for line in p.stdout:
            sw.row(line)
        reap(p, cmd)
        if state.get("park", "off") == park:
            time.sleep(2)                    # it exited by itself (unplugged, error): retry like feed_process


def _mhz(v: str) -> float:
    v = v.strip().upper()
    mult = {"K": 1e-3, "M": 1.0, "G": 1e3}.get(v[-1:], 1e-6)
    return float(v.rstrip("KMG")) * mult


def sweep_interval_s(spec: str) -> int:
    """rtl_power wants its integration interval to cover one full pass of the range
    ("buggy if a full sweep takes longer than the interval"). A hop covers ~2 MHz and takes
    roughly 30 ms, so a 50 MHz range gets 1 s and the full 24–1766 MHz about 26 s."""
    try:
        lo, hi = spec.split(":")[:2]
        hops = abs(_mhz(hi) - _mhz(lo)) / 2.0
    except (ValueError, IndexError):
        return 1
    return max(1, math.ceil(hops * 0.03))


def feed_rtl_power(specs: "list[str]", device: str = "", dwell_s: int = 15) -> None:
    """One range: rtl_power runs continuously. Several: they take turns on the same dongle,
    dwell_s seconds each (rtl_power -e), so VHF and 800 MHz can share one RTL-SDR.
    device is an index or serial (rtl_power -d matches either). Each dongle gets its own thread."""
    sw = Sweep("rtlpower")          # one floor map keyed by Hz, so the ranges don't disturb each other
    dev = f" -d {shlex.quote(device)}" if device else ""
    if len(specs) == 1:
        cmd = f"rtl_power -f {specs[0]}{dev} -i {sweep_interval_s(specs[0])} -g 30 -"
        while True:
            sw.settle_until = time.time() + SETTLE_S + sweep_interval_s(specs[0])
            if not run_once(cmd, sw.row):
                return
            time.sleep(2)
        return
    while True:
        for spec in specs:
            i = sweep_interval_s(spec)
            sw.settle_until = time.time() + SETTLE_S + i      # first integration after a retune is discarded
            if not run_once(f"rtl_power -f {spec}{dev} -i {i} -g 30 -e {max(dwell_s, 2 * i)}s -", sw.row):
                return
            time.sleep(0.5)         # let the dongle close before the next range opens it


def group_sweeps(specs: "list[str]", default_device: str) -> "dict[str, list[str]]":
    """'24M:895M:250k@00000978' pins that range to a dongle; ranges without @ use --rtl-power-device."""
    groups: "dict[str, list[str]]" = {}
    for s in specs:
        spec, _, dev = s.partition("@")
        groups.setdefault(dev or default_device, []).append(spec)
    return groups


# ---------------------------------------------------------------- ADS-B (dump1090 / readsb, SBS BaseStation port 30003)

def feed_dump1090(hostport: str) -> None:
    host, _, port = hostport.partition(":")
    port = int(port or 30003)
    seen = {}
    while True:
        try:
            with socket.create_connection((host, port), timeout=10) as sk:
                f = sk.makefile("r", encoding="utf-8", errors="replace")
                for line in f:
                    p = line.strip().split(",")
                    if len(p) < 22 or p[0] != "MSG":
                        continue
                    icao = p[4].strip().lower()
                    if not icao:
                        continue
                    now = time.time()
                    if now - seen.get(icao, 0) < 5:
                        continue
                    seen[icao] = now
                    callsign = p[10].strip()
                    x = {}
                    if p[11]: x["alt_ft"] = p[11]
                    if p[12]: x["gs_kt"] = p[12]
                    if p[14] and p[15]: x["lat"], x["lon"] = p[14], p[15]
                    if p[17]: x["squawk"] = p[17]
                    emit({"t": "rf", "id": f"adsb/{icao}", "n": f"aircraft {callsign or icao.upper()}",
                          "f": 1090, "r": -60, "src": "adsb", "x": x})
        except OSError as e:
            print(f"bridge: dump1090 {hostport}: {e}; retrying", file=sys.stderr)
            time.sleep(5)


# ---------------------------------------------------------------- feeds

# rtl_433 model names → what they probably are. Anything else lands as unclassified RF
# with the model as its name, which is still useful on the map.
RTL433_CAT = {
    "TPMS": ("VEHICLE", "LIKELY"),
    "Schrader": ("VEHICLE", "LIKELY"),
    "Toyota": ("VEHICLE", "LIKELY"),
    "Ford": ("VEHICLE", "LIKELY"),
    "Citroen": ("VEHICLE", "LIKELY"),
    "Abarth": ("VEHICLE", "LIKELY"),
    "Renault": ("VEHICLE", "LIKELY"),
    "Hyundai": ("VEHICLE", "LIKELY"),
    "PMV": ("VEHICLE", "LIKELY"),
    "Elantra": ("VEHICLE", "LIKELY"),
    "Acurite": ("IOT", "LIKELY"),
    "Oregon": ("IOT", "LIKELY"),
    "LaCrosse": ("IOT", "LIKELY"),
    "Ambient": ("IOT", "LIKELY"),
    "Fineoffset": ("IOT", "LIKELY"),
    "Honeywell": ("IOT", "LIKELY"),
    "DSC": ("IOT", "LIKELY"),
    "Interlogix": ("IOT", "LIKELY"),
    "Generic-Remote": ("BEACON", "POSSIBLE"),
}


def feed_rtl433(line: str) -> None:
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return
    model = str(d.get("model", "rtl_433"))
    ident = str(d.get("id", d.get("sensor_id", d.get("channel", ""))))
    cat, conf = ("UNKNOWN", "POSSIBLE")
    for k, v in RTL433_CAT.items():
        if k.lower() in model.lower():
            cat, conf = v
            break
    if "tpms" in model.lower() or "pressure" in d:
        cat, conf = ("VEHICLE", "LIKELY")
    freq_f = d.get("freq") or d.get("freq1")
    try:
        freq_f = float(freq_f) if freq_f else 0.0
    except (TypeError, ValueError):
        freq_f = 0.0
    freq = int(freq_f)
    rssi = d.get("rssi")
    try:
        rssi = int(float(rssi))
    except (TypeError, ValueError):
        rssi = -70
    x = {k: str(v) for k, v in d.items() if k in ("pressure_kPa", "pressure_PSI", "temperature_C", "battery_ok", "mic", "type")}
    if FCC and cat == "UNKNOWN" and freq_f:
        lic = FCC.label(freq_f)
        if lic:
            _, cat, conf, lx = lic
            x.update(lx)
    emit({"t": "rf", "id": f"{model}/{ident}", "n": model, "f": freq, "r": rssi, "cat": cat, "conf": conf, "src": "rtl433", "x": x})


def feed_kismet_json(path: str) -> None:
    """Re-reads a Kismet 'devices' JSON export every few seconds and relays Wi-Fi APs."""
    seen = {}
    while True:
        try:
            with open(path) as f:
                devs = json.load(f)
        except Exception:
            devs = []
        now = time.time()
        for d in devs if isinstance(devs, list) else []:
            if d.get("kismet.device.base.phyname") != "IEEE802.11":
                continue
            mac = d.get("kismet.device.base.macaddr", "").lower()
            if len(mac) != 17 or now - seen.get(mac, 0) < 2:
                continue
            seen[mac] = now
            ssid = d.get("kismet.device.base.name", "") or ""
            ch = d.get("kismet.device.base.channel", "0")
            try:
                ch = int(str(ch).split(".")[0])
            except ValueError:
                ch = 0
            rssi = d.get("kismet.device.base.signal", {}).get("kismet.common.signal.last_signal", -70)
            emit({"t": "wifi", "id": mac, "n": ssid, "r": int(rssi), "ch": ch, "src": "kismet"})
        time.sleep(3)


def feed_stdin(mode: str) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if mode == "rtl433":
            feed_rtl433(line)
        else:
            if line.startswith("{"):
                out.put(line + "\n")
    if mode == "rtl433":
        # rtl_433 has exited (dongle busy, unplugged, wrong serial). Don't keep a Bluetooth link up
        # with a dead radio behind it: exit so the service restarts both.
        print("bridge: rtl_433 stopped sending; exiting so the service restarts it", file=sys.stderr)
        os._exit(1)


# ---------------------------------------------------------------- BLE peripheral

def blink(secs: int) -> None:
    """Identify: flash the Pi's activity LED (needs root) and say so on the console either way."""
    secs = max(1, min(30, secs))
    print(f"{FW}: identify — blinking for {secs} s (code {state.get('code', '?')})", file=sys.stderr)
    led = next((p for p in ("/sys/class/leds/ACT", "/sys/class/leds/led0") if os.path.isdir(p)), None)
    if not led:
        return
    try:
        with open(f"{led}/trigger", "w") as f:
            f.write("none")
        end = time.time() + secs
        on = False
        while time.time() < end:
            on = not on
            with open(f"{led}/brightness", "w") as f:
                f.write("1" if on else "0")
            time.sleep(0.15)
        with open(f"{led}/trigger", "w") as f:
            f.write("mmc0")
    except OSError:
        pass


def run_peripheral(name: str) -> None:
    from bluezero import adapter, peripheral
    from bluezero import advertisement as bz_ad

    def ad_failed(error):
        # Without the advert the phone can't find the companion, while the radios keep running:
        # a half-alive bridge. Exit so systemd restarts it (5 s later, once BlueZ has let go).
        print(f"bridge: Bluetooth advertisement failed ({error}); exiting so the service retries", file=sys.stderr)
        os._exit(1)
    bz_ad.register_ad_error_cb = ad_failed      # bluezero looks this name up when it registers

    dongle = list(adapter.Adapter.available())[0]
    code = dongle.address.replace(":", "")[-4:].upper()
    if name in ("", "Wavelens Pi", "Wavelens-Pi"):
        name = f"Wavelens-{code}"          # same scheme as the ESP32: the code is the end of the BT address
    state["name"], state["code"] = name, code
    dev = peripheral.Peripheral(dongle.address, local_name=name)
    dev.add_service(srv_id=1, uuid=SVC, primary=True)

    def on_rx(value, options):
        try:
            cmd = json.loads(bytes(value).decode("utf-8", "replace").strip() or "{}")
        except json.JSONDecodeError:
            return
        c = cmd.get("c")
        if c == "ping":
            hello()
            emit({"t": "status", "msg": "ok", "band": state["band"], "mode": state["mode"]})
        elif c == "blink":
            threading.Thread(target=blink, args=(int(cmd.get("v") or 5),), daemon=True).start()
            emit({"t": "status", "msg": "blinking", "band": state["band"], "mode": state["mode"]})
        elif c == "pos":
            # Opt-in from the phone: its GPS fix, so licensed sites can be ranked by distance.
            # Kept in memory only, never written anywhere, forgotten after two minutes.
            if cmd.get("lat") is None:
                state["lat"], state["lon"], state["pos_at"] = None, None, 0.0     # the phone turned sharing off
            else:
                try:
                    state["lat"], state["lon"] = float(cmd["lat"]), float(cmd["lon"])
                    state["pos_at"] = time.time()
                except (KeyError, TypeError, ValueError):
                    pass
        elif c == "park":
            v = str(cmd.get("v") or "off")
            state["park"] = v if v in PARK_RANGES else "off"
            if state["park"] != "off" and not HACKRF_ON:
                msg = "park noted; no HackRF on this bridge (add --hackrf-sweep)"
            elif state["park"] != "off":
                msg = f"parked: HackRF on {PARK_RANGES[state['park']].replace(':', '–')} MHz (radios keying up nearby)"
            else:
                msg = "sweeping the full range"
            emit({"t": "status", "msg": msg, "band": state["band"], "mode": state["mode"], "park": state["park"]})
        elif c in ("band", "mode", "ch"):
            state[c] = cmd.get("v")
            # An SDR bridge has nothing to park; say so once rather than pretend.
            emit({"t": "status", "msg": f"{c} noted; this bridge relays what rtl_433/kismet give it", "band": state["band"], "mode": state["mode"]})

    chars = {}

    def notify_cb(notifying, characteristic):
        chars["tx"] = characteristic if notifying else None
        state["linked"] = bool(notifying)
        if notifying:
            hello()

    dev.add_characteristic(srv_id=1, chr_id=1, uuid=TX, value=[], notifying=False,
                           flags=["notify"], notify_callback=notify_cb)
    dev.add_characteristic(srv_id=1, chr_id=2, uuid=RX, value=[], notifying=False,
                           flags=["write", "write-without-response"], write_callback=on_rx)

    def pump():
        while True:
            try:
                line = out.get(timeout=0.5)
            except Empty:
                continue
            tx = chars.get("tx")
            if tx is None:
                continue
            data = line.encode("utf-8")
            for i in range(0, len(data), 180):      # under a 247-byte MTU; the app reassembles on newline
                try:
                    tx.set_value(list(data[i:i + 180]))
                except Exception:
                    break
                time.sleep(0.005)

    threading.Thread(target=pump, daemon=True).start()
    print(f"{FW}: advertising as {name!r} on {dongle.address}  (pair code {code})", file=sys.stderr)
    # systemd stops the service with SIGTERM. bluezero only withdraws its advert on Ctrl+C, so a
    # restart would leave BlueZ holding the old advert and the next start fails to register one.
    # Withdraw it ourselves; GLib's own signal hook, because the GLib main loop owns the thread.
    try:
        import signal
        from gi.repository import GLib

        def on_term():
            try:
                mgr, adv = getattr(dev, "ad_manager", None), getattr(dev, "advert", None)
                if mgr is not None and adv is not None:
                    mgr.unregister_advertisement(adv)
            except Exception as e:                      # already gone, or BlueZ restarting
                print(f"bridge: advert not withdrawn ({e})", file=sys.stderr)
            os._exit(0)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, on_term)
    except Exception as e:
        print(f"bridge: no SIGTERM hook ({e})", file=sys.stderr)
    dev.publish()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rtl433", action="store_true", help="stdin is rtl_433 -F json")
    ap.add_argument("--hackrf-sweep", action="store_true", help="run hackrf_sweep and report carriers by band")
    ap.add_argument("--hackrf-range", default="1:6000", help="MHz range for hackrf_sweep (default 1:6000, the whole range)")
    ap.add_argument("--hackrf-bin", type=int, default=500000, help="bin width Hz (default 500000)")
    ap.add_argument("--hackrf-park-range", metavar="LO:HI", default="",
                    help="MHz slice the HackRF parks on when the phone asks (default 796:816, public-safety radios)")
    ap.add_argument("--rtl-power", metavar="SPEC", action="append", default=[],
                    help="run rtl_power with this -f spec, e.g. 150M:174M:12.5k; repeat for several ranges, which take turns; "
                         "add @SERIAL to put a range on a specific dongle (one sweep per dongle runs in parallel)")
    ap.add_argument("--rtl-power-device", metavar="N|SERIAL", default="",
                    help="which RTL-SDR rtl_power uses: index or serial (see rtl_test); default the first free one")
    ap.add_argument("--rtl-power-dwell", metavar="SECONDS", type=int, default=15,
                    help="seconds on each range when several --rtl-power ranges are given (default 15)")
    ap.add_argument("--dump1090", metavar="HOST:PORT", help="relay aircraft from a dump1090/readsb SBS port")
    ap.add_argument("--kismet-json", metavar="PATH", help="relay Wi-Fi devices from a Kismet JSON export")
    ap.add_argument("--fcc-pack", metavar="PATH", help="local FCC license pack from tools/fcc_local_pack.py; labels licensed carriers")
    ap.add_argument("--echo", action="store_true", help="print every line sent to the phone on stderr (see it with journalctl -u wavelens-bridge -f)")
    ap.add_argument("--name", default="", help="advertised name (default Wavelens-XXXX from the adapter address)")
    a = ap.parse_args()

    global FCC, ECHO, HACKRF_ON
    ECHO = a.echo
    if a.fcc_pack:
        FCC = FccPack(a.fcc_pack)
        CAPS.append("fcc")
        print(f"bridge: FCC pack loaded, {len(FCC.entries)} licensed frequencies", file=sys.stderr)

    if a.kismet_json:
        CAPS.append("wifi")
        threading.Thread(target=feed_kismet_json, args=(a.kismet_json,), daemon=True).start()
    if a.hackrf_park_range:
        PARK_RANGES["le"] = a.hackrf_park_range
    if a.hackrf_sweep:
        HACKRF_ON = True
        CAPS.append("sweep")
        CAPS.append("park")
        threading.Thread(target=feed_hackrf, args=(a.hackrf_range, a.hackrf_bin), daemon=True).start()
    if a.rtl_power:
        CAPS.append("sweep")
        for dev, specs in group_sweeps(a.rtl_power, a.rtl_power_device).items():
            threading.Thread(target=feed_rtl_power, args=(specs, dev, max(3, a.rtl_power_dwell)), daemon=True).start()
    if a.dump1090:
        CAPS.append("adsb")
        threading.Thread(target=feed_dump1090, args=(a.dump1090,), daemon=True).start()
    if a.rtl433 or not sys.stdin.isatty():
        threading.Thread(target=feed_stdin, args=("rtl433" if a.rtl433 else "raw",), daemon=True).start()
    run_peripheral(a.name)


if __name__ == "__main__":
    main()
