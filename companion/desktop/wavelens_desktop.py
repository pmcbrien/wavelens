#!/usr/bin/env python3
"""Wavelens desktop client: talk to a companion radio from a Windows, Mac or Linux computer.

The companion (a Raspberry Pi bridge, an ESP32) is a Bluetooth LE peripheral that speaks one line
protocol: newline-delimited JSON on one characteristic, commands written to another, and — on an
SDR bridge — audio frames on a third. The Android app is one client; this is another. It prints
every hit as it arrives, takes commands from the keyboard, and plays Listen / Follow audio through
the computer's speakers. Receive only, like everything else here.

    pip install bleak sounddevice
    python wavelens_desktop.py                 # finds the nearest companion and connects
    python wavelens_desktop.py --name Wavelens-3F2A
    python wavelens_desktop.py --raw           # print the JSON lines as they come

Commands (type at the prompt):
    ping                      hello + status                 park le | park off
    listen 446.075 [mode]     mode: auto fm am wbfm dmr p25 nxdn dstar (default auto)
    follow                    scanner: tune every fresh voice-band hit
    stop                      stop listening / following     sq 30         squelch
    lockout 460.6 [...]       frequencies Follow never tunes (send the whole list)
    quit

Bluetooth on Windows: works with the built-in adapter through WinRT (no pairing in Settings needed —
the companion does not bond). If the companion isn't found, make sure the phone isn't connected to
it: a BLE peripheral takes one central at a time.
"""
import argparse
import asyncio
import json
import struct
import sys
import time

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("pip install bleak")

SVC = "57617665-6c65-6e73-0100-000000000001"
TX = "57617665-6c65-6e73-0100-000000000002"     # device → us, notify
RX = "57617665-6c65-6e73-0100-000000000003"     # us → device, write
AU = "57617665-6c65-6e73-0100-000000000004"     # device → us, audio frames

IMA_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]
IMA_STEP = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88,
            97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
            724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660,
            4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
            18500, 20350, 22385, 24623, 27086, 29794, 32767]


def ima_decode(frame: bytes) -> bytes:
    """One 164-byte frame: seq, predictor (int16 LE), step index, then 320 samples as 4-bit codes."""
    if len(frame) < 5:
        return b""
    _, pred, idx = struct.unpack("<BhB", frame[:4])
    idx = max(0, min(88, idx))
    out = []
    for b in frame[4:]:
        for code in (b >> 4, b & 15):
            step = IMA_STEP[idx]
            d = step >> 3
            if code & 4:
                d += step
            if code & 2:
                d += step >> 1
            if code & 1:
                d += step >> 2
            pred = pred - d if code & 8 else pred + d
            pred = max(-32768, min(32767, pred))
            idx = max(0, min(88, idx + IMA_INDEX[code & 7]))
            out.append(pred)
    return struct.pack(f"<{len(out)}h", *out)


class Audio:
    """8 kHz mono playback through the default output; silent if sounddevice isn't installed."""

    def __init__(self):
        self.stream = None
        try:
            import sounddevice as sd
            self.sd = sd
        except ImportError:
            self.sd = None
            print("(no sound: pip install sounddevice to hear Listen)")

    def feed(self, frame: bytes) -> None:
        if self.sd is None:
            return
        if self.stream is None:
            self.stream = self.sd.RawOutputStream(samplerate=8000, channels=1, dtype="int16", blocksize=320)
            self.stream.start()
        pcm = ima_decode(frame)
        if pcm:
            self.stream.write(pcm)


def describe(o: dict) -> str:
    t = o.get("t")
    if t == "rf":
        x = o.get("x") or {}
        f = x.get("exact_mhz", "").split(" ")[0] or (str(o.get("f")) if o.get("f") else "")
        return f"{time.strftime('%H:%M:%S')}  {f:>10} MHz  {o.get('r', ''):>4} dB  {o.get('src', ''):8} {o.get('n', '')}"
    if t == "status":
        extra = ""
        l = o.get("listen")
        if isinstance(l, dict):
            extra = f"  [listen {l.get('mhz')} {l.get('asked') or l.get('mode')}{' · ' + l['detected'] if l.get('detected') else ''}{' · follow' if l.get('follow') else ''}]"
        return f"{time.strftime('%H:%M:%S')}  status: {o.get('msg', '')}{extra}"
    if t == "hello":
        return f"hello from {o.get('name')} ({o.get('fw')}) caps={','.join(o.get('caps', []))}"
    if t in ("wifi", "ble"):
        return f"{time.strftime('%H:%M:%S')}  {t:5} {o.get('r', ''):>4} dBm  {o.get('n') or o.get('mac') or ''}"
    return json.dumps(o)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", default="", help="companion name, e.g. Wavelens-3F2A (default: the nearest one)")
    ap.add_argument("--address", default="", help="Bluetooth address instead of a name")
    ap.add_argument("--raw", action="store_true", help="print the JSON lines as they come")
    ap.add_argument("--timeout", type=float, default=10.0, help="seconds to scan")
    a = ap.parse_args()

    target = None
    if a.address:
        target = a.address
    else:
        print(f"scanning for a companion ({a.timeout:.0f} s)...")
        best = None
        for d, adv in (await BleakScanner.discover(timeout=a.timeout, return_adv=True)).values():
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            name = d.name or adv.local_name or ""
            if SVC in uuids or name.startswith("Wavelens"):
                if a.name and name != a.name:
                    continue
                if best is None or (adv.rssi or -200) > best[1]:
                    best = (d.address, adv.rssi or -200, name)
        if best is None:
            sys.exit("no companion found. Is it on, and is the phone disconnected from it?")
        target = best[0]
        print(f"found {best[2]} at {best[1]} dBm")

    audio = Audio()
    buf = bytearray()
    loop = asyncio.get_running_loop()

    def on_tx(_, data: bytearray) -> None:
        buf.extend(data)
        while b"\n" in buf:
            line, _, rest = bytes(buf).partition(b"\n")
            buf[:] = rest
            try:
                o = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            print(json.dumps(o) if a.raw else describe(o))

    def on_au(_, data: bytearray) -> None:
        audio.feed(bytes(data))

    async with BleakClient(target) as client:
        print("connected")
        await client.start_notify(TX, on_tx)
        try:
            await client.start_notify(AU, on_au)
        except Exception:
            print("(this companion has no audio characteristic: Listen needs an SDR bridge)")

        async def send(o: dict) -> None:
            data = (json.dumps(o, separators=(",", ":")) + "\n").encode()
            for i in range(0, len(data), 100):          # the bridge reassembles on newline
                await client.write_gatt_char(RX, data[i:i + 100], response=True)

        await send({"c": "ping"})
        print("type 'help' for commands")
        last = {"c": "listen", "v": "follow"}          # what 'sq' re-sends with the new squelch
        while True:
            line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
            if not line:
                continue
            w = line.split()
            c = w[0].lower()
            try:
                if c in ("quit", "exit", "q"):
                    break
                elif c == "help":
                    print(__doc__.split("Commands (type at the prompt):")[1].split("Bluetooth on Windows")[0])
                elif c == "ping":
                    await send({"c": "ping"})
                elif c == "listen" and len(w) >= 2:
                    last = {"c": "listen", "mhz": float(w[1]), "mode": w[2] if len(w) > 2 else "auto", "tol": 0.003}
                    await send(last)
                elif c == "follow":
                    last = {"c": "listen", "v": "follow"}
                    await send(last)
                elif c == "stop":
                    await send({"c": "listen", "v": "off"})
                elif c == "sq" and len(w) >= 2:
                    last = dict(last, sq=int(w[1]))
                    await send(last)
                elif c == "lockout":
                    await send({"c": "listen", "v": "lockout", "mhz": [float(v) for v in w[1:]]})
                elif c == "park" and len(w) >= 2:
                    await send({"c": "park", "v": w[1]})
                elif c == "blink":
                    await send({"c": "blink", "v": 5})
                else:
                    print("? (help)")
            except (ValueError, IndexError):
                print("? (help)")
        await send({"c": "listen", "v": "off"})


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
