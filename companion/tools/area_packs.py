#!/usr/bin/env python3
"""
Build the files a Wavelens companion downloads to set itself up for where it is (companion/pi/area.py):

    index.json                   what exists: postcode tables by country, licence packs by country and region
    postcodes-<CC>.json.gz       postcode → [lat, lon, region code, district name], one file per country
    fcc-US-<ST>.jsonl.gz         every active FCC licence with a location in that state, one file per state:
                                 a header line, then one entry per line, so a Pi can filter it as it reads

Run by .github/workflows/area-packs.yml once a week; the result is published as the "area" release of the
public repo. Sources, all public:

  - FCC Universal Licensing System bulk files (l_LMpriv.zip, l_LMcomm.zip): licence records, United States.
  - GeoNames postal codes (https://download.geonames.org/export/zip/allCountries.zip), Creative Commons
    Attribution 4.0: postcode centroids for about a hundred countries. This is what makes the area setting
    work outside the US: a country and a postcode become a position, with no ZIP-only table in between.

A country with a postcode table and no licence pack still gets its position and district; licence names need
a public licence database for that country and a reader for it here (see LICENCE_SOURCES).

    ./area_packs.py --uls-zip l_LMpriv.zip --uls-zip l_LMcomm.zip --geonames allCountries.zip -o out/
    ./area_packs.py --selftest
"""
import argparse
import datetime
import gzip
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fcc_local_pack  # noqa: E402

# States, DC and the territories the FCC licenses in, in groups: each group is one pair of passes over the
# bulk files, so the whole country never sits in memory at once. Big states first, spread across groups.
US_STATES = ("CA TX FL NY PA IL OH MI GA NC NJ VA WA MA IN AZ TN MO MD WI MN CO AL SC LA KY OR OK CT IA MS AR "
             "KS UT NV NM WV NE ID HI ME NH RI MT DE SD AK ND VT DC WY PR VI GU AS MP").split()
# Countries with a licence source read here. Others get a postcode table only.
LICENCE_SOURCES = {"US": "FCC Universal Licensing System public access files"}


def norm_postcode(s: str) -> str:
    """The key a postcode is stored and looked up under: upper case, letters and digits only."""
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def write_gz(path: str, obj) -> int:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    # mtime=0: the same data gives the same bytes, so an unchanged file is not a changed release asset
    with open(path, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
        g.write(raw)
    return len(raw)


def write_jsonl_gz(path: str, header: dict, rows) -> int:
    n = 0
    with open(path, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
        for obj in [header] + list(rows):
            line = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            n += len(line)
            g.write(line)
    return n


def geonames_rows(path: str):
    """Rows of a GeoNames postal file: a .zip holding one .txt, or the .txt itself. Tab-separated:
    country, postcode, place, admin1 name, admin1 code, admin2 name, admin2 code, admin3 name, admin3 code,
    latitude, longitude, accuracy."""
    def lines(f):
        for line in io.TextIOWrapper(f, encoding="utf-8", errors="replace"):
            cells = line.rstrip("\n").split("\t")
            if len(cells) >= 11:
                yield cells
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".txt") and "readme" not in name.lower():
                    with zf.open(name) as f:
                        yield from lines(f)
    else:
        with open(path, "rb") as f:
            yield from lines(f)


def build_postcodes(path: str) -> dict:
    """{country: {postcode: [lat, lon, region, district]}}. A postcode listed under several places keeps the
    mean position and the first place's region and district."""
    acc = {}
    for c in geonames_rows(path):
        cc, code = c[0].strip().upper(), norm_postcode(c[1])
        try:
            lat, lon = float(c[9]), float(c[10])
        except ValueError:
            continue
        if len(cc) != 2 or not code or (lat == 0 and lon == 0):
            continue
        region = (c[4] or c[3]).strip()
        district = (c[5] or "").strip()
        e = acc.setdefault(cc, {}).get(code)
        if e is None:
            acc[cc][code] = [lat, lon, 1, region, district]
        else:
            e[0] += lat; e[1] += lon; e[2] += 1
    return {cc: {k: [round(v[0] / v[2], 4), round(v[1] / v[2], 4), v[3], v[4]] for k, v in codes.items()}
            for cc, codes in acc.items()}


def build_fcc_states(zips, out_dir: str, group_size: int = 10, states=US_STATES) -> list:
    """Write fcc-US-<ST>.jsonl.gz for every state; return their index entries."""
    made = []
    today = datetime.date.today().isoformat()
    for i in range(0, len(states), group_size):
        group = set(states[i:i + group_size])
        entries = fcc_local_pack.build(zips, states=group)
        by_state = {st: [] for st in group}
        for e in entries:
            for st in {a.split("|", 1)[0] for a in e.get("areas", ())} & group:
                # only this state's counties: the Pi matches a county inside the state the file is for
                by_state[st].append({**e, "areas": [a for a in e["areas"] if a.startswith(st + "|")]})
        for st in sorted(group):
            rows = by_state[st]
            if not rows:
                continue
            name = f"fcc-US-{st}.jsonl.gz"
            size = write_jsonl_gz(os.path.join(out_dir, name), {
                "format": "wavelens-licences/1", "country": "US", "region": st, "built": today,
                "source": LICENCE_SOURCES["US"], "entries": len(rows)}, rows)
            made.append({"country": "US", "region": st, "file": name, "entries": len(rows),
                         "licences": len({r["call"] for r in rows}), "bytes": size})
            print(f"  {name}: {len(rows)} frequencies", file=sys.stderr)
        del entries, by_state
    return made


def build_all(uls_zips, geonames, out_dir: str, group_size: int = 10, states=US_STATES) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    index = {"format": "wavelens-area-index/1", "built": datetime.date.today().isoformat(),
             "postcodes": {}, "licences": [],
             "credits": ["Postcode positions: GeoNames (geonames.org), CC BY 4.0",
                         "US licence records: FCC Universal Licensing System"]}
    if geonames:
        for cc, codes in sorted(build_postcodes(geonames).items()):
            name = f"postcodes-{cc}.json.gz"
            write_gz(os.path.join(out_dir, name), {"format": "wavelens-postcodes/1", "country": cc, "codes": codes})
            index["postcodes"][cc] = {"file": name, "codes": len(codes)}
        print(f"postcodes: {len(index['postcodes'])} countries, "
              f"{sum(v['codes'] for v in index['postcodes'].values())} codes", file=sys.stderr)
    if uls_zips:
        index["licences"] = build_fcc_states(uls_zips, out_dir, group_size, states)
        print(f"licences: {len(index['licences'])} state packs, "
              f"{sum(v['entries'] for v in index['licences'])} frequencies", file=sys.stderr)
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1)
    return index


# ---------------------------------------------------------------- self-test on synthetic data

SYNTH_GEONAMES = "\n".join("\t".join(r) for r in [
    # invented places: the US rows sit where fcc_local_pack's synthetic licences are (27.9 N, 82.8 W)
    ["US", "00001", "Exampleville", "Florida", "FL", "Example", "001", "", "", "27.9000", "-82.8000", "4"],
    ["US", "00001", "Exampleville Heights", "Florida", "FL", "Example", "001", "", "", "27.9200", "-82.8200", "4"],
    ["US", "00002", "Nearby", "Florida", "FL", "Sample", "003", "", "", "28.1000", "-82.7000", "4"],
    ["US", "00009", "Farville", "Texas", "TX", "Faraway", "005", "", "", "31.0000", "-100.0000", "4"],
    ["XX", "AB1 2", "Testby", "Testshire", "", "", "", "", "", "51.5000", "-0.1000", "4"],
]) + "\n"


def selftest() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        gn = os.path.join(d, "geo.zip")
        with zipfile.ZipFile(gn, "w") as zf:
            zf.writestr("allCountries.txt", SYNTH_GEONAMES)
            zf.writestr("readme.txt", "not data")
        uls = os.path.join(d, "uls.zip")
        with open(uls, "wb") as f:
            f.write(fcc_local_pack._synthetic_zip())
        out = os.path.join(d, "out")
        index = build_all([uls], gn, out, group_size=2, states=("FL", "TX", "MI"))
        assert set(index["postcodes"]) == {"US", "XX"}, index["postcodes"]
        with gzip.open(os.path.join(out, "postcodes-US.json.gz"), "rt", encoding="utf-8") as f:
            us = json.load(f)["codes"]
        assert us["00001"] == [27.91, -82.81, "FL", "Example"], us["00001"]       # mean of the two places
        with gzip.open(os.path.join(out, "postcodes-XX.json.gz"), "rt", encoding="utf-8") as f:
            assert json.load(f)["codes"]["AB12"] == [51.5, -0.1, "Testshire", ""]  # normalised key, region by name
        assert [x["region"] for x in index["licences"]] == ["FL"], index["licences"]   # TX has no active licence, MI none
        with gzip.open(os.path.join(out, "fcc-US-FL.jsonl.gz"), "rt", encoding="utf-8") as f:
            head, *fl = [json.loads(line) for line in f]
        assert head["region"] == "FL" and head["entries"] == len(fl) == 3, head
        assert {e["call"] for e in fl} == {"WQZZ001", "WQZZ002"}
        assert all(e["areas"] == ["FL|EXAMPLE"] for e in fl)
        a = open(os.path.join(out, "postcodes-US.json.gz"), "rb").read()
        build_all([uls], gn, out, group_size=2, states=("FL",))
        assert a == open(os.path.join(out, "postcodes-US.json.gz"), "rb").read()   # same data, same bytes
    print("selftest ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uls-zip", action="append", default=[], help="FCC ULS bulk zip (repeatable)")
    ap.add_argument("--geonames", help="GeoNames postal codes: allCountries.zip or a country's .txt")
    ap.add_argument("-o", "--out", default="area-out")
    ap.add_argument("--group-size", type=int, default=10, help="states per pass over the FCC files (memory against time)")
    ap.add_argument("--states", help="comma-separated states to build (default: all)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.uls_zip and not a.geonames:
        ap.error("give --uls-zip and/or --geonames")
    states = tuple(s.strip().upper() for s in a.states.split(",")) if a.states else US_STATES
    build_all(a.uls_zip, a.geonames, a.out, a.group_size, states)
    return 0


if __name__ == "__main__":
    sys.exit(main())
