#!/usr/bin/env python3
"""Wavelens Scanner: the owner's own frequency list.

One file, /etc/wavelens/frequencies.json (updates never touch /etc/wavelens), edited from the admin
page's Frequencies tab and read by the bridge, which names what it hears from it and never Follows a
channel marked locked out. Standard library only; runs on Python 3.9 and later.

Built on published standards and openly documented formats, so a list is never trapped here:

  stored as      JSON (RFC 8259), format "wavelens-frequencies/1", described by SCHEMA below
                 (JSON Schema 2020-12). Frequencies are integer hertz, so nothing is lost to rounding.
  emission       ITU emission designators (Radio Regulations Appendix 1, ITU-R SM.1138), e.g. 11K2F3E
  tones          CTCSS in hertz (the TIA-603 tone set is offered, any 60 to 260 Hz is accepted), DCS as
                 three octal digits, P25 NAC as hex, DMR colour code 0 to 15, NXDN RAN 0 to 63
  time, place    ISO 8601 in UTC; WGS 84 latitude and longitude in decimal degrees
  in and out     this JSON, plain CSV (RFC 4180), the CHIRP generic CSV that radio programming tools
                 exchange, Gqrx bookmark files, and SDR++ frequency manager lists
  in only        a RadioReference conventional-frequency CSV export of your own

Every save keeps the file it replaced (the last KEEP_BACKUPS of them) and is written whole or not at
all: a power cut mid-save leaves the previous list.

    python3 freqdb.py list | import FILE | export --format chirp | check FILE
"""
from __future__ import annotations

import bisect
import csv
import datetime
import decimal
import io
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time

try:
    import fcntl
except ImportError:          # not on Windows; the tests and the desktop tools still import this module
    fcntl = None

FORMAT = "wavelens-frequencies/1"
STORE = os.environ.get("WAVELENS_FREQ_FILE", "/etc/wavelens/frequencies.json")
BACKUP_DIR = os.environ.get("WAVELENS_FREQ_BACKUPS", "/var/lib/wavelens/frequency-backups")
KEEP_BACKUPS = 20
MAX_CHANNELS = 20000
MIN_HZ, MAX_HZ = 100_000, 30_000_000_000          # 100 kHz to 30 GHz: anything outside is a typing slip
RTL_HZ = (24_000_000, 1_766_000_000)              # what the kit's RTL-SDRs tune; outside it a channel is reference only

# How the Scanner can listen to a channel. The first six are what the bridge tunes; the rest are kept
# faithfully for import and export and listened to as "auto".
MODES = ("auto", "fm", "am", "dmr", "p25", "nxdn", "wfm", "usb", "lsb", "cw", "dstar", "ysf", "data")
LISTEN_MODES = ("fm", "am", "dmr", "p25", "nxdn")
MODE_NAMES = {"auto": "Auto", "fm": "FM", "am": "AM", "dmr": "DMR", "p25": "P25", "nxdn": "NXDN", "wfm": "Wide FM",
              "usb": "USB", "lsb": "LSB", "cw": "CW", "dstar": "D-STAR", "ysf": "System Fusion", "data": "Data"}
TONE_TYPES = ("", "ctcss", "dcs", "nac", "cc", "ran")
TONE_NAMES = {"": "None", "ctcss": "CTCSS tone (Hz)", "dcs": "DCS code", "nac": "P25 NAC (hex)",
              "cc": "DMR colour code", "ran": "NXDN RAN"}
# The TIA-603 CTCSS tones, then the eight extra ones most radios also carry.
CTCSS = ("67.0", "69.3", "71.9", "74.4", "77.0", "79.7", "82.5", "85.4", "88.5", "91.5", "94.8", "97.4", "100.0",
         "103.5", "107.2", "110.9", "114.8", "118.8", "123.0", "127.3", "131.8", "136.5", "141.3", "146.2", "151.4",
         "156.7", "162.2", "167.9", "173.8", "179.9", "186.2", "192.8", "203.5", "206.5", "210.7", "218.1", "225.7",
         "229.1", "233.6", "241.8", "250.3", "254.1",
         "159.8", "165.5", "171.3", "177.3", "183.5", "189.9", "196.6", "199.5")
# Who uses a channel: the agency types the bridge and the apps already show.
SERVICES = ("", "Law enforcement", "Fire / rescue", "EMS / ambulance", "911 / emergency management", "Public safety",
            "Local government", "Federal", "Hospital / medical", "School / campus", "Airport", "Transit / roads",
            "Utility", "Security company", "Business", "Amateur radio", "Marine", "Data / telemetry", "Other")

# Bandwidth (three digits and a unit letter where the decimal point goes), class of emission, optional details.
EMISSION_RE = re.compile(r"^(H\d{3}|[1-9](?:[HKMG]\d\d|\d[HKMG]\d|\d\d[HKMG]))"
                         r"([NAHRJLBCFGDPKMQVWX])([0-9X])([NABCDEFWX])([A-HJ-NWX][NCFTWX])?$")
_EMISSION_UNIT = {"H": 1, "K": 1_000, "M": 1_000_000, "G": 1_000_000_000}

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Wavelens frequency list",
    "type": "object",
    "required": ["format", "channels"],
    "properties": {
        "format": {"const": FORMAT},
        "revision": {"type": "integer", "minimum": 0, "description": "counts saves; a backup is named by it"},
        "updated": {"type": "string", "format": "date-time"},
        "channels": {"type": "array", "maxItems": MAX_CHANNELS, "items": {
            "type": "object",
            "required": ["id", "name", "freq_hz"],
            "properties": {
                "id": {"type": "string", "pattern": "^[0-9a-f]{12}$"},
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "freq_hz": {"type": "integer", "minimum": MIN_HZ, "maximum": MAX_HZ,
                            "description": "what you hear on: the base or repeater output"},
                "input_hz": {"type": ["integer", "null"], "minimum": MIN_HZ, "maximum": MAX_HZ,
                             "description": "the repeater input (mobiles and handhelds transmit here)"},
                "mode": {"enum": list(MODES)},
                "emission": {"type": "string", "description": "ITU emission designator, e.g. 11K2F3E; may be empty"},
                "bandwidth_hz": {"type": ["integer", "null"], "minimum": 100, "maximum": 20_000_000},
                "tone_type": {"enum": list(TONE_TYPES)},
                "tone": {"type": "string", "description": "CTCSS Hz '127.3', DCS '023' (or '023I' inverted), NAC hex '293', colour code '1', RAN '12'"},
                "group": {"type": "string", "maxLength": 40},
                "service": {"type": "string", "maxLength": 40},
                "tags": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 24}},
                "lockout": {"type": "boolean", "description": "the Scanner's Follow never tunes it"},
                "notes": {"type": "string", "maxLength": 500},
                "lat": {"type": ["number", "null"], "minimum": -90, "maximum": 90},
                "lon": {"type": ["number", "null"], "minimum": -180, "maximum": 180},
                "source": {"type": "string", "maxLength": 40},
                "created": {"type": "string", "format": "date-time"},
                "updated": {"type": "string", "format": "date-time"},
            }}},
    },
}


class StoreError(Exception):
    """The list could not be read or written; the message is fit to show the owner."""


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- reading what people type

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f  ]")
_FREQ_RE = re.compile(r"^\s*([0-9][0-9 ]*(?:[.,][0-9]+)?|[.,][0-9]+)\s*(hz|khz|mhz|ghz)?\s*$", re.I)
_UNIT = {"hz": 1, "khz": 1_000, "mhz": 1_000_000, "ghz": 1_000_000_000}


def text(value, limit: int) -> str:
    """One line of plain text: control characters out, spaces collapsed, cut to limit."""
    return " ".join(_CTRL_RE.sub(" ", "" if value is None else str(value)).split())[:limit].strip()


def parse_hz(value) -> int:
    """'155.475', '155,475 MHz', '462562.5 kHz', '155475000' → integer hertz. A bare number below 30000
    is megahertz (what everyone writes), anything larger is hertz. Raises ValueError with a plain reason."""
    if isinstance(value, bool):
        raise ValueError("not a frequency")
    if isinstance(value, int):
        value = str(value)
    m = _FREQ_RE.match(str(value or ""))
    if not m:
        raise ValueError("not a frequency: write it in MHz, like 155.475")
    try:
        num = decimal.Decimal(m.group(1).replace(" ", "").replace(",", "."))
    except decimal.InvalidOperation:
        raise ValueError("not a frequency: write it in MHz, like 155.475") from None
    unit = (m.group(2) or "").lower()
    mult = _UNIT[unit] if unit else (1_000_000 if num < 30000 else 1)
    hz = int((num * mult).to_integral_value(rounding=decimal.ROUND_HALF_UP))
    if not MIN_HZ <= hz <= MAX_HZ:
        raise ValueError("frequency out of range: 0.1 MHz to 30000 MHz")
    return hz


def mhz(hz) -> str:
    """Integer hertz → '155.47500' (five places, more when the frequency needs them)."""
    s = f"{decimal.Decimal(int(hz)) / 1_000_000:.6f}"
    return s[:-1] if s.endswith("0") else s


def emission_bandwidth(designator: str):
    """The necessary bandwidth in hertz an ITU designator states ('11K2F3E' → 11200), or None."""
    m = EMISSION_RE.match((designator or "").upper())
    if not m:
        return None
    bw = m.group(1)
    for i, ch in enumerate(bw):
        if ch in _EMISSION_UNIT:
            digits = bw[:i] + "." + bw[i + 1:]
            hz = int(decimal.Decimal(digits) * _EMISSION_UNIT[ch])
            return hz if hz >= 100 else None      # sub-100 Hz designators exist; nothing here listens that narrowly
    return None


def clean_tone(kind: str, value) -> str:
    """The stored form of a tone or code, or ValueError saying what is wrong with it."""
    v = text(value, 16).upper().replace("HZ", "").strip()
    if kind == "":
        return ""
    if not v:
        raise ValueError(f"{TONE_NAMES[kind]}: no value given")
    if kind == "ctcss":
        try:
            hz = decimal.Decimal(v.replace(",", "."))
        except decimal.InvalidOperation:
            raise ValueError("CTCSS tone: a number in Hz, like 127.3") from None
        if not 60 <= hz <= 260:
            raise ValueError("CTCSS tone: 60 to 260 Hz")
        return f"{hz:.1f}"
    if kind == "dcs":
        m = re.match(r"^D?([0-7]{1,3})\s*([NI]?)$", v)
        if not m:
            raise ValueError("DCS code: three octal digits, like 023 (add I for inverted)")
        return m.group(1).zfill(3) + ("I" if m.group(2) == "I" else "")
    if kind == "nac":
        m = re.match(r"^(?:0X|\$)?([0-9A-F]{1,3})$", v)
        if not m:
            raise ValueError("P25 NAC: up to three hex digits, like 293")
        return m.group(1).zfill(3)
    limit = {"cc": 15, "ran": 63}[kind]
    if not v.isdigit() or int(v) > limit:
        raise ValueError(f"{TONE_NAMES[kind]}: a number from 0 to {limit}")
    return str(int(v))


def clean_channel(raw: dict, existing: "dict | None" = None) -> dict:
    """A stored channel from form fields or an imported row. Raises ValueError listing every problem,
    so a form can show them all at once."""
    errs, ch = [], dict(existing or {})
    name = text(raw.get("name"), 64)
    if not name:
        errs.append("name: needed")
    ch["name"] = name
    for key, needed in (("freq_hz", True), ("input_hz", False)):
        v = raw.get(key)
        if v in (None, ""):
            if needed:
                errs.append("frequency: needed")
            ch[key] = None
            continue
        try:
            ch[key] = parse_hz(v)
        except ValueError as e:
            errs.append(("frequency: " if needed else "input frequency: ") + str(e).replace("not a frequency: ", ""))
    if ch.get("input_hz") and ch.get("input_hz") == ch.get("freq_hz"):
        ch["input_hz"] = None                  # simplex written as a pair
    mode = text(raw.get("mode"), 12).lower() or "auto"
    if mode not in MODES:
        errs.append(f"mode: one of {', '.join(MODES)}")
        mode = "auto"
    ch["mode"] = mode
    emission = text(raw.get("emission"), 12).upper()
    if emission and not EMISSION_RE.match(emission):
        errs.append("emission: an ITU designator like 11K2F3E, or leave it empty")
    ch["emission"] = emission
    bw = raw.get("bandwidth_hz")
    if bw in (None, ""):
        ch["bandwidth_hz"] = emission_bandwidth(emission)
    else:
        try:
            # a bare number here is hertz, never megahertz (parse_hz would read "12500" as 12.5 GHz)
            ch["bandwidth_hz"] = int(decimal.Decimal(str(bw).replace(",", ".").strip()))
            if not 100 <= ch["bandwidth_hz"] <= 20_000_000:
                raise ValueError
        except (ValueError, decimal.InvalidOperation):
            errs.append("bandwidth: in Hz, 100 to 20000000")
            ch["bandwidth_hz"] = None
    kind = text(raw.get("tone_type"), 8).lower()
    if kind not in TONE_TYPES:
        errs.append("tone type: unknown")
        kind = ""
    try:
        ch["tone_type"], ch["tone"] = kind, clean_tone(kind, raw.get("tone"))
    except ValueError as e:
        errs.append(str(e))
        ch["tone_type"], ch["tone"] = "", ""
    ch["group"] = text(raw.get("group"), 40)
    ch["service"] = text(raw.get("service"), 40)
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = re.split(r"[,;]", tags)
    seen = []
    for t in tags:
        t = text(t, 24)
        if t and t.lower() not in [s.lower() for s in seen]:
            seen.append(t)
    ch["tags"] = seen[:8]
    lock = raw.get("lockout")
    ch["lockout"] = lock if isinstance(lock, bool) else str(lock or "").strip().lower() in ("1", "true", "yes", "on", "s", "y")
    ch["notes"] = text(raw.get("notes"), 500)
    for key, lim in (("lat", 90.0), ("lon", 180.0)):
        v = raw.get(key)
        if v in (None, ""):
            ch[key] = None
            continue
        try:
            ch[key] = round(float(str(v).replace(",", ".")), 6)
            if not -lim <= ch[key] <= lim:      # also refuses NaN
                raise ValueError
        except ValueError:
            errs.append(f"{'latitude' if key == 'lat' else 'longitude'}: decimal degrees, {-lim:g} to {lim:g}")
            ch[key] = None
    if (ch["lat"] is None) != (ch["lon"] is None):
        errs.append("position: give both latitude and longitude, or neither")
    ch["source"] = text(raw.get("source") or ch.get("source") or "manual", 40)
    if errs:
        raise ValueError("; ".join(errs))
    stamp = now_iso()
    cid = str(ch.get("id") or "")
    ch["id"] = cid if re.match(r"^[0-9a-f]{12}$", cid) else secrets.token_hex(6)
    ch["created"] = ch.get("created") or stamp
    ch["updated"] = stamp
    return ch


def dedupe_key(ch: dict) -> tuple:
    """Two entries are the same channel when frequency and tone or code agree."""
    return (ch["freq_hz"], ch.get("tone_type") or "", ch.get("tone") or "")


# ---------------------------------------------------------------- the store

_LOCK = threading.Lock()


class Store:
    """The list on disk. Reads are plain; every change goes through edit(), which holds a file lock
    (the admin page and a shell `freqdb.py import` cannot interleave), backs up, and replaces the file."""

    def __init__(self, path: str = "", backup_dir: str = ""):
        self.path = path or STORE
        self.backup_dir = backup_dir or BACKUP_DIR

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
        except FileNotFoundError:
            return {"format": FORMAT, "revision": 0, "updated": "", "channels": []}
        except (OSError, ValueError) as e:
            raise StoreError(f"the frequency list could not be read ({type(e).__name__}: {e}). "
                             "Put a backup back from the Backups page.") from None
        if not isinstance(doc, dict) or doc.get("format") != FORMAT or not isinstance(doc.get("channels"), list):
            raise StoreError("the frequency list on this Scanner is not in a format this version reads. "
                             "Put a backup back from the Backups page.")
        doc["channels"] = [c for c in doc["channels"] if isinstance(c, dict) and isinstance(c.get("freq_hz"), int)]
        doc.setdefault("revision", 0)
        return doc

    def _backup(self, revision: int) -> None:
        if not os.path.isfile(self.path):
            return
        os.makedirs(self.backup_dir, mode=0o755, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(self.path, os.path.join(self.backup_dir, f"frequencies-{stamp}-r{revision}.json"))
        for old in self.backups()[KEEP_BACKUPS:]:
            try:
                os.unlink(os.path.join(self.backup_dir, old["name"]))
            except OSError:
                pass

    def backups(self) -> list:
        """[{name, revision, when, bytes, channels}] newest first."""
        out = []
        try:
            names = sorted(os.listdir(self.backup_dir), reverse=True)
        except OSError:
            return out
        for n in names:
            m = re.match(r"^frequencies-(\d{8}T\d{6}Z)-r(\d+)\.json$", n)
            if not m:
                continue
            p = os.path.join(self.backup_dir, n)
            try:
                with open(p, encoding="utf-8") as f:
                    count = len(json.load(f).get("channels") or [])
            except (OSError, ValueError, AttributeError):
                count = None
            t = m.group(1)
            out.append({"name": n, "revision": int(m.group(2)), "channels": count, "bytes": os.path.getsize(p),
                        "when": f"{t[0:4]}-{t[4:6]}-{t[6:8]}T{t[9:11]}:{t[11:13]}:{t[13:15]}Z"})
        return out

    def backup_path(self, name: str) -> str:
        """The path of a backup by its listed name; anything else (a path, a made-up name) is refused."""
        if name not in [b["name"] for b in self.backups()]:
            raise StoreError("no such backup")
        return os.path.join(self.backup_dir, name)

    def _write(self, doc: dict) -> None:
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".frequencies-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def edit(self, change, recover: bool = False):
        """Run change(doc) on the current list under the lock and save the result. change may return a
        value, which edit returns. Raises StoreError when the list cannot be read or written. With
        recover, an unreadable list is replaced instead of refused (it is still kept as a backup)."""
        with _LOCK:
            lock = None
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                if fcntl is not None:
                    lock = open(self.path + ".lock", "w", encoding="utf-8")
                    fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    doc = self.load()
                except StoreError:
                    if not recover:
                        raise
                    doc = {"format": FORMAT, "revision": int(time.time()) % 1_000_000, "channels": []}
                result = change(doc)
                if len(doc["channels"]) > MAX_CHANNELS:
                    raise StoreError(f"too many channels: the list holds up to {MAX_CHANNELS}")
                doc["channels"].sort(key=lambda c: (c["freq_hz"], c.get("name", "").lower()))
                self._backup(doc.get("revision", 0))
                doc["format"], doc["revision"], doc["updated"] = FORMAT, int(doc.get("revision", 0)) + 1, now_iso()
                self._write(doc)
                return result
            except OSError as e:
                raise StoreError(f"the frequency list could not be saved ({e})") from None
            finally:
                if lock is not None:
                    lock.close()

    def restore(self, name: str) -> int:
        """Put a backup back (the current list becomes a backup itself). Returns its channel count."""
        src = self.backup_path(name)
        try:
            with open(src, encoding="utf-8") as f:
                old = json.load(f)
            channels = [clean_channel(_from_native(c), {"id": c.get("id"), "created": c.get("created")})
                        for c in old["channels"]]
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            raise StoreError(f"that backup could not be read ({type(e).__name__})") from None

        def put(doc):
            doc["channels"] = channels
            return len(channels)
        return self.edit(put, recover=True)


def _from_native(c: dict) -> dict:
    """A stored channel as clean_channel input (hertz given with their unit, so no guessing)."""
    raw = dict(c)
    for k in ("freq_hz", "input_hz"):
        raw[k] = f"{c[k]} Hz" if isinstance(c.get(k), int) else ""
    return raw


def merge(doc: dict, incoming: list, mode: str = "add") -> dict:
    """Fold imported channels into a list. mode: 'add' keeps what is there and skips repeats, 'update'
    overwrites repeats with the imported entry, 'replace' discards the current list first.
    Returns {"added", "updated", "skipped"}."""
    if mode == "replace":
        doc["channels"] = []
    have = {dedupe_key(c): i for i, c in enumerate(doc["channels"])}
    n = {"added": 0, "updated": 0, "skipped": 0}
    for ch in incoming:
        k = dedupe_key(ch)
        if k in have:
            if mode == "update":
                old = doc["channels"][have[k]]
                doc["channels"][have[k]] = dict(ch, id=old["id"], created=old.get("created") or ch["created"])
                n["updated"] += 1
            else:
                n["skipped"] += 1
            continue
        have[k] = len(doc["channels"])
        doc["channels"].append(ch)
        n["added"] += 1
    return n


# ---------------------------------------------------------------- import

IMPORT_FORMATS = {"wavelens": "Wavelens frequency list (JSON)", "csv": "Plain CSV", "chirp": "CHIRP CSV",
                  "gqrx": "Gqrx bookmarks", "sdrpp": "SDR++ frequency manager (JSON)",
                  "radioreference": "RadioReference CSV export"}
EXPORT_FORMATS = {"wavelens": ("Wavelens frequency list (JSON)", "wavelens-frequencies.json", "application/json"),
                  "csv": ("Plain CSV (opens in any spreadsheet)", "wavelens-frequencies.csv", "text/csv"),
                  "chirp": ("CHIRP CSV (radio programming tools)", "wavelens-chirp.csv", "text/csv"),
                  "gqrx": ("Gqrx bookmarks", "bookmarks.csv", "text/csv"),
                  "sdrpp": ("SDR++ frequency manager", "wavelens-sdrpp.json", "application/json")}
CSV_COLUMNS = ("name", "frequency_mhz", "input_mhz", "mode", "emission", "bandwidth_hz", "tone_type", "tone", "group",
               "service", "tags", "lockout", "notes", "lat", "lon")

_CHIRP_MODE = {"fm": "FM", "nfm": "NFM", "wfm": "WFM", "am": "AM", "nam": "AM", "dv": "dstar", "usb": "usb", "lsb": "lsb",
               "cw": "cw", "cwr": "cw", "ncw": "cw", "ncwr": "cw", "p25": "p25", "dmr": "dmr", "dn": "ysf", "auto": "auto",
               "rtty": "data", "rttyr": "data", "fsk": "data", "fskr": "data", "dig": "data", "pkt": "data"}
_GQRX_MODE = {"narrow fm": "fm", "wfm (mono)": "wfm", "wfm (stereo)": "wfm", "wfm (oirt)": "wfm", "am": "am",
              "am-sync": "am", "lsb": "lsb", "usb": "usb", "cw-l": "cw", "cw-u": "cw", "raw i/q": "data", "demod off": "auto"}
_SDRPP_MODE = {0: "fm", 1: "wfm", 2: "am", 3: "am", 4: "usb", 5: "cw", 6: "lsb", 7: "data"}
_RR_MODE = {"fm": "fm", "fmn": "fm", "am": "am", "dmr": "dmr", "dmre": "dmr", "p25": "p25", "p25e": "p25",
            "nxdn": "nxdn", "nxdn48": "nxdn", "nxdn96": "nxdn", "d-star": "dstar", "ysf": "ysf", "telm": "data"}
_RR_SERVICE = (("law", "Law enforcement"), ("corrections", "Law enforcement"), ("fire", "Fire / rescue"),
               ("ems", "EMS / ambulance"), ("hospital", "Hospital / medical"), ("school", "School / campus"),
               ("aircraft", "Airport"), ("airport", "Airport"), ("public works", "Local government"),
               ("security", "Security company"), ("transport", "Transit / roads"), ("railroad", "Transit / roads"),
               ("utilit", "Utility"), ("federal", "Federal"), ("military", "Federal"),
               ("emergency", "911 / emergency management"), ("interop", "Public safety"), ("multi-", "Public safety"),
               ("ham", "Amateur radio"), ("business", "Business"), ("media", "Business"), ("data", "Data / telemetry"))


def detect_format(data: bytes) -> str:
    """Which import format a file is, from its content (the name is not trusted). '' when unknown."""
    head = data[:4096].decode("utf-8-sig", errors="replace")
    stripped = head.lstrip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(data.decode("utf-8-sig"))
        except ValueError:
            return ""
        if isinstance(doc, dict) and doc.get("format") == FORMAT:
            return "wavelens"
        if isinstance(doc, dict) and ("bookmarks" in doc or "lists" in doc):
            return "sdrpp"
        return ""
    first = next((l for l in head.splitlines() if l.strip()), "")
    cols = [c.strip().strip('"').lower() for c in first.split(",")]
    if "frequency output" in cols:
        return "radioreference"
    if "location" in cols and "frequency" in cols and "duplex" in cols:
        return "chirp"
    if ";" in head and re.search(r"(?mi)^\s*#\s*frequency\s*;", head):
        return "gqrx"
    if any(c in cols for c in ("frequency_mhz", "frequency", "freq", "mhz", "frequency_hz", "hz")):
        return "csv"
    return ""


def _rows(data: bytes) -> "tuple[list, list]":
    txt = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(txt, newline=""))
    rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items() if k is not None} for r in reader]
    return [h.strip().lower() for h in (reader.fieldnames or [])], rows


def _pick(row: dict, *names) -> str:
    for n in names:
        if row.get(n):
            return row[n]
    return ""


def _parse_wavelens(data):
    doc = json.loads(data.decode("utf-8-sig"))
    for c in doc.get("channels") or []:
        if isinstance(c, dict):
            yield _from_native(c), text(c.get("name"), 64)


def _parse_csv(data):
    _, rows = _rows(data)
    for r in rows:
        # a column that names its unit is taken at its word; a bare "frequency" goes by parse_hz's rule
        hz, in_mhz = _pick(r, "frequency_hz", "hz"), _pick(r, "frequency_mhz", "mhz")
        freq = f"{hz} Hz" if hz else f"{in_mhz} MHz" if in_mhz else _pick(r, "frequency", "freq", "rx", "rx frequency")
        inp = _pick(r, "input_mhz")
        inp = f"{inp} MHz" if inp else _pick(r, "input_hz") and f"{_pick(r, 'input_hz')} Hz" or _pick(r, "input", "tx", "tx frequency")
        name = _pick(r, "name", "alpha tag", "label", "description", "channel")
        kind = _pick(r, "tone_type").lower()
        tone = _pick(r, "tone", "ctcss", "pl", "pl tone")
        if tone and not kind:
            kind = "dcs" if re.match(r"^D?[0-7]{3}\s*[NI]?$", tone, re.I) and "." not in tone else "ctcss"
        yield ({"name": name or freq, "freq_hz": freq, "input_hz": inp, "mode": _pick(r, "mode").lower(),
                "emission": _pick(r, "emission"), "bandwidth_hz": _pick(r, "bandwidth_hz", "bandwidth"),
                "tone_type": kind, "tone": tone, "group": _pick(r, "group", "bank", "category"),
                "service": _pick(r, "service", "agency"), "tags": _pick(r, "tags"), "lockout": _pick(r, "lockout", "skip"),
                "notes": _pick(r, "notes", "comment"), "lat": _pick(r, "lat", "latitude"),
                "lon": _pick(r, "lon", "lng", "longitude")}, name or freq)


def _parse_chirp(data):
    _, rows = _rows(data)
    for r in rows:
        freq = r.get("frequency", "")
        if not freq or not r.get("name") and not freq.strip("0."):
            continue                              # CHIRP writes empty memory slots as blank rows
        raw = {"name": r.get("name") or freq, "freq_hz": f"{freq} MHz", "notes": r.get("comment", ""),
               "lockout": r.get("skip", "").upper() == "S"}
        mode = _CHIRP_MODE.get(r.get("mode", "").lower(), "auto").lower()
        raw["mode"] = "fm" if mode in ("fm", "nfm") else mode
        if r.get("mode", "").upper() == "NFM":
            raw["bandwidth_hz"] = "12500"
        duplex, off = r.get("duplex", ""), r.get("offset", "") or "0"
        try:
            f_hz, off_hz = parse_hz(f"{freq} MHz"), int(decimal.Decimal(off) * 1_000_000)
            if duplex == "+" and off_hz:
                raw["input_hz"] = f"{f_hz + off_hz} Hz"
            elif duplex == "-" and off_hz:
                raw["input_hz"] = f"{f_hz - off_hz} Hz"
            elif duplex == "split" and off_hz:
                raw["input_hz"] = f"{off_hz} Hz"
        except (ValueError, decimal.InvalidOperation):
            pass                                  # clean_channel reports the frequency
        tone = r.get("tone", "").upper()
        if tone == "TONE":
            raw["tone_type"], raw["tone"] = "ctcss", r.get("rtonefreq", "")
        elif tone in ("TSQL", "TSQL-R"):
            raw["tone_type"], raw["tone"] = "ctcss", r.get("ctonefreq", "")
        elif tone in ("DTCS", "DTCS-R"):
            pol = r.get("dtcspolarity", "NN").upper()
            raw["tone_type"], raw["tone"] = "dcs", r.get("dtcscode", "") + ("I" if pol[-1:] == "R" else "")
        yield raw, raw["name"]


def _parse_gqrx(data):
    txt = data.decode("utf-8-sig", errors="replace")
    in_marks = False
    for line in txt.splitlines():
        if re.match(r"(?i)^\s*#\s*frequency\s*;", line):
            in_marks = True
            continue
        if not in_marks or not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = [c.strip() for c in line.split(";")]
        if len(cells) < 2 or not cells[0].isdigit():
            continue
        cells += [""] * (5 - len(cells))
        tags = [t.strip() for t in cells[4].split(",") if t.strip() and t.strip().lower() != "untagged"]
        yield ({"name": cells[1] or cells[0], "freq_hz": f"{cells[0]} Hz", "mode": _GQRX_MODE.get(cells[2].lower(), "auto"),
                "bandwidth_hz": cells[3] if cells[3].isdigit() and int(cells[3]) >= 100 else "",
                "group": tags[0] if tags else "", "tags": tags[1:]}, cells[1] or cells[0])


def _parse_sdrpp(data):
    doc = json.loads(data.decode("utf-8-sig"))
    lists = doc.get("lists") if isinstance(doc.get("lists"), dict) else {"": doc}
    for list_name, body in lists.items():
        marks = (body or {}).get("bookmarks") if isinstance(body, dict) else None
        for name, b in (marks or {}).items():
            if not isinstance(b, dict):
                continue
            try:
                hz, bw = int(round(float(b.get("frequency", 0)))), int(round(float(b.get("bandwidth", 0) or 0)))
                mode = _SDRPP_MODE.get(int(b.get("mode", 0)), "auto")
            except (TypeError, ValueError):
                hz, bw, mode = 0, 0, "auto"
            yield ({"name": name, "freq_hz": f"{hz} Hz", "mode": mode, "bandwidth_hz": bw if bw >= 100 else "",
                    "group": text(list_name, 40)}, text(name, 64))


def _parse_radioreference(data):
    _, rows = _rows(data)
    for r in rows:
        out = re.sub(r"[ca*\s]+$", "", r.get("frequency output", ""), flags=re.I)
        if not out:
            continue
        inp = re.sub(r"[ca*\s]+$", "", r.get("frequency input", ""), flags=re.I)
        agency = r.get("agency/category") or r.get("nationwide agency") or ""
        name = r.get("alpha tag") or r.get("description") or out
        tag = r.get("tag", "").lower()
        raw = {"name": name, "freq_hz": f"{out} MHz", "input_hz": f"{inp} MHz" if inp else "",
               "mode": _RR_MODE.get(r.get("mode", "").lower(), "auto"), "group": agency,
               "service": next((label for key, label in _RR_SERVICE if key in tag), ""),
               "notes": " · ".join(x for x in (r.get("description", "") if r.get("alpha tag") else "",
                                               r.get("fcc callsign", ""), r.get("county", "")) if x)}
        tone = r.get("pl output tone", "").upper()
        m = re.match(r"^([\d.]+)\s*PL$", tone) or re.match(r"^(\d{2,3}\.\d)$", tone)
        d = re.match(r"^(\d{3})\s*DPL$", tone)
        nac = re.match(r"^([0-9A-F]{1,3})\s*NAC$", tone)
        cc = re.match(r"^CC\s*(\d{1,2})$", tone)
        ran = re.match(r"^RAN\s*(\d{1,2})$", tone)
        if m:
            raw["tone_type"], raw["tone"] = "ctcss", m.group(1)
        elif d:
            raw["tone_type"], raw["tone"] = "dcs", d.group(1)
        elif nac:
            raw["tone_type"], raw["tone"] = "nac", nac.group(1)
        elif cc:
            raw["tone_type"], raw["tone"] = "cc", cc.group(1)
        elif ran:
            raw["tone_type"], raw["tone"] = "ran", ran.group(1)
        yield raw, name


_PARSERS = {"wavelens": _parse_wavelens, "csv": _parse_csv, "chirp": _parse_chirp, "gqrx": _parse_gqrx,
            "sdrpp": _parse_sdrpp, "radioreference": _parse_radioreference}


def parse_import(data: bytes, fmt: str = "") -> dict:
    """Read a file in any supported format. Returns {"format", "channels": [clean channels, repeats within
    the file dropped], "problems": ["row 7 (Name): reason", ...], "repeats": n}. Raises ValueError when the
    file is none of the formats or cannot be read at all; one bad row never loses the rest."""
    fmt = fmt or detect_format(data)
    if fmt not in _PARSERS:
        raise ValueError("not a file this page reads: a Wavelens list, a CSV with a frequency column, CHIRP CSV, "
                         "Gqrx bookmarks, an SDR++ frequency manager list or a RadioReference CSV export")
    channels, problems, seen, repeats = [], [], set(), 0
    try:
        for i, (raw, label) in enumerate(_PARSERS[fmt](data), start=1):
            if len(channels) >= MAX_CHANNELS:
                problems.append(f"stopped at {MAX_CHANNELS} channels: the rest of the file was not read")
                break
            raw["source"] = f"import:{fmt}"
            try:
                ch = clean_channel(raw)
            except ValueError as e:
                if len(problems) < 200:
                    problems.append(f"row {i} ({text(label, 40) or '?'}): {e}")
                continue
            k = dedupe_key(ch)
            if k in seen:
                repeats += 1
                continue
            seen.add(k)
            channels.append(ch)
    except (ValueError, csv.Error, AttributeError, TypeError) as e:
        raise ValueError(f"the file could not be read as {IMPORT_FORMATS[fmt]} ({type(e).__name__})") from None
    return {"format": fmt, "channels": channels, "problems": problems, "repeats": repeats}


# ---------------------------------------------------------------- export

def _csv_text(header, rows, delimiter=",") -> str:
    out = io.StringIO(newline="")
    w = csv.writer(out, delimiter=delimiter, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return out.getvalue()


def _safe_cell(value: str) -> str:
    """A spreadsheet runs a cell that starts with = + - @ as a formula. Names and notes are free text
    (and may have come from someone else's file), so such a cell is given a leading apostrophe."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def export(channels: list, fmt: str) -> str:
    """The list as text in one of EXPORT_FORMATS."""
    if fmt == "wavelens":
        return json.dumps({"format": FORMAT, "updated": now_iso(), "channels": channels}, ensure_ascii=False, indent=1) + "\n"
    if fmt == "csv":
        return _csv_text(CSV_COLUMNS, [
            [_safe_cell(c["name"]), mhz(c["freq_hz"]), mhz(c["input_hz"]) if c.get("input_hz") else "", c.get("mode", "auto"),
             c.get("emission", ""), c.get("bandwidth_hz") or "", c.get("tone_type", ""), c.get("tone", ""),
             _safe_cell(c.get("group", "")), c.get("service", ""), _safe_cell(", ".join(c.get("tags") or [])),
             "yes" if c.get("lockout") else "", _safe_cell(c.get("notes", "")),
             "" if c.get("lat") is None else c["lat"], "" if c.get("lon") is None else c["lon"]] for c in channels])
    if fmt == "chirp":
        head = ["Location", "Name", "Frequency", "Duplex", "Offset", "Tone", "rToneFreq", "cToneFreq", "DtcsCode",
                "DtcsPolarity", "RxDtcsCode", "CrossMode", "Mode", "TStep", "Skip", "Power", "Comment", "URCALL",
                "RPT1CALL", "RPT2CALL", "DVCODE"]
        mode_out = {"fm": "FM", "wfm": "WFM", "am": "AM", "dmr": "DMR", "p25": "P25", "dstar": "DV", "ysf": "DN",
                    "usb": "USB", "lsb": "LSB", "cw": "CW", "data": "DIG"}
        rows = []
        for i, c in enumerate(channels):
            duplex, offset = "", "0.000000"
            if c.get("input_hz"):
                diff = c["input_hz"] - c["freq_hz"]
                if abs(diff) > 100_000_000:
                    duplex, offset = "split", f"{c['input_hz'] / 1e6:.6f}"
                else:
                    duplex, offset = ("+" if diff > 0 else "-"), f"{abs(diff) / 1e6:.6f}"
            tone, rtone, dcs, pol = "", "88.5", "023", "NN"
            if c.get("tone_type") == "ctcss":
                tone, rtone = "Tone", c["tone"]
            elif c.get("tone_type") == "dcs":
                tone, dcs = "DTCS", c["tone"][:3]
                pol = "RR" if c["tone"].endswith("I") else "NN"
            mode = mode_out.get(c.get("mode", ""), "FM")
            if mode == "FM" and (c.get("bandwidth_hz") or 25000) <= 12500:
                mode = "NFM"
            rows.append([i, _safe_cell(c["name"]), f"{c['freq_hz'] / 1e6:.6f}", duplex, offset, tone, rtone, rtone, dcs, pol,
                         dcs, "Tone->Tone", mode, "5.00", "S" if c.get("lockout") else "", "",
                         _safe_cell(c.get("notes", "")), "", "", "", ""])
        return _csv_text(head, rows)
    if fmt == "gqrx":
        mode_out = {"fm": "Narrow FM", "wfm": "WFM (mono)", "am": "AM", "usb": "USB", "lsb": "LSB", "cw": "CW-U"}
        groups = sorted({(c.get("group") or "Untagged").replace(";", ",") for c in channels} | {"Untagged"})
        lines = ["# Tag name          ;  color"] + [f"{g:<20}; #c0c0c0" for g in groups]
        lines += ["", "# Frequency ; Name                     ; Modulation          ;  Bandwidth; Tags"]
        for c in channels:
            name = c["name"].replace(";", ",")
            tag = (c.get("group") or "Untagged").replace(";", ",")
            lines.append(f"{c['freq_hz']:>12}; {name:<25}; {mode_out.get(c.get('mode', ''), 'Narrow FM'):<20}; "
                         f"{c.get('bandwidth_hz') or 12500:>10}; {tag}")
        return "\n".join(lines) + "\n"
    if fmt == "sdrpp":
        mode_out = {"fm": 0, "wfm": 1, "am": 2, "usb": 4, "cw": 5, "lsb": 6, "data": 7}
        marks, used = {}, set()
        for c in channels:
            name = c["name"]
            if name in used:                      # the format keys bookmarks by name
                name = f"{name} {mhz(c['freq_hz'])}"
            n, base = 2, name
            while name in used:
                name, n = f"{base} ({n})", n + 1
            used.add(name)
            marks[name] = {"bandwidth": float(c.get("bandwidth_hz") or 12500), "frequency": float(c["freq_hz"]),
                           "mode": mode_out.get(c.get("mode", ""), 0)}
        return json.dumps({"bookmarks": marks}, ensure_ascii=False, indent=4) + "\n"
    raise ValueError("unknown export format")


# ---------------------------------------------------------------- what the bridge asks

class Lookup:
    """The list as the bridge uses it: what is on a frequency, and whether Follow should leave it alone.
    Re-read when the file changes (checked at most every few seconds), so an edit on the admin page is in
    use without a restart. Never raises: a missing or damaged list is an empty one."""

    CHECK_EVERY = 5.0
    LOCK_TOL = 0.0065        # MHz: a hit this close to a locked-out channel is that channel (half a 12.5 kHz step)

    def __init__(self, path: str = ""):
        self.path = path or STORE
        self._stamp, self._checked = None, 0.0
        self._data = ([], [], [])   # (rows, their MHz, locked-out MHz): swapped whole, so a reader on another thread never sees half
        self.count = 0

    def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._checked < self.CHECK_EVERY and self._stamp is not None:
            return
        self._checked = now
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = (0, 0)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        rows = []
        try:
            channels = Store(self.path).load()["channels"] if stamp != (0, 0) else []
        except Exception as e:  # noqa: BLE001 - see the docstring
            print(f"bridge: frequency list not read: {e}", file=sys.stderr)
            channels = []
        for c in channels:
            rows.append((c["freq_hz"] / 1e6, "output", c))
            if isinstance(c.get("input_hz"), int):
                rows.append((c["input_hz"] / 1e6, "input", c))
        rows.sort(key=lambda r: r[0])
        self._data = (rows, [r[0] for r in rows], sorted(m for m, side, c in rows if c.get("lockout") and side == "output"))
        self.count = len(channels)

    def near(self, f_mhz: float, tol_khz: float = 12.5) -> list:
        """[(distance MHz, side, channel)] within half of tol either way (plus the dongle's own error), nearest first."""
        self._refresh()
        rows, keys, _ = self._data
        half = max(tol_khz, 12.5) / 2000.0 + 0.004
        lo, hi = bisect.bisect_left(keys, f_mhz - half), bisect.bisect_right(keys, f_mhz + half)
        return sorted(((abs(m - f_mhz), side, c) for m, side, c in rows[lo:hi]), key=lambda h: h[0])

    def locked(self, f_mhz: float) -> bool:
        self._refresh()
        locked = self._data[2]
        i = bisect.bisect_left(locked, f_mhz - self.LOCK_TOL)
        return i < len(locked) and locked[i] <= f_mhz + self.LOCK_TOL

    def label(self, f_mhz: float, tol_khz: float = 12.5):
        """(name, cat, conf, extras) in the shape the bridge's licence lookups return, or None.
        A wide sweep bin holding several of the owner's channels lists them and names none."""
        hits = self.near(f_mhz, tol_khz)
        if not hits:
            return None
        names = []
        for _, _, c in hits:
            if c["name"] not in names:
                names.append(c["name"])
        if len(names) > 1 and tol_khz > 25:
            listed = ", ".join(names[:4]) + (" …" if len(names) > 4 else "")
            return (f"{len(names)} of your channels in this {tol_khz:.0f} kHz: {listed}", "UNKNOWN", "POSSIBLE",
                    {"own_channels": "; ".join(names[:8]),
                     "ambiguous": f"a {tol_khz:.0f} kHz sweep bin spans several channels; a 12.5 kHz sweep names the one"})
        _, side, c = hits[0]
        centre = c["input_hz"] if side == "input" else c["freq_hz"]
        mode = c.get("mode", "auto")
        x = {"agency": c.get("service") or "Your list", "own_channel": "from the frequency list on this Scanner",
             "own_id": c.get("id", ""),
             "listen": mode if mode in LISTEN_MODES else "auto", "lic_mhz": f"{centre / 1e6:.5f}"}
        if c.get("group"):
            x["own_group"] = c["group"]
        if c.get("tone_type") and c.get("tone"):
            x["own_tone"] = f"{c['tone_type'].upper()} {c['tone']}"
        if c.get("emission"):
            x["emission"] = c["emission"]
        if c.get("notes"):
            x["own_notes"] = c["notes"][:120]
        if c.get("lockout"):
            x["locked_out"] = "locked out in your frequency list: Follow never tunes it"
        if side == "input":
            x["repeater_input"] = "the mobile/handheld side of a repeater pair — heard only from close by"
            return f"{c['name']} · keying up nearby", "UNKNOWN", "LIKELY", x
        return c["name"], "UNKNOWN", "POSSIBLE", x


# ---------------------------------------------------------------- command line

def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="The Scanner's own frequency list (the admin page's Frequencies tab).")
    ap.add_argument("--file", default="", help=f"the list (default {STORE})")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="print the list")
    p = sub.add_parser("import", help="add the channels in a file")
    p.add_argument("path")
    p.add_argument("--mode", choices=("add", "update", "replace"), default="add")
    p = sub.add_parser("export", help="write the list to standard output")
    p.add_argument("--format", choices=sorted(EXPORT_FORMATS), default="wavelens")
    p = sub.add_parser("check", help="read a file and say what an import would take from it, changing nothing")
    p.add_argument("path")
    sub.add_parser("schema", help="print the JSON Schema of the stored format")
    a = ap.parse_args(argv)
    store = Store(a.file)
    try:
        if a.cmd == "schema":
            print(json.dumps(SCHEMA, indent=1))
        elif a.cmd == "list":
            for c in store.load()["channels"]:
                print(f"{mhz(c['freq_hz']):>12}  {c.get('mode', 'auto'):5} {'L' if c.get('lockout') else ' '} {c['name']}"
                      + (f"  [{c['group']}]" if c.get("group") else ""))
        elif a.cmd == "export":
            sys.stdout.write(export(store.load()["channels"], a.format))
        elif a.cmd in ("import", "check"):
            with open(a.path, "rb") as f:
                got = parse_import(f.read())
            for line in got["problems"]:
                print("  left out: " + line, file=sys.stderr)
            if a.cmd == "check":
                print(f"{IMPORT_FORMATS[got['format']]}: {len(got['channels'])} channels readable, "
                      f"{len(got['problems'])} rows with problems, {got['repeats']} repeats")
            else:
                n = store.edit(lambda doc: merge(doc, got["channels"], a.mode))
                print(f"{IMPORT_FORMATS[got['format']]}: {n['added']} added, {n['updated']} updated, {n['skipped']} already there")
        else:
            ap.print_help()
    except (StoreError, ValueError, OSError) as e:
        print(f"freqdb: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
