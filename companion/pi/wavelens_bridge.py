#!/usr/bin/env python3
"""
Wavelens companion bridge — Raspberry Pi / carputer / any Linux box with BlueZ.

Turns the machine into the BLE peripheral the Wavelens app looks for, and relays
whatever the radios attached to it produce. Feeds can be combined; add your own by
writing Wavelens lines (see ../README.md) to stdin.

  rtl_433 -F json | ./wavelens_bridge.py --rtl433        # RTL-SDR: 433/315/915 MHz decoders (TPMS, remotes, sensors)
  ./wavelens_bridge.py --hackrf-sweep                    # HackRF: 1 MHz–6 GHz energy sweep → carriers by band
  ./wavelens_bridge.py --rtl-power 300M:1000M:50k        # RTL-SDR: the same idea below 1.7 GHz
  ./wavelens_bridge.py --rtl-power 150M:174M:12.5k --rtl-power 806M:824M:12.5k --rtl-power-device 00001090
                                                         # several ranges take turns on one dongle; pin it by serial
  ./wavelens_bridge.py --dump1090 localhost:30003        # ADS-B aircraft from dump1090 / readsb (SBS port)
  ./wavelens_bridge.py --kismet-json /tmp/kismet.json    # Kismet export from a monitor-mode Wi-Fi adapter
  ./wavelens_bridge.py --fcc-pack fcc-local.json ...     # label licensed carriers by name (see ../tools/fcc_local_pack.py)
  ./wavelens_bridge.py                                   # raw: stdin already in Wavelens line format

Several at once:  rtl_433 -F json | ./wavelens_bridge.py --rtl433 --hackrf-sweep --dump1090 localhost:30003

Needs: python3-bluezero (pip install bluezero), BlueZ 5.50+, the adapter powered, and
whichever of rtl_433 / hackrf_sweep / rtl_power / dump1090 you use on PATH. Receive only:
nothing here ever transmits. Status: written against the documented APIs and CSV formats;
not yet run on a Pi by the author.
"""

import argparse
import bisect
import json
import math
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from queue import Queue, Empty

SVC = "57617665-6c65-6e73-0100-000000000001"
TX = "57617665-6c65-6e73-0100-000000000002"
RX = "57617665-6c65-6e73-0100-000000000003"
FW = "wavelens-bridge 0.1"

out: "Queue[str]" = Queue(maxsize=256)
state = {"linked": False, "band": "all", "mode": "rf", "ch": "hop", "lat": None, "lon": None, "pos_at": 0.0}


def emit(obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    try:
        out.put_nowait(line)
    except Exception:
        pass  # full: drop, the next packet will come


CAPS = ["rf"]


def hello() -> None:
    emit({"t": "hello", "fw": FW, "name": state.get("name", "Wavelens-Pi"), "code": state.get("code", ""), "caps": sorted(set(CAPS))})


# ---------------------------------------------------------------- local FCC license pack

class FccPack:
    """Frequency → licensee lookup built by ../tools/fcc_local_pack.py.

    A hit at 453.21 MHz becomes "Licensed: Example County Sheriff's Office · WQZZ001 · public
    safety" with a category hint from the licensee's name and radio service. When the phone
    has shared its position (opt-in), the nearest licensed site's distance is included and
    the nearest licensee wins when several share a frequency.
    """

    def __init__(self, path: str):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.entries = d.get("entries", [])
        self.keys = [e["mhz"] for e in self.entries]           # sorted by the builder
        self.area = d.get("area", {})

    def lookup(self, mhz: float, tol_khz: float = 12.5):
        """Entries whose assigned frequency (or band) covers mhz ± tol. Nearest-first when a position is known."""
        lo = bisect.bisect_left(self.keys, mhz - 5.0)
        hits = []
        for e in self.entries[lo:]:
            if e["mhz"] > mhz + 5.0:
                break
            half = max(e.get("bw_khz") or 12.5, tol_khz) / 2000.0
            if e.get("mhz_hi"):
                ok = e["mhz"] - half <= mhz <= e["mhz_hi"] + half
            else:
                ok = abs(e["mhz"] - mhz) <= half
            if ok:
                hits.append(e)
        lat, lon = state.get("lat"), state.get("lon")
        if lat is not None and lon is not None and time.time() - state.get("pos_at", 0) < 120:
            def dist(e):
                ds = [haversine_km(lat, lon, la, lo_) for la, lo_ in e.get("sites", [])]
                return min(ds) if ds else 1e9
            hits.sort(key=dist)
            return [(e, dist(e)) for e in hits]
        return [(e, None) for e in hits]

    def label(self, mhz: float, tol_khz: float = 12.5):
        """(name, cat, conf, extras) for a carrier, or None when nothing is licensed there."""
        hits = self.lookup(mhz, tol_khz)
        if not hits:
            return None
        # A mobile/handheld (station class MO…) frequency is a repeater input: radios transmit there
        # toward the tower, and a low-power handheld is only audible from close by. Prefer that reading.
        inputs = [(h, dd) for h, dd in hits if is_input(h)]
        e, d = (inputs or hits)[0]
        who = e['name'] or e['call']
        if is_input(e):
            name = f"Radio transmitting nearby · {who} repeater input · {e['call']}"
        else:
            name = f"Licensed: {who} · {e['call']} · {e['service_label']}"
        x = {"licensee": e["name"], "call_sign": e["call"], "service": e["service_label"], "place": e.get("place", "")}
        if is_input(e):
            x["repeater_input"] = "a mobile or handheld transmitting up to its repeater — heard only from close by"
        if d is not None and d < 1e8:
            x["site_km"] = f"{d:.1f}"
        if len(hits) > 1:
            x["other_licensees"] = "; ".join(f"{h['name'] or h['call']} ({h['call']})" for h, _ in hits[1:4])
        return name, e.get("cat") or "UNKNOWN", "LIKELY", x


def is_input(e: dict) -> bool:
    """FCC station class MO, MO3 … = mobile / portable units; on a repeater pair that is the input side."""
    return str(e.get("class", "")).upper().startswith("MO")


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


FCC: "FccPack | None" = None


# ---------------------------------------------------------------- spectrum sweeps (HackRF, rtl_power)

# What a lone carrier in a band most likely is. The phone treats these as hints only.
# (lo_MHz, hi_MHz, name, category, confidence); first match wins; bands not listed are
# reported as an unclassified carrier. Wi-Fi/BLE bands are skipped: everything is there.
BANDS = [
    (5645, 5945, "5.8 GHz carrier (FPV video, or Wi-Fi ch 149+)", "DRONE", "POSSIBLE"),
    (5150, 5644, None, None, None),                       # 5 GHz Wi-Fi: skip
    (2400, 2500, None, None, None),                       # 2.4 GHz Wi-Fi / BLE: skip
    (1240, 1300, "1.2–1.3 GHz video / telemetry link", "DRONE", "POSSIBLE"),
    (1559, 1610, None, None, None),                       # GNSS: skip
    (902, 928, "900 MHz ISM burst (LoRa, telemetry, meters)", "IOT", "POSSIBLE"),
    (863, 870, "868 MHz ISM burst (EU LoRa, sensors)", "IOT", "POSSIBLE"),
    (433.05, 434.79, "433 MHz ISM burst (remotes, sensors, TPMS)", "IOT", "POSSIBLE"),
    (314, 316, "315 MHz burst (TPMS, key fob)", "VEHICLE", "POSSIBLE"),
    (136, 174, "VHF voice / data", "UNKNOWN", "POSSIBLE"),
    (806, 824, "Radio keying up nearby (800 MHz repeater input)", "UNKNOWN", "POSSIBLE"),
    (851, 869, "800 MHz tower output (regional, not nearby)", "UNKNOWN", "POSSIBLE"),
    (400, 420, "UHF data / telemetry", "UNKNOWN", "POSSIBLE"),
    (450, 470, "UHF voice / data", "UNKNOWN", "POSSIBLE"),
]


def band_of(mhz: float):
    for lo, hi, name, cat, conf in BANDS:
        if lo <= mhz <= hi:
            return name, cat, conf
    return f"{mhz:.1f} MHz carrier", "UNKNOWN", "POSSIBLE"


class Sweep:
    """Parses hackrf_sweep / rtl_power CSV rows and reports bins that rise above a rolling floor.

    Row format (both tools): date, time, hz_low, hz_high, hz_bin_width, samples, dB, dB, dB, ...
    """

    def __init__(self, src: str, threshold_db: float = 18.0, rate_s: float = 2.0, bucket_mhz: float = 1.0):
        self.src, self.th, self.rate, self.bucket = src, threshold_db, rate_s, bucket_mhz
        self.floor = {}       # bin start Hz → EMA of dB
        self.last = {}        # bucket → last report time

    def row(self, line: str) -> None:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            return
        try:
            lo, hi, step = float(parts[2]), float(parts[3]), float(parts[4])
            vals = [float(v) for v in parts[6:] if v]
        except ValueError:
            return
        now = time.time()
        for i, db in enumerate(vals):
            hz = lo + i * step
            f = self.floor.get(hz)
            if f is None:
                self.floor[hz] = db
                continue
            # slow floor: a carrier that stays for minutes becomes the floor, which is what we want for a drive
            self.floor[hz] = f * 0.97 + db * 0.03
            if db - f < self.th:
                continue
            mhz = hz / 1e6
            x = {"over_floor_db": f"{db - f:.0f}", "bin_hz": f"{step:.0f}"}
            lic = FCC.label(mhz, tol_khz=max(step / 1000.0, 12.5)) if FCC else None
            if lic:
                name, cat, conf, lx = lic
                x.update(lx)
                ident = f"fcc/{lx['call_sign']}/{mhz:.3f}"
            else:
                name, cat, conf = band_of(mhz)
                if name is None:
                    continue
                ident = None
            bucket = round(mhz / self.bucket)
            if now - self.last.get(bucket, 0) < self.rate:
                continue
            self.last[bucket] = now
            emit({"t": "rf", "id": ident or f"{self.src}/{bucket * self.bucket:.0f}", "n": name,
                  "f": int(round(mhz)), "r": int(round(db)), "cat": cat, "conf": conf, "src": self.src, "x": x})


def run_once(cmd: str, handler) -> bool:
    """Run a tool until it exits, handing it each stdout line. False if it isn't installed."""
    try:
        p = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except FileNotFoundError:
        print(f"bridge: {cmd.split()[0]} not found on PATH", file=sys.stderr)
        return False
    for line in p.stdout:
        handler(line)
    p.wait()
    return True


def feed_process(cmd: str, handler) -> None:
    """Run a sweep tool and hand it each stdout line; restart it if it exits."""
    while run_once(cmd, handler):
        time.sleep(2)


def feed_hackrf(range_mhz: str, bin_hz: int) -> None:
    sw = Sweep("hackrf")
    feed_process(f"hackrf_sweep -f {range_mhz} -w {bin_hz} -l 32 -g 20", sw.row)


def feed_rtl_power(specs: "list[str]", device: str = "", dwell_s: int = 15) -> None:
    """One range: rtl_power runs continuously. Several: they take turns on the same dongle,
    dwell_s seconds each (rtl_power -e), so VHF and 800 MHz can share one RTL-SDR.
    device is an index or serial (rtl_power -d matches either)."""
    sw = Sweep("rtlpower")          # one floor map keyed by Hz, so the ranges don't disturb each other
    dev = f" -d {shlex.quote(device)}" if device else ""
    if len(specs) == 1:
        feed_process(f"rtl_power -f {specs[0]}{dev} -i 1 -g 30 -", sw.row)
        return
    while True:
        for spec in specs:
            if not run_once(f"rtl_power -f {spec}{dev} -i 1 -g 30 -e {dwell_s}s -", sw.row):
                return
            time.sleep(0.5)         # let the dongle close before the next range opens it


# ---------------------------------------------------------------- ADS-B (dump1090 / readsb, SBS BaseStation port 30003)

def feed_dump1090(hostport: str) -> None:
    host, _, port = hostport.partition(":")
    port = int(port or 30003)
    seen = {}
    while True:
        try:
            with socket.create_connection((host, port), timeout=10) as sk:
                f = sk.makefile("r", encoding="utf-8", errors="replace")
                for line in f:
                    p = line.strip().split(",")
                    if len(p) < 22 or p[0] != "MSG":
                        continue
                    icao = p[4].strip().lower()
                    if not icao:
                        continue
                    now = time.time()
                    if now - seen.get(icao, 0) < 5:
                        continue
                    seen[icao] = now
                    callsign = p[10].strip()
                    x = {}
                    if p[11]: x["alt_ft"] = p[11]
                    if p[12]: x["gs_kt"] = p[12]
                    if p[14] and p[15]: x["lat"], x["lon"] = p[14], p[15]
                    if p[17]: x["squawk"] = p[17]
                    emit({"t": "rf", "id": f"adsb/{icao}", "n": f"aircraft {callsign or icao.upper()}",
                          "f": 1090, "r": -60, "src": "adsb", "x": x})
        except OSError as e:
            print(f"bridge: dump1090 {hostport}: {e}; retrying", file=sys.stderr)
            time.sleep(5)


# ---------------------------------------------------------------- feeds

# rtl_433 model names → what they probably are. Anything else lands as unclassified RF
# with the model as its name, which is still useful on the map.
RTL433_CAT = {
    "TPMS": ("VEHICLE", "LIKELY"),
    "Schrader": ("VEHICLE", "LIKELY"),
    "Toyota": ("VEHICLE", "LIKELY"),
    "Ford": ("VEHICLE", "LIKELY"),
    "Citroen": ("VEHICLE", "LIKELY"),
    "Abarth": ("VEHICLE", "LIKELY"),
    "Renault": ("VEHICLE", "LIKELY"),
    "Hyundai": ("VEHICLE", "LIKELY"),
    "PMV": ("VEHICLE", "LIKELY"),
    "Elantra": ("VEHICLE", "LIKELY"),
    "Acurite": ("IOT", "LIKELY"),
    "Oregon": ("IOT", "LIKELY"),
    "LaCrosse": ("IOT", "LIKELY"),
    "Ambient": ("IOT", "LIKELY"),
    "Fineoffset": ("IOT", "LIKELY"),
    "Honeywell": ("IOT", "LIKELY"),
    "DSC": ("IOT", "LIKELY"),
    "Interlogix": ("IOT", "LIKELY"),
    "Generic-Remote": ("BEACON", "POSSIBLE"),
}


def feed_rtl433(line: str) -> None:
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return
    model = str(d.get("model", "rtl_433"))
    ident = str(d.get("id", d.get("sensor_id", d.get("channel", ""))))
    cat, conf = ("UNKNOWN", "POSSIBLE")
    for k, v in RTL433_CAT.items():
        if k.lower() in model.lower():
            cat, conf = v
            break
    if "tpms" in model.lower() or "pressure" in d:
        cat, conf = ("VEHICLE", "LIKELY")
    freq_f = d.get("freq") or d.get("freq1")
    try:
        freq_f = float(freq_f) if freq_f else 0.0
    except (TypeError, ValueError):
        freq_f = 0.0
    freq = int(freq_f)
    rssi = d.get("rssi")
    try:
        rssi = int(float(rssi))
    except (TypeError, ValueError):
        rssi = -70
    x = {k: str(v) for k, v in d.items() if k in ("pressure_kPa", "pressure_PSI", "temperature_C", "battery_ok", "mic", "type")}
    if FCC and cat == "UNKNOWN" and freq_f:
        lic = FCC.label(freq_f)
        if lic:
            _, cat, conf, lx = lic
            x.update(lx)
    emit({"t": "rf", "id": f"{model}/{ident}", "n": model, "f": freq, "r": rssi, "cat": cat, "conf": conf, "src": "rtl433", "x": x})


def feed_kismet_json(path: str) -> None:
    """Re-reads a Kismet 'devices' JSON export every few seconds and relays Wi-Fi APs."""
    seen = {}
    while True:
        try:
            with open(path) as f:
                devs = json.load(f)
        except Exception:
            devs = []
        now = time.time()
        for d in devs if isinstance(devs, list) else []:
            if d.get("kismet.device.base.phyname") != "IEEE802.11":
                continue
            mac = d.get("kismet.device.base.macaddr", "").lower()
            if len(mac) != 17 or now - seen.get(mac, 0) < 2:
                continue
            seen[mac] = now
            ssid = d.get("kismet.device.base.name", "") or ""
            ch = d.get("kismet.device.base.channel", "0")
            try:
                ch = int(str(ch).split(".")[0])
            except ValueError:
                ch = 0
            rssi = d.get("kismet.device.base.signal", {}).get("kismet.common.signal.last_signal", -70)
            emit({"t": "wifi", "id": mac, "n": ssid, "r": int(rssi), "ch": ch, "src": "kismet"})
        time.sleep(3)


def feed_stdin(mode: str) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if mode == "rtl433":
            feed_rtl433(line)
        else:
            if line.startswith("{"):
                out.put(line + "\n")
    if mode == "rtl433":
        # rtl_433 has exited (dongle busy, unplugged, wrong serial). Don't keep a Bluetooth link up
        # with a dead radio behind it: exit so the service restarts both.
        print("bridge: rtl_433 stopped sending; exiting so the service restarts it", file=sys.stderr)
        os._exit(1)


# ---------------------------------------------------------------- BLE peripheral

def blink(secs: int) -> None:
    """Identify: flash the Pi's activity LED (needs root) and say so on the console either way."""
    secs = max(1, min(30, secs))
    print(f"{FW}: identify — blinking for {secs} s (code {state.get('code', '?')})", file=sys.stderr)
    led = next((p for p in ("/sys/class/leds/ACT", "/sys/class/leds/led0") if os.path.isdir(p)), None)
    if not led:
        return
    try:
        with open(f"{led}/trigger", "w") as f:
            f.write("none")
        end = time.time() + secs
        on = False
        while time.time() < end:
            on = not on
            with open(f"{led}/brightness", "w") as f:
                f.write("1" if on else "0")
            time.sleep(0.15)
        with open(f"{led}/trigger", "w") as f:
            f.write("mmc0")
    except OSError:
        pass


def run_peripheral(name: str) -> None:
    from bluezero import adapter, peripheral

    dongle = list(adapter.Adapter.available())[0]
    code = dongle.address.replace(":", "")[-4:].upper()
    if name in ("", "Wavelens Pi", "Wavelens-Pi"):
        name = f"Wavelens-{code}"          # same scheme as the ESP32: the code is the end of the BT address
    state["name"], state["code"] = name, code
    dev = peripheral.Peripheral(dongle.address, local_name=name)
    dev.add_service(srv_id=1, uuid=SVC, primary=True)

    def on_rx(value, options):
        try:
            cmd = json.loads(bytes(value).decode("utf-8", "replace").strip() or "{}")
        except json.JSONDecodeError:
            return
        c = cmd.get("c")
        if c == "ping":
            hello()
            emit({"t": "status", "msg": "ok", "band": state["band"], "mode": state["mode"]})
        elif c == "blink":
            threading.Thread(target=blink, args=(int(cmd.get("v") or 5),), daemon=True).start()
            emit({"t": "status", "msg": "blinking", "band": state["band"], "mode": state["mode"]})
        elif c == "pos":
            # Opt-in from the phone: its GPS fix, so licensed sites can be ranked by distance.
            # Kept in memory only, never written anywhere, forgotten after two minutes.
            if cmd.get("lat") is None:
                state["lat"], state["lon"], state["pos_at"] = None, None, 0.0     # the phone turned sharing off
            else:
                try:
                    state["lat"], state["lon"] = float(cmd["lat"]), float(cmd["lon"])
                    state["pos_at"] = time.time()
                except (KeyError, TypeError, ValueError):
                    pass
        elif c in ("band", "mode", "ch"):
            state[c] = cmd.get("v")
            # An SDR bridge has nothing to park; say so once rather than pretend.
            emit({"t": "status", "msg": f"{c} noted; this bridge relays what rtl_433/kismet give it", "band": state["band"], "mode": state["mode"]})

    chars = {}

    def notify_cb(notifying, characteristic):
        chars["tx"] = characteristic if notifying else None
        state["linked"] = bool(notifying)
        if notifying:
            hello()

    dev.add_characteristic(srv_id=1, chr_id=1, uuid=TX, value=[], notifying=False,
                           flags=["notify"], notify_callback=notify_cb)
    dev.add_characteristic(srv_id=1, chr_id=2, uuid=RX, value=[], notifying=False,
                           flags=["write", "write-without-response"], write_callback=on_rx)

    def pump():
        while True:
            try:
                line = out.get(timeout=0.5)
            except Empty:
                continue
            tx = chars.get("tx")
            if tx is None:
                continue
            data = line.encode("utf-8")
            for i in range(0, len(data), 180):      # under a 247-byte MTU; the app reassembles on newline
                try:
                    tx.set_value(list(data[i:i + 180]))
                except Exception:
                    break
                time.sleep(0.005)

    threading.Thread(target=pump, daemon=True).start()
    print(f"{FW}: advertising as {name!r} on {dongle.address}  (pair code {code})", file=sys.stderr)
    dev.publish()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rtl433", action="store_true", help="stdin is rtl_433 -F json")
    ap.add_argument("--hackrf-sweep", action="store_true", help="run hackrf_sweep and report carriers by band")
    ap.add_argument("--hackrf-range", default="300:6000", help="MHz range for hackrf_sweep (default 300:6000)")
    ap.add_argument("--hackrf-bin", type=int, default=500000, help="bin width Hz (default 500000)")
    ap.add_argument("--rtl-power", metavar="SPEC", action="append", default=[],
                    help="run rtl_power with this -f spec, e.g. 150M:174M:12.5k; repeat for several ranges, which take turns")
    ap.add_argument("--rtl-power-device", metavar="N|SERIAL", default="",
                    help="which RTL-SDR rtl_power uses: index or serial (see rtl_test); default the first free one")
    ap.add_argument("--rtl-power-dwell", metavar="SECONDS", type=int, default=15,
                    help="seconds on each range when several --rtl-power ranges are given (default 15)")
    ap.add_argument("--dump1090", metavar="HOST:PORT", help="relay aircraft from a dump1090/readsb SBS port")
    ap.add_argument("--kismet-json", metavar="PATH", help="relay Wi-Fi devices from a Kismet JSON export")
    ap.add_argument("--fcc-pack", metavar="PATH", help="local FCC license pack from tools/fcc_local_pack.py; labels licensed carriers")
    ap.add_argument("--name", default="", help="advertised name (default Wavelens-XXXX from the adapter address)")
    a = ap.parse_args()

    global FCC
    if a.fcc_pack:
        FCC = FccPack(a.fcc_pack)
        CAPS.append("fcc")
        print(f"bridge: FCC pack loaded, {len(FCC.entries)} licensed frequencies", file=sys.stderr)

    if a.kismet_json:
        CAPS.append("wifi")
        threading.Thread(target=feed_kismet_json, args=(a.kismet_json,), daemon=True).start()
    if a.hackrf_sweep:
        CAPS.append("sweep")
        threading.Thread(target=feed_hackrf, args=(a.hackrf_range, a.hackrf_bin), daemon=True).start()
    if a.rtl_power:
        CAPS.append("sweep")
        threading.Thread(target=feed_rtl_power, args=(a.rtl_power, a.rtl_power_device, max(3, a.rtl_power_dwell)),
                         daemon=True).start()
    if a.dump1090:
        CAPS.append("adsb")
        threading.Thread(target=feed_dump1090, args=(a.dump1090,), daemon=True).start()
    if a.rtl433 or not sys.stdin.isatty():
        threading.Thread(target=feed_stdin, args=("rtl433" if a.rtl433 else "raw",), daemon=True).start()
    run_peripheral(a.name)


if __name__ == "__main__":
    main()
