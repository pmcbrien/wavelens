"""Spotters (ESP32 companions) plugged into the Pi over USB.

The bridge drives every ESP32 it finds on a USB-serial port: it says {"c":"host"} so the board talks over the
cable instead of Bluetooth, gives each one a job, and relays what it hears to the phone over the Pi's single
Bluetooth link, tagged with which unit heard it. The phone needs no extra pairing and no extra Bluetooth load,
however many boards are plugged in.

Jobs ("roles"):
  ble          Bluetooth only, full-time
  wifi:1,6,11  Wi-Fi only, parked on those channels (one channel, or a few it cycles through)
  wifi:hop     Wi-Fi only, hopping every channel
  both         Wi-Fi + Bluetooth, time-shared (what a lone Spotter does)
Unless the owner picks one (kept in /etc/wavelens/spotters.json by USB port), roles follow what is attached:
the Pi's units, plus a Spotter the phone already holds over Bluetooth, split Bluetooth and the three busy
Wi-Fi channels between them. See auto_roles().

A unit is known by its USB port (/dev/serial/by-path), not its serial number: cheap boards often share one,
and a job should stay with the socket it is plugged into. The admin page's USB flasher writes the port to
PAUSE_FILE, and the unit lets go of it until the flash is done.

Direction finding: each Spotter can be told which way its antenna faces ({"c":"face"}, firmware cap "df";
kept on the board and reported in its hello). With two or more facing different ways, every hit relayed to
the phone carries "brg": the power-weighted direction of the sides that heard the device in the last few
seconds — a rough quadrant, never more. Units with a facing also keep their Bluetooth scan on (mode "both")
whatever their Wi-Fi job, so Bluetooth devices are heard on every side. For a Wi-Fi device on a channel only
one unit is parked on, the phone asks for a converge ({"c":"df","id":BSSID,"ch":N}): every faced unit tunes
to that channel for a moment ({"c":"tune"}) and goes back to its job on its own.

Standard library only (termios), like the rest of the bridge.
"""
from __future__ import annotations

import glob
import json
import math
import statistics
import os
import select
import termios
import threading
import time

ROLES_FILE = "/etc/wavelens/spotters.json"
PAUSE_FILE = "/run/wavelens/spotter-pause"      # one device path per line, or "all": the admin page is flashing
BY_PATH = "/dev/serial/by-path"
ESP_IDS = {("10c4", "ea60"), ("1a86", "7523"), ("1a86", "55d4"), ("303a", "1001"), ("303a", "4001")}
# Direction finding (same mounts and maths as the desktop Find tab, desktop/wavelens/df.py).
FACES = {"front": 0, "front-right": 45, "right": 90, "rear-right": 135, "rear": 180, "rear-left": 225,
         "left": 270, "front-left": 315}
DF_FRESH_S = 6.0            # a unit counts towards a bearing only if it heard the device this recently
DF_LEVEL_S = 4.0            # its level is the median of this many seconds of readings
DF_KEEP = 32                # readings kept per device per unit
DF_MAX_DEVICES = 2000       # devices tracked at once; the oldest are dropped
DF_TUNE_MS = 2000           # a converge's tune window
DF_REPEAT_S = 6.0           # one converge per device this often at most
COMPASS = ("ahead", "ahead-right", "right", "behind-right", "behind", "behind-left", "left", "ahead-left")
HOST_EVERY_S = 3.0          # until the board answers, ask again (it may still be booting after the port opens)
STALE_S = 30.0              # no line for this long: say so in the status list


def port_label(by_path_name: str) -> str:
    """'platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0-port0' -> 'USB 1.3' (the socket people see)."""
    try:
        part = by_path_name.split("-usb-")[1].split(":")[1]
        return "USB " + part
    except IndexError:
        return by_path_name[-20:]


def tty_vidpid(tty: str) -> "tuple[str, str] | None":
    """USB vendor/product of /dev/ttyUSB0 / ttyACM0, read from sysfs."""
    d = os.path.realpath(f"/sys/class/tty/{os.path.basename(tty)}/device")
    for _ in range(5):
        try:
            with open(os.path.join(d, "idVendor")) as a, open(os.path.join(d, "idProduct")) as b:
                return a.read().strip(), b.read().strip()
        except OSError:
            d = os.path.dirname(d)
    return None


def discover() -> "dict[str, str]":
    """{unit id (by-path name): /dev/ttyX} for every ESP32-looking USB-serial port."""
    found = {}
    for link in sorted(glob.glob(os.path.join(BY_PATH, "*"))):
        dev = os.path.realpath(link)
        if not (os.path.basename(dev).startswith("ttyUSB") or os.path.basename(dev).startswith("ttyACM")):
            continue
        if tty_vidpid(dev) in ESP_IDS:
            found[os.path.basename(link)] = dev
    return found


def bearing(levels: "dict[str, float]", mounts: "dict[str, int]"):
    """(degrees 0-359 clockwise from the front, agreement 0-1, units used) from {unit: dBm} and {unit: mount
    degrees}; None when fewer than two units on different sides have a level."""
    used = [(mounts[u], lv) for u, lv in levels.items() if lv is not None and mounts.get(u) is not None]
    if len(used) < 2 or len({d for d, _ in used}) < 2:
        return None
    top = max(lv for _, lv in used)
    x = y = total = 0.0
    for deg, lv in used:
        w = 10 ** ((lv - top) / 10.0)           # relative power: 3 dB stronger pulls twice as hard
        x += w * math.sin(math.radians(deg))
        y += w * math.cos(math.radians(deg))
        total += w
    if math.hypot(x, y) < 1e-9:
        return None                             # perfectly balanced: no side wins
    deg = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
    return round(deg) % 360, min(1.0, math.hypot(x, y) / total), len(used)


def compass_word(deg: float) -> str:
    return COMPASS[int(((deg % 360) + 22.5) // 45) % 8]


class Bearings:
    """Per-device, per-unit signal readings, and the bearing they give."""

    def __init__(self):
        self.log: "dict[str, dict[str, list]]" = {}
        self.lock = threading.Lock()

    def add(self, dev: str, unit: str, dbm, now: float) -> None:
        if not dev or not unit or not isinstance(dbm, (int, float)) or dbm <= -127:
            return
        with self.lock:
            per = self.log.get(dev)
            if per is None:
                if len(self.log) >= DF_MAX_DEVICES:      # drop the device heard longest ago
                    oldest = min(self.log, key=lambda k: max((r[-1][0] for r in self.log[k].values() if r), default=0))
                    del self.log[oldest]
                per = self.log[dev] = {}
            rs = per.setdefault(unit, [])
            rs.append((now, float(dbm)))
            if len(rs) > DF_KEEP:
                del rs[:len(rs) - DF_KEEP]

    def levels(self, dev: str, now: float) -> "dict[str, float]":
        with self.lock:
            per = self.log.get(dev) or {}
            out = {}
            for unit, rs in per.items():
                if rs and now - rs[-1][0] <= DF_FRESH_S:
                    vals = [r for t, r in rs if now - t <= DF_LEVEL_S] or [rs[-1][1]]
                    out[unit] = statistics.median(vals)
            return out

    def of(self, dev: str, mounts: "dict[str, int]", now: float) -> "dict | None":
        """The "brg" field for a device line, or None."""
        lv = self.levels(dev, now)
        b = bearing(lv, mounts)
        if b is None:
            return None
        deg, agree, n = b
        return {"deg": deg, "agree": round(agree, 2), "n": n, "word": compass_word(deg),
                "sides": {u: round(v) for u, v in lv.items() if mounts.get(u) is not None}}


def role_commands(role: str, df: bool = False) -> "list[dict]":
    """The firmware commands for a job. df: the unit has a facing and at least one other faced unit is there,
    so it keeps Bluetooth on beside its Wi-Fi channels (a Bluetooth device then has a level on every side)."""
    if role == "ble":
        return [{"c": "mode", "v": "ble"}]
    if role.startswith("wifi"):
        chans = role.partition(":")[2]
        mode = "both" if df else "wifi"
        if chans in ("", "hop"):
            return [{"c": "mode", "v": mode}, {"c": "ch", "v": "hop"}]
        nums = [int(x) for x in chans.split(",") if x.strip().isdigit() and 1 <= int(x) <= 177]
        return [{"c": "mode", "v": mode}, {"c": "ch", "v": nums or "hop"}]
    return [{"c": "mode", "v": "both"}, {"c": "ch", "v": "hop"}]


def auto_roles(units: "list[str]", phone_spotter: bool) -> "dict[str, str]":
    """Split the work between the Pi's units (sorted by port, so it is stable) and, when the phone holds a
    Spotter over Bluetooth, that one too. The Pi's units park on the busy Wi-Fi channels (1, 6, 11), where a
    drive-past camera is heard within a beacon or two; the phone's Spotter keeps Wi-Fi + Bluetooth on every
    channel (see phone_role()), so a network on 3, 9 or 13 is still heard, and Bluetooth is still covered."""
    n = len(units)
    if n == 0:
        return {}
    if phone_spotter:                       # the phone's Spotter hops everything; the Pi's park on 1, 6, 11
        plans = {1: ["wifi:1,6,11"], 2: ["ble", "wifi:1,6,11"], 3: ["ble", "wifi:1,6", "wifi:11"],
                 4: ["ble", "wifi:1", "wifi:6", "wifi:11"]}
    else:
        plans = {1: ["both"], 2: ["ble", "wifi:1,6,11"], 3: ["ble", "wifi:1,6", "wifi:11"],
                 4: ["ble", "wifi:1", "wifi:6", "wifi:11"]}
    plan = plans.get(n) or (plans[max(plans)] + ["wifi:hop"] * (n - max(plans)))
    return dict(zip(units, plan))


def role_label(role: str) -> str:
    if role == "ble":
        return "Bluetooth only"
    if role == "both":
        return "Wi-Fi + Bluetooth"
    if role.startswith("wifi"):
        ch = role.partition(":")[2]
        return "Wi-Fi, all channels" if ch in ("", "hop") else f"Wi-Fi ch {ch.replace(',', ', ')}"
    return role


class Unit:
    def __init__(self, uid: str, dev: str, hub: "Hub"):
        self.uid, self.dev, self.hub = uid, dev, hub
        self.fd: "int | None" = None
        self.role = "both"
        self.hosted = False
        self.ver = ""
        self.name = ""
        self.last = 0.0
        self.hits = 0
        self.msg = "starting"
        self.stop = False
        self.paused = False
        self.applied = ""
        self.face = ""                                  # which way its antenna faces ("" = not set), from its hello
        self.caps: "list[str]" = []
        self.chip = ""
        threading.Thread(target=self.run, name=f"spotter {uid[-12:]}", daemon=True).start()

    # -- serial
    def open(self) -> None:
        fd = os.open(self.dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0                                    # iflag: raw
        attrs[1] = 0                                    # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0                                    # lflag: no echo, no canonical
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 0, 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        self.fd = fd

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.hosted = False
        self.applied = ""

    def send(self, obj: dict) -> None:
        if self.fd is None:
            return
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        try:
            os.write(self.fd, data)
        except OSError as e:
            self.hub.log(f"spotters: {self.label()}: write failed ({e})")

    def label(self) -> str:
        return port_label(self.uid)

    def df_on(self) -> bool:
        """This unit takes part in direction finding: it has a facing and another faced unit is hosted."""
        return bool(self.face) and self.hub.faced_count() >= 2

    def apply_role(self) -> None:
        key = self.role + ("+df" if self.df_on() else "")
        if self.applied == key:
            return
        for c in role_commands(self.role, df=self.df_on()):
            self.send(c)
            time.sleep(0.05)
        self.applied = key
        self.hub.log(f"spotters: {self.label()} ({self.name or 'ESP32'}): {role_label(self.role)}")

    def paused_now(self) -> bool:
        try:
            with open(PAUSE_FILE) as f:
                want = {ln.strip() for ln in f if ln.strip()}
        except OSError:
            return False
        return "all" in want or self.dev in want or os.path.join(BY_PATH, self.uid) in want

    # -- main loop
    def run(self) -> None:
        buf = b""
        asked = 0.0
        while not self.stop:
            if self.paused_now():
                if not self.paused:
                    self.hub.log(f"spotters: {self.label()}: let go of the port (being flashed)")
                    self.close(); self.paused = True; self.msg = "being flashed"
                time.sleep(1)
                continue
            if self.paused:
                self.paused = False
                time.sleep(2)                               # the board reboots after a flash
            if self.fd is None:
                try:
                    self.open(); buf = b""; asked = 0.0; self.msg = "waiting for the board"
                except OSError as e:
                    self.msg = f"can't open {self.dev}: {e.strerror}"
                    time.sleep(5)
                    continue
            now = time.time()
            if not self.hosted and now - asked > HOST_EVERY_S:
                self.send({"c": "host"}); asked = now
            try:
                r, _, _ = select.select([self.fd], [], [], 1.0)
                chunk = os.read(self.fd, 4096) if r else b""
            except OSError as e:
                self.hub.log(f"spotters: {self.label()}: port lost ({e.strerror}); will reopen")
                self.close(); time.sleep(2)
                continue
            if r and not chunk:                             # readable but empty: unplugged
                self.close(); time.sleep(2)
                continue
            buf += chunk
            if len(buf) > 65536:
                buf = buf[-4096:]
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                self.on_line(line.strip())

    def on_line(self, raw: bytes) -> None:
        if not raw.startswith(b"@{"):
            return                                          # boot text and debug prints
        try:
            o = json.loads(raw[1:].decode("utf-8", "replace"))
        except ValueError:
            return
        self.last = time.time()
        t = o.get("t")
        if t == "hello":
            first = not self.hosted
            self.hosted = True
            self.ver = str(o.get("ver") or "")
            self.name = str(o.get("name") or "")
            self.caps = [str(c) for c in (o.get("caps") or []) if isinstance(c, str)]
            self.chip = str(o.get("chip") or "")
            face = str(o.get("face") or "")
            face = face if face in FACES else ""
            if face != self.face:
                self.face = face
                self.hub.log(f"spotters: {self.label()}: facing {face or 'not set'}")
                self.hub.faces_changed()
            if first:
                self.hub.log(f"spotters: {self.label()}: {self.name or 'ESP32'} {self.ver} hosted over USB")
                self.applied = ""
                self.hub.changed()
            self.apply_role()
        elif t == "status":
            self.msg = str(o.get("msg") or self.msg)
        elif t in ("wifi", "ble", "rf"):
            self.hits += 1
            o["u"] = self.label()                           # which unit heard it; the phone shows it
            self.hub.heard(o, self)
            self.hub.emit(o)
        elif t == "tune":
            if o.get("ok") is False:
                self.hub.log(f"spotters: {self.label()}: direction reading refused: {o.get('msg') or '?'}")

    def info(self) -> dict:
        age = time.time() - self.last if self.last else None
        return {"u": self.label(), "id": self.uid, "name": self.name, "ver": self.ver, "role": self.role,
                "role_label": role_label(self.role), "auto": self.uid not in self.hub.chosen,
                "hosted": self.hosted, "ok": bool(self.hosted and age is not None and age < STALE_S),
                "hits": self.hits, "msg": self.msg, "face": self.face, "df": "df" in self.caps,
                "ghz5": "5g" in self.caps}


class Hub:
    """All Pi-hosted Spotters. emit(obj) sends a line to the phone; log(text) goes to the journal."""

    def __init__(self, emit, log):
        self.emit_fn, self.log = emit, log
        self.units: "dict[str, Unit]" = {}
        self.chosen: "dict[str, str]" = self.load()
        self.phone_spotter = False
        self.lock = threading.Lock()
        self.on_change = None
        self.bearings = Bearings()
        self.df_asked: "dict[str, float]" = {}          # device -> when its last converge was sent
        threading.Thread(target=self.watch, name="spotters", daemon=True).start()

    def emit(self, obj: dict) -> None:
        self.emit_fn(obj)

    # -- persistence of owner-chosen roles
    def load(self) -> "dict[str, str]":
        try:
            with open(ROLES_FILE) as f:
                d = json.load(f)
            return {k: str(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def roles_mtime(self) -> float:
        try:
            return os.path.getmtime(ROLES_FILE)
        except OSError:
            return 0.0

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(ROLES_FILE), exist_ok=True)
            tmp = ROLES_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.chosen, f, indent=1)
            os.replace(tmp, ROLES_FILE)
        except OSError as e:
            self.log(f"spotters: could not save roles ({e})")

    # -- roles
    def replan(self) -> None:
        with self.lock:
            auto = auto_roles([u for u in sorted(self.units) if u not in self.chosen], self.phone_spotter)
            for uid, unit in self.units.items():
                unit.role = self.chosen.get(uid) or auto.get(uid, "both")
                if unit.hosted:
                    unit.apply_role()

    def set_role(self, who: str, role: str) -> str:
        """Owner's choice from the phone or admin page. role "auto" goes back to the automatic split."""
        uid = next((k for k, u in self.units.items() if who in (k, u.label())), None)
        if uid is None:
            return f"no Spotter on {who}"
        if role == "auto":
            self.chosen.pop(uid, None)
        elif role in ("ble", "both") or role.startswith("wifi"):
            self.chosen[uid] = role
        else:
            return f"unknown job {role}"
        self.save()
        self.replan()
        self.changed()
        return f"{port_label(uid)}: {role_label(self.units[uid].role)}"

    # -- direction finding
    def mounts(self) -> "dict[str, int]":
        return {u.label(): FACES[u.face] for u in self.units.values() if u.hosted and u.face in FACES}

    def faced_count(self) -> int:
        return len({u.face for u in self.units.values() if u.hosted and u.face in FACES})

    def faces_changed(self) -> None:
        for unit in list(self.units.values()):
            if unit.hosted:
                unit.apply_role()                        # Bluetooth stays on beside Wi-Fi once two sides are set
        self.changed()

    def heard(self, o: dict, unit: "Unit") -> None:
        """A hit from a unit: note its level, and put the bearing on the line when the sides give one."""
        dev = str(o.get("id") or "")
        now = time.time()
        self.bearings.add(dev, unit.label(), o.get("r"), now)
        if unit.face and self.faced_count() >= 2:
            b = self.bearings.of(dev, self.mounts(), now)
            if b:
                o["brg"] = b

    def set_face(self, who: str, face: str) -> str:
        """Owner's choice: which way a Pi-hosted Spotter faces. Saved on the board itself."""
        unit = next((u for k, u in self.units.items() if who in (k, u.label())), None)
        if unit is None:
            return f"no Spotter on {who}"
        face = (face or "").strip().lower().replace("back", "rear")
        if face in ("", "off", "none"):
            face = ""
        elif face not in FACES:
            return "facing must be " + ", ".join(FACES) + " or off"
        if "df" not in unit.caps:
            return f"{unit.label()}: update its firmware for direction finding"
        unit.send({"c": "face", "v": face})              # the board answers with a hello carrying it
        return f"{unit.label()}: facing {face or 'not set'}"

    def converge(self, dev: str, ch: int) -> str:
        """Every faced unit listens on the device's Wi-Fi channel for a moment, so all sides measure it at once."""
        now = time.time()
        if not dev or not isinstance(ch, int) or ch <= 0:
            return "direction reading: need a device id and its channel"
        if now - self.df_asked.get(dev, 0.0) < DF_REPEAT_S:
            return "direction reading: already measuring"
        units = [u for u in self.units.values() if u.hosted and u.face and "df" in u.caps
                 and (ch <= 14 or "5g" in u.caps)]
        if len({u.face for u in units}) < 2:
            return "direction reading: needs two Spotters facing different ways that can hear that channel"
        self.df_asked[dev] = now
        if len(self.df_asked) > 500:
            for k in sorted(self.df_asked, key=self.df_asked.get)[:250]:
                del self.df_asked[k]
        for u in units:
            u.send({"c": "tune", "ch": ch, "ms": DF_TUNE_MS, "id": dev})
        return f"direction reading on channel {ch} with {len(units)} Spotters"

    def set_phone_spotter(self, present: bool) -> None:
        if present != self.phone_spotter:
            self.phone_spotter = present
            self.replan()

    def phone_role(self) -> "str | None":
        """The job the phone should give its own Bluetooth Spotter while the Pi has units: Wi-Fi + Bluetooth,
        hopping every channel — the all-channel, all-radio catch-all next to the Pi's parked units."""
        if self.phone_spotter and self.units:
            return "both"
        return None

    def changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception as e:                       # never let a UI refresh kill a unit thread
                self.log(f"spotters: refresh failed ({e})")

    def info(self) -> "list[dict]":
        return [u.info() for _, u in sorted(self.units.items())]

    # -- hot-plug
    def watch(self) -> None:
        seen_mtime = self.roles_mtime()
        while True:
            try:
                m = self.roles_mtime()
                if m != seen_mtime:                         # the admin page changed a job
                    seen_mtime = m
                    self.chosen = self.load()
                    self.replan()
                    self.changed()
                now = discover()
                gone = set(self.units) - set(now)
                new = set(now) - set(self.units)
                for uid in gone:
                    self.log(f"spotters: {port_label(uid)} unplugged")
                    self.units[uid].stop = True
                    self.units[uid].close()
                    del self.units[uid]
                for uid in sorted(new):
                    self.log(f"spotters: ESP32 on {port_label(uid)} ({now[uid]})")
                    self.units[uid] = Unit(uid, now[uid], self)
                if gone or new:
                    self.replan()
                    self.changed()
            except Exception as e:
                self.log(f"spotters: scan failed ({e})")
            time.sleep(5)
