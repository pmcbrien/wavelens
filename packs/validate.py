#!/usr/bin/env python3
"""Check Wavelens country packs before they are shared.

    python3 packs/validate.py                 # every <cc>.json in this folder
    python3 packs/validate.py au.json nz.json # just these (TEMPLATE.json is skipped, so *.json works)
    python3 packs/validate.py --signatures path/to/signatures.json   # also check disable_rules ids

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


def _list(v) -> list:
    return v if isinstance(v, list) else []


def check(pack, fname: str, pack_ids) -> list:
    if not isinstance(pack, dict):
        return [f"the file must hold one JSON object {{...}}, not a {type(pack).__name__}"]
    p = []
    for k in ("rules", "disable_rules", "legal", "maintainers"):
        if k in pack and not isinstance(pack[k], list): p.append(f'"{k}" must be a list [...]')
    if "companion" in pack and not isinstance(pack["companion"], (dict, type(None))): p.append('"companion" must be an object {...}')
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
    legal = pack.get("legal")
    if not isinstance(legal, list) or not legal or not all(isinstance(x, str) and x.strip() for x in legal):
        p.append('"legal" must be a non-empty list of strings: what the law here says about receiving radio and detectors')
    for rid in _list(pack.get("disable_rules")):
        if pack_ids is not None and rid not in pack_ids: p.append(f'disable_rules: "{rid}" is not a rule in signatures.json')
    seen = set()
    for i, r in enumerate(_list(pack.get("rules"))):
        if not isinstance(r, dict):
            p.append(f"rules[{i}]: each rule must be an object {{...}}, not a {type(r).__name__}"); continue
        rid = str(r.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", rid): p.append(f"rules[{i}]: id must be 2-64 letters, digits, . _ -")
        if rid in seen: p.append(f"duplicate rule id {rid}")
        seen.add(rid)
        if r.get("category") not in CATEGORIES: p.append(f"{rid}: unknown category {r.get('category')}")
        if r.get("confidence", "POSSIBLE") not in ("POSSIBLE", "LIKELY", "CONFIRMED"): p.append(f"{rid}: bad confidence")
        m = r.get("match") or {}
        if not isinstance(m, dict):
            p.append(f"{rid}: match must be an object {{...}}"); continue
        for k in ("ouis", "services", "manufacturers", "sources"):
            if k in m and not isinstance(m[k], list): p.append(f"{rid}: {k} must be a list")
        if "regions" in r and not isinstance(r["regions"], list): p.append(f"{rid}: regions must be a list")
        if not set(m) & PREDICATES: p.append(f"{rid}: match needs at least one predicate")
        for k in m:
            if k not in PREDICATES: p.append(f"{rid}: unknown predicate {k}")
        for k in ("nameRegex", "ssidRegex"):
            if k in m:
                if not m[k]: p.append(f"{rid}: {k} is empty")
                elif not isinstance(m[k], str): p.append(f"{rid}: {k} must be a string")
                else:
                    try:
                        re.compile(m[k])
                    except re.error as e:
                        p.append(f"{rid}: {k} does not compile: {e}")
        for o in _list(m.get("ouis")):
            if not re.fullmatch(r"[0-9A-Fa-f]{6}", str(o).replace(":", "").replace("-", "")): p.append(f"{rid}: bad OUI {o}")
        if m.get("ouis") and m.get("publicAddr") is not True: p.append(f"{rid}: OUI rules need \"publicAddr\": true")
        for c in _list(r.get("regions")):
            if not re.fullmatch(r"[A-Za-z]{2}", str(c)): p.append(f"{rid}: bad region {c}")
    idle = pack.get("scanner_idle")
    if idle is not None and not (isinstance(idle, list) and len(idle) == 2 and all(isinstance(x, (int, float)) for x in idle)
                                 and idle[1] - idle[0] >= 0.1 and idle[0] >= 1 and idle[1] <= 6000):
        p.append('"scanner_idle" must be null or [lo, hi] in MHz')
    comp = pack.get("companion") if isinstance(pack.get("companion"), dict) else {}
    if "bands" in comp and not isinstance(comp["bands"], list): p.append("companion.bands must be a list")
    # the country's radio plan for a Raspberry Pi companion (all optional; see companion/pi/regions.py)
    spec = re.compile(r"^\d+(\.\d+)?M:\d+(\.\d+)?M:\d+(\.\d+)?k$")
    sw = comp.get("sweeps")
    if sw is not None:
        if not isinstance(sw, dict) or set(sw) - {"vhf", "uhf", "ism"}:
            p.append('companion.sweeps must be {"vhf": [...], "uhf": [...], "ism": [...]}')
        else:
            for k, lst in sw.items():
                for sp in lst if isinstance(lst, list) else [None]:
                    ok = isinstance(sp, str) and spec.match(sp)
                    if ok:
                        lo, hi = (float(x[:-1]) for x in sp.split(":")[:2])
                        ok = 24 <= lo < hi <= 1766
                    if not ok: p.append(f'companion.sweeps.{k}: each range is "loM:hiM:stepk" within 24–1766 MHz, e.g. "403M:430M:12.5k"')
    for key, modes in (("cc_bands", None), ("follow_bands", ("am", "auto"))):
        lst = comp.get(key)
        if lst is None: continue
        if not isinstance(lst, list): p.append(f"companion.{key} must be a list"); continue
        for i, b in enumerate(lst):
            if not (isinstance(b, dict) and isinstance(b.get("lo"), (int, float)) and isinstance(b.get("hi"), (int, float)) and 24 <= b["lo"] < b["hi"] <= 1766):
                p.append(f"companion.{key}[{i}]: needs lo < hi (MHz, 24–1766)")
            elif modes and b.get("mode", "auto") not in modes:
                p.append(f"companion.{key}[{i}]: mode must be am or auto")
    for i, b in enumerate(_list(comp.get("bands"))):
        if not isinstance(b, dict):
            p.append(f"companion.bands[{i}]: must be an object {{...}}"); continue
        if not (isinstance(b.get("lo"), (int, float)) and isinstance(b.get("hi"), (int, float)) and b["hi"] > b["lo"]):
            p.append(f"companion.bands[{i}]: needs lo < hi (MHz)")
        if b.get("kind", "") not in KINDS: p.append(f"companion.bands[{i}]: kind must be one of {sorted(KINDS)}")
        if b.get("cat", "UNKNOWN") not in CATEGORIES: p.append(f"companion.bands[{i}]: unknown cat")
    if "pager_bands" in comp:
        pb = comp["pager_bands"]
        if not isinstance(pb, list) or not all(isinstance(x, list) and len(x) == 2 and all(isinstance(v, (int, float)) for v in x) and x[0] < x[1] for x in pb):
            p.append("companion.pager_bands must be a list of [lo, hi] pairs in MHz, lo < hi")
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
        if os.path.basename(path) == "TEMPLATE.json":   # the blank form, not a pack (python3 validate.py *.json)
            continue
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
            print(f"{os.path.basename(path)}: OK ({pack['name']} v{pack['pack_version']}, {len(_list(pack.get('rules')))} rules)")
        bad |= bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
