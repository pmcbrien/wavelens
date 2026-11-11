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
      "rtl433_args": "-f 433.92M -f 915M -H 30 -g 40",
      "sweeps": {"vhf": ["144M:174M:12.5k"], "uhf": ["403M:430M:12.5k", "450M:520M:12.5k"], "ism": ["915M:928M:25k"]},
      "cc_bands": [{"lo": 403, "hi": 430, "name": "400 MHz"}],      # where trunked control channels are looked for
      "follow_bands": [{"lo": 476.4, "hi": 477.42, "mode": "auto"}]  # what the scanner's Follow may tune (am | auto)
    }

The last three are the country's radio plan: which ranges the RTL-SDRs sweep, where the control-channel
hunt looks, and which bands Follow tunes. Each one left out keeps the built-in (United States) list.

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


_SPEC = __import__("re").compile(r"^(\d+(?:\.\d+)?)M:(\d+(?:\.\d+)?)M:(\d+(?:\.\d+)?)k$")


def _spec_ok(sp) -> bool:
    """An rtl_power range "403M:430M:12.5k" inside what an RTL-SDR tunes, low end first."""
    m = _SPEC.match(sp) if isinstance(sp, str) else None
    return bool(m) and 24.0 <= float(m.group(1)) < float(m.group(2)) <= 1766.0 and 1.0 <= float(m.group(3)) <= 1000.0


def radio_plan(pack) -> dict:
    """The pack's radio plan, checked: {"sweeps": {"vhf": [...], "uhf": [...], "ism": [...]}, "cc_bands":
    [(spec, lo, hi, label)], "follow_bands": [(lo, hi, mode)]}. A key the pack leaves out, or gets wrong,
    is absent from the result, and the bridge keeps its built-in list for it."""
    comp = (pack or {}).get("companion") or {}
    out = {}
    sw = comp.get("sweeps")
    if isinstance(sw, dict):
        got = {k: [sp for sp in (sw.get(k) or []) if _spec_ok(sp)] for k in ("vhf", "uhf", "ism")}
        if got["vhf"] or got["uhf"]:
            out["sweeps"] = got
    try:
        cc = [(f"{float(b['lo']):g}M:{float(b['hi']):g}M:12.5k", float(b["lo"]), float(b["hi"]), str(b.get("name") or f"{float(b['lo']):g} MHz"))
              for b in comp.get("cc_bands") or []]
        if cc and all(_spec_ok(c[0]) for c in cc):
            out["cc_bands"] = cc
    except (KeyError, TypeError, ValueError):
        print("bridge: pack cc_bands unreadable: keeping the built-in list", file=sys.stderr)
    try:
        fb = [(float(b["lo"]), float(b["hi"]), str(b.get("mode") or "auto")) for b in comp.get("follow_bands") or []]
        if fb and all(lo < hi and mode in ("am", "auto") for lo, hi, mode in fb):
            out["follow_bands"] = fb
    except (KeyError, TypeError, ValueError):
        print("bridge: pack follow_bands unreadable: keeping the built-in list", file=sys.stderr)
    return out
