#!/usr/bin/env python3
"""
Wavelens companion bridge — Raspberry Pi / carputer / any Linux box with BlueZ.

Turns the machine into the BLE peripheral the Wavelens app looks for, and relays
whatever the radios attached to it produce. Feeds can be combined; add your own by
writing Wavelens lines (see ../README.md) to stdin.

  rtl_433 -F json | ./wavelens_bridge.py --rtl433        # RTL-SDR: 433/315/915 MHz decoders (TPMS, remotes, sensors)
  ./wavelens_bridge.py --hackrf-sweep                    # HackRF: 1 MHz–6 GHz energy sweep → carriers by band
  ./wavelens_bridge.py --rtl-power 300M:1000M:50k        # RTL-SDR: the same idea below 1.7 GHz
  ./wavelens_bridge.py --dump1090 localhost:30003        # ADS-B aircraft from dump1090 / readsb (SBS port)
  ./wavelens_bridge.py --kismet-json /tmp/kismet.json    # Kismet export from a monitor-mode Wi-Fi adapter
  ./wavelens_bridge.py                                   # raw: stdin already in Wavelens line format

Several at once:  rtl_433 -F json | ./wavelens_bridge.py --rtl433 --hackrf-sweep --dump1090 localhost:30003

Needs: python3-bluezero (pip install bluezero), BlueZ 5.50+, the adapter powered, and
whichever of rtl_433 / hackrf_sweep / rtl_power / dump1090 you use on PATH. Receive only:
nothing here ever transmits. Status: written against the documented APIs and CSV formats;
not yet run on a Pi by the author.
"""

import argparse
import json
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
state = {"linked": False, "band": "all", "mode": "rf", "ch": "hop"}


def emit(obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    try:
        out.put_nowait(line)
    except Exception:
        pass  # full: drop, the next packet will come


CAPS = ["rf"]


def hello() -> None:
    emit({"t": "hello", "fw": FW, "name": "Wavelens Pi", "caps": sorted(set(CAPS))})


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
            name, cat, conf = band_of(mhz)
            if name is None:
                continue
            bucket = round(mhz / self.bucket)
            if now - self.last.get(bucket, 0) < self.rate:
                continue
            self.last[bucket] = now
            emit({"t": "rf", "id": f"{self.src}/{bucket * self.bucket:.0f}", "n": name, "f": int(round(mhz)),
                  "r": int(round(db)), "cat": cat, "conf": conf, "src": self.src,
                  "x": {"over_floor_db": f"{db - f:.0f}", "bin_hz": f"{step:.0f}"}})


def feed_process(cmd: str, handler) -> None:
    """Run a sweep tool and hand it each stdout line; restart it if it exits."""
    while True:
        try:
            p = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except FileNotFoundError:
            print(f"bridge: {cmd.split()[0]} not found on PATH", file=sys.stderr)
            return
        for line in p.stdout:
            handler(line)
        time.sleep(2)


def feed_hackrf(range_mhz: str, bin_hz: int) -> None:
    sw = Sweep("hackrf")
    feed_process(f"hackrf_sweep -f {range_mhz} -w {bin_hz} -l 32 -g 20", sw.row)


def feed_rtl_power(spec: str) -> None:
    sw = Sweep("rtlpower")
    feed_process(f"rtl_power -f {spec} -i 1 -g 30 -", sw.row)


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
    freq = d.get("freq") or d.get("freq1")
    try:
        freq = int(float(freq)) if freq else 0
    except (TypeError, ValueError):
        freq = 0
    rssi = d.get("rssi")
    try:
        rssi = int(float(rssi))
    except (TypeError, ValueError):
        rssi = -70
    x = {k: str(v) for k, v in d.items() if k in ("pressure_kPa", "pressure_PSI", "temperature_C", "battery_ok", "mic", "type")}
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


# ---------------------------------------------------------------- BLE peripheral

def run_peripheral(name: str) -> None:
    from bluezero import adapter, peripheral

    dongle = list(adapter.Adapter.available())[0]
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
    print(f"{FW}: advertising as {name!r} on {dongle.address}", file=sys.stderr)
    dev.publish()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rtl433", action="store_true", help="stdin is rtl_433 -F json")
    ap.add_argument("--hackrf-sweep", action="store_true", help="run hackrf_sweep and report carriers by band")
    ap.add_argument("--hackrf-range", default="300:6000", help="MHz range for hackrf_sweep (default 300:6000)")
    ap.add_argument("--hackrf-bin", type=int, default=500000, help="bin width Hz (default 500000)")
    ap.add_argument("--rtl-power", metavar="SPEC", help="run rtl_power with this -f spec, e.g. 300M:1000M:50k")
    ap.add_argument("--dump1090", metavar="HOST:PORT", help="relay aircraft from a dump1090/readsb SBS port")
    ap.add_argument("--kismet-json", metavar="PATH", help="relay Wi-Fi devices from a Kismet JSON export")
    ap.add_argument("--name", default="Wavelens Pi")
    a = ap.parse_args()

    if a.kismet_json:
        CAPS.append("wifi")
        threading.Thread(target=feed_kismet_json, args=(a.kismet_json,), daemon=True).start()
    if a.hackrf_sweep:
        CAPS.append("sweep")
        threading.Thread(target=feed_hackrf, args=(a.hackrf_range, a.hackrf_bin), daemon=True).start()
    if a.rtl_power:
        CAPS.append("sweep")
        threading.Thread(target=feed_rtl_power, args=(a.rtl_power,), daemon=True).start()
    if a.dump1090:
        CAPS.append("adsb")
        threading.Thread(target=feed_dump1090, args=(a.dump1090,), daemon=True).start()
    if a.rtl433 or not sys.stdin.isatty():
        threading.Thread(target=feed_stdin, args=("rtl433" if a.rtl433 else "raw",), daemon=True).start()
    run_peripheral(a.name)


if __name__ == "__main__":
    main()
