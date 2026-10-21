#!/usr/bin/env python3
"""Validate a Wavelens share pack and merge it into community/emitters.geojson.

Rejects anything that is not fixed public infrastructure, anything with a
coordinate finer than ~100 m, and anything that looks like an address or
identifier. Exit 0 on success; prints a short report for the PR body.
"""
import json, re, sys, datetime

ALLOWED = {"ALPR", "TRAFFIC_SENSOR", "ACOUSTIC"}
MAX_FEATURES = 2000
MAC = re.compile(r"\b([0-9a-f]{2}[:\-]){5}[0-9a-f]{2}\b", re.I)
HEXBLOB = re.compile(r"\b[0-9a-f]{12,}\b", re.I)

def fail(msg):
    print(f"::error::{msg}"); sys.exit(1)

def main(pack_path, map_path, out_path):
    try:
        pack = json.load(open(pack_path))
    except Exception as e:
        fail(f"not JSON: {e}")
    if pack.get("type") != "FeatureCollection":
        fail("not a GeoJSON FeatureCollection")
    feats = pack.get("features") or []
    if not feats:
        fail("no features")
    if len(feats) > MAX_FEATURES:
        fail(f"too many features ({len(feats)} > {MAX_FEATURES})")

    clean = []
    problems = []
    for i, f in enumerate(feats):
        pr = f.get("properties") or {}
        g = f.get("geometry") or {}
        cat = pr.get("category")
        if cat not in ALLOWED:
            problems.append(f"#{i}: category {cat!r} not shareable"); continue
        if g.get("type") != "Point":
            problems.append(f"#{i}: not a Point"); continue
        try:
            lon, lat = float(g["coordinates"][0]), float(g["coordinates"][1])
        except Exception:
            problems.append(f"#{i}: bad coordinates"); continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            problems.append(f"#{i}: coordinates out of range"); continue
        if round(lat, 3) != lat or round(lon, 3) != lon:
            problems.append(f"#{i}: coordinates finer than 3 decimals"); continue
        h = str(pr.get("hash", ""))
        if not re.fullmatch(r"[0-9a-f]{8,16}", h):
            problems.append(f"#{i}: bad hash"); continue
        text = " ".join(str(v) for v in pr.values() if v is not None)
        if MAC.search(text) or HEXBLOB.search(text.replace(h, "")):
            problems.append(f"#{i}: contains an address-like identifier"); continue
        for k in ("first", "last"):
            v = pr.get(k)
            if v is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)):
                problems.append(f"#{i}: {k} is finer than a day"); break
        else:
            keep = {k: pr.get(k) for k in ("hash","category","source","name","vendor","rules","services","sd_prefix","mfg_prefix","sessions","first","last")}
            keep["contributed"] = datetime.date.today().isoformat()
            clean.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": keep})

    if not clean:
        fail("nothing passed validation: " + "; ".join(problems[:5]))

    cur = json.load(open(map_path))
    by_hash = {f["properties"]["hash"]: f for f in cur.get("features", [])}
    added = updated = 0
    for f in clean:
        h = f["properties"]["hash"]
        if h in by_hash:
            old = by_hash[h]["properties"]
            old["sessions"] = int(old.get("sessions") or 0) + int(f["properties"].get("sessions") or 0)
            old["last"] = max(str(old.get("last") or ""), str(f["properties"].get("last") or ""))
            old["reports"] = int(old.get("reports") or 1) + 1
            updated += 1
        else:
            f["properties"]["reports"] = 1
            by_hash[h] = f; added += 1
    cur["features"] = sorted(by_hash.values(), key=lambda x: x["properties"]["hash"])
    json.dump(cur, open(out_path, "w"), indent=1)
    print(f"accepted {len(clean)} of {len(feats)} features: {added} new, {updated} updated; "
          f"{len(problems)} rejected")
    for p in problems[:10]:
        print("  rejected " + p)

if __name__ == "__main__":
    main(*sys.argv[1:4])
