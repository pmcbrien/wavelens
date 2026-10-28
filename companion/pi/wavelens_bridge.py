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
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from queue import Queue, Empty

SVC = "57617665-6c65-6e73-0100-000000000001"
TX = "57617665-6c65-6e73-0100-000000000002"
RX = "57617665-6c65-6e73-0100-000000000003"
AU = "57617665-6c65-6e73-0100-000000000004"      # device → phone, notify: audio frames while listening
FW = "wavelens-bridge 0.1"

out: "Queue[str]" = Queue(maxsize=256)
aout: "Queue[bytes]" = Queue(maxsize=50)        # two seconds of audio; if the link can't keep up, frames drop
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
    if obj.get("t") == "rf" and obj.get("src") in ("hackrf", "rtlpower"):
        trunk_note(obj)
        if LISTEN.get("req") is not None:
            try:
                r = float(str((obj.get("x") or {}).get("exact_mhz", "")).split()[0])
                if abs(r - LISTEN["mhz"]) < 0.06:
                    LISTEN["carrier_at"] = time.time()
                    hone(r)
            except (ValueError, IndexError):
                pass
        if LISTEN.get("follow"):
            follow_hit(obj)


CAPS = ["rf"]


def hello() -> None:
    emit({"t": "hello", "fw": FW, "name": state.get("name", "Wavelens-Pi"), "code": state.get("code", ""), "caps": sorted(set(CAPS))})



# ---------------------------------------------------------------- RadioReference overlay (Lab)

# Agency names as the people who use them say them: "Hillsborough County Sheriff's Office" → HCSO,
# "Largo Police Department" → LPD, "Clearwater Fire Rescue" → CFR. Initials of the place words
# plus the office's usual suffix; applied to headline names only, the full text stays in the extras.
_PLACE = r"\b([A-Za-z][\w.&'-]*(?: [A-Za-z][\w.&'-]*){0,3}?)"
_ABBR = [   # FCC licensee names are often ALL CAPS, so these ignore case
    (re.compile(_PLACE + r" County Sheriff'?s? (?:Office|Department)\b", re.I), "CSO"),
    (re.compile(_PLACE + r" Sheriff'?s? (?:Office|Department)\b", re.I), "SO"),
    (re.compile(_PLACE + r" Police Department\b", re.I), "PD"),
    (re.compile(_PLACE + r" Fire Rescue\b", re.I), "FR"),
    (re.compile(_PLACE + r" Fire Department\b", re.I), "FD"),
    (re.compile(_PLACE + r" Emergency Medical Services\b", re.I), "EMS"),
    (re.compile(_PLACE + r" County Public Schools\b", re.I), "CPS"),
    (re.compile(_PLACE + r" Public Schools\b", re.I), "PS"),
]
_SKIP_WORDS = {"of", "the", "and", "de", "la", "at", "city", "town", "village"}


def abbrev(text: str) -> str:
    def initials(place: str) -> str:
        return "".join(w[0] for w in place.split() if w.lower() not in _SKIP_WORDS).upper()
    out = text
    for rx, suffix in _ABBR:
        out = rx.sub(lambda m: initials(m.group(1)) + suffix, out)
    return out


# Tag column of a RadioReference export → the agency-type labels the FCC pack uses (so the
# "keys up nearby" agency chips and the law-enforcement switch work from this table too).
RR_TAG_AGENCY = [
    (("law",), "Law enforcement"), (("corrections",), "Law enforcement"), (("fire",), "Fire / rescue"),
    (("ems",), "EMS / ambulance"), (("hospital",), "Hospital / medical"), (("schools", "school"), "School / campus"),
    (("aircraft", "airport"), "Airport"), (("public works",), "Local government"), (("security",), "Security company"),
    (("transportation", "transit"), "Transit / roads"), (("utilities", "utility"), "Utility"),
    (("federal", "military"), "Federal"), (("emergency ops", "emergency"), "911 / emergency management"),
    (("interop", "multi-"), "Public safety"), (("railroad",), "Transit / roads"),
    (("ham",), "Amateur radio"), (("media",), "Business"), (("business",), "Business"),
]
# Mode column → how to listen (the app's Listen uses it instead of guessing from the band).
RR_LISTEN = {"fm": "fm", "fmn": "fm", "am": "am", "dmr": "dmr", "dmre": "dmr", "p25": "p25", "p25e": "p25",
             "nxdn48": "nxdn", "nxdn96": "nxdn", "nxdn": "nxdn", "project 25": "p25", "telm": "fm",   # telemetry/paging: FM, the pager decoder reads it
             "edacs": "trunked", "ltr": "trunked"}
RR_PUBLIC = ("county", "city", "sheriff", "police", "law", "state", "fire", "ems", "town", "village", "government")


class RrPack:
    """Frequency → what RadioReference says is there, from one or more of its CSV exports
    (columns: Frequency Output, Frequency Input, FCC Callsign, Agency/Category, Description, Alpha Tag,
    PL Output Tone, PL Input Tone, Mode, Class Station Code, Tag). Both the output and the input
    frequency of a pair are indexed: a hit on the input side is a mobile or handheld keying up nearby.
    The data is the user's own export, loaded from the Pi; nothing from it is shipped or stored.
    Wavelens Lab only, by use: the Play app never asks for Listen and shows a name like any other."""

    def __init__(self, paths, counties=()):
        """paths in order of preference: the county exports first, then a statewide one, then nationwide.
        A statewide export carries a County column: rows for the given counties (or "Statewide") rank
        ahead of other counties' rows, which stay usable for a drive out of the area."""
        import csv
        self.rows = []          # (mhz, side, row); row["_rank"] orders competing matches
        want = {c.strip().lower() for c in counties if c.strip()}
        for rank, path in enumerate(paths):
            with open(path, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    row = {k.strip(): (v or "").strip() for k, v in r.items() if k}
                    # A nationwide export names the agency in its own first column and leaves Agency/Category empty.
                    if not row.get("Agency/Category") and row.get("Nationwide Agency"):
                        row["Agency/Category"] = row["Nationwide Agency"]
                    county = row.get("County", "")
                    row["_rank"] = rank * 2 + (0 if not county or county.lower() == "statewide" or county.lower() in want else 1)
                    try:
                        out = float(row.get("Frequency Output") or 0)
                    except ValueError:
                        out = 0.0
                    try:
                        inp = float(row.get("Frequency Input") or 0)
                    except ValueError:
                        inp = 0.0
                    if 24.0 <= out <= 6000.0:
                        self.rows.append((out, "output", row))
                    if 24.0 <= inp <= 6000.0:      # a mangled input (one export had 46053250.0) is skipped
                        self.rows.append((inp, "input", row))
        self.rows.sort(key=lambda t: t[0])
        self.keys = [t[0] for t in self.rows]

    @staticmethod
    def _freq(hit) -> float:
        _, side, r = hit
        return float(r.get("Frequency Input") if side == "input" else r.get("Frequency Output"))

    def lookup(self, mhz: float, tol_khz: float):
        half = max(tol_khz, 12.5) / 1000.0 / 2 + 0.004   # + the dongle's own error (10 ppm at 400 MHz)
        lo = bisect.bisect_left(self.keys, mhz - half)
        hi = bisect.bisect_right(self.keys, mhz + half)
        return [(abs(f - mhz), side, row) for f, side, row in self.rows[lo:hi]]

    @staticmethod
    def agency(row: dict) -> str:
        tag = (row.get("Tag") or "").lower()
        if tag == "trs":                     # a trunked site: judge by whose system it is
            who = (row.get("Agency/Category") or "").lower()
            return "Public safety" if any(k in who for k in RR_PUBLIC) else "Business"
        if tag == "data":
            return "Data / telemetry"
        for keys, label in RR_TAG_AGENCY:
            if any(k in tag for k in keys):
                return label
        who = (row.get("Agency/Category") or "").lower()       # "Other", blank: judge by the agency's name
        if any(k in who for k in ("sheriff", "police", "law enforcement", "highway patrol")):
            return "Law enforcement"
        if "fire" in who:
            return "Fire / rescue"
        return "Business" if tag else "Licensed"

    def label(self, mhz: float, tol_khz: float = 12.5):
        """(name, cat, conf, extras) like FccPack.label, or None. Nearest entry wins; several in a wide
        bin are listed instead of named, as the FCC pack does."""
        hits = self.lookup(mhz, tol_khz)
        if not hits:
            return None
        best = min(r["_rank"] for _, _, r in hits)
        hits = [h for h in hits if h[2]["_rank"] == best]        # the most local source that knows the frequency
        hits.sort(key=lambda t: t[0])
        distinct = {(r.get("Alpha Tag") or r.get("Description")) for _, _, r in hits}
        if len(distinct) > 1 and tol_khz > 25:
            tags = ", ".join(sorted(distinct)[:4]) + (" …" if len(distinct) > 4 else "")
            return (f"{len(distinct)} known channels in this {tol_khz:.0f} kHz: {tags}", "UNKNOWN", "POSSIBLE",
                    {"rr_channels": "; ".join(sorted(distinct)[:8]),
                     "ambiguous": f"a {tol_khz:.0f} kHz sweep bin spans several channels; an RTL-SDR sweep at 12.5 kHz names the one"})
        _, side, r = hits[0]
        agency, desc, alpha = r.get("Agency/Category", ""), r.get("Description", ""), r.get("Alpha Tag", "")
        mode, tag, call = r.get("Mode", ""), r.get("Tag", ""), r.get("FCC Callsign", "")
        ag = self.agency(r)
        trs = tag.upper() == "TRS"
        if ag == "Amateur radio":
            agency = f"Amateur {agency}"
        agency, desc = abbrev(agency), abbrev(desc)
        # RadioReference splits "Pinellas County" / "Sheriff - MDT": fold that into PCSO · MDT, and a city's
        # "Police …" into <initials>PD …
        m = re.match(r"(?i)^sheriff'?s?(?: office)?\s*[-–:·]?\s*(.*)$", desc)
        if m and agency.lower().endswith(" county"):
            agency, desc = abbrev(agency + " Sheriff's Office"), m.group(1).strip()
        m = re.match(r"(?i)^police(?: department)?\s*[-–:·]?\s*(.*)$", desc)
        if m and not trs and " " not in agency.strip() or (m and agency.lower().startswith(("city of", "town of"))):
            agency, desc = abbrev(agency + " Police Department"), m.group(1).strip()
        who = agency if trs or not desc else f"{agency} · {desc}"
        if alpha and alpha.strip().lower() not in who.lower() and who.lower().replace(" · ", " ") not in alpha.lower():
            who += f" ({alpha.strip()})"     # the alpha tag is the channel's everyday name: in the headline
        # The name is who: agency and channel description (alpha tag, mode, tone go in the extras, which the
        # app shows on the card's second line). The input side of a pair says so: a radio keying up nearby.
        if trs:
            name, conf = f"{agency} · {desc} (trunked site)", "POSSIBLE"
        elif side == "input":
            name, conf = f"{who} · keying up nearby", "LIKELY"
        else:
            name, conf = who, "POSSIBLE"
        x = {"rr_agency": agency, "rr_description": desc, "rr_tag": tag, "rr_mode": mode, "agency": ag,
             "listen": RR_LISTEN.get(mode.lower(), "auto"), "lic_mhz": f"{self._freq(hits[0]):.5f}"}
        if alpha: x["rr_alpha"] = alpha
        if r.get("County"): x["rr_county"] = r["County"]
        if call: x["call_sign"] = call
        tone = r.get("PL Output Tone") if side == "output" else r.get("PL Input Tone")
        if tone: x["rr_tone"] = tone
        if mode.lower().endswith("e") and mode.lower() in ("dmre", "p25e"):
            x["encrypted"] = "RadioReference marks this channel encrypted: nothing to hear"
        if side == "input":
            x["repeater_input"] = "the mobile/handheld side of a repeater pair — heard only from close by"
        elif trs:
            x["trunked_site"] = "a trunked system's site channel; Follow system from the Companion screen with its control channel"
        else:
            x["base_output"] = "the repeater or base output: heard for miles, not a sign of anything nearby"
        return name, "UNKNOWN", conf, x


RR: "RrPack | None" = None

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
        """(name, cat, conf, extras) for a carrier, or None when nothing is licensed there.

        What a licence record can and can't say, so that nothing here raises a false alarm:
        - The category is never a device type the app alerts on. A licence says whose radio is on
          that frequency, not that it is a body cam, a camera or a traffic sensor; those categories
          are mapped away even when an older pack carries them. Low-threat hints (vehicle, IoT)
          are kept for the list colouring.
        - A repeater input (station class MO: mobiles and handhelds transmit there) is LIKELY to be
          a radio nearby. A base or repeater output is heard from miles away: POSSIBLE, and said so.
        - A wide sweep bin (the HackRF's 500 kHz) can hold dozens of 12.5 kHz land-mobile channels.
          With several licensees in the bin no single one is named: the hit says how many, and which
          agency types, at POSSIBLE. Only a narrow bin (rtl_power at 12.5 kHz) or a lone match names
          the licensee.
        """
        hits = self.lookup(mhz, tol_khz)
        if not hits:
            return None
        agencies = sorted({agency_of(h) for h, _ in hits})
        if len(hits) > 1 and tol_khz > 25:
            kinds = ", ".join(agencies[:4]) + (" …" if len(agencies) > 4 else "")
            name = f"Licensed radio nearby · {len(hits)} licensees in this {tol_khz:.0f} kHz ({kinds})"
            x = {"agency": kinds, "licensees": "; ".join(f"{h['name'] or h['call']} ({h['call']})" for h, _ in hits[:6]),
                 "ambiguous": (f"a {tol_khz:.0f} kHz sweep bin spans many {12.5:g} kHz channels; an RTL-SDR sweep "
                               "at 12.5 kHz names the licensee")}
            return name, "UNKNOWN", "POSSIBLE", x
        # A mobile/handheld (station class MO…) frequency is a repeater input: radios transmit there
        # toward the tower, and a low-power handheld is only audible from close by. Prefer that reading.
        inputs = [(h, dd) for h, dd in hits if is_input(h)]
        e, d = (inputs or hits)[0]
        who = abbrev(e['name']) if e['name'] else e['call']
        agency = agency_of(e)
        # The registrant leads the name: "Example County Sheriff · law enforcement radio nearby · WQZZ001".
        if is_input(e):
            name, conf = f"{who} · keying up nearby", "LIKELY"
        else:
            name, conf = f"{who} · base / repeater (regional)", "POSSIBLE"
        x = {"agency": agency, "licensee": e["name"], "call_sign": e["call"], "service": e["service_label"],
             "place": e.get("place", ""), "lic_mhz": f"{e['mhz']:.5f}"}   # the licensed centre: Listen tunes it exactly
        if is_input(e):
            x["repeater_input"] = "a mobile or handheld transmitting up to its repeater — heard only from close by"
        else:
            x["base_output"] = "a base station or repeater output: heard for miles, not a sign of anything nearby"
        if d is not None and d < 1e8:
            x["site_km"] = f"{d:.1f}"
        if len(hits) > 1:
            x["other_licensees"] = "; ".join(f"{h['name'] or h['call']} ({h['call']})" for h, _ in hits[1:4])
        cat = e.get("cat") or "UNKNOWN"
        if cat in FCC_NO_DEVICE_CATS:
            cat = "UNKNOWN"
        return name, cat, conf, x


# Categories a licence record must never assert: the app alerts on them and names a device
# ("Body cam", "Traffic sensor", "Camera") that a licence says nothing about.
FCC_NO_DEVICE_CATS = {"LE_GEAR", "ALPR", "CAMERA", "TRAFFIC_SENSOR", "DRONE", "TOOL", "ACOUSTIC", "TRACKER"}


def agency_of(e: dict) -> str:
    """Agency type stored by fcc_local_pack.py, or the radio-service label on an older pack."""
    return e.get("agency") or e.get("service_label") or "Licensed"


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


def licensed_label(mhz: float, tol_khz: float):
    """What is known to be on a frequency: the RadioReference overlay first (curated, with mode and
    tone), then the FCC pack. Either returns (name, cat, conf, extras) or None."""
    if RR:
        lic = RR.label(mhz, tol_khz=tol_khz)
        if lic:
            return lic
    return FCC.label(mhz, tol_khz=tol_khz) if FCC else None
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
    B(1, 26.96, None),                                # AM broadcast and HF: worldwide propagation, nothing nearby
    B(27.41, 30, None),                               # HF (CB at 26.96–27.41 stays: a transmitter nearby)

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
      steady=("Steady 900 MHz carrier: meter collector, LoRa gateway, cordless phone — or a toll / RFID reader on the road", "IOT", "POSSIBLE")),
    B(863, 870, "868 MHz ISM burst (EU LoRa, sensors)", "IOT"),
    B(49.8, 49.9, "49 MHz baby monitor, toy or old cordless phone", "IOT"),
    B(72, 76, "72 MHz steady carrier: radio-control model (RC aircraft), or hearing-assist / paging", "DRONE", kind="persist"),

    # ---- video and drone links
    # 5645–5849 is shared with 5 GHz Wi-Fi (ch 132–165). Analog FPV video transmits continuously; Wi-Fi
    # comes and goes with traffic. kind "video": reported only after VIDEO_N passes in a row.
    B(5645, 5849, "5.8 GHz steady carrier: FPV video, or a busy Wi-Fi access point", "DRONE", kind="video"),
    B(5850, 5925, "5.9 GHz burst: connected-vehicle radio (C-V2X roadside unit or vehicle)", "VEHICLE",
      steady=("5.9 GHz steady carrier: FPV video on a high channel, or Wi-Fi ch 169–177", "DRONE", "POSSIBLE")),
    B(5030, 5091, "Drone command-link band (5030–5091 MHz, aviation UAS control)", "DRONE", kind="persist"),
    B(5091, 5150, "Airport surface network (AeroMACS)", "NETWORK"),
    B(1240, 1300, "1.2 GHz carrier: video or telemetry link (also ham 23 cm)", "DRONE", kind="persist"),
    B(2025, 2110, "News / broadcast video link (2 GHz: ENG trucks, news helicopters)", "CAMERA", kind="video"),
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
VIDEO_N = 6          # kind "video": passes in a row before a carrier shared with Wi-Fi is reported
CHAIN_S = 30.0       # a bin's run of hot passes breaks if it isn't revisited within this long
ALWAYS_S = 180.0     # a 1 MHz bucket hot for this long without a break counts as always on: background
STORM_FRESH_S = 10.0 # a bucket hot for less than this is "fresh": only fresh hits make, or fall to, an overload burst
BG_RATE_S = 60.0     # background bands: at most one report per band per minute
FLOOR_S = 40.0       # rolling floor time constant (a carrier that stays this long becomes the floor)
FLOOR_UP_GNSS_S = 1200.0   # ... rising in the GNSS bands, so a jammer riding with you stays visible
FLOOR_HOT_S = 600.0        # ... rising under a bin that is already reported, so a held carrier keeps reporting
FLOOR_WARM_S = 60.0        # a bin's floor follows at the normal rate for this long after first sight, whatever the level

# Parked: the HackRF stops sweeping 1 MHz–6 GHz and stays on one 20 MHz slice, so a short transmission
# there is caught on the pass it happens instead of within a second. "le" is the body-cam-and-radio
# preset the phone sends: 796–816 MHz, where public-safety handhelds and car radios transmit (700 MHz
# public-safety mobile 799–805, 800 MHz repeater inputs 806–815). Energy only: "a radio keyed up
# nearby", never who or what was said. Set another slice with --hackrf-park-range.
PARK_RANGES = {"le": "796:816"}
HACKRF_STALL_S = 10.0   # a full 1 MHz–6 GHz pass takes about a second; this long with no rows means it hung


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

    def __init__(self, src: str, threshold_db: float = 18.0, rate_s: float = 2.0, bucket_mhz: float = 1.0,
                 mask: "list[tuple[float, float]] | None" = None, ppm: float = 0.0):
        self.src, self.th, self.rate, self.bucket = src, threshold_db, rate_s, bucket_mhz
        self.ppm = ppm            # reported MHz are scaled by (1 + ppm/1e6): the radio's crystal error, measured
        self.peak_ok, self.tol_hz = True, 0.0
        self.mask = [(lo * 1e6, hi * 1e6) for lo, hi in (mask or [])]   # Hz ranges never reported (the unit's spurs)
        self.floor = {}       # bin start Hz → EMA of dB
        self.last = {}        # bucket → last report time
        self.gps_prev = {}    # GNSS row start (MHz) → last time that row looked jammed
        self.settle_until = 0.0
        self.quiet_until = 0.0    # floors learn but nothing is reported until then (a sweep's first FLOOR_WARM_S)
        self.since = {}       # bin Hz → when it was first seen (the floor is "warm" after FLOOR_WARM_S)
        self.seen = {}        # bin Hz → time of the previous visit
        self.runs = {}        # bin Hz → [passes in a row over the floor, last dB, bin width]
        self.bhot = {}        # 1 MHz bucket → [first hot, last hot]: a spur drifts across bins, the bucket holds

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
            if db != db or db in (float("inf"), float("-inf")):
                continue                     # rtl_power prints nan for bins it cropped; never a hit
            hz = lo + i * step
            prev_visit = self.seen.get(hz)
            self.seen[hz] = now
            f = self.floor.get(hz)
            if f is None:
                self.floor[hz] = db
                self.since[hz] = now
                continue
            # slow floor: a carrier that stays for minutes becomes the floor, which is what we want for a drive.
            # Not in the GNSS bands: there the true floor is the empty band, and a jammer riding with you must
            # not fade into it, so the floor rises ~30x more slowly there (and falls at the normal rate).
            # Time-based, so the floor moves at the same speed whether a pass takes a second (full sweep)
            # or a few milliseconds (parked): about 40 s to follow a change, ~20 min upward in GNSS.
            w = gnss_window(hz)
            dt = min(max((now - prev_visit) if prev_visit is not None else 1.2, 0.001), 30.0)
            # The floor follows the band with a 40 s time constant, so a tower fades into the background
            # on a drive. But a bin that is already well over the floor must not be absorbed at that
            # rate: a radio held keyed (Find it) or a video link would stop being reported after 40 s.
            # Hot bins rise with FLOOR_HOT_S instead; the floor still falls at the normal rate.
            # ... but only once the floor has had FLOOR_WARM_S to settle: a sweep tool's first pass can
            # come before its gain is applied, 30-40 dB low, and a floor that started there must be
            # allowed to catch up fast, or the whole spectrum looks hot for ten minutes.
            warm = now - self.since.get(hz, now) >= FLOOR_WARM_S
            hot = db - f >= self.th and warm
            tau = FLOOR_UP_GNSS_S if (w and db > f and warm) else FLOOR_HOT_S if (hot and db > f) else FLOOR_S
            k = min(1.0, dt / tau)
            self.floor[hz] = f * (1 - k) + db * k
            if any(lo <= hz < hi for lo, hi in self.mask):
                continue                     # one of this unit's own spurs (--hackrf-mask): floor only, never reported
            if now < self.quiet_until:
                continue                     # the floor isn't trusted yet (see FLOOR_WARM_S): learn it, report nothing
            if w:
                if not w[2] <= hz <= w[3]:
                    continue                 # GNSS but outside the judged window: floor only, never reported
                # Judged per row below: a jammer is wide and steady, so one hot bin is not enough.
                g = gnss_rows.setdefault(w, [0, []])
                g[0] += 1
                if db - f >= self.th:
                    g[1].append((hz, db, db - f))
                continue
            # Where in the bin is the carrier? A carrier spreads over two or three bins (FFT leakage), so
            # only the local peak reports, placed by a parabola through the peak and its neighbours
            # (dB). That puts a narrow carrier within about a quarter bin: 446.075 reads 446.07, not 446.1.
            left = vals[i - 1] if i > 0 else None
            right = vals[i + 1] if i + 1 < len(vals) else None
            self.peak_ok = (left is None or db >= left) and (right is None or db > right)
            off, self.tol_hz = 0.0, step / 2
            if left is not None and right is not None:
                den = left - 2 * db + right
                if den < 0:
                    off = max(-0.5, min(0.5, 0.5 * (left - right) / den))
                    self.tol_hz = step / 4
            mhz = (hz + step / 2 + off * step) / 1e6 * (1 + self.ppm / 1e6)   # hz is the bin's lower edge
            if db - f < self.th:
                run = self.runs.pop(hz, None)       # quiet on this pass: the run of hot passes is over
                if run and run[0] < STEADY_N:
                    b = band_of(mhz)
                    if b["steady"] and not licensed_label(mhz, max(step / 1000.0, 12.5)):
                        # it came and went: report it now with the burst label
                        self.fresh = self.peak_ok = True
                        self.report(mhz, run[1], step, run[1] - f, b["name"], b["cat"], b["conf"], b, None, now, {})
                continue
            # How long this 1 MHz bucket has been hot without a break of CHAIN_S. Judged per bucket, not
            # per bin: a spur wanders across neighbouring bins, and a per-bin count would keep restarting.
            bucket = round(mhz / self.bucket)
            bh = self.bhot.get(bucket)
            if bh is None or now - bh[1] > CHAIN_S:
                bh = self.bhot[bucket] = [now, now]
            bh[1] = now
            hot_for = now - bh[0]
            run = self.runs.get(hz)
            if run and prev_visit is not None and 0.2 < now - prev_visit <= CHAIN_S:
                run[0] += 1
                run[1] = db
            elif not run or prev_visit is None or now - prev_visit > CHAIN_S:
                run = self.runs[hz] = [1, db, step]
            n = run[0]
            # Hot for ALWAYS_S without a break: a fixed transmitter, or one of the receiver's own spurs
            # (a HackRF shows a few, some near full scale). Background from here on: once a minute,
            # and never part of the overload judgement, where a strong spur would otherwise be "the cause".
            self.always = hot_for >= ALWAYS_S
            # Fresh: this bucket rose just now. Only fresh hits can be an overload burst, and only fresh
            # hits are dropped as one. A bucket that has been hot for a while is established: it goes
            # through, and it is not "a band that rose together" with anything.
            self.fresh = hot_for < STORM_FRESH_S
            x = {"over_floor_db": f"{db - f:.0f}", "bin_hz": f"{step:.0f}"}
            lic = licensed_label(mhz, max(step / 1000.0, 12.5))
            if lic:
                name, cat, conf, lx = lic
                x.update(lx)
                if cat in PERSIST and n < 2:
                    continue
                # a named licensee keeps its own id; an ambiguous wide-bin hit falls back to the 1 MHz bucket id
                ident = f"fcc/{lx['call_sign']}/{mhz:.3f}" if lx.get("call_sign") else None
                self.report(mhz, db, step, db - f, name, cat, conf, None, ident, now, x)
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
                if b["kind"] == "video":
                    if n < VIDEO_N:
                        continue             # Wi-Fi bursts; analog video is on every pass
                    x["steady"] = f"over the floor on {n} passes in a row"
            self.report(mhz, db, step, db - f, name, cat, conf, b, None, now, x)
        for w, (n, hot) in gnss_rows.items():
            self.gps_row(lo, step, n, hot, now, w[4])

    def report(self, mhz, db, step, over, name, cat, conf, b, ident, now, x) -> None:
        if not self.peak_ok:
            return                           # a shoulder of a carrier whose peak bin reports instead
        if not x:
            x = {"over_floor_db": f"{over:.0f}", "bin_hz": f"{step:.0f}"}
        # f goes out rounded to whole MHz (and sweep ids are 1 MHz buckets); exact_mhz is the best the
        # sweep knows: the interpolated peak, to a quarter bin, plus the radio's ppm error unless --ppm is set.
        x.setdefault("exact_mhz", f"{mhz:.4f}".rstrip("0").rstrip(".") + f" ± {self.tol_hz / 1e6:g}")
        fresh = getattr(self, "fresh", True)
        if b is not None and b["kind"] == "bg":
            key, rate = "bg/" + b["key"], BG_RATE_S
            ident = ident or f"{self.src}/bg/{b['key']}"
            x["background"] = "broadcast, tower or beacon: everywhere, shown for completeness"
        elif getattr(self, "always", False):
            bucket = round(mhz / self.bucket)
            key, rate = ("always", bucket), BG_RATE_S
            ident = ident or f"{self.src}/{bucket * self.bucket:.0f}"
            x["always_on"] = ("on the air continuously for minutes: a fixed transmitter, or a spur the receiver "
                              "makes itself (a HackRF shows a few, some very strong). Shown once a minute.")
        else:
            bucket = round(mhz / self.bucket)
            key, rate = (bucket, cat if b is not None and b["steady"] and cat == b["steady"][1] else ""), self.rate
            if b is not None and b["key"] in RADAR_BANDS:
                # a radar's pulses drift across several MHz: one entry per radar band, not one per MHz
                key = ("band", b["key"])
                ident = ident or f"{self.src}/band/{b['key']}"
            ident = ident or f"{self.src}/{bucket * self.bucket:.0f}"
        if now - self.last.get(key, 0) < rate:
            return
        self.last[key] = now
        if b is not None:
            x.setdefault("band", f"{b['lo']:g}–{b['hi']:g} MHz")
        line = {"t": "rf", "id": ident, "n": name, "f": int(round(mhz)), "r": int(round(db)),
                "cat": cat, "conf": conf, "src": self.src, "x": x}
        if (b is not None and b["kind"] == "bg") or getattr(self, "always", False):
            emit(line)                       # background and always-on bins bypass the overload gate
        else:
            storm_gate(self.src).push(now, b["key"] if b is not None else str(ident), line, fresh)

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


# ---------------------------------------------------------------- broadband bursts
#
# An SDR with no front-end filter (the HackRF especially) overloads when a strong transmitter is
# right beside it, usually the phone or the Pi's own Wi-Fi. For that moment, false carriers appear
# across the whole spectrum at once, at about the same level: "military radar", "satellite C-band",
# "flight-test telemetry" in a living room. Real signals rise one or two bands at a time. So sweep
# reports are held for STORM_HOLD_S, and when STORM_BANDS or more unrelated bands rose within that
# window, the fresh part of the batch is thinned to its strongest and the phone gets one status line.
# "Rose together" means rose: a bucket that has been hot for a while (a spur, a steady carrier) is not
# part of the burst and is never dropped by it. Without that, a receiver with a few permanent spurs
# would be "overloaded" in every window, and a radio keyed up beside it would be dropped every time.
# Bands that are only radar: a radar's pulses drift across several MHz, so these report as one entry
# per band. Not 420–450 (70 cm amateur shares it) or 1030 MHz: there each carrier is its own transmitter.
RADAR_BANDS = {"2700-2900", "2900-3100", "3100-3450"}
STORM_HOLD_S = 2.5
STORM_BANDS = 4
STORM_NOTE_S = 60.0
STORM_KEEP_DB = 5.0
STORM_KEEP_OVER_DB = 30.0   # a hit this far over its floor is a real strong signal nearby: never dropped as overload


class StormGate:
    def __init__(self, src: str):
        self.src, self.q, self.lock, self.noted = src, deque(), threading.Lock(), 0.0
        threading.Thread(target=self._loop, daemon=True).start()

    def push(self, now: float, band: str, line: dict, fresh: bool = True) -> None:
        with self.lock:
            self.q.append((now, band, line, fresh))

    def _loop(self) -> None:
        while True:
            time.sleep(0.25)
            self.flush(time.time())

    def flush(self, now: float) -> None:
        """Release every window that has been held for STORM_HOLD_S (call again until nothing is due)."""
        while True:
            with self.lock:
                if not self.q or now - self.q[0][0] < STORM_HOLD_S:
                    return
                t0 = self.q[0][0]
                batch = []
                while self.q and self.q[0][0] <= t0 + STORM_HOLD_S:
                    batch.append(self.q.popleft())
            self._judge(batch)

    def _judge(self, batch: list) -> None:
            now = time.time()
            rose = [(band, line) for _, band, line, fresh in batch if fresh]
            # Smear: a strong carrier beside an unfiltered receiver spreads over several MHz of its own
            # band (a comb of 435-441 from one handheld). Several fresh buckets in one band in one window
            # are one signal: keep the strongest, say so on it.
            by_band = {}
            for band, line in rose:
                by_band.setdefault(band, []).append(line)
            smeared = []
            for band, lines in by_band.items():
                if len(lines) >= SMEAR_N:
                    lines.sort(key=lambda l: l["r"], reverse=True)
                    lines[0].setdefault("x", {})["smear"] = (f"{len(lines)} carriers in this band at once: one strong "
                                                            "signal spread by the receiver; the strongest is shown")
                    smeared += lines[1:]
            rose = [(band, line) for band, line in rose if line not in smeared]
            bands = {band for band, _ in rose}
            if len(bands) >= STORM_BANDS:
                # Overload has a cause: the strongest thing in the burst, and preferably something that can
                # actually be beside the receiver (a radio, a phone, a key fob), not a "4.7 GHz microwave link"
                # that is really the receiver's own product of it. Keep the cause and what's within
                # STORM_KEEP_DB of it; drop the rest. When nothing in the burst looks like a nearby
                # transmitter, keep the strongest and anything STORM_KEEP_OVER_DB over its floor (radar).
                # Established hits (not fresh) were there before the burst and go through untouched.
                cause = [line for _, line in rose if near_cause(line)]
                if cause:
                    top = max(line["r"] for line in cause)
                    keep = [line for line in cause
                            if line["r"] >= top - STORM_KEEP_DB
                            or float(line.get("x", {}).get("over_floor_db", 0)) >= STORM_KEEP_OVER_DB]
                    keep = drop_harmonics(keep)
                    why = "the transmitter that caused it"
                else:
                    top = max(line["r"] for _, line in rose)
                    keep = [line for _, line in rose
                            if line["r"] >= top - STORM_KEEP_DB
                            or float(line.get("x", {}).get("over_floor_db", 0)) >= STORM_KEEP_OVER_DB]
                    why = "the strongest"
                for line in keep:
                    line.setdefault("x", {})["burst"] = f"{why} of {len(bands)} bands that rose together"
                for _, _, line, fresh in batch:
                    if not fresh or line in keep:
                        emit(line)
                if now - self.noted > STORM_NOTE_S:
                    self.noted = now
                    names = ", ".join(sorted({l["n"] for l in keep}))[:120]
                    msg = (f"{self.src}: {len(bands)} bands rose together; kept {why} ({names}) and "
                           "ignored the rest as receiver overload. A strong transmitter close by, or a radar beam "
                           "sweeping past, does this; lower the gain if it happens often.")
                    print("bridge: " + msg, file=sys.stderr)
                    emit({"t": "status", "msg": msg, "band": state["band"], "mode": state["mode"]})
                return
            for _, _, line, _ in batch:
                if line not in smeared:
                    emit(line)


SMEAR_N = 3


def near_cause(line: dict) -> bool:
    """A hit in a band where a transmitter beside the receiver is the normal explanation: two-way
    radios, phones, key fobs, ISM devices. Not radar, not microwave links, not altimeters."""
    n = (line.get("n") or "").lower()
    if "altimeter" in n or "radar (" in n and "radio" not in n:
        return False
    return (line.get("cat") in ("PHONE", "VEHICLE", "IOT")
            or "radio" in n or "nearby" in n or "walkie" in n or "keying" in n or "two-way" in n)


def drop_harmonics(keep: list) -> list:
    """A kept hit at 2x, 3x or 4x a stronger kept hit (to 0.3 %) is the receiver's harmonic of it."""
    keep = sorted(keep, key=lambda l: l["r"], reverse=True)
    out = []
    for line in keep:
        f = float(str(line.get("x", {}).get("exact_mhz", line["f"])).split()[0])
        for strong in out:
            fs = float(str(strong.get("x", {}).get("exact_mhz", strong["f"])).split()[0])
            if any(abs(f - k * fs) <= 0.003 * f for k in (2, 3, 4)):
                break
        else:
            out.append(line)
    return out


_storm_gates: "dict[str, StormGate]" = {}


def storm_gate(src: str) -> StormGate:
    g = _storm_gates.get(src)
    if g is None:
        g = _storm_gates[src] = StormGate(src)
    return g


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


def reap(p, cmd: str, planned: bool = False) -> None:
    """Wait for a tool; if it lasted under 10 s, log what it said, since that is a failure, not a sweep.
    planned: the tool was told when to stop (rtl_power -e taking turns), so a clean exit is not logged."""
    p.wait()
    if planned and p.returncode == 0:
        return
    if p.returncode == -15:
        return                                  # we stopped it (a listen took the dongle): not a failure
    if time.time() - p.started_at < 10:
        time.sleep(0.2)                         # let the drain thread catch the last lines
        why = " | ".join(p.err_tail) or "no message"
        print(f"bridge: {cmd.split()[0]} exited after {time.time() - p.started_at:.1f} s (code {p.returncode}): {why}",
              file=sys.stderr)


def run_once(cmd: str, handler, planned: bool = False, stop_when=None) -> bool:
    """Run a tool until it exits, handing it each stdout line. False if it isn't installed.
    stop_when: polled five times a second; when it turns true the tool is stopped (the dongle is wanted)."""
    p = spawn(cmd)
    if p is None:
        return False
    if stop_when is not None:
        def watch():
            while p.poll() is None:
                if stop_when():
                    p.terminate()
                    return
                time.sleep(0.2)
        threading.Thread(target=watch, daemon=True).start()
    for line in p.stdout:
        handler(line)
    reap(p, cmd, planned or (stop_when is not None and stop_when()))
    return True


def feed_process(cmd: str, handler) -> None:
    """Run a sweep tool and hand it each stdout line; restart it if it exits."""
    while run_once(cmd, handler):
        time.sleep(2)


def parse_mask(spec: str) -> "list[tuple[float, float]]":
    """'4770:4805,3215:3240' (MHz) → [(4770.0, 4805.0), (3215.0, 3240.0)]"""
    out = []
    for part in (spec or "").replace(" ", "").split(","):
        if not part:
            continue
        lo, hi = part.split(":")
        out.append((float(lo), float(hi)))
    return out


def feed_hackrf(range_mhz: str, bin_hz: int, lna: int = 32, vga: int = 20, mask: str = "", ppm: float = 0.0) -> None:
    """hackrf_sweep over range_mhz, or over the parked slice while the phone has asked to park.
    A park change stops the running sweep and starts the other one."""
    sw = Sweep("hackrf", mask=parse_mask(mask), ppm=ppm)
    if sw.mask:
        print("bridge: hackrf: masking " + ", ".join(f"{lo/1e6:g}-{hi/1e6:g} MHz" for lo, hi in sw.mask), file=sys.stderr)
    while True:
        park = state.get("park", "off")
        rng = PARK_RANGES.get(park, range_mhz)
        cmd = f"hackrf_sweep -f {rng} -w {bin_hz} -l {lna} -g {vga}"
        p = spawn(cmd)
        if p is None:
            return
        sw.settle_until = time.time() + SETTLE_S + 3.0   # first passes come before the gains are applied
        # A full sweep's floor is untrustworthy for its first minute (the gains arrive late, the floor starts
        # 30-40 dB low): learn it quietly. A parked slice sweeps thousands of times a minute and settles fast.
        sw.quiet_until = time.time() + (FLOOR_WARM_S if rng == range_mhz else 8.0)
        print(f"bridge: hackrf_sweep {rng} MHz" + (f" (parked: {park})" if park in PARK_RANGES else ""), file=sys.stderr)

        p.last_line = time.time()

        def watch(proc=p, was=park):
            while proc.poll() is None:
                if state.get("park", "off") != was:
                    proc.terminate()
                    return
                # A HackRF behind a busy USB hub can stop streaming without hackrf_sweep exiting: the radio
                # sits on one frequency and nothing is reported. Kill it and start over.
                if time.time() - proc.last_line > HACKRF_STALL_S:
                    print(f"bridge: hackrf_sweep stalled (no data for {HACKRF_STALL_S:.0f} s); restarting", file=sys.stderr)
                    proc.kill()
                    return
                time.sleep(0.2)

        threading.Thread(target=watch, daemon=True).start()
        for line in p.stdout:
            p.last_line = time.time()
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


def feed_rtl_power(specs: "list[str]", device: str = "", dwell_s: int = 15, ppm: int = 0, gain: int = 30) -> None:
    """One range: rtl_power runs continuously. Several: they take turns on the same dongle,
    dwell_s seconds each (rtl_power -e), so VHF and 800 MHz can share one RTL-SDR.
    device is an index or serial (rtl_power -d matches either). Each dongle gets its own thread."""
    sw = Sweep("rtlpower")          # one floor map keyed by Hz, so the ranges don't disturb each other
    dev = f" -d {shlex.quote(device)}" if device else ""
    busy = lambda: listen_busy(device)      # the phone is listening on this dongle: stop, and wait

    def wait_free():
        while busy():
            time.sleep(0.3)
    if len(specs) == 1:
        cmd = f"rtl_power -f {specs[0]}{dev} -i {sweep_interval_s(specs[0])} {(' -g ' + str(gain)) if gain else ''}{(' -p ' + str(ppm)) if ppm else ''} -"
        while True:
            wait_free()
            sw.settle_until = time.time() + SETTLE_S + sweep_interval_s(specs[0])
            if not run_once(cmd, sw.row, stop_when=busy):
                return
            time.sleep(2)
        return
    while True:
        for spec in specs:
            wait_free()
            i = sweep_interval_s(spec)
            sw.settle_until = time.time() + SETTLE_S + i      # first integration after a retune is discarded
            if not run_once(f"rtl_power -f {spec}{dev} -i {i} {(' -g ' + str(gain)) if gain else ''}{(' -p ' + str(ppm)) if ppm else ''} -e {max(dwell_s, 2 * i)}s -", sw.row, planned=True, stop_when=busy):
                return
            time.sleep(0.5)         # let the dongle close before the next range opens it


def group_sweeps(specs: "list[str]", default_device: str) -> "dict[str, list[str]]":
    """'24M:895M:250k@00000978' pins that range to a dongle; ranges without @ use --rtl-power-device."""
    groups: "dict[str, list[str]]" = {}
    for s in specs:
        spec, _, dev = s.partition("@")
        groups.setdefault(dev or default_device, []).append(spec)
    return groups


# ---------------------------------------------------------------- listen: demodulate one frequency and stream the audio

# The phone asks to hear a hit. The bridge borrows an RTL-SDR (the one rtl_power sweeps with, by default;
# that sweep pauses), runs rtl_fm on the frequency and sends 8 kHz mono audio over BLE as IMA ADPCM:
# 4 bits a sample, 40 ms frames of 164 bytes, about 4 kB/s. rtl_fm's squelch keeps the link quiet
# between transmissions. A listen ends by itself after LISTEN_MAX_S unless the phone renews it.
# Receive only: rtl_fm demodulates, it never transmits.
LISTEN_MAX_S = 300.0
LISTEN_RATE = 8000
LISTEN_FRAME = 320          # samples per frame: 40 ms
LISTEN_MODES = {"fm": "-M fm -s 16k", "am": "-M am -s 16k", "wbfm": "-M wbfm"}
# Digital voice: rtl_tcp serves the dongle, dsd-fme demodulates and decodes, and sends 8 kHz PCM to a UDP
# port the bridge listens on. TDMA modes (DMR, P25 phase 2) come out stereo, one slot per channel: mixed.
# Encrypted traffic decodes to nothing; that is the law and the maths, not a setting.
DIGITAL_MODES = {"dmr": ("-fs", 2), "p25": ("-f1", 1), "nxdn": ("-fi", 1), "dstar": ("-fd", 1),
                 "auto": ("-fa", 1),       # auto: dsd-fme's own detection (DMR, P25 phase 1 and 2); see AUTO_* below
                 # Trunked systems: mhz is the control channel; dsd-fme follows the system's grants, retuning
                 # the dongle to each voice channel and back (P25 phase 1/2, DMR Tier III / Cap+ / Con+). -ft is
                 # its trunking preset (P25p1 + p2 + DMR, C4FM); "trunkq" adds -mq for QPSK/LSM simulcast sites.
                 # Encrypted calls are never tuned (dsd-fme's default). Lab only; the app sends it from the
                 # Companion screen's "Trunked systems" section.
                 "trunk": ("-ft -T", 2), "trunkq": ("-ft -T -mq", 2),
                 # EDACS (analog voice on a 9600-baud control channel) and DMR Cap+/Con+/Tier III need a channel map:
                 # built from the RadioReference site's frequencies, LCN/LSN in ascending-frequency order (the usual
                 # convention; dsd-fme shows in its log whether calls follow). Motorola Type II / SmartNet and LTR
                 # are not decodable by dsd-fme at all.
                 "trunke": ("-fE -T", 1), "trunkd": ("-fs -T", 2)}
TRUNK_MODES = ("trunk", "trunkq", "trunke", "trunkd")
ENC_RE = re.compile(r"\b(ENC|Encrypted|ALG(?:ID)?\s*[:=]?\s*(?:0x)?[0-9A-Fa-f]+)\b", re.I)
CALL_RE = re.compile(r"\bTG[:= ]\s*(\d+)")
SRC_RE = re.compile(r"\bSRC[:= ]\s*(\d+)")
# Mode "auto": start in dsd-fme's auto-detect; it says on stderr what sync it finds ("Sync: +P25p1"), which
# also tells us the audio layout (TDMA is stereo). If the carrier is on and no digital sync shows within
# AUTO_WAIT_S, it's analog: restart as narrow FM. Decided once per tune; the app's renewals keep "auto".
AUTO_WAIT_S = 3.0
# Channel plans: a sweep places a carrier to within a quarter bin (±25 kHz on a HackRF, ±3 kHz on an
# RTL-SDR), but radios sit on channel rasters. Snap to the likeliest channel within the tolerance:
# (lo, hi, [(step_khz, offset_khz, preference)...]) — lower preference wins when several rasters fit.
CHANNEL_PLANS = [
    (26.96, 27.41, [(10, 5, 0)]),                              # CB: 26.965, 26.975, ... (x.xx5)
    (29.6, 54.0, [(20, 0, 0), (5, 0, 1)]),                      # low band
    (118.0, 137.0, [(25, 0, 0), (8.333, 0, 1)]),                # air band: 25 kHz (8.33 kHz in Europe)
    (144.0, 148.0, [(15, 0, 0), (5, 0, 1)]),                    # 2 m: 15 kHz repeater raster, 5 kHz elsewhere
    (156.0, 162.05, [(25, 0, 0), (12.5, 0, 1)]),                # marine: 25 kHz channels
    (137.0, 174.0, [(12.5, 0, 0), (7.5, 0, 1), (2.5, 0, 2)]),   # VHF land mobile: 12.5 kHz, with 7.5 kHz offsets
    (216.0, 225.0, [(12.5, 0, 0), (6.25, 0, 1)]),
    (420.0, 450.0, [(25, 0, 0), (12.5, 0, 1), (5, 0, 2)]),      # 70 cm: 25 kHz repeaters, 5 kHz raster
    (462.5375, 462.7375, [(12.5, 0, 0)]),                       # FRS/GMRS: 462.5500 … 462.7250 on 12.5 kHz
    (467.5375, 467.7375, [(12.5, 0, 0)]),
    (450.0, 470.0, [(25, 0, 0), (12.5, 0, 1), (6.25, 0, 2), (2.5, 0, 3)]),  # UHF land mobile: 25 kHz primaries, then interstitials
    (806.0, 824.0, [(12.5, 0, 0), (6.25, 0, 1)]),
    (851.0, 869.0, [(12.5, 0, 0), (6.25, 0, 1)]),
    (896.0, 902.0, [(12.5, 0, 0)]), (935.0, 941.0, [(12.5, 0, 0)]),
]
HONE_MIN_READINGS = 3        # sweep readings of the tuned carrier before re-snapping from their average
HONE_RATE_S = 10.0           # at most one re-tune this often
# AFC on analog FM: rtl_fm's discriminator output has a DC level proportional to how far the carrier is
# from the tuned frequency (full scale 16384 = half the demod rate). Half a second of open squelch
# measures that to ~100 Hz, which is how the listen lands on the exact channel after a sweep's ±25 kHz.
AFC_RATE_IN = 16000.0
# Pager data (POCSAG, FLEX) on an analog FM channel: multimon-ng decodes it from rtl_fm's audio when
# it is installed (apt install multimon-ng). It runs beside every analog listen; the first page
# decoded marks the channel as pager data, mutes the audio (it's tone bursts and squeal) and the
# pages go to the phone as lines instead. Wavelens Lab only; the store app never listens.
PAGER_RATE = 22050           # multimon-ng wants 22050 Hz s16 mono
PAGER_CMD = "multimon-ng -t raw -a POCSAG512 -a POCSAG1200 -a POCSAG2400 -a FLEX -e -u -"
PAGE_RE = re.compile(r"^(POCSAG\d+|FLEX)[:|]\s*(.*)$")
PAGE_ADDR_RE = re.compile(r"Address:\s*(\d+)|\[(\d+)\]")
PAGE_MSG_RE = re.compile(r"(?:Alpha|Numeric|ALN|NUM|TON|BIN):\s*(.*)$|(?:ALN|NUM)\s+(.*)$")
AFC_FRAMES = 12              # 40 ms frames with signal before judging (about half a second)
AFC_MIN_HZ = 1500            # smaller offsets are the dongle's crystal, not a wrong channel


def snap_channel(mhz: float, tol_mhz: float) -> float:
    """The likeliest channel for a carrier placed at mhz ± tol. Rasters of the band in preference order;
    a raster point within the tolerance (plus a 20 % margin) wins, nearest first among equal preference.
    Outside any plan, or nothing within tolerance, the reading itself at 2.5 kHz."""
    best = None
    for lo, hi, rasters in CHANNEL_PLANS:
        if not lo <= mhz <= hi:
            continue
        for step_khz, off_khz, pref in rasters:
            step = step_khz / 1000.0
            g = round((mhz - off_khz / 1000.0) / step) * step + off_khz / 1000.0
            d = abs(g - mhz)
            if d <= tol_mhz * 1.2 + 0.004:   # + 10 ppm at 400 MHz: an uncorrected dongle's own error
                cand = (pref, d, g)
                if best is None or cand < best:
                    best = cand
        break
    if best is not None:
        return round(best[2], 4)
    return round(round(mhz / 0.0025) * 0.0025, 4)
AUTO_NO_CARRIER_WAIT_S = 8.0   # no sweep running to tell us the carrier is on: decide on time since tune alone
TDMA_SYNCS = ("DMR", "P25p2", "X2")
DSD_UDP_PORT = 23470
RTL_TCP_PORT = 23471
LISTEN = {"proc": None, "req": None, "gen": 0, "dev": "", "ppm": 0, "mhz": 0.0, "mode": "", "asked": "", "sq": 0,
          "raw_mhz": 0.0, "tol": 0.05, "readings": [], "honed_at": 0.0,   # the sweep's placing, and later readings
          "until": 0.0, "seq": 0, "follow": False, "follow_until": 0.0, "tuned_at": 0.0, "last_audio": 0.0,
          "chan_at": 0.0,                   # when this channel was first asked for (tuned_at restarts on AUTO → FM)
          "carrier_at": 0.0, "detected": "",   # auto: when the sweep last saw the tuned carrier; what dsd-fme found
          "call": "", "call_at": 0.0,       # trunk: the talkgroup dsd-fme is following right now, and when it started
          "starting": False,                # the worker is bringing the tools up (dongle spoken for, proc not yet set)
          "recent": {}, "played": {},      # follow: candidate hits (key → (seen, mhz, mode)); key → when it was left
          "lockout": []}                   # MHz the phone told us never to follow (a pager, a busy repeater)
LOCKOUT_TOL = 0.02                         # a hit within this of a locked-out frequency is the same channel


def locked_out(mhz: float) -> bool:
    return any(abs(mhz - f) <= LOCKOUT_TOL for f in LISTEN["lockout"])
LISTEN_CV = threading.Condition()

# Follow: a scanner. Every fresh sweep hit in a band where voice is analog and legal to hear gets tuned
# and played; the dongle stays while the squelch is open, hangs on FOLLOW_HANG_S after it closes, then
# the next hit takes it. Not digital trunked bands (P25/DMR: rtl_fm would play buzzing), not broadcast.
FOLLOW_BANDS = [(26.96, 27.41, "am"), (29.6, 54.0, "auto"), (118.0, 137.0, "am"), (137.0, 174.0, "auto"),
                (216.0, 225.0, "auto"), (400.0, 470.0, "auto")]
FOLLOW_HANG_S = 3.0
FOLLOW_MIN_S = 4.0          # once tuned, give the squelch this long to open before another hit can take over
FOLLOW_MAX_S = 30.0         # a channel gets at most this long, then the next waiting signal takes the dongle
FOLLOW_PROBE_S = AUTO_NO_CARRIER_WAIT_S + 3.0   # AUTO still deciding (no audio flows while dsd-fme probes): not "quiet"
FOLLOW_COOLDOWN_S = 60.0    # a channel just played isn't returned to for this long (a busy repeater would hog it)
FOLLOW_RECENT_S = 60.0      # a hit older than this is no longer a candidate
FOLLOW_TTL_S = 15 * 60.0    # Follow stops by itself this long after the app's last renewal (it renews every minute)


def follow_busy(now: float) -> bool:
    """The current channel keeps the dongle: still tuning, still being probed for a digital sync, just
    tuned, or audio in the last few seconds."""
    if LISTEN["proc"] is None or now - LISTEN["tuned_at"] < FOLLOW_MIN_S or now - LISTEN["last_audio"] < FOLLOW_HANG_S:
        return True
    return (LISTEN["mode"] == "auto" or LISTEN["mode"] in DIGITAL_MODES) and now - LISTEN["chan_at"] < FOLLOW_PROBE_S


def follow_mode(mhz: float):
    for lo, hi, mode in FOLLOW_BANDS:
        if lo <= mhz <= hi:
            return mode
    return None


IMA_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]
IMA_STEP = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88,
            97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
            724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660,
            4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
            18500, 20350, 22385, 24623, 27086, 29794, 32767]


class Ima:
    """IMA ADPCM encoder (the DVI/IMA tables every decoder knows). Each frame carries the predictor
    and step index it starts from, so a lost frame costs 40 ms of audio and nothing after it."""

    def __init__(self):
        self.pred, self.idx = 0, 0

    def frame(self, seq: int, pcm: bytes) -> bytes:
        n = len(pcm) // 2
        head = struct.pack("<BhB", seq & 0xFF, self.pred, self.idx)
        out = bytearray(head)
        pred, idx = self.pred, self.idx
        byte, high = 0, True
        for s in struct.unpack(f"<{n}h", pcm[:n * 2]):
            step = IMA_STEP[idx]
            diff = s - pred
            code = 0
            if diff < 0:
                code, diff = 8, -diff
            d = step >> 3
            if diff >= step:
                code |= 4
                diff -= step
                d += step
            step >>= 1
            if diff >= step:
                code |= 2
                diff -= step
                d += step
            step >>= 1
            if diff >= step:
                code |= 1
                d += step
            pred = pred - d if code & 8 else pred + d
            pred = -32768 if pred < -32768 else 32767 if pred > 32767 else pred
            idx += IMA_INDEX[code & 7]
            idx = 0 if idx < 0 else 88 if idx > 88 else idx
            if high:
                byte = code << 4
            else:
                out.append(byte | code)
            high = not high
        if not high:
            out.append(byte)
        self.pred, self.idx = pred, idx
        return bytes(out)


def follow_key(mhz: float) -> int:
    return int(round(mhz / 0.0125))         # one channel = one 12.5 kHz slot


# Automatic trunk following (Lab). A trunked system's control channel is the site frequency that never
# stops transmitting; the sweep sees it on every pass while voice channels come and go. Every hit on a
# RadioReference "TRS" row is counted here per site; asked to follow a system, the bridge tries the
# site's most constant frequency first and the next if dsd-fme finds no control-channel sync.
TRUNK = {"auto": False, "qpsk": False, "seen": {}, "tries": [], "site": None, "cc_at": 0.0, "map": ""}
TRUNK_SEEN_S = 180.0        # a site frequency counts as a candidate for this long after its last hit
TRUNK_CC_WAIT_S = 12.0      # dsd-fme must show a sync on a candidate within this, else the next is tried
TRUNK_IDLE_S = 240.0        # Follow leaves a trunked system after this long without a call


def trunk_note(obj: dict) -> None:
    x = obj.get("x") or {}
    if "trunked_site" not in x:
        return
    try:
        mhz = round(float(str(x.get("exact_mhz", "")).split()[0]), 4)
    except (ValueError, IndexError):
        return
    try:
        lic = float(x.get("lic_mhz") or 0)
    except ValueError:
        lic = 0.0
    if lic:
        mhz = round(lic, 5)
    site = (x.get("rr_agency") or "?", x.get("rr_description") or "?", (x.get("rr_mode") or "").lower())
    n, _, _, _ = TRUNK["seen"].get(mhz, (0, 0.0, -200, site))
    try:
        lvl = int(obj.get("r") or -200)
    except (TypeError, ValueError):
        lvl = -200
    # An always-on carrier (the sweep reports those once a minute) is the control channel's signature:
    # it outweighs the once-per-pass reports of voice channels that come and go.
    TRUNK["seen"][mhz] = (n + (12 if "always_on" in x else 1), time.time(), lvl, site)


def trunk_candidates(site=None):
    """Site frequencies seen lately, most constant (then strongest) first; site=None → the busiest site."""
    now = time.time()
    seen = {m: v for m, v in TRUNK["seen"].items() if now - v[1] <= TRUNK_SEEN_S and v[3][2] in ("project 25", "dmr", "nxdn", "edacs")}
    if not seen:
        return None, []
    if site is None:
        by_site = {}
        for m, (n, _, _, st) in seen.items():
            by_site[st] = by_site.get(st, 0) + n
        site = max(by_site, key=by_site.get)
    cands = sorted((m for m, v in seen.items() if v[3] == site), key=lambda m: (-seen[m][0], -seen[m][2]))
    return site, cands


def trunk_start(site=None, qpsk=None) -> bool:
    """Follow a trunked system from its control channel, found from the sweep. False when nothing is known."""
    site, cands = trunk_candidates(site)
    if not cands:
        return False
    qpsk = TRUNK["qpsk"] if qpsk is None else qpsk
    kind = site[2]
    if kind == "edacs":
        mode = "trunke"
    elif kind == "dmr":
        mode = "trunkd"
    else:
        mode = "trunkq" if (qpsk or kind == "project 25") else "trunk"   # P25 county systems are simulcast: QPSK
    TRUNK["map"] = write_trunk_map(site) if mode in ("trunke", "trunkd") else ""
    TRUNK["site"], TRUNK["tries"], TRUNK["cc_at"] = site, cands[1:5], time.time()
    listen_status(f"trunked: trying {cands[0]:.4f} MHz as the control channel of {site[0]} ({site[1]})")
    listen_start(cands[0], mode, LISTEN["sq"], 0.0)
    return True


def write_trunk_map(site) -> str:
    """dsd-fme channel map for a site: 'lcn,hz' per line, LCN/LSN by ascending frequency (the common
    convention; not guaranteed). All of the site's frequencies the overlay lists, not just those heard."""
    if not RR:
        return ""
    freqs = sorted({f for f, side, r in RR.rows if side == "output" and (r.get("Tag") or "").upper() == "TRS"
                    and (r.get("Agency/Category") or "?") == site[0] and (r.get("Description") or "?") == site[1]})
    if not freqs:
        return ""
    path = "/tmp/wavelens-trunk-map.csv"
    try:
        with open(path, "w") as f:
            for i, mhz in enumerate(freqs, 1):
                f.write(f"{i},{int(round(mhz * 1e6))}\n")
    except OSError:
        return ""
    print(f"bridge: trunk map for {site[0]} ({site[1]}): {len(freqs)} channels, LCN by ascending frequency", file=sys.stderr)
    return path


def trunk_next(why: str) -> None:
    """The candidate showed no control channel: the next one, or give up."""
    if TRUNK["tries"]:
        nxt = TRUNK["tries"].pop(0)
        TRUNK["cc_at"] = time.time()
        site = TRUNK["site"] or ("?", "?", "")
        listen_status(f"trunked: {why}; trying {nxt:.4f} MHz for {site[0]}")
        listen_start(nxt, LISTEN["mode"], LISTEN["sq"], 0.0)
    else:
        listen_stop("no control channel found")
        listen_status("trunked: no control channel found among the site frequencies seen; type one on the Companion screen")


def follow_hit(obj: dict) -> None:
    x = obj.get("x") or {}
    if "trunked_site" in x:
        # A trunked site's channel is not voice to play; with automatic trunking on, the system is followed instead.
        if TRUNK["auto"] and (LISTEN["req"] is None or LISTEN["mode"] not in TRUNK_MODES):
            site = (x.get("rr_agency") or "?", x.get("rr_description") or "?", (x.get("rr_mode") or "").lower())
            if LISTEN["req"] is None or not follow_busy(time.time()):
                trunk_start(site)
        return
    if "always_on" in x or "background" in x:
        return
    tol = 0.05
    try:
        parts = str(x.get("exact_mhz", "")).split()
        mhz = float(parts[0])
        if len(parts) >= 3:
            tol = float(parts[2])
    except (ValueError, IndexError):
        mhz = float(obj.get("f") or 0)
    mode = follow_mode(mhz)
    if mode is None or locked_out(mhz):
        return
    known = str(x.get("listen") or "")      # the RadioReference overlay's mode for this channel
    if known in ("fm", "am", "dmr", "p25", "nxdn"):
        mode = known                         # no 8 s probe: tune it as what it is
    try:
        lic = float(x.get("lic_mhz") or 0)   # the licence's own centre beats any channel-plan guess
    except ValueError:
        lic = 0.0
    if lic and abs(lic - mhz) <= max(tol, 0.0125):
        mhz, tol = lic, 0.0
    now = time.time()
    LISTEN["recent"][follow_key(mhz)] = (now, mhz, mode, tol)
    if LISTEN["req"] is not None:
        if abs(LISTEN["mhz"] - mhz) < 0.02:
            LISTEN["until"] = now + LISTEN_MAX_S
            return                           # already on it
        if follow_busy(now):
            return                           # still tuning or probing, or busy with a live one: it's queued in "recent"
    follow_next(now)


def follow_next(now: float) -> bool:
    """Tune the newest candidate that hasn't just been played. False when nothing is waiting."""
    recent, played = LISTEN["recent"], LISTEN["played"]
    for k in [k for k, (seen, _, _, _) in recent.items() if now - seen > FOLLOW_RECENT_S]:
        del recent[k]
    for k in [k for k, left in played.items() if now - left > FOLLOW_COOLDOWN_S]:
        del played[k]
    cur = follow_key(LISTEN["mhz"]) if LISTEN["req"] is not None else None
    cands = [(seen, k, mhz, mode, tol) for k, (seen, mhz, mode, tol) in recent.items() if k not in played and k != cur]
    if not cands:
        return False
    _, _, mhz, mode, tol = max(cands)
    listen_start(mhz, mode, LISTEN["sq"], tol)
    return True


def follow_leave(now: float, why: str) -> None:
    """Done with the current channel: remember it so it isn't returned to at once, then move on or wait."""
    if LISTEN["req"] is not None:
        LISTEN["played"][follow_key(LISTEN["mhz"])] = now
    if not follow_next(now):
        listen_stop(why)
        listen_status(f"follow: {why}; waiting for the next signal")


def follow_watch() -> None:
    while True:
        time.sleep(1)
        now = time.time()
        if not LISTEN["follow"]:
            continue
        if now > LISTEN["follow_until"]:
            LISTEN["follow"] = False
            listen_stop("follow timed out (5 min without the app renewing it)")
            listen_status("follow ended")
            continue
        if LISTEN["proc"] is None:
            continue
        if LISTEN["mode"] in TRUNK_MODES:
            if now - max(LISTEN["chan_at"], LISTEN.get("call_at", 0.0)) >= TRUNK_IDLE_S:
                follow_leave(now, f"no call on the trunked system for {TRUNK_IDLE_S / 60:.0f} min")
            continue
        if now - LISTEN["chan_at"] >= FOLLOW_MAX_S:
            follow_leave(now, f"{FOLLOW_MAX_S:.0f} s on {LISTEN['mhz']:.4f} MHz is enough")
        elif not follow_busy(now):
            # It went quiet: take the newest waiting signal, if any. The channel just left gets the same
            # cooldown as one that used its 30 s, or two dead carriers would be tuned turn and turn about.
            if LISTEN["req"] is not None and LISTEN["recent"] and any(k != follow_key(LISTEN["mhz"]) and k not in LISTEN["played"] for k in LISTEN["recent"]):
                LISTEN["played"][follow_key(LISTEN["mhz"])] = now
            follow_next(now)


def listen_busy(dev: str) -> bool:
    """The dongle is spoken for from the moment a listen is asked for until it ends — not just while
    rtl_fm runs. Otherwise the sweep restarts in the gap and grabs it back (usb_claim_interface -6)."""
    return (LISTEN["req"] is not None or LISTEN["proc"] is not None or LISTEN["starting"]) and LISTEN["dev"] == dev


def listen_status(msg: str) -> None:
    on = LISTEN["proc"] is not None
    emit({"t": "status", "msg": msg, "band": state["band"], "mode": state["mode"],
          "listen": {"mhz": LISTEN["mhz"] if on else 0, "mode": LISTEN["mode"] if on else "",
                     "asked": LISTEN["asked"] if on else "", "detected": LISTEN["detected"] if on else "",
                     "call": LISTEN["call"] if on else "",
                     "sq": LISTEN["sq"], "follow": bool(LISTEN["follow"]),
                     "lockout": LISTEN["lockout"]} if (on or LISTEN["follow"]) else "off"})


def listen_stop(why: str = "stopped") -> None:
    with LISTEN_CV:
        was = LISTEN["req"] is not None
        LISTEN["req"] = None
        LISTEN["gen"] += 1
        LISTEN_CV.notify()
    if was:
        print(f"bridge: listen: {why}", file=sys.stderr)


def listen_start(mhz: float, mode: str, sq: int, tol: float = 0.05) -> None:
    """Ask the listener thread for a frequency. One thread, one rtl_fm: a new request while one is
    starting or playing replaces it (the generation changes and the old one winds down).
    mhz is where the sweep placed the carrier, tol how far it may be off; the listen is on the likeliest
    channel for that (snap_channel), and later sweep readings may move it (hone)."""
    mode = mode if mode in LISTEN_MODES or mode in DIGITAL_MODES else "fm"
    raw = mhz
    mhz = round(mhz, 4) if mode in TRUNK_MODES else snap_channel(mhz, tol)   # a control channel is given exactly
    with LISTEN_CV:
        same = (LISTEN["req"] is not None and abs(LISTEN["raw_mhz"] - raw) < 0.0005 and LISTEN["asked"] == mode
                and LISTEN["sq"] == sq)
        LISTEN["until"] = time.time() + LISTEN_MAX_S
        if same:
            return                           # a renewal: just push the deadline out
        if LISTEN["req"] is not None and abs(LISTEN["mhz"] - mhz) < 0.0005 and LISTEN["asked"] == mode and LISTEN["sq"] == sq:
            LISTEN["raw_mhz"] = raw          # another reading of the same channel: nothing to retune
            return
        LISTEN["req"] = (mhz, mode, sq)
        LISTEN["mhz"], LISTEN["mode"], LISTEN["asked"], LISTEN["sq"] = mhz, mode, mode, sq
        LISTEN["raw_mhz"], LISTEN["tol"], LISTEN["readings"], LISTEN["honed_at"] = raw, tol, [raw], 0.0
        LISTEN["carrier_at"], LISTEN["detected"], LISTEN["call"] = 0.0, "", ""
        LISTEN["chan_at"] = time.time()
        LISTEN["gen"] += 1
        LISTEN_CV.notify()
    if abs(mhz - raw) >= 0.0005:
        print(f"bridge: listen: {raw:.4f} read as channel {mhz:.4f}", file=sys.stderr)


def listen_resolve(mode: str, why: str, mhz: "float | None" = None) -> None:
    """Restart the current listen with a definite mode and/or a corrected frequency. The asked mode
    stays what the app asked ("auto"), so its renewals don't undo the decision."""
    with LISTEN_CV:
        if LISTEN["req"] is None:
            return
        cur_mhz, cur_mode, sq = LISTEN["req"]
        mhz = cur_mhz if mhz is None else mhz
        LISTEN["req"] = (mhz, mode, sq)
        LISTEN["mode"], LISTEN["mhz"] = mode, mhz
        LISTEN["gen"] += 1
        LISTEN_CV.notify()
    print(f"bridge: listen: → {mhz:.4f} MHz {mode} ({why})", file=sys.stderr)


def hone(reading: float) -> None:
    """Each sweep reading of the tuned carrier refines its position; when the average lands on another
    channel than the one tuned, retune there. A HackRF's ±25 kHz is more than a 12.5 kHz channel."""
    if LISTEN["mode"] in TRUNK_MODES or LISTEN["tol"] == 0.0:
        return                               # dsd-fme owns the tuning on a trunked system; a licensed centre is exact
    rd = LISTEN["readings"]
    rd.append(reading)
    del rd[:-8]
    now = time.time()
    if len(rd) < HONE_MIN_READINGS or now - LISTEN["honed_at"] < HONE_RATE_S or LISTEN["proc"] is None:
        return
    mean = sum(rd) / len(rd)
    target = snap_channel(mean, LISTEN["tol"] / len(rd) ** 0.5 + 0.003)
    if abs(target - LISTEN["mhz"]) >= 0.002:
        LISTEN["honed_at"] = now
        del rd[:]
        listen_resolve(LISTEN["req"][1], f"honed from {len(rd) or HONE_MIN_READINGS} readings averaging {mean:.4f}", mhz=target)
        listen_status(f"retuned to {target:.4f} MHz: the carrier averages {mean:.4f}")


def listen_worker() -> None:
    while True:
        with LISTEN_CV:
            while LISTEN["req"] is None:
                LISTEN_CV.wait()
            req, gen = LISTEN["req"], LISTEN["gen"]
        serve_listen(req, gen)


SYNC_RE = re.compile(r"Sync:\s*[+-]?\s*([A-Za-z0-9]+)")


def stop_tools(procs: list) -> None:
    """Stop a listen's tools, client first (dsd-fme, then rtl_tcp: rtl_tcp can sit on the dongle until its
    client is gone), and kill what hasn't exited within a second and a half. A tool that lingers keeps the
    dongle, and then neither the sweep nor the next listen can open it (usb_claim_interface -6)."""
    for q in reversed(procs):
        try:
            q.terminate()
        except Exception:
            pass
    deadline = time.time() + 1.5
    for q in reversed(procs):
        while q.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        if q.poll() is None:
            try:
                q.kill()
                q.wait(timeout=1)
            except Exception:
                pass


def kill_strays() -> None:
    """A bridge that died mid-listen leaves rtl_tcp/dsd-fme holding the dongle: clear them before starting."""
    subprocess.call(["pkill", "-f", f"rtl_tcp -a 127.0.0.1 -p {RTL_TCP_PORT}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call(["pkill", "-f", f"udp:127.0.0.1:{DSD_UDP_PORT}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_tool(cmd: str, gen: int, attempts: int = 12, **kw):
    """Start a radio tool, retrying while the dongle is still being released by the sweep.
    Returns the process, None if it never came up, or False if the tool isn't installed."""
    for attempt in range(attempts):
        if LISTEN["gen"] != gen:
            return None                      # superseded while we were getting the dongle
        try:
            p = subprocess.Popen(shlex.split(cmd), stdout=kw.get("stdout", subprocess.DEVNULL), stderr=subprocess.PIPE)
        except FileNotFoundError:
            return False
        p.err_tail = deque(maxlen=3)
        p.sync, p.sync_at = "", 0.0
        p.call, p.call_at = "", 0.0

        def drain(proc=p):                   # dsd-fme chatters on stderr; an undrained pipe would stall it
            for ln in proc.stderr:
                ln = ln.decode("utf-8", "replace").strip()
                if not ln:
                    continue
                proc.err_tail.append(ln)
                if kw.get("watch_sync"):
                    m = SYNC_RE.search(ln)
                    if m and m.group(1).lower() not in ("no", "none", "lost"):
                        proc.sync, proc.sync_at = m.group(1), time.time()
                    m = CALL_RE.search(ln)
                    if m:
                        src = SRC_RE.search(ln)
                        enc = ENC_RE.search(ln)
                        tok = enc.group(1).strip() if enc else ""
                        clear = bool(re.search(r"(?i)alg(?:id)?\s*[:=]?\s*(?:0x80|0x0|0|128)$", tok))   # P25 ALGID 0x80 = clear
                        proc.call = (f"TG {m.group(1)}" + (f" from {src.group(1)}" if src else "")
                                     + (f" · encrypted ({tok})" if tok and not clear else ""))
                        proc.call_at = time.time()
        threading.Thread(target=drain, daemon=True).start()
        time.sleep(0.5)
        if p.poll() is None:
            return p
    return None


def start_pager(gen: int, mhz: float):
    """multimon-ng on the analog audio; its decoded pages go to the phone as {"t":"page"} lines."""
    try:
        pm = subprocess.Popen(shlex.split(PAGER_CMD), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return None

    def reader():
        for raw in pm.stdout:
            ln = raw.decode("utf-8", "replace").strip()
            m = PAGE_RE.match(ln)
            if not m or LISTEN["gen"] != gen:
                continue
            proto, rest = m.group(1), m.group(2)
            am = PAGE_ADDR_RE.search(rest)
            mm = PAGE_MSG_RE.search(rest)
            msg = ((mm.group(1) or mm.group(2)) if mm else rest).strip()
            if not msg:
                continue
            kind = "FLEX" if proto == "FLEX" else "POCSAG"
            if LISTEN["detected"] != kind:
                LISTEN["detected"] = kind
                listen_status(f"{kind} pager data on {mhz:.4f} MHz: pages shown, audio muted")
            emit({"t": "page", "f": round(mhz, 4), "proto": proto, "addr": (am.group(1) or am.group(2)) if am else "",
                  "msg": msg[:300], "src": "rtlfm"})
    threading.Thread(target=reader, daemon=True).start()
    return pm


def serve_listen(req: tuple, gen: int) -> None:
    LISTEN["starting"] = True
    try:
        _serve_listen(req, gen)
    finally:
        LISTEN["starting"] = False


def _serve_listen(req: tuple, gen: int) -> None:
    mhz, mode, sq = req
    dev = f" -d {shlex.quote(LISTEN['dev'])}" if LISTEN["dev"] else ""
    procs = []
    sock = None
    pager, out_rate, demod_rate = False, LISTEN_RATE, float(AFC_RATE_IN)
    if mode in DIGITAL_MODES:
        flag, chans = DIGITAL_MODES[mode]
        kill_strays()
        p = start_tool(f"rtl_tcp -a 127.0.0.1 -p {RTL_TCP_PORT}{dev}", gen)
        if p is False:
            listen_stop("rtl_tcp missing"); listen_status("rtl_tcp not found on the Pi (apt install rtl-sdr)"); return
        if p is None:
            if LISTEN["gen"] == gen:
                listen_stop("no dongle"); listen_status("could not open the RTL-SDR for listening (is it pinned with @SERIAL?)")
            return
        procs.append(p)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", DSD_UDP_PORT))
        sock.settimeout(0.5)
        time.sleep(0.8)                      # rtl_tcp needs a moment before it accepts a client
        # rtltcp:host:port:freq:gain:ppm:bw(kHz):squelch:volume — dsd-fme does its own FM demodulation.
        # No -N: that turns the ncurses screen ON, and under systemd there is no terminal to open.
        bw = 24 if mode in TRUNK_MODES else 12
        cmap = f" -C {shlex.quote(TRUNK['map'])}" if mode in ("trunke", "trunkd") and TRUNK.get("map") else ""
        d = start_tool(f"dsd-fme {flag}{cmap} -i rtltcp:127.0.0.1:{RTL_TCP_PORT}:{mhz:.4f}M:40:{LISTEN['ppm']}:{bw}:{sq}:2 "
                       f"-o udp:127.0.0.1:{DSD_UDP_PORT}", gen, attempts=4, watch_sync=True)
        if d is False or d is None:
            stop_tools(procs)
            sock.close()
            if d is False:
                listen_stop("dsd-fme missing")
                listen_status("digital voice needs dsd-fme on the Pi: run companion/pi/install-dsd.sh (10 min build)")
            elif LISTEN["gen"] == gen:
                listen_stop("dsd-fme failed"); listen_status("dsd-fme could not start; see journalctl -u wavelens-bridge")
            return
        procs.append(d)
        p = d
    else:
        chans = 1
        ppm = f" -p {LISTEN['ppm']}" if LISTEN["ppm"] else ""
        dc = "" if mode == "fm" else " -E dc"    # FM keeps its DC: it is the AFC reading (removed below)
        # With a pager decoder available, narrow FM is demodulated at its rate and fed to both the
        # decoder and (resampled) the phone; without it, 16 kHz in and 8 kHz out as before.
        pager = mode == "fm" and shutil.which("multimon-ng") is not None
        rate_in = PAGER_RATE if pager else int(AFC_RATE_IN)
        demod_rate = float(rate_in)
        demod = f"-M fm -s {rate_in}" if mode == "fm" else LISTEN_MODES[mode]
        out_rate = rate_in if pager else LISTEN_RATE
        cmd = f"rtl_fm -f {mhz:.4f}M {demod} -r {out_rate} -l {sq}{dc} -g 40{ppm}{dev} -"
        p = start_tool(cmd, gen, stdout=subprocess.PIPE)
        if p is False:
            listen_stop("rtl_fm missing"); listen_status("rtl_fm not found on the Pi (apt install rtl-sdr)"); return
        if p is None:
            if LISTEN["gen"] == gen:
                listen_stop("no dongle"); listen_status("could not open the RTL-SDR for listening (is it pinned with @SERIAL?)")
            return
        procs.append(p)
        if pager:
            pm = start_pager(gen, mhz)
            if pm is not None:
                procs.append(pm)
            else:
                pager = False
    LISTEN["proc"] = p
    LISTEN["starting"] = False
    LISTEN["tuned_at"] = LISTEN["last_audio"] = time.time()
    print(f"bridge: listen: {mhz:.4f} MHz {mode} (squelch {sq})", file=sys.stderr)
    listen_status(f"listening on {mhz:.4f} MHz ({mode.upper()}); the RTL-SDR sweep is paused")
    enc = Ima()
    in_rate = out_rate if sock is None else LISTEN_RATE
    need = int(in_rate * LISTEN_FRAME / LISTEN_RATE) * 2      # one 40 ms frame at the input rate
    buf = b""

    def watch():                             # squelched, the tools send nothing, so the reads below can't notice
        nonlocal chans
        while p.poll() is None:
            if LISTEN["gen"] != gen:
                break                        # retuned or stopped
            now = time.time()
            if now > LISTEN["until"]:
                listen_stop("timed out (5 min); tap Listen again to continue")
                break
            if mode in TRUNK_MODES:
                call = getattr(p, "call", "")
                if call and now - p.call_at < 8.0:
                    if LISTEN["call"] != call:
                        LISTEN["call"] = call
                        LISTEN["call_at"] = now
                        listen_status(f"trunked call: {call}")
                if not LISTEN["detected"] and not call and now - LISTEN["tuned_at"] >= TRUNK_CC_WAIT_S and TRUNK["cc_at"]:
                    TRUNK["cc_at"] = 0.0         # once per candidate
                    trunk_next(f"no control channel on {mhz:.4f} MHz")
                    break
                elif LISTEN["call"]:
                    LISTEN["call"] = ""
                    listen_status(f"on the control channel {mhz:.4f} MHz, waiting for a call")
                sync = getattr(p, "sync", "")
                if sync and now - p.sync_at < 5.0 and LISTEN["detected"] != sync:
                    LISTEN["detected"] = sync
                    chans = 2 if sync.upper().startswith(TDMA_SYNCS) else 1
            elif mode == "auto":
                sync = getattr(p, "sync", "")
                if sync and now - p.sync_at < 5.0:
                    if LISTEN["detected"] != sync:
                        LISTEN["detected"] = sync
                        chans = 2 if sync.upper().startswith(TDMA_SYNCS) else 1
                        listen_status(f"{sync} on {mhz:.4f} MHz")
                elif not LISTEN["detected"]:
                    carrier = LISTEN["carrier_at"]
                    since_tune = now - LISTEN["tuned_at"]
                    if (carrier and now - carrier < 2.0 and since_tune >= AUTO_WAIT_S) or since_tune >= AUTO_NO_CARRIER_WAIT_S:
                        LISTEN["detected"] = "FM"
                        listen_resolve("fm", "carrier with no digital sync")
                        break
            time.sleep(0.5)
        stop_tools(procs)
    threading.Thread(target=watch, daemon=True).start()

    def read_chunk():
        if sock is None:
            return p.stdout.read(need - len(buf))
        while p.poll() is None and LISTEN["gen"] == gen:
            try:
                data = sock.recv(8192)
            except socket.timeout:
                continue
            if chans == 2:                   # two TDMA slots, L and R: mix to mono
                n = len(data) // 4
                lr = struct.unpack(f"<{2 * n}h", data[:4 * n])
                data = struct.pack(f"<{n}h", *[max(-32768, min(32767, (lr[2 * i] + lr[2 * i + 1]) // 2)) for i in range(n)])
            return data
        return b""

    ended_itself = True
    afc = []                                 # offsets (Hz) from frames with signal, analog FM only

    def afc_frame(pcm: bytes) -> bytes:
        """Measure the carrier offset from this frame's DC level, then remove the DC so it plays clean.
        Retune when enough frames agree the carrier sits on another channel."""
        n = len(pcm) // 2
        samples = struct.unpack(f"<{n}h", pcm)
        mean = sum(samples) / n
        if max(samples) - min(samples) > 600:            # audio present, not the squelch tail
            afc.append(mean / 16384.0 * (demod_rate / 2))
            if len(afc) >= AFC_FRAMES:
                srt = sorted(afc)
                off_hz = srt[len(srt) // 2]
                del afc[:]
                now = time.time()
                if abs(off_hz) >= AFC_MIN_HZ and now - LISTEN["honed_at"] >= HONE_RATE_S:
                    target = snap_channel(mhz + off_hz / 1e6, 0.003)
                    if abs(target - mhz) >= 0.002:
                        LISTEN["honed_at"] = now
                        listen_resolve(mode, f"carrier {off_hz:+.0f} Hz from the tuned frequency", mhz=target)
                        listen_status(f"retuned to {target:.4f} MHz: the carrier was {off_hz / 1000:+.1f} kHz off")
        m = int(mean)
        if pager:
            try:                                         # the decoder gets the full-rate audio, DC removed
                procs[-1].stdin.write(struct.pack(f"<{n}h", *[max(-32768, min(32767, v - m)) for v in samples]))
                procs[-1].stdin.flush()
            except Exception:
                pass
        if n != LISTEN_FRAME:                            # 22050 → 8000 for the phone: linear interpolation
            step = n / LISTEN_FRAME
            out = []
            for i in range(LISTEN_FRAME):
                x = i * step
                j = int(x)
                j2 = min(j + 1, n - 1)
                v = samples[j] + (samples[j2] - samples[j]) * (x - j) - m
                out.append(max(-32768, min(32767, int(v))))
            return struct.pack(f"<{LISTEN_FRAME}h", *out)
        return struct.pack(f"<{n}h", *[max(-32768, min(32767, v - m)) for v in samples])

    try:
        while True:
            chunk = read_chunk()
            if not chunk:
                ended_itself = LISTEN["gen"] == gen
                break
            buf += chunk
            while len(buf) >= need:
                pcm, buf = buf[:need], buf[need:]
                if sock is None and mode == "fm":
                    pcm = afc_frame(pcm)
                LISTEN["last_audio"] = time.time()
                if LISTEN["detected"] in ("POCSAG", "FLEX"):
                    continue                 # pager data: nothing to hear; the pages go as lines
                LISTEN["seq"] = (LISTEN["seq"] + 1) & 0xFF
                try:
                    aout.put_nowait(enc.frame(LISTEN["seq"], pcm))
                except Exception:
                    pass                     # the link is behind: drop this frame, keep the latest
    finally:
        stop_tools(procs)                    # the dongle stays "busy" until the tools are gone (listen_busy)
        LISTEN["proc"] = None
        if sock is not None:
            sock.close()
        if ended_itself and LISTEN["req"] is not None and LISTEN["gen"] == gen:
            if time.time() > LISTEN["until"]:
                listen_status("listening timed out after 5 min; tap Listen again to continue")
            else:
                time.sleep(0.2)
                err = list(getattr(p, "err_tail", []))[-2:]
                listen_stop(f"{procs[-1].args[0]} exited")
                listen_status("listening ended: " + (" | ".join(err) or "the decoder exited"))
        elif LISTEN["req"] is None:
            listen_status("listening stopped; sweep resumes")


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
    if (FCC or RR) and cat == "UNKNOWN" and freq_f:
        lic = licensed_label(freq_f, 12.5)
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


def drop_stale_centrals() -> None:
    """A restart leaves the phone's connection alive in BlueZ while the GATT server it was talking to
    is gone: the app shows "connected" and hears nothing. Disconnect whoever is still attached, so the
    app sees the drop and reconnects to this instance."""
    try:
        import dbus
        bus = dbus.SystemBus()
        om = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
        for path, ifaces in om.GetManagedObjects().items():
            d = ifaces.get("org.bluez.Device1")
            if d and d.get("Connected"):
                try:
                    dbus.Interface(bus.get_object("org.bluez", path), "org.bluez.Device1").Disconnect()
                    print("bridge: dropped a central left over from the previous run; it will reconnect", file=sys.stderr)
                except Exception as e:
                    print(f"bridge: could not drop a stale central ({e})", file=sys.stderr)
    except Exception as e:
        print(f"bridge: stale-central check skipped ({e})", file=sys.stderr)


def run_peripheral(name: str) -> None:
    from bluezero import adapter, peripheral
    from bluezero import advertisement as bz_ad

    ADVERT_MARK = "/run/wavelens-advert-failed"   # run-bridge.sh restarts bluetooth when it finds this

    def ad_failed(error):
        # Without the advert the phone can't find the companion, while the radios keep running:
        # a half-alive bridge. Leave a mark so the next start restarts BlueZ, and exit.
        print(f"bridge: Bluetooth advertisement failed ({error}); exiting so the service restarts bluetooth and retries",
              file=sys.stderr)
        try:
            open(ADVERT_MARK, "w").close()
        except OSError:
            pass
        os._exit(1)

    def ad_ok():
        print("Advertisement registered", file=sys.stderr)
        try:
            os.remove(ADVERT_MARK)
        except OSError:
            pass
    bz_ad.register_ad_error_cb = ad_failed      # bluezero looks these names up when it registers
    bz_ad.register_ad_cb = ad_ok

    dongle = list(adapter.Adapter.available())[0]
    code = dongle.address.replace(":", "")[-4:].upper()
    if name in ("", "Wavelens Pi", "Wavelens-Pi"):
        name = f"Wavelens-{code}"          # same scheme as the ESP32: the code is the end of the BT address
    state["name"], state["code"] = name, code
    drop_stale_centrals()
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
        elif c == "listen":
            if "listen" not in CAPS:
                emit({"t": "status", "msg": "no RTL-SDR to listen with (add --rtl-power …@SERIAL or --listen-device)",
                      "band": state["band"], "mode": state["mode"], "listen": "off"})
            elif str(cmd.get("v") or "") == "off":
                LISTEN["follow"] = False
                listen_stop("stopped")
                listen_status("listening stopped")
            elif str(cmd.get("v") or "") == "lockout":
                # The phone's list of frequencies Follow must never tune (it resends it on every connect).
                try:
                    lst = sorted({round(float(f), 4) for f in (cmd.get("mhz") or [])})[:200]
                except (TypeError, ValueError):
                    lst = []
                LISTEN["lockout"] = lst
                for k in [k for k, (_, m, _, _) in LISTEN["recent"].items() if locked_out(m)]:
                    del LISTEN["recent"][k]
                if LISTEN["follow"] and LISTEN["req"] is not None and locked_out(LISTEN["mhz"]):
                    follow_leave(time.time(), f"{LISTEN['mhz']:.4f} MHz locked out")
                emit({"t": "status", "msg": f"lockout: {len(lst)} frequencies Follow will skip", "band": state["band"],
                      "mode": state["mode"], "lockout": lst})
            elif str(cmd.get("v") or "") == "trunkauto":
                TRUNK["auto"] = bool(cmd.get("on", True))
                TRUNK["qpsk"] = bool(cmd.get("qpsk", TRUNK["qpsk"]))
                listen_status("trunked systems: Follow takes them automatically" if TRUNK["auto"] else "trunked systems: manual only")
            elif str(cmd.get("v") or "") == "trunk":
                TRUNK["qpsk"] = bool(cmd.get("qpsk", TRUNK["qpsk"]))
                try:
                    mhz = float(cmd.get("mhz") or 0)
                except (TypeError, ValueError):
                    mhz = 0.0
                if mhz and 24.0 <= mhz <= 1766.0:
                    TRUNK["tries"], TRUNK["cc_at"] = [], 0.0
                    listen_start(mhz, "trunkq" if TRUNK["qpsk"] else "trunk", LISTEN["sq"], 0.0)
                elif not trunk_start(None):
                    listen_status("trunked: no trunked-system site seen by the sweep yet (needs the RadioReference overlay); type a control channel")
            elif str(cmd.get("v") or "") == "follow":
                sq = cmd.get("sq")
                if sq is not None:
                    LISTEN["sq"] = max(0, min(1000, int(sq)))
                fresh = not LISTEN["follow"]
                LISTEN["follow"], LISTEN["follow_until"] = True, time.time() + FOLLOW_TTL_S
                if fresh:
                    listen_status("following hits: each new carrier in a voice band is tuned and played")
            else:
                try:
                    mhz = float(cmd.get("mhz"))
                except (TypeError, ValueError):
                    return
                if 24.0 <= mhz <= 1766.0:
                    sq = cmd.get("sq")
                    try:
                        tol = max(0.0, min(0.5, float(cmd.get("tol", 0.05))))
                    except (TypeError, ValueError):
                        tol = 0.05
                    listen_start(mhz, str(cmd.get("mode") or "fm"),
                                 max(0, min(1000, int(sq))) if sq is not None else LISTEN["sq"], tol)
                else:
                    emit({"t": "status", "msg": f"{mhz:g} MHz is outside the RTL-SDR's 24–1766 MHz; can't listen there",
                          "band": state["band"], "mode": state["mode"], "listen": "off"})
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

    def au_notify_cb(notifying, characteristic):
        chars["au"] = characteristic if notifying else None
        if not notifying and LISTEN["req"] is not None:
            LISTEN["follow"] = False
            listen_stop("stopped (phone unsubscribed)")

    dev.add_characteristic(srv_id=1, chr_id=3, uuid=AU, value=[], notifying=False,
                           flags=["notify"], notify_callback=au_notify_cb)

    def pump():
        while True:
            au = chars.get("au")
            frame = None
            if au is not None:
                try:
                    frame = aout.get_nowait()
                except Empty:
                    frame = None
            if frame is not None:
                try:
                    au.set_value(list(frame))     # one 164-byte frame per notification, under a 247-byte MTU
                except Exception:
                    pass
                time.sleep(0.004)
                continue
            try:
                line = out.get(timeout=0.02 if au is not None else 0.5)
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
            # The radio tools go first, hard: rtl_fm / rtl_power ignore SIGTERM while blocked in a USB
            # read, and systemd would wait out TimeoutStopSec before killing them (a slow restart that
            # also leaves the phone's link dangling). Our own children only: pkill -P.
            try:
                subprocess.call(["pkill", "-KILL", "-P", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
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
    ap.add_argument("--hackrf-lna", type=int, default=32, help="HackRF LNA gain 0-40 dB in 8 dB steps (default 32); lower it if nearby transmitters overload the sweep")
    ap.add_argument("--hackrf-vga", type=int, default=20, help="HackRF baseband gain 0-62 dB in 2 dB steps (default 20)")
    ap.add_argument("--hackrf-mask", metavar="LO:HI[,LO:HI...]", default="",
                    help="MHz ranges never reported: this unit's own spurs, found with find_spurs.py (antenna off)")
    ap.add_argument("--hackrf-ppm", type=float, default=0.0,
                    help="HackRF crystal correction: reported MHz are scaled by (1 + ppm/1e6). Key a radio on a known "
                         "frequency, read exact_mhz, then ppm = (known - read) / read * 1e6 (typically -20..20)")
    ap.add_argument("--hackrf-park-range", metavar="LO:HI", default="",
                    help="MHz slice the HackRF parks on when the phone asks (default 796:816, public-safety radios)")
    ap.add_argument("--rtl-power", metavar="SPEC", action="append", default=[],
                    help="run rtl_power with this -f spec, e.g. 150M:174M:12.5k; repeat for several ranges, which take turns; "
                         "add @SERIAL to put a range on a specific dongle (one sweep per dongle runs in parallel)")
    ap.add_argument("--rtl-power-device", metavar="N|SERIAL", default="",
                    help="which RTL-SDR rtl_power uses: index or serial (see rtl_test); default the first free one")
    ap.add_argument("--listen-device", metavar="SERIAL", default="",
                    help="RTL-SDR the phone can listen with (rtl_fm); default: the first --rtl-power dongle pinned with @SERIAL")
    ap.add_argument("--listen-squelch", type=int, default=30,
                    help="rtl_fm squelch while listening (default 30; 0 = never mute, static between transmissions)")
    ap.add_argument("--rtl-gain", type=int, default=30,
                    help="RTL-SDR tuner gain for rtl_power in dB (default 30; 0 = the dongle's own AGC, which rides out a "
                         "handheld keyed beside it instead of smearing it over 10 MHz)")
    ap.add_argument("--rtl-ppm", type=int, default=0,
                    help="RTL-SDR crystal correction passed to rtl_power -p (rtl_test -p measures it; 0 for TCXO dongles)")
    ap.add_argument("--rtl-power-dwell", metavar="SECONDS", type=int, default=15,
                    help="seconds on each range when several --rtl-power ranges are given (default 15)")
    ap.add_argument("--dump1090", metavar="HOST:PORT", help="relay aircraft from a dump1090/readsb SBS port")
    ap.add_argument("--kismet-json", metavar="PATH", help="relay Wi-Fi devices from a Kismet JSON export")
    ap.add_argument("--fcc-pack", metavar="PATH", help="local FCC license pack from tools/fcc_local_pack.py; labels licensed carriers")
    ap.add_argument("--rr-pack", metavar="CSV", action="append", default=[],
                    help="a RadioReference frequency export (CSV); repeatable, most local first (county, then statewide, then "
                         "nationwide). Names hits by agency, description, alpha tag and mode, on both sides of a repeater pair. "
                         "Preferred over --fcc-pack where it has the frequency")
    ap.add_argument("--rr-county", metavar="NAME", action="append", default=[],
                    help="county (repeatable) whose rows in a statewide export rank ahead of other counties'")
    ap.add_argument("--echo", action="store_true", help="print every line sent to the phone on stderr (see it with journalctl -u wavelens-bridge -f)")
    ap.add_argument("--name", default="", help="advertised name (default Wavelens-XXXX from the adapter address)")
    a = ap.parse_args()

    global FCC, RR, ECHO, HACKRF_ON
    ECHO = a.echo
    if a.fcc_pack:
        FCC = FccPack(a.fcc_pack)
        CAPS.append("fcc")
        print(f"bridge: FCC pack loaded, {len(FCC.entries)} licensed frequencies", file=sys.stderr)
    if a.rr_pack:
        RR = RrPack(a.rr_pack, a.rr_county)
        CAPS.append("rr")
        print(f"bridge: RadioReference overlay loaded, {len(RR.rows)} frequencies from {len(a.rr_pack)} file(s)", file=sys.stderr)

    if a.kismet_json:
        CAPS.append("wifi")
        threading.Thread(target=feed_kismet_json, args=(a.kismet_json,), daemon=True).start()
    if a.hackrf_park_range:
        PARK_RANGES["le"] = a.hackrf_park_range
    if a.hackrf_sweep:
        HACKRF_ON = True
        CAPS.append("sweep")
        CAPS.append("park")
        threading.Thread(target=feed_hackrf, args=(a.hackrf_range, a.hackrf_bin, a.hackrf_lna, a.hackrf_vga, a.hackrf_mask, a.hackrf_ppm), daemon=True).start()
    if a.rtl_power:
        CAPS.append("sweep")
        groups = group_sweeps(a.rtl_power, a.rtl_power_device)
        for dev, specs in groups.items():
            threading.Thread(target=feed_rtl_power, args=(specs, dev, max(3, a.rtl_power_dwell), a.rtl_ppm, a.rtl_gain), daemon=True).start()
        if not a.listen_device:
            a.listen_device = next((d for d in groups if d), "")
    LISTEN["sq"], LISTEN["ppm"] = a.listen_squelch, a.rtl_ppm
    if a.listen_device:
        LISTEN["dev"] = a.listen_device
        CAPS.append("listen")
        kill_strays()
        threading.Thread(target=listen_worker, daemon=True).start()
        threading.Thread(target=follow_watch, daemon=True).start()
    if a.dump1090:
        CAPS.append("adsb")
        threading.Thread(target=feed_dump1090, args=(a.dump1090,), daemon=True).start()
    if a.rtl433 or not sys.stdin.isatty():
        threading.Thread(target=feed_stdin, args=("rtl433" if a.rtl433 else "raw",), daemon=True).start()
    run_peripheral(a.name)


if __name__ == "__main__":
    main()
