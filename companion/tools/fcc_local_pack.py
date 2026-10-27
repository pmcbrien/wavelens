#!/usr/bin/env python3
"""
Build a local FCC license pack for the Wavelens companion bridge.

Every VHF / UHF / 700 / 800 / 900 MHz carrier a HackRF or RTL-SDR hears is on a
frequency somebody holds an FCC license for — a police department, a county DOT, a
utility, a tow company — with a name, a call sign and usually a site location. This
tool pulls those out of the FCC Universal Licensing System (ULS) bulk files for one
area and writes a compact JSON pack the bridge uses to label sweep hits:

    "Licensed: <County> Sheriff's Office · WQAB123 · public safety" instead of "460 MHz carrier"

Unlicensed bands (2.4 / 5 GHz Wi-Fi and BLE, 433 / 915 MHz ISM, 5.8 GHz video) have no
license records, so this adds nothing there; the signature pack in the app covers those.

Usage
-----
  # 1. download the weekly bulk files (large: private land mobile ~600 MB, commercial ~150 MB,
  #    microwave ~250 MB). They are plain zips of pipe-delimited tables.
  mkdir -p uls && cd uls
  curl -O https://data.fcc.gov/download/pub/uls/complete/l_LMpriv.zip
  curl -O https://data.fcc.gov/download/pub/uls/complete/l_LMcomm.zip
  curl -O https://data.fcc.gov/download/pub/uls/complete/l_micro.zip      # optional: microwave links
  cd ..

  # 2. build the pack for where you drive. Either a radius around a point (uses the licensed
  #    site coordinates; misses area-wide mobile licenses) or a state + counties (catches both):
  ./fcc_local_pack.py --zip uls/l_LMpriv.zip --zip uls/l_LMcomm.zip \\
        --state FL --county Pinellas --county Hillsborough --county Pasco -o fcc-local.json
  ./fcc_local_pack.py --zip uls/l_LMpriv.zip --lat 27.9 --lon -82.8 --radius-km 50 -o fcc-local.json

  # 3. hand it to the bridge
  ./wavelens_bridge.py --hackrf-sweep --fcc-pack fcc-local.json

  ./fcc_local_pack.py --selftest    # runs the parser on a synthetic in-memory ULS zip

Record layout follows the FCC "Public Access Database Definitions" for the ULS files
(HD = header, EN = entity, LO = location, FR = frequency, EM = emission). Column
positions are the constants below; if the FCC adds a column, fix them there.
Only Active licenses are kept. Licensee names are public records.
"""

import argparse
import io
import json
import math
import re
import sys
import zipfile
from collections import defaultdict

# ---- ULS column positions (0-based, after the two-letter record type) -------------------
# HD: header.  call_sign, license_status, radio_service_code
HD_CALL, HD_STATUS, HD_SERVICE = 4, 5, 6
# EN: entity.  call_sign, entity_type, entity_name, city, state
EN_CALL, EN_TYPE, EN_NAME, EN_CITY, EN_STATE = 4, 5, 7, 16, 17
# LO: location.  call_sign, location_number, city, county, state, lat d/m/s/dir, lon d/m/s/dir
LO_CALL, LO_NUM, LO_CITY, LO_COUNTY, LO_STATE = 4, 8, 12, 13, 14
LO_LAT_D, LO_LAT_M, LO_LAT_S, LO_LAT_DIR = 19, 20, 21, 22
LO_LON_D, LO_LON_M, LO_LON_S, LO_LON_DIR = 23, 24, 25, 26
# FR: frequency.  call_sign, location_number, class_station_code, frequency_assigned (MHz), frequency_upper_band
FR_CALL, FR_LOC, FR_CLASS, FR_FREQ, FR_UPPER, FR_STATUS = 4, 6, 8, 10, 11, 17
# EM: emission.  call_sign, location_number, emission_code (e.g. 11K2F3E)
EM_CALL, EM_LOC, EM_CODE = 4, 5, 9

# Radio service codes → (label, Wavelens category hint). Public safety pool and the
# old public-safety services first; everything else is business/industrial.
SERVICES = {
    "PW": ("public safety", "UNKNOWN"),
    "YW": ("public safety (trunked)", "UNKNOWN"),
    "GW": ("public safety (700/800 MHz)", "UNKNOWN"),
    "GP": ("public safety (700 MHz)", "UNKNOWN"),
    "SP": ("public safety (700 MHz narrowband)", "UNKNOWN"),
    "SY": ("public safety (700 MHz trunked)", "UNKNOWN"),
    "YE": ("public safety (800 MHz trunked)", "UNKNOWN"),
    "IG": ("industrial / business", "IOT"),
    "YG": ("industrial / business (trunked)", "IOT"),
    "GB": ("business (800 MHz)", "IOT"),
    "YB": ("business (800 MHz trunked)", "IOT"),
    "GE": ("public safety / special emergency", "UNKNOWN"),
    "GI": ("industrial / business (800 MHz)", "IOT"),
    "IK": ("industrial / business (PLMRS)", "IOT"),
    "MW": ("microwave link", "NETWORK"),
    "MG": ("microwave (industrial / business)", "NETWORK"),
    "CF": ("common carrier fixed point-to-point", "NETWORK"),
    "TI": ("TV intercity relay", "NETWORK"),
    "CD": ("paging / radiotelephone", "NETWORK"),
    "CP": ("paging", "NETWORK"),
    "RS": ("radio location", "UNKNOWN"),
    "LN": ("900 MHz business location / monitoring", "UNKNOWN"),
}

# Agency type, from the licensee's name: (pattern, label shown in the app, category hint). First match
# wins, so the specific ones come first ("County Fire Rescue" is fire, not local government). When no
# name matches, agency_for() falls back to the radio service: public-safety pool → "Public safety".
AGENCY = [
    (r"\b(ems|ambulance|emergency medical|paramedics?|medic unit|ambulette)\b", "EMS / ambulance", "UNKNOWN"),
    (r"\b(fire|rescue|fire district|fire control|fire department)\b", "Fire / rescue", "UNKNOWN"),
    (r"\b(hospital|medical center|health system|healthcare|health care|clinic)\b", "Hospital / medical", "UNKNOWN"),
    (r"\b(911|e-?911|emergency management|emergency communications?|communications district|dispatch)\b", "911 / emergency management", "UNKNOWN"),
    (r"\b(police|sheriff|constable|highway patrol|state patrol|marshal|corrections|department of law enforcement)\b", "Law enforcement", "UNKNOWN"),
    (r"\b(school|schools|school board|school district|college|university)\b", "School / campus", "UNKNOWN"),
    (r"\b(airport|aviation authority)\b", "Airport", "UNKNOWN"),
    (r"\b(port authority|seaport|marina|harbor|harbour)\b", "Port / marine", "UNKNOWN"),
    (r"\b(department of transportation|dept of transportation|dot|turnpike|expressway|toll|transit|transportation authority|bus)\b", "Transit / roads", "UNKNOWN"),
    (r"\b(electric|power|energy|water|sewer|wastewater|gas|utility|utilities|cooperative)\b", "Utility", "IOT"),
    (r"\b(security|alarm|protective|guard)\b", "Security company", "UNKNOWN"),
    (r"\b(towing|wrecker|roadside)\b", "Towing / roadside", "VEHICLE"),
    (r"\b(taxi|cab|limo|limousine|shuttle)\b", "Taxi / shuttle", "VEHICLE"),
    (r"\b(delivery|trucking|logistics|freight|courier)\b", "Delivery / trucking", "VEHICLE"),
    (r"\b(united states|u\.?s\.? department|federal|national park)\b", "Federal", "UNKNOWN"),
    (r"\b(city of|county of|town of|village of|county|public works|municipal)\b", "Local government", "UNKNOWN"),
]
AGENCY = [(re.compile(rx, re.I), label, cat) for rx, label, cat in AGENCY]

PUBLIC_SAFETY_SERVICES = {"PW", "YW", "GW", "GP", "SP", "SY", "YE", "GE"}


def agency_for(name: str, service: str):
    """(agency label, category hint) for a licensee, or (None, None) when there's nothing to say."""
    for rx, label, cat in AGENCY:
        if rx.search(name or ""):
            return label, cat
    if service in PUBLIC_SAFETY_SERVICES:
        return "Public safety", None
    if service in ("IG", "YG", "GB", "YB", "GI", "IK"):
        return "Business", None
    return None, None


# Licensee name patterns that sharpen the category. First match wins. Only low-threat hints
# (vehicle, IoT) that colour the list: a licence never asserts a device type the app alerts on.
NAME_HINTS = [
    (re.compile(r"\b(electric|power|energy|water|gas|utility|utilities|cooperative)\b", re.I), "IOT"),
    (re.compile(r"\b(towing|wrecker|taxi|cab|limo|delivery|trucking|logistics)\b", re.I), "VEHICLE"),
]


def dms(d, m, s, hemi):
    try:
        v = float(d or 0) + float(m or 0) / 60 + float(s or 0) / 3600
    except ValueError:
        return None
    if v == 0:
        return None
    return -v if hemi in ("S", "W") else v


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def emission_khz(code: str):
    """11K2F3E → 11.2 kHz; 20K0F1D → 20 kHz; 1M25D7W → 1250 kHz. None when unparsable."""
    m = re.match(r"^(\d*)([HKMG])(\d*)", code or "")
    if not m:
        return None
    whole, unit, frac = m.groups()
    try:
        v = float(f"{whole or 0}.{frac or 0}")
    except ValueError:
        return None
    return v * {"H": 0.001, "K": 1, "M": 1000, "G": 1_000_000}[unit]


def iter_records(zf: zipfile.ZipFile, member: str):
    """Yield split rows from one table inside a ULS zip (e.g. 'LO.dat'). Missing member → nothing."""
    try:
        info = zf.getinfo(member)
    except KeyError:
        return
    with zf.open(info) as raw:
        for line in io.TextIOWrapper(raw, encoding="latin-1", errors="replace"):
            row = line.rstrip("\r\n").split("|")
            if len(row) > 10:
                yield row


def build(zips, state=None, counties=(), center=None, radius_km=None, include_mobiles=True):
    counties_u = {c.strip().upper() for c in counties}
    state_u = (state or "").strip().upper()
    locs = {}           # call_sign → list of (lat, lon, city, county, dist_km or None)
    in_area = set()     # call signs with at least one location in the area

    # Pass 1: locations
    for zp in zips:
        with zipfile.ZipFile(zp) as zf:
            for r in iter_records(zf, "LO.dat"):
                call = r[LO_CALL].strip()
                if not call:
                    continue
                lat = dms(r[LO_LAT_D], r[LO_LAT_M], r[LO_LAT_S], r[LO_LAT_DIR].strip())
                lon = dms(r[LO_LON_D], r[LO_LON_M], r[LO_LON_S], r[LO_LON_DIR].strip())
                county, st = r[LO_COUNTY].strip().upper(), r[LO_STATE].strip().upper()
                ok, dist = False, None
                if center and lat is not None and lon is not None:
                    dist = haversine_km(center[0], center[1], lat, lon)
                    ok = dist <= (radius_km or 50)
                if state_u and st == state_u and (not counties_u or county in counties_u):
                    ok = True                                   # area-wide mobile licenses land here
                if not ok:
                    continue
                in_area.add(call)
                locs.setdefault(call, []).append((lat, lon, r[LO_CITY].strip().title(), county.title(), dist))

    if not in_area:
        return []

    # Pass 2: header (status + service), entity (name), emissions, frequencies — only for in-area call signs
    service, names, emis, freqs = {}, {}, defaultdict(list), defaultdict(list)
    for zp in zips:
        with zipfile.ZipFile(zp) as zf:
            for r in iter_records(zf, "HD.dat"):
                c = r[HD_CALL].strip()
                if c in in_area and r[HD_STATUS].strip() == "A":
                    service[c] = r[HD_SERVICE].strip()
            for r in iter_records(zf, "EN.dat"):
                c = r[EN_CALL].strip()
                if c in in_area and r[EN_TYPE].strip() == "L":          # L = licensee
                    names[c] = r[EN_NAME].strip() or " ".join(x for x in (r[9], r[11]) if x).strip()
            for r in iter_records(zf, "EM.dat"):
                c = r[EM_CALL].strip()
                if c in in_area:
                    k = emission_khz(r[EM_CODE].strip())
                    if k:
                        emis[c].append(k)
            for r in iter_records(zf, "FR.dat"):
                c = r[FR_CALL].strip()
                if c not in in_area or c not in service:
                    continue
                if r[FR_STATUS].strip() not in ("", "A"):
                    continue
                try:
                    f = float(r[FR_FREQ])
                except ValueError:
                    continue
                if f <= 0:
                    continue
                upper = None
                try:
                    upper = float(r[FR_UPPER]) if r[FR_UPPER].strip() else None
                except ValueError:
                    pass
                cls = r[FR_CLASS].strip()
                if not include_mobiles and cls in ("MO", "MO3", "MO5", "MO6", "MO7", "MO8"):
                    continue
                freqs[c].append((f, upper, cls))

    # Assemble: one entry per (call sign, frequency)
    out = []
    for c, fl in freqs.items():
        svc = service.get(c, "")
        svc_label, cat = SERVICES.get(svc, (svc or "licensed", "UNKNOWN"))
        name = names.get(c, "")
        for rx, hint in NAME_HINTS:
            if rx.search(name):
                cat = hint
                break
        agency, acat = agency_for(name, svc)
        if acat:
            cat = acat
        bw = max(emis[c]) if emis.get(c) else 12.5
        sites = [(la, lo) for la, lo, *_ in locs.get(c, []) if la is not None and lo is not None]
        place = next((f"{ci}, {co} Co." for _, _, ci, co, _ in locs.get(c, []) if ci or co), "")
        seen = set()
        for f, upper, cls in fl:
            key = (round(f, 4), upper)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "mhz": round(f, 4),
                "mhz_hi": round(upper, 4) if upper else None,
                "bw_khz": round(bw, 1),
                "call": c,
                "name": name,
                "service": svc,
                "service_label": svc_label,
                "class": cls,
                "cat": cat,
                "agency": agency or svc_label,
                "place": place,
                "sites": [[round(la, 5), round(lo, 5)] for la, lo in sites[:8]],
            })
    out.sort(key=lambda e: (e["mhz"], e["call"]))
    return out


# ---------------------------------------------------------------- self-test on synthetic data

def _synthetic_zip() -> bytes:
    """A tiny ULS-shaped zip with invented licensees. Nothing in it is a real record."""
    def pad(prefix, fields, width):
        row = [prefix] + fields
        row += [""] * (width - len(row))
        return "|".join(row) + "\n"
    hd = (pad("HD", ["1", "", "", "WQZZ001", "A", "PW"], 60) +
          pad("HD", ["2", "", "", "WQZZ002", "A", "IG"], 60) +
          pad("HD", ["3", "", "", "WQZZ003", "E", "IG"], 60))       # expired: must be dropped
    en = (pad("EN", ["1", "", "", "WQZZ001", "L", "", "Example County Sheriff's Office", "", "", "", "", "", "", "", "", "Exampleville", "FL"], 30) +
          pad("EN", ["2", "", "", "WQZZ002", "L", "", "Sample Towing LLC", "", "", "", "", "", "", "", "", "Exampleville", "FL"], 30) +
          pad("EN", ["3", "", "", "WQZZ003", "L", "", "Old Expired Co", "", "", "", "", "", "", "", "", "Exampleville", "FL"], 30))
    lo = (pad("LO", ["1", "", "", "WQZZ001", "", "", "", "1", "", "", "", "Exampleville", "EXAMPLE", "FL", "", "", "", "", "27", "54", "0", "N", "82", "48", "0", "W"], 40) +
          pad("LO", ["2", "", "", "WQZZ002", "", "", "", "1", "", "", "", "Exampleville", "EXAMPLE", "FL", "", "", "", "", "0", "0", "0", "N", "0", "0", "0", "W"], 40) +
          pad("LO", ["3", "", "", "WQZZ003", "", "", "", "1", "", "", "", "Exampleville", "EXAMPLE", "FL", "", "", "", "", "27", "54", "0", "N", "82", "48", "0", "W"], 40) +
          pad("LO", ["4", "", "", "WQZZ004", "", "", "", "1", "", "", "", "Farville", "FARAWAY", "TX", "", "", "", "", "31", "0", "0", "N", "100", "0", "0", "W"], 40))
    fr = (pad("FR", ["1", "", "", "WQZZ001", "", "1", "", "FB2", "", "453.2125", "", "", "", "", "", "", "A"], 30) +
          pad("FR", ["1", "", "", "WQZZ001", "", "1", "", "MO", "", "458.2125", "", "", "", "", "", "", "A"], 30) +
          pad("FR", ["2", "", "", "WQZZ002", "", "1", "", "MO", "", "464.5500", "", "", "", "", "", "", "A"], 30) +
          pad("FR", ["3", "", "", "WQZZ003", "", "1", "", "FB", "", "461.1000", "", "", "", "", "", "", "A"], 30))
    em = (pad("EM", ["1", "", "", "WQZZ001", "1", "", "", "", "11K2F3E"], 20) +
          pad("EM", ["2", "", "", "WQZZ002", "1", "", "", "", "20K0F1D"], 20))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("HD.dat", hd); zf.writestr("EN.dat", en); zf.writestr("LO.dat", lo); zf.writestr("FR.dat", fr); zf.writestr("EM.dat", em)
    return buf.getvalue()


def selftest() -> int:
    z = io.BytesIO(_synthetic_zip())
    by_county = build([z], state="FL", counties=["Example"])
    z.seek(0)
    by_radius = build([z], center=(27.9, -82.8), radius_km=20)
    calls = sorted({e["call"] for e in by_county})
    assert calls == ["WQZZ001", "WQZZ002"], calls                       # expired WQZZ003 and TX WQZZ004 dropped
    sheriff = [e for e in by_county if e["call"] == "WQZZ001"]
    assert {e["mhz"] for e in sheriff} == {453.2125, 458.2125}
    assert sheriff[0]["cat"] == "UNKNOWN" and sheriff[0]["bw_khz"] == 11.2 and sheriff[0]["sites"]   # never a device type
    tow = [e for e in by_county if e["call"] == "WQZZ002"][0]
    assert tow["cat"] == "VEHICLE" and tow["bw_khz"] == 20.0 and tow["sites"] == []   # 0/0 coords dropped
    assert sorted({e["call"] for e in by_radius}) == ["WQZZ001"]      # the mobile-only licence has no site
    assert sheriff[0]["agency"] == "Law enforcement" and tow["agency"] == "Towing / roadside"
    for nm, svc, want in [("Example County Fire Rescue", "PW", "Fire / rescue"),
                          ("Example County EMS", "PW", "EMS / ambulance"),
                          ("Example Regional Medical Center", "IG", "Hospital / medical"),
                          ("Example County Emergency Management", "PW", "911 / emergency management"),
                          ("Example County School Board", "IG", "School / campus"),
                          ("Example Electric Cooperative", "IG", "Utility"),
                          ("Example City Public Works", "PW", "Local government"),
                          ("Example Holdings LLC", "PW", "Public safety"),
                          ("Example Holdings LLC", "IG", "Business")]:
        got = agency_for(nm, svc)[0]
        assert got == want, (nm, got, want)
    print(f"selftest ok: {len(by_county)} entries by county, {len(by_radius)} by radius")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", action="append", default=[], help="ULS bulk zip (repeatable): l_LMpriv.zip, l_LMcomm.zip, l_micro.zip")
    ap.add_argument("--state", help="two-letter state, with --county")
    ap.add_argument("--county", action="append", default=[], help="county name (repeatable); with --state")
    ap.add_argument("--lat", type=float); ap.add_argument("--lon", type=float)
    ap.add_argument("--radius-km", type=float, default=50.0)
    ap.add_argument("--no-mobiles", action="store_true", help="keep fixed/base stations only")
    ap.add_argument("-o", "--out", default="fcc-local.json")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.zip:
        ap.error("at least one --zip is required (see the docstring for the download URLs)")
    if not (a.state or (a.lat is not None and a.lon is not None)):
        ap.error("give --state (and --county) or --lat/--lon")
    center = (a.lat, a.lon) if a.lat is not None and a.lon is not None else None
    entries = build(a.zip, state=a.state, counties=a.county, center=center, radius_km=a.radius_km, include_mobiles=not a.no_mobiles)
    pack = {
        "format": "wavelens-fcc-local/1",
        "area": {"state": a.state, "counties": a.county, "center": center, "radius_km": a.radius_km if center else None},
        "source": "FCC ULS public access files",
        "entries": entries,
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(pack, f, separators=(",", ":"))
    licensees = len({e["call"] for e in entries})
    print(f"wrote {a.out}: {len(entries)} frequencies from {licensees} active licenses", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
