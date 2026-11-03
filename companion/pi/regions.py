"""Local packs for the Wavelens bridge.

The phone sends {"c": "region", "v": "AU"} on every connect: the country code of the local pack in use.
The bridge looks for a pack with that "country" in, in order:

    /etc/wavelens/packs/*.json
    ~/.config/wavelens/packs/*.json
    <this folder>/packs/*.json            (installed by install-service.sh / update.sh)
    <repo>/packs/*.json                   (running from a checkout)

It is the same file the phone imports (community packs: github.com/pmcbrien/wavelens/tree/main/packs).
Only its "companion" section matters here:

    "companion": {
      "drop_us_bands": true,            # forget the band names that come from the US allocation table
      "bands": [ {"lo": 476.4, "hi": 477.4, "name": "UHF CB radio nearby", "cat": "VEHICLE", "kind": ""} ],
      "rtl433_args": "-f 433.92M -f 915M -H 30 -g 40"
    }

Bands from the pack are matched before the built-in table (first match wins). With drop_us_bands the
US-only entries are skipped, so a frequency the pack does not name falls back to "N MHz carrier"
rather than a US label that is wrong locally. Energy-only, as everywhere: a label is a hint.
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIRS = ["/etc/wavelens/packs", os.path.expanduser("~/.config/wavelens/packs"), os.path.join(HERE, "packs"),
        os.path.normpath(os.path.join(HERE, "..", "..", "packs"))]

# BANDS keys (lo-hi as the bridge writes them) whose labels come from the US allocation table, US cellular
# band plan or US-only services. Kept here, not as a flag on each entry, so the main table stays as it was.
US_ONLY = {
    "462.54-462.74", "467.54-467.74",                      # FRS / GMRS
    "314-316", "299-319", "389.5-390.5",                   # US key fob / garage remote frequencies
    "902-928",                                             # US ISM span (AU is 915–928)
    "663-698", "699-716", "777-787", "788-798", "799-805", "806-815", "815-824", "824-849",
    "1710-1780", "1850-1920", "2000-2020", "2305-2315",    # US uplinks
    "160.2-161.6", "162.4-162.55", "162.05-174", "138-144", "148-150.8", "150.8-174",
    "406.1-420", "450-470", "608-614", "769-775", "896-902", "935-941",
    "1390-1400", "1427-1435", "1780-1850", "4400-4940", "4940-4990",
    "54-88", "174-216", "470-608", "614-652", "716-768", "851-869", "869-894", "929-932", "941-960",
    "1920-2000", "2110-2200", "2315-2345", "2345-2360", "2496-2690", "3450-3550", "3550-3700",
    "3700-3980", "5850-5925", "30-50", "400-406", "420-450",
}


def _band(e: dict) -> dict:
    lo, hi = float(e["lo"]), float(e["hi"])
    steady = e.get("steady")
    if isinstance(steady, dict):
        steady = (steady.get("name"), steady.get("cat", "UNKNOWN"), steady.get("conf", "POSSIBLE"))
    return {"lo": lo, "hi": hi, "name": e.get("name"), "cat": e.get("cat", "UNKNOWN"), "conf": e.get("conf", "POSSIBLE"),
            "kind": e.get("kind", ""), "steady": steady, "key": f"region:{lo:g}-{hi:g}"}


def find(code: str):
    """The pack for this two-letter code, or None. Later directories in DIRS lose to earlier ones."""
    code = (code or "").upper()
    for d in DIRS:
        for path in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                with open(path, encoding="utf-8") as f:
                    pack = json.load(f)
            except (OSError, ValueError) as e:
                print(f"bridge: pack {path} unreadable: {e}", file=sys.stderr)
                continue
            if pack.get("kind") == "wavelens-country" and str(pack.get("country", "")).upper() == code:
                pack["_path"] = path
                return pack
    return None


def table(pack, base: list) -> list:
    """The band table to use under this pack (None = the US table unchanged)."""
    if not pack:
        return base
    comp = pack.get("companion") or {}
    own = []
    for e in comp.get("bands") or []:
        try:
            own.append(_band(e))
        except (KeyError, TypeError, ValueError):
            print(f"bridge: pack band entry skipped: {e}", file=sys.stderr)
    drop = bool(comp.get("drop_us_bands"))
    rest = [b for b in base if not (drop and b["key"] in US_ONLY)]
    return own + rest


def rtl433_args(pack, default: str) -> str:
    if not pack:
        return default
    return str((pack.get("companion") or {}).get("rtl433_args") or default)
