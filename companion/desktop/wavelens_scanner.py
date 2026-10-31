#!/usr/bin/env python3
"""Wavelens Scanner for Windows / Mac / Linux: the app's Scanner tab as a desktop window.

Talks to a companion radio (a Raspberry Pi bridge or an ESP32) over Bluetooth LE — the same one-page
protocol the Android app uses — and shows what the phone shows: the frequency and talkgroup being heard,
the systems ranked nearby, the spectrum around the tuned channel, the recent calls and the companion's
own journal. SCAN / STOP, HOLD, SKIP, the POLICE / FIRE / EMS / OTHER filter and the hand-off time all
work. Listen audio plays through the computer's speakers. Receive only.

    pip install bleak sounddevice
    python wavelens_scanner.py                 # finds the nearest companion and connects
    python wavelens_scanner.py --name Wavelens-3F2A
    python wavelens_scanner.py --demo          # no radio: a scripted session, for a look at the window

Needs Python 3.9+ with tkinter (included with python.org's Windows and Mac installers; on Debian/Ubuntu
`apt install python3-tk`). One central at a time: disconnect the phone from the companion first.
Bluetooth on Windows uses the built-in adapter through WinRT; no pairing in Settings is needed.
"""
import argparse
import asyncio
import base64
import json
import queue
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from wavelens_desktop import SVC, TX, RX, AU, ima_decode  # noqa: E402  (the reference ADPCM decoder)

# the app's palette
INK, SLATE, LINE, AMBER, RED, GREEN, BLUE, TEXT, MUTED = "#0a0c11", "#141925", "#262d3b", "#f2b705", "#e53935", "#43a047", "#29b6f6", "#e8e6df", "#8a909b"
CAT_COLOR = {"police": BLUE, "fire": RED, "ems": GREEN, "other": AMBER}


# ---------------------------------------------------------------- audio: 8 kHz mono with a small jitter buffer
class Audio:
    def __init__(self):
        self.q = queue.Queue(maxsize=50)
        self.primed = False
        try:
            import sounddevice as sd
            self.stream = sd.RawOutputStream(samplerate=8000, channels=1, dtype="int16", blocksize=320, callback=self._cb)
            self.stream.start()
        except Exception as e:                 # no sounddevice / no output device: the window still works
            self.stream = None
            self.why = str(e)

    def _cb(self, out, frames, t, status):
        if not self.primed and self.q.qsize() < 4:
            out[:] = b"\0" * len(out); return  # prefill 160 ms before starting, like the phone
        self.primed = True
        try:
            out[:] = self.q.get_nowait()
        except queue.Empty:
            out[:] = b"\0" * len(out)
            self.primed = False

    def feed(self, frame: bytes) -> None:
        pcm = ima_decode(frame)
        if not pcm or self.stream is None:
            return
        if self.q.full():
            try: self.q.get_nowait()
            except queue.Empty: pass
        self.q.put_nowait(pcm)


# ---------------------------------------------------------------- Bluetooth: bleak on its own thread
class Link(threading.Thread):
    """Connects, feeds JSON objects and audio frames to the window through queues, takes commands back."""

    def __init__(self, name: str, address: str, inbox: queue.Queue, audio: Audio):
        super().__init__(daemon=True)
        self.name_want, self.address, self.inbox, self.audio = name, address, inbox, audio
        self.outbox: "asyncio.Queue | None" = None
        self.loop = None

    def send(self, obj: dict) -> None:
        if self.loop and self.outbox is not None:
            self.loop.call_soon_threadsafe(self.outbox.put_nowait, obj)

    def run(self):
        asyncio.run(self._main())

    async def _main(self):
        from bleak import BleakClient, BleakScanner
        self.loop = asyncio.get_running_loop()
        self.outbox = asyncio.Queue()
        while True:
            target, label = self.address, self.address
            if not target:
                self.inbox.put({"t": "_link", "s": "scanning for a companion…"})
                best = None
                for d, adv in (await BleakScanner.discover(timeout=8.0, return_adv=True)).values():
                    uuids = [u.lower() for u in (adv.service_uuids or [])]
                    nm = d.name or adv.local_name or ""
                    if (SVC in uuids or nm.startswith("Wavelens")) and (not self.name_want or nm == self.name_want):
                        if best is None or (adv.rssi or -200) > best[1]:
                            best = (d.address, adv.rssi or -200, nm)
                if best is None:
                    self.inbox.put({"t": "_link", "s": "no companion found — is it on, and is the phone disconnected from it? retrying…"})
                    await asyncio.sleep(5); continue
                target, label = best[0], f"{best[2]} ({best[1]} dBm)"
            buf = bytearray()

            def on_tx(_, data):
                buf.extend(data)
                while b"\n" in buf:
                    line, _, rest = bytes(buf).partition(b"\n"); buf[:] = rest
                    try: self.inbox.put(json.loads(line.decode("utf-8", "replace")))
                    except ValueError: pass

            try:
                async with BleakClient(target) as client:
                    self.inbox.put({"t": "_link", "s": f"connected to {label}"})
                    await client.start_notify(TX, on_tx)
                    try: await client.start_notify(AU, lambda _, d: self.audio.feed(bytes(d)))
                    except Exception: pass
                    await self._write(client, {"c": "ping"})
                    last_ping = time.time()
                    while client.is_connected:
                        try:
                            obj = await asyncio.wait_for(self.outbox.get(), timeout=1.0)
                            await self._write(client, obj)
                        except asyncio.TimeoutError:
                            pass
                        if time.time() - last_ping > 15:          # keepalive, renews follow / spec / log
                            await self._write(client, {"c": "ping"}); last_ping = time.time()
            except Exception as e:
                self.inbox.put({"t": "_link", "s": f"link lost ({e.__class__.__name__}); reconnecting…"})
                await asyncio.sleep(3)

    @staticmethod
    async def _write(client, obj):
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        for i in range(0, len(data), 100):
            await client.write_gatt_char(RX, data[i:i + 100], response=True)


# ---------------------------------------------------------------- demo: a scripted session, no radio
class Demo(threading.Thread):
    def __init__(self, inbox: queue.Queue):
        super().__init__(daemon=True); self.inbox = inbox

    def send(self, obj):                                      # the window's commands come here
        v = obj.get("v")
        if v == "cats": self.cats = obj.get("cats") or []
        if v == "hold": self.hold = obj.get("on")

    def run(self):
        import math, random
        self.cats, self.hold = ["police"], False
        systems = [{"mhz": 856.9625, "db": 38, "name": "Pinellas County (Countywide Simulcast)", "skip": False},
                   {"mhz": 857.9625, "db": 34, "name": "Statewide Law Enforcement Radio System (P25)", "skip": False},
                   {"mhz": 851.5875, "db": 31, "name": "Pinellas County (Central Site)", "skip": False},
                   {"mhz": 859.4688, "db": 31, "name": "unlisted P25 system (NAC 800)", "skip": False},
                   {"mhz": 855.3375, "db": 25, "name": "Duke Energy P25", "skip": True}]
        calls = [("TG 1400 · SPPD D1 S. (Law Dispatch)", 1400, "SPPD D1 S. (Law Dispatch)", "police"),
                 ("TG 512 · FD 1A (Fire Dispatch) · muted", 512, "FD 1A (Fire Dispatch)", "fire"),
                 ("TG 1402 · SPPD D2 N. (Law Dispatch)", 1402, "SPPD D2 N. (Law Dispatch)", "police"),
                 ("TG 2210 · PCSO Disp North (Law Dispatch)", 2210, "PCSO Disp North (Law Dispatch)", "police")]
        self.inbox.put({"t": "_link", "s": "demo: connected to Wavelens-DEMO (no radio)"})
        self.inbox.put({"t": "hello", "fw": "wavelens-bridge 0.1", "name": "Wavelens-DEMO", "code": "DEMO",
                        "caps": ["listen", "log", "rr", "sweep", "trunk"], "spec": [144, 869],
                        "hw": {"line": "2 RTL-SDR (00000978: listen, 00001090: sensors) · no HackRF", "missing": []}})
        base = {"mhz": 856.9625, "mode": "trunkq", "asked": "trunkq", "detected": "P25p1", "call": "", "sq": 30, "follow": True,
                "lockout": [], "system": "Pinellas County (Countywide Simulcast)", "hold": False, "tglock": 0, "tgavoid": [],
                "systems": systems, "cats": self.cats, "tgs": [], "idle": 5}
        for ln in ["bridge: hardware: 2 RTL-SDR (00000978: listen, 00001090: sensors) · no HackRF",
                   "bridge: control-channel hunt: 800 MHz (sweep, 3 passes): 18 steady — 856.9625 (+38 dB), 857.9625 (+34 dB), …",
                   "bridge: listen: 856.9625 MHz trunkq (squelch 30)", "dsd-fme: Sync: +P25p1 NAC/CC: 800; TSBK"]:
            self.inbox.put({"t": "log", "ts": int(time.time()), "l": ln})
        self.inbox.put({"t": "status", "msg": "trunked: locked onto the control channel 856.9625 MHz (P25p1, QPSK) — waiting for a call", "listen": dict(base)})
        tgs, i, t0 = [], 0, time.time()
        while True:
            t = time.time() - t0
            n = 200
            bins = []
            for k in range(n):
                f = 854.4625 + k * 5.0 / n
                v = -105 + 6 * random.random()
                for s in systems:
                    v = max(v, -105 + s["db"] + 12 - 40 * abs(f - s["mhz"]) * 10)
                v += 15 * math.exp(-((f - 856.9625) ** 2) / 0.00002)
                bins.append(max(0, min(255, int((v + 120) * 2))))
            self.inbox.put({"t": "spec", "lo": 854.4625, "hi": 859.4625, "b": base64.b64encode(bytes(bins)).decode()})
            if int(t) % 9 == 0 and int(t * 2) % 2 == 0:
                c = calls[i % len(calls)]; i += 1
                tgs.append({"ts": int(time.time()), "tg": c[1], "name": c[2], "enc": False, "cat": c[3]}); tgs = tgs[-8:]
                call = c[0] if (not self.cats or c[3] in self.cats or "muted" in c[0]) else c[0] + " · muted"
                self.inbox.put({"t": "status", "msg": f"trunked call: {call}", "listen": dict(base, call=call, tgs=tgs, cats=self.cats, hold=self.hold)})
                self.inbox.put({"t": "log", "ts": int(time.time()), "l": f"dsd-fme: Sync: +P25p1 NAC/CC: 800; LDU1  {c[0].split(' · ')[0]}"})
            elif int(t) % 9 == 5 and int(t * 2) % 2 == 0:
                self.inbox.put({"t": "status", "msg": "on the control channel 856.9625 MHz, waiting for a call", "listen": dict(base, tgs=tgs, cats=self.cats, hold=self.hold)})
            time.sleep(0.5)


# ---------------------------------------------------------------- the window
class Scanner(tk.Tk):
    def __init__(self, link, inbox: queue.Queue, demo: bool):
        super().__init__()
        self.link, self.inbox = link, inbox
        self.title("Wavelens Scanner")
        self.configure(bg=INK)
        self.geometry("1180x760")
        self.minsize(900, 600)
        self.spec = None                      # (lo, hi, [db...])
        self.cats, self.hold, self.idle, self.listen = ["police"], False, 5, {}
        self.scanning = False
        self._build()
        self.after(100, self._pump)
        if not demo:
            self.after(1500, lambda: self.link.send({"c": "spec", "on": True, "lo": 851, "hi": 869}))
            self.after(1600, lambda: self.link.send({"c": "log", "on": True}))
            self.after(60000, self._renew)

    # layout: left column = readout + spectrum + controls; right = systems, calls, journal
    def _build(self):
        st = ttk.Style(self); st.theme_use("clam")
        st.configure(".", background=INK, foreground=TEXT, fieldbackground=SLATE, bordercolor=LINE)
        st.configure("TButton", background=SLATE, foreground=TEXT, padding=(14, 8), relief="flat", font=("Segoe UI", 11, "bold"))
        st.map("TButton", background=[("active", LINE)])
        st.configure("Go.TButton", background=AMBER, foreground="#111")
        st.configure("Stop.TButton", background=RED, foreground="#fff")
        for k, c in CAT_COLOR.items():
            st.configure(f"{k}.TButton", background=c, foreground="#111" if k != "fire" else "#fff")
        st.configure("Treeview", background=SLATE, foreground=TEXT, fieldbackground=SLATE, rowheight=24, font=("Segoe UI", 10))
        st.configure("Treeview.Heading", background=INK, foreground=MUTED, font=("Segoe UI", 9))

        top = tk.Frame(self, bg=INK); top.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(top, text="Wavelens", bg=INK, fg=TEXT, font=("Segoe UI", 16, "bold")).pack(side="left")
        self.linkvar = tk.StringVar(value="connecting…")
        tk.Label(top, textvariable=self.linkvar, bg=INK, fg=MUTED, font=("Segoe UI", 10)).pack(side="left", padx=14)
        self.hwvar = tk.StringVar(value="")
        tk.Label(top, textvariable=self.hwvar, bg=INK, fg=MUTED, font=("Segoe UI", 9)).pack(side="right")

        body = tk.Frame(self, bg=INK); body.pack(fill="both", expand=True, padx=16, pady=12)
        left = tk.Frame(body, bg=INK); left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(body, bg=INK, width=400); right.pack(side="right", fill="both", padx=(16, 0)); right.pack_propagate(False)

        card = tk.Frame(left, bg=SLATE, highlightbackground=LINE, highlightthickness=1); card.pack(fill="x")
        self.freqvar = tk.StringVar(value="— . ————")
        f = tk.Frame(card, bg=SLATE); f.pack(anchor="w", padx=18, pady=(14, 0))
        tk.Label(f, textvariable=self.freqvar, bg=SLATE, fg=AMBER, font=("Consolas", 44, "bold")).pack(side="left")
        tk.Label(f, text=" MHz", bg=SLATE, fg=MUTED, font=("Segoe UI", 16)).pack(side="left", anchor="s", pady=(0, 10))
        self.callvar = tk.StringVar(value="not listening")
        tk.Label(card, textvariable=self.callvar, bg=SLATE, fg=TEXT, font=("Segoe UI", 15), anchor="w", wraplength=680, justify="left").pack(fill="x", padx=18)
        self.sysvar = tk.StringVar(value="")
        tk.Label(card, textvariable=self.sysvar, bg=SLATE, fg=BLUE, font=("Segoe UI", 12), anchor="w").pack(fill="x", padx=18)
        self.statvar = tk.StringVar(value="")
        tk.Label(card, textvariable=self.statvar, bg=SLATE, fg=MUTED, font=("Consolas", 10), anchor="w", wraplength=680, justify="left").pack(fill="x", padx=18, pady=(4, 14))

        self.canvas = tk.Canvas(left, bg="#0d1017", height=190, highlightthickness=1, highlightbackground=LINE)
        self.canvas.pack(fill="x", pady=(12, 0))
        self.canvas.bind("<Configure>", lambda e: self._draw_spec())
        self.canvas.bind("<Button-1>", self._tap_spec)
        tk.Label(left, text="±2.5 MHz around the tuned channel · click to listen there", bg=INK, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 8))

        row = tk.Frame(left, bg=INK); row.pack(fill="x", pady=(4, 6))
        self.scanbtn = ttk.Button(row, text="▶  SCAN", style="Go.TButton", command=self._scan); self.scanbtn.pack(side="left")
        self.holdbtn = ttk.Button(row, text="HOLD", command=self._hold); self.holdbtn.pack(side="left", padx=8)
        ttk.Button(row, text="SKIP ▶▶", command=lambda: self.link.send({"c": "listen", "v": "skip"})).pack(side="left")
        ttk.Button(row, text="Stop", command=self._stop).pack(side="left", padx=8)

        row2 = tk.Frame(left, bg=INK); row2.pack(fill="x", pady=(0, 6))
        self.catbtns = {}
        for k in ("police", "fire", "ems", "other"):
            b = ttk.Button(row2, text=k.upper(), command=lambda k=k: self._cat(k)); b.pack(side="left", padx=(0, 8)); self.catbtns[k] = b
        self.catvar = tk.StringVar(value="")
        tk.Label(row2, textvariable=self.catvar, bg=INK, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=6)

        row3 = tk.Frame(left, bg=INK); row3.pack(fill="x")
        self.idlevar = tk.StringVar(value="Hand off after 5 s of silence")
        tk.Label(row3, textvariable=self.idlevar, bg=INK, fg=TEXT, font=("Segoe UI", 10)).pack(side="left")
        ttk.Button(row3, text="−", width=3, command=lambda: self._idle(-1)).pack(side="left", padx=(10, 4))
        ttk.Button(row3, text="+", width=3, command=lambda: self._idle(+1)).pack(side="left")
        tk.Label(row3, text="HOLD overrides it", bg=INK, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=10)

        tk.Label(right, text="SYSTEMS NEARBY", bg=INK, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.systree = ttk.Treeview(right, columns=("db", "name"), show="headings", height=6, selectmode="browse")
        self.systree.heading("db", text="MHz · dB"); self.systree.heading("name", text="system")
        self.systree.column("db", width=110, anchor="w", stretch=False); self.systree.column("name", width=270, anchor="w")
        self.systree.pack(fill="x", pady=(4, 10)); self.systree.bind("<Double-1>", self._tune_system)
        tk.Label(right, text="RECENT CALLS", bg=INK, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.calltree = ttk.Treeview(right, columns=("t", "tg", "name"), show="headings", height=7)
        for c, w, h in (("t", 70, "time"), ("tg", 50, "TG"), ("name", 260, "talkgroup")):
            self.calltree.heading(c, text=h); self.calltree.column(c, width=w, anchor="w", stretch=(c == "name"))
        self.calltree.pack(fill="x", pady=(4, 10)); self.calltree.bind("<Double-1>", self._lock_tg)
        for k, c in CAT_COLOR.items(): self.calltree.tag_configure(k, foreground=c)
        tk.Label(right, text="COMPANION JOURNAL", bg=INK, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.journal = tk.Text(right, bg="#0d1017", fg=MUTED, font=("Consolas", 8), height=12, wrap="word", relief="flat", highlightthickness=1, highlightbackground=LINE)
        self.journal.pack(fill="both", expand=True, pady=(4, 0))
        self._cats_ui()

    # ---- commands to the companion
    def _scan(self):
        self.scanning = True
        self.link.send({"c": "listen", "v": "trunkauto", "on": True, "qpsk": False, "idle": self.idle})
        self.link.send({"c": "listen", "v": "follow", "sq": 30})
        self.scanbtn.configure(text="■  STOP", style="Stop.TButton", command=self._stop)
    def _stop(self):
        self.scanning = False
        self.link.send({"c": "listen", "v": "off"})
        self.scanbtn.configure(text="▶  SCAN", style="Go.TButton", command=self._scan)
    def _hold(self):
        self.hold = not self.hold
        self.link.send({"c": "listen", "v": "hold", "on": self.hold})
        self.holdbtn.configure(text="HOLD ●" if self.hold else "HOLD")
    def _cat(self, k):
        self.cats = [] if self.cats == [k] else [k]
        self.link.send({"c": "listen", "v": "cats", "cats": self.cats}); self._cats_ui()
    def _cats_ui(self):
        for k, b in self.catbtns.items():
            b.configure(style=f"{k}.TButton" if k in self.cats else "TButton")
        self.catvar.set("Hearing " + " + ".join(c.upper() for c in self.cats) + " · click again for all" if self.cats else "Hearing every talkgroup · pick a category to hear only that")
    def _idle(self, d):
        step = 5 if (self.idle >= 10 and d > 0) or (self.idle > 10 and d < 0) else 1
        self.idle = max(2, min(120, self.idle + d * step))
        self.idlevar.set(f"Hand off after {self.idle} s of silence")
        self.link.send({"c": "listen", "v": "trunkauto", "on": True, "qpsk": False, "idle": self.idle})
    def _tune_system(self, _):
        sel = self.systree.selection()
        if sel:
            self.link.send({"c": "listen", "v": "system", "mhz": float(self.systree.item(sel[0], "values")[0].split()[0])})
    def _lock_tg(self, _):
        sel = self.calltree.selection()
        if sel:
            self.link.send({"c": "listen", "v": "tglock", "tg": int(self.calltree.item(sel[0], "values")[1])})
    def _tap_spec(self, e):
        if self.spec:
            lo, hi, _ = self.spec
            mhz = lo + (hi - lo) * e.x / max(1, self.canvas.winfo_width())
            self.link.send({"c": "listen", "mhz": round(mhz, 4), "mode": "auto", "tol": 0.003})
    def _renew(self):
        self.link.send({"c": "spec", "on": True, "lo": 851, "hi": 869}); self.link.send({"c": "log", "on": True})
        if self.scanning: self.link.send({"c": "listen", "v": "follow", "sq": 30})
        self.after(60000, self._renew)

    # ---- frames from the companion
    def _pump(self):
        try:
            while True:
                self._handle(self.inbox.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _handle(self, o: dict):
        t = o.get("t")
        if t == "_link":
            self.linkvar.set(o["s"])
        elif t == "hello":
            hw = o.get("hw") or {}
            self.hwvar.set(hw.get("line", "")); self.linkvar.set(f"{o.get('name')} · {o.get('fw')}")
            if hw.get("missing"): self.hwvar.set(hw["line"])
        elif t == "status":
            l = o.get("listen")
            self.statvar.set(o.get("msg", ""))
            if isinstance(l, dict):
                self.listen = l
                self.freqvar.set(f"{l['mhz']:.4f}" if l.get("mhz") else "— . ————")
                self.callvar.set(l.get("call") or ("control channel · waiting for a call" if l.get("detected") else ("listening" if l.get("mhz") else "not listening")))
                self.sysvar.set(l.get("system") or "")
                self.hold = bool(l.get("hold")); self.holdbtn.configure(text="HOLD ●" if self.hold else "HOLD")
                if l.get("idle"): self.idle = int(l["idle"]); self.idlevar.set(f"Hand off after {self.idle} s of silence")
                self.systree.delete(*self.systree.get_children())
                for s in l.get("systems") or []:
                    self.systree.insert("", "end", values=(f"{s['mhz']:.4f}  +{s['db']}", (s.get("name") or "unnamed") + ("  (skipped)" if s.get("skip") else "")))
                self.calltree.delete(*self.calltree.get_children())
                for c in reversed(l.get("tgs") or []):
                    self.calltree.insert("", "end", values=(time.strftime("%H:%M:%S", time.localtime(c["ts"])), c["tg"], (c.get("name") or "") + (" · encrypted" if c.get("enc") else "")), tags=(c.get("cat") or "other",))
        elif t == "spec":
            try:
                bins = base64.b64decode(o["b"])
                self.spec = (float(o["lo"]), float(o["hi"]), [b / 2.0 - 120 for b in bins]); self._draw_spec()
            except Exception:
                pass
        elif t == "log":
            self.journal.insert("end", time.strftime("%H:%M:%S ", time.localtime(o.get("ts", time.time()))) + o.get("l", "") + "\n")
            if int(self.journal.index("end-1c").split(".")[0]) > 300: self.journal.delete("1.0", "2.0")
            self.journal.see("end")

    def _draw_spec(self):
        c = self.canvas; c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if not self.spec or w < 10:
            c.create_text(w / 2, h / 2, text="spectrum arrives once the companion is sweeping", fill=MUTED, font=("Segoe UI", 10)); return
        lo, hi, db = self.spec
        top, bot = max(db) + 3, min(db) - 3
        pts = []
        for i, v in enumerate(db):
            pts += [i * w / max(1, len(db) - 1), h - (v - bot) / max(1e-6, top - bot) * (h - 20) - 10]
        c.create_polygon(*([0, h] + pts + [w, h]), fill="#3a2d05", outline="")
        c.create_line(*pts, fill=AMBER, width=1.5)
        for k in range(5):
            x = k * w / 4
            c.create_line(x, 0, x, h, fill=LINE); c.create_text(x + 3, h - 2, text=f"{lo + (hi - lo) * k / 4:.3f}", fill=MUTED, anchor="sw", font=("Consolas", 8))
        mhz = (self.listen or {}).get("mhz")
        if mhz and lo <= mhz <= hi:
            x = (mhz - lo) / (hi - lo) * w; c.create_line(x, 0, x, h, fill=BLUE, width=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", default=""); ap.add_argument("--address", default="")
    ap.add_argument("--demo", action="store_true", help="no radio: a scripted session")
    ap.add_argument("--screenshot", default="", help=argparse.SUPPRESS)
    a = ap.parse_args()
    inbox = queue.Queue()
    audio = Audio()
    link = Demo(inbox) if a.demo else Link(a.name, a.address, inbox, audio)
    link.start()
    win = Scanner(link, inbox, a.demo)
    if a.screenshot:
        def shot():
            win.update()
            try:
                import subprocess; subprocess.run(["import", "-window", "root", a.screenshot]); win.destroy()
            except Exception: win.destroy()
        win.after(6000, shot)
    win.mainloop()


if __name__ == "__main__":
    main()
