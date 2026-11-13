#!/usr/bin/env python3
"""Where this companion is used, and the local data that goes with it.

    sudo wavelens-area set US 49749            a country and a postcode
    sudo wavelens-area set AU 2000 --radius-km 80
    sudo wavelens-area set --country DE --lat 52.52 --lon 13.40     no postcode table needed
    sudo wavelens-area refresh                 download again for the area already set
    sudo wavelens-area status                  what is set and what is loaded
    sudo wavelens-area clear                   forget the area and remove its downloaded data

The area is one small file, /etc/wavelens/area.json: country, postcode, position, radius, and the region and
districts (US: state and counties) within that radius. Setting it downloads, from the public repo's "area"
release (built weekly by .github/workflows/area-packs.yml):

  - the country's postcode table, to turn the postcode into a position and find the districts around it;
  - where a public licence database exists for the country (today: the United States, FCC), the licence
    packs for the regions in range, cut down to those districts and that radius → area/fcc-local.json.

A country without a licence pack still gets its position and districts; the bridge then names carriers
from the band table of the country's pack (packs/<cc>.json) alone.

The bridge reads the area folder by itself at start (see inputs_for_bridge): the licence pack, and any
RadioReference exports the owner put in area/rr/. Nothing is listed by hand in WAVELENS_ARGS, and a
companion with no area set loads no local data at all, never somebody else's.

Standard library only; runs on Python 3.9 (Pi OS Bullseye) and later.
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

AREA_FILE = os.environ.get("WAVELENS_AREA_FILE", "/etc/wavelens/area.json")
AREA_DIR = os.environ.get("WAVELENS_AREA_DIR", "/var/lib/wavelens/area")
BASE_URL = os.environ.get("WAVELENS_AREA_URL", "https://github.com/pmcbrien/wavelens/releases/download/area").rstrip("/")
BRIDGE_UNIT = "wavelens-bridge"
DEFAULT_RADIUS_KM = 50.0
MAX_RADIUS_KM = 300.0
MAX_DOWNLOAD = 400 * 1024 * 1024      # a state's licence pack unpacks to tens of MB; this is a ceiling, not a size


class AreaError(Exception):
    """Something the owner can act on; printed as one line, no traceback."""


def log(msg: str) -> None:
    print(msg, flush=True)


def norm_postcode(s: str) -> str:
    """Upper case, letters and digits only: the key postcodes are stored under (tools/area_packs.py)."""
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def norm_district(s: str) -> str:
    """A district name as both sources spell it the same: 'St. Clair', 'SAINT CLAIR' → 'STCLAIR'."""
    s = "".join(ch for ch in (s or "").upper() if ch.isalnum() or ch == " ").strip()
    for suffix in (" COUNTY", " PARISH", " BOROUGH", " CENSUS AREA", " MUNICIPALITY"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    if s.startswith("SAINT "):
        s = "ST " + s[6:]
    return s.replace(" ", "")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, a)))


# ---------------------------------------------------------------- the area file

def read_area(path: str = "") -> dict:
    """The area set on this companion, or {} when none is (or the file is unreadable)."""
    try:
        with open(path or AREA_FILE, encoding="utf-8") as f:
            a = json.load(f)
        return a if isinstance(a, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_atomic(path: str, obj, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".area-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- downloads

def _open_url(name: str, base: str = ""):
    url = f"{base or BASE_URL}/{name}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wavelens-area"})
        return urllib.request.urlopen(req, timeout=60)   # noqa: S310 - https or a test's file://
    except urllib.error.HTTPError as e:
        raise AreaError(f"{name}: not available (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AreaError(f"{name}: download failed ({getattr(e, 'reason', e)}). Is the Pi online?") from None


def fetch(name: str, base: str = "") -> bytes:
    try:
        with _open_url(name, base) as r:
            data = r.read(MAX_DOWNLOAD + 1)
    except OSError as e:
        raise AreaError(f"{name}: download failed ({e}). Is the Pi online?") from None
    if len(data) > MAX_DOWNLOAD:
        raise AreaError(f"{name}: larger than expected, not used")
    return data


def download(name: str, dest_dir: str, base: str = "") -> str:
    """Stream a file to disk (a big state's licence pack is too much to hold in a Pi's memory)."""
    os.makedirs(dest_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".dl-")
    try:
        got = 0
        with os.fdopen(fd, "wb") as out, _open_url(name, base) as r:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_DOWNLOAD:
                    raise AreaError(f"{name}: larger than expected, not used")
                out.write(chunk)
    except OSError as e:
        os.unlink(tmp)
        raise AreaError(f"{name}: download failed ({e}). Is the Pi online?") from None
    except AreaError:
        os.unlink(tmp)
        raise
    return tmp


def iter_pack(path: str):
    """Entries of a licence pack: gzip, one JSON object per line, the first line a header (skipped)."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i == 0 or not line.strip():
                    continue
                yield json.loads(line)
    except (OSError, ValueError, EOFError) as e:
        raise AreaError(f"licence pack unreadable ({e})") from None


def fetch_json(name: str, base: str = ""):
    data = fetch(name, base)
    try:
        if name.endswith(".gz"):
            data = gzip.decompress(data)
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError, EOFError) as e:
        raise AreaError(f"{name}: unreadable ({e})") from None


# ---------------------------------------------------------------- resolving a place

def lookup_postcode(codes: dict, postcode: str):
    """(key, [lat, lon, region, district]) for a postcode, or (None, None). Tables hold whole codes for some
    countries and only the leading part for others (GB outward codes, CA forward sortation areas), so a miss
    is retried on ever shorter leading parts, down to two characters."""
    key = norm_postcode(postcode)
    while len(key) >= 2:
        if key in codes:
            return key, codes[key]
        key = key[:-1]
    return None, None


def districts_within(codes: dict, lat: float, lon: float, radius_km: float) -> dict:
    """{region: [district, ...]} for every postcode within the radius: the districts in range, by region."""
    # a degree of latitude is ~111 km: skip the trigonometry for rows that cannot be in range
    dlat = radius_km / 111.0 + 0.01
    found = {}
    for v in codes.values():
        try:
            la, lo, region, district = v[0], v[1], v[2], v[3]
        except (IndexError, TypeError):
            continue
        if abs(la - lat) > dlat or haversine_km(lat, lon, la, lo) > radius_km:
            continue
        s = found.setdefault(region or "", set())
        if district:
            s.add(district)
    return {r: sorted(d) for r, d in sorted(found.items())}


def nearest(codes: dict, lat: float, lon: float):
    best, row = 9e9, None
    for v in codes.values():
        try:
            d = haversine_km(lat, lon, v[0], v[1])
        except (IndexError, TypeError):
            continue
        if d < best:
            best, row = d, v
    return row, best


def resolve(country: str, postcode: str = "", lat=None, lon=None, radius_km: float = DEFAULT_RADIUS_KM,
            index=None, base: str = "") -> dict:
    """The area record for a country and a postcode, or a country and a position."""
    country = (country or "").strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise AreaError("country must be a two-letter code, e.g. US, CA, AU, GB, DE")
    try:
        radius_km = float(radius_km)
    except (TypeError, ValueError):
        raise AreaError("radius must be a number of kilometres") from None
    if not 5 <= radius_km <= MAX_RADIUS_KM:
        raise AreaError(f"radius must be between 5 and {MAX_RADIUS_KM:g} km")
    index = index if index is not None else fetch_json("index.json", base)
    table = (index.get("postcodes") or {}).get(country)
    codes = fetch_json(table["file"], base).get("codes", {}) if table else {}
    area = {"country": country, "radius_km": radius_km, "postcode": "", "region": "", "district": "", "regions": {}}
    if postcode:
        if not codes:
            raise AreaError(f"no postcode table for {country}: give a position instead (--lat and --lon)")
        key, row = lookup_postcode(codes, postcode)
        if row is None:
            raise AreaError(f"postcode {postcode!r} is not in the {country} table: check it, or give --lat and --lon")
        area.update(postcode=key, lat=row[0], lon=row[1], region=row[2] or "", district=row[3] or "")
    else:
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            raise AreaError("give a postcode, or both --lat and --lon") from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise AreaError("latitude is -90 to 90, longitude -180 to 180")
        area.update(lat=round(lat, 4), lon=round(lon, 4))
        if codes:
            row, km = nearest(codes, lat, lon)
            if row is not None and km <= max(radius_km, 100.0):
                area.update(region=row[2] or "", district=row[3] or "")
    if codes:
        area["regions"] = districts_within(codes, area["lat"], area["lon"], radius_km)
    if area["region"] and area["region"] not in area["regions"]:
        area["regions"][area["region"]] = [area["district"]] if area["district"] else []
    return area


# ---------------------------------------------------------------- licence packs

def filter_entries(entries, region: str, districts, lat: float, lon: float, radius_km: float) -> list:
    """Entries of one region's licence pack that belong to this area: a location in one of the districts
    (this catches area-wide mobile licences, which have no site), or a site within the radius."""
    want = {f"{region}|{norm_district(d)}" for d in districts}
    out = []
    for e in entries:
        ok = any(f"{a.split('|', 1)[0]}|{norm_district(a.split('|', 1)[-1])}" in want for a in e.get("areas", ()))
        if not ok:
            for s in e.get("sites") or ():
                try:
                    if haversine_km(lat, lon, float(s[0]), float(s[1])) <= radius_km:
                        ok = True
                        break
                except (TypeError, ValueError, IndexError):
                    continue
        if ok:
            out.append({k: v for k, v in e.items() if k != "areas"})
    return out


def build_licence_pack(area: dict, index: dict, base: str = "", work_dir: str = "") -> dict | None:
    """The local licence pack for an area, or None when its country has no licence data."""
    packs = [p for p in index.get("licences") or [] if p.get("country") == area["country"]]
    if not packs:
        return None
    by_region = {p.get("region"): p for p in packs}
    entries, used = [], []
    for region, districts in (area.get("regions") or {}).items():
        p = by_region.get(region)
        if not p:
            continue
        log(f"  licence pack {p['file']} …")
        tmp = download(p["file"], work_dir or AREA_DIR, base)
        try:
            got = filter_entries(iter_pack(tmp), region, districts, area["lat"], area["lon"], area["radius_km"])
        finally:
            os.unlink(tmp)
        log(f"    {len(got)} of {p.get('entries', '?')} frequencies are in this area")
        entries += got
        used.append(region)
    seen, uniq = set(), []
    for e in sorted(entries, key=lambda e: (e.get("mhz", 0), e.get("call", ""))):
        k = (e.get("call"), e.get("mhz"), e.get("mhz_hi"))
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return {"format": "wavelens-fcc-local/1", "built": index.get("built"),
            "area": {"country": area["country"], "regions": used, "center": [area["lat"], area["lon"]],
                     "radius_km": area["radius_km"]},
            "source": "FCC ULS public access files" if area["country"] == "US" else "public licence records",
            "entries": uniq}


# ---------------------------------------------------------------- what the bridge loads

def rr_kind(path: str) -> int:
    """Order for RadioReference exports found in area/rr/: trunked sites and talkgroups (0), a county's
    frequencies (1), a statewide export with a County column (2), a nationwide one (3). The bridge takes
    frequency exports most local first."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            head = [h.strip() for h in next(csv.reader([f.readline()]), [])]
    except (OSError, UnicodeDecodeError):
        return 9
    if ("Frequencies" in head and ("Site Dec" in head or "Site Hex" in head)) or ("Decimal" in head and "Alpha Tag" in head):
        return 0
    if "Frequency Output" not in head:
        return 9                      # not an export the bridge reads: left out
    if "Nationwide Agency" in head:
        return 3
    return 2 if "County" in head else 1


def inputs_for_bridge(area_dir: str = "", area_file: str = "") -> dict:
    """{"fcc": path or "", "rr": [paths], "counties": [names], "area": {...}} from the area folder and file.
    Never raises: a companion with a broken area folder still starts."""
    out = {"fcc": "", "rr": [], "counties": [], "area": {}}
    try:
        d = area_dir or AREA_DIR
        area = read_area(area_file)
        out["area"] = area
        fcc = os.path.join(d, "fcc-local.json")
        if os.path.isfile(fcc) and os.path.getsize(fcc) > 2:
            out["fcc"] = fcc
        ranked = sorted((rr_kind(p), os.path.basename(p).lower(), p) for p in glob.glob(os.path.join(d, "rr", "*.csv")))
        out["rr"] = [p for k, _, p in ranked if k < 9]
        counties = list((area.get("regions") or {}).get(area.get("region", ""), []))
        home = area.get("district")
        if home and home in counties:                    # the home district first
            counties.remove(home)
            counties.insert(0, home)
        out["counties"] = counties
    except Exception as e:  # noqa: BLE001 - see the docstring
        print(f"bridge: area folder not read: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- commands

def restart_bridge(quiet: bool = False) -> None:
    try:
        rc = subprocess.run(["systemctl", "restart", BRIDGE_UNIT], capture_output=True, text=True, timeout=60).returncode
    except (OSError, subprocess.SubprocessError):
        rc = 1
    if not quiet:
        log("bridge restarted" if rc == 0 else f"could not restart {BRIDGE_UNIT}: restart it to load the new data")


def apply(area: dict, index: dict, base: str = "", area_dir: str = "", area_file: str = "") -> dict:
    """Download the licence data for an area and store both. Returns the stored area record."""
    d = area_dir or AREA_DIR
    os.makedirs(os.path.join(d, "rr"), exist_ok=True)
    pack = build_licence_pack(area, index, base, d)
    fcc = os.path.join(d, "fcc-local.json")
    if pack is None:
        if os.path.exists(fcc):
            os.unlink(fcc)            # the last area's licences are wrong here
        log(f"  no public licence data for {area['country']} yet: carriers are named from the band table only")
        area["licences"] = 0
    else:
        write_json_atomic(fcc, pack)
        area["licences"] = len(pack["entries"])
        log(f"  wrote {fcc}: {area['licences']} licensed frequencies")
    area["data_built"] = index.get("built") or ""
    area["updated"] = time.strftime("%Y-%m-%d %H:%M")
    old = read_area(area_file)
    for k in ("follow",):                 # owner switches survive a new area
        if k in old and k not in area:
            area[k] = old[k]
    try:
        area["fcc_bytes"] = os.path.getsize(fcc) if os.path.exists(fcc) else 0
    except OSError:
        area["fcc_bytes"] = 0
    write_json_atomic(area_file or AREA_FILE, area)
    return area


def describe(area: dict) -> str:
    if not area:
        return "no area set"
    where = " ".join(x for x in (area.get("country", ""), area.get("postcode", "")) if x)
    place = ", ".join(x for x in (area.get("district", ""), area.get("region", "")) if x)
    n = sum(len(v) for v in (area.get("regions") or {}).values())
    return (f"{where}{' (' + place + ')' if place else ''} at {area.get('lat')}, {area.get('lon')}, "
            f"{area.get('radius_km', DEFAULT_RADIUS_KM):g} km: {n} district(s), "
            f"{area.get('licences', 0)} licensed frequencies, data of {area.get('data_built') or '?'}")


def cmd_set(a) -> int:
    words = list(a.where)
    country, postcode = a.country, a.postcode
    # "set US 49749", "set 49749" (country from --country, the area already set, or US), "set AU 2000"
    if words and len(words[0]) == 2 and words[0].isalpha():
        country = country or words.pop(0)
    if words:
        postcode = postcode or " ".join(words)
    country = country or read_area().get("country") or "US"
    log(f"area: {country.upper()} {postcode or f'{a.lat}, {a.lon}'} within {a.radius_km:g} km")
    index = fetch_json("index.json")
    area = resolve(country, postcode, a.lat, a.lon, a.radius_km, index)
    for region, districts in area["regions"].items():
        log(f"  {region or '(no region)'}: {', '.join(districts) or 'no districts named'}")
    area = apply(area, index)
    log(describe(area))
    if not a.no_restart:
        restart_bridge()
    return 0


def cmd_refresh(a) -> int:
    old = read_area()
    if not old:
        log("no area set: nothing to refresh")
        return 0
    index = fetch_json("index.json")
    if not a.force and old.get("data_built") and old.get("data_built") == index.get("built"):
        log(f"area data is current ({old['data_built']})")
        return 0
    area = resolve(old.get("country", ""), old.get("postcode", ""), old.get("lat"), old.get("lon"),
                   old.get("radius_km", DEFAULT_RADIUS_KM), index)
    area = apply(area, index)
    log(describe(area))
    if not a.no_restart:
        restart_bridge()
    return 0


def cmd_status(a) -> int:
    area = read_area()
    got = inputs_for_bridge()
    if a.json:
        print(json.dumps({"area": area, "fcc": got["fcc"], "rr": got["rr"], "counties": got["counties"]}))
        return 0
    log(describe(area))
    log(f"licence pack: {got['fcc'] or 'none'}")
    log(f"RadioReference exports in {os.path.join(AREA_DIR, 'rr')}: "
        f"{', '.join(os.path.basename(p) for p in got['rr']) or 'none'}")
    return 0


def cmd_follow(a) -> int:
    """Follow me: the bridge moves the area to where the phone is (it must share its position) when it has
    travelled out of the middle of the current one. The switch is kept in the area file."""
    area = read_area()
    if not area:
        log("no area set: set one first (wavelens-area set US 49749), then turn follow on")
        return 1
    if a.state in ("on", "off"):
        area["follow"] = a.state == "on"
        write_json_atomic(AREA_FILE, area)
    log(f"follow me is {'on' if area.get('follow') else 'off'}")
    return 0


def cmd_clear(a) -> int:
    for p in (AREA_FILE, os.path.join(AREA_DIR, "fcc-local.json")):
        if os.path.exists(p):
            os.unlink(p)
    log("area cleared (RadioReference exports in area/rr/ are yours and were left alone)")
    if not a.no_restart:
        restart_bridge()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wavelens-area", description="Set where this Wavelens companion is used.")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("set", help="set the area and download its data")
    s.add_argument("where", nargs="*", help="[COUNTRY] POSTCODE, e.g. US 49749, AU 2000, GB SW1A")
    s.add_argument("--country", default="", help="two-letter country code")
    s.add_argument("--postcode", "--zip", dest="postcode", default="")
    s.add_argument("--lat", type=float)
    s.add_argument("--lon", type=float)
    s.add_argument("--radius-km", type=float, default=DEFAULT_RADIUS_KM)
    s.add_argument("--no-restart", action="store_true", help="don't restart the bridge afterwards")
    s.set_defaults(fn=cmd_set)
    r = sub.add_parser("refresh", help="download again for the area already set, if newer data is published")
    r.add_argument("--force", action="store_true")
    r.add_argument("--no-restart", action="store_true")
    r.set_defaults(fn=cmd_refresh)
    t = sub.add_parser("status", help="what is set and what the bridge will load")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_status)
    fl = sub.add_parser("follow", help="move the area with the phone's position: on, off, or no word to show")
    fl.add_argument("state", nargs="?", choices=["on", "off"])
    fl.set_defaults(fn=cmd_follow)
    c = sub.add_parser("clear", help="forget the area")
    c.add_argument("--no-restart", action="store_true")
    c.set_defaults(fn=cmd_clear)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return a.fn(a)
    except AreaError as e:
        log(f"error: {e}")
        return 1
    except PermissionError as e:
        log(f"error: {e}. Run with sudo.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
