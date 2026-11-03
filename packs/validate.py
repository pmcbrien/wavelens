#!/usr/bin/env python3
"""Check Wavelens country packs before they are shared.

    python3 country-packs/validate.py                 # every <cc>.json in this folder
    python3 country-packs/validate.py au.json nz.json # just these
    python3 country-packs/validate.py --signatures path/to/signatures.json   # also check disable_rules ids

Exits 1 and lists every problem if any pack fails. Needs only the Python standard library.
The app runs the same checks when a pack is imported, so a pack that passes here will import.
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CATEGORIES = {"ALPR", "ACOUSTIC", "DRONE", "TOOL", "LE_GEAR", "TRACKER", "TRAFFIC_SENSOR", "CAMERA", "FLEET",
              "VEHICLE", "PHONE", "WEARABLE", "IOT", "BEACON", "NETWORK", "CELL_SITE", "UNKNOWN"}
PREDICATES = {"nameRegex", "ssidRegex", "ouis", "services", "manufacturers", "mfgPrefix", "sdPrefix", "sources", "publicAddr"}
KINDS = {"", "persist", "bg", "video"}


def check(pack: dict, fname: str, pack_ids) -> list:
    p = []
    if pack.get("kind") != "wavelens-country": p.append('"kind" must be "wavelens-country"')
    if pack.get("format") != 1: p.append('"format" must be 1')
    cc = str(pack.get("country", ""))
    if not re.fullmatch(r"[A-Z]{2}", cc): p.append('"country" must be a two-letter ISO code in capitals')
    elif fname != f"{cc.lower()}.json": p.append(f'file must be named {cc.lower()}.json')
    if not str(pack.get("name", "")).strip(): p.append('"name" is required')
    if not isinstance(pack.get("pack_version"), int) or pack["pack_version"] < 1: p.append('"pack_version" must be an integer >= 1')
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(pack.get("pack_date", ""))): p.append('"pack_date" must be YYYY-MM-DD')
    if pack.get("status", "testing") not in ("stable", "testing"): p.append('"status" must be stable or testing')
    if pack.get("units", "metric") not in ("metric", "imperial"): p.append('"units" must be metric or imperial')
    if not pack.get("maintainers"): p.append('"maintainers" must name at least one person or group')
    for rid in pack.get("disable_rules", []):
        if pack_ids is not None and rid not in pack_ids: p.append(f'disable_rules: "{rid}" is not a rule in signatures.json')
    seen = set()
    for i, r in enumerate(pack.get("rules", [])):
        rid = str(r.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", rid): p.append(f"rules[{i}]: id must be 2-64 letters, digits, . _ -")
        if rid in seen: p.append(f"duplicate rule id {rid}")
        seen.add(rid)
        if r.get("category") not in CATEGORIES: p.append(f"{rid}: unknown category {r.get('category')}")
        if r.get("confidence", "POSSIBLE") not in ("POSSIBLE", "LIKELY", "CONFIRMED"): p.append(f"{rid}: bad confidence")
        if not r.get("source"): p.append(f"{rid}: \"source\" (where the identifier comes from) is required")
        m = r.get("match") or {}
        if not set(m) & PREDICATES: p.append(f"{rid}: match needs at least one predicate")
        for k in m:
            if k not in PREDICATES: p.append(f"{rid}: unknown predicate {k}")
        for k in ("nameRegex", "ssidRegex"):
            if k in m:
                if not m[k]: p.append(f"{rid}: {k} is empty")
                else:
                    try:
                        re.compile(m[k])
                    except re.error as e:
                        p.append(f"{rid}: {k} does not compile: {e}")
        for o in m.get("ouis", []):
            if not re.fullmatch(r"[0-9A-Fa-f]{6}", str(o).replace(":", "").replace("-", "")): p.append(f"{rid}: bad OUI {o}")
        if m.get("ouis") and m.get("publicAddr") is not True: p.append(f"{rid}: OUI rules need \"publicAddr\": true")
        for c in r.get("regions", []):
            if not re.fullmatch(r"[A-Za-z]{2}", str(c)): p.append(f"{rid}: bad region {c}")
    idle = pack.get("scanner_idle")
    if idle is not None and not (isinstance(idle, list) and len(idle) == 2 and all(isinstance(x, (int, float)) for x in idle)
                                 and idle[1] - idle[0] >= 0.1 and idle[0] >= 1 and idle[1] <= 6000):
        p.append('"scanner_idle" must be null or [lo, hi] in MHz')
    comp = pack.get("companion") or {}
    for i, b in enumerate(comp.get("bands", [])):
        if not (isinstance(b.get("lo"), (int, float)) and isinstance(b.get("hi"), (int, float)) and b["hi"] > b["lo"]):
            p.append(f"companion.bands[{i}]: needs lo < hi (MHz)")
        if b.get("kind", "") not in KINDS: p.append(f"companion.bands[{i}]: kind must be one of {sorted(KINDS)}")
        if b.get("cat", "UNKNOWN") not in CATEGORIES: p.append(f"companion.bands[{i}]: unknown cat")
    if "rtl433_args" in comp and not re.fullmatch(r"[A-Za-z0-9 .:=,_-]*", str(comp["rtl433_args"])):
        p.append("companion.rtl433_args: only plain rtl_433 flags (letters, digits, spaces, . : = , _ -)")
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--signatures", help="signatures.json, to check that disable_rules names real rules")
    a = ap.parse_args()
    pack_ids = None
    if a.signatures:
        with open(a.signatures, encoding="utf-8") as f:
            pack_ids = {r["id"] for r in json.load(f)["rules"]}
    files = a.files or sorted(f for f in glob.glob(os.path.join(HERE, "*.json")) if re.fullmatch(r"[a-z]{2}\.json", os.path.basename(f)))
    bad = False
    for path in files:
        path = path if os.path.exists(path) else os.path.join(HERE, path)
        try:
            with open(path, encoding="utf-8") as f:
                pack = json.load(f)
        except (OSError, ValueError) as e:
            print(f"{os.path.basename(path)}: {e}"); bad = True; continue
        problems = check(pack, os.path.basename(path), pack_ids)
        for pr in problems:
            print(f"{os.path.basename(path)}: {pr}")
        if not problems:
            print(f"{os.path.basename(path)}: OK ({pack['name']} v{pack['pack_version']}, {len(pack.get('rules', []))} rules)")
        bad |= bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
