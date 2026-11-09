#!/usr/bin/env python3
"""Wavelens Scanner — companion admin web UI for the Raspberry Pi.

A small, dependency-free (Python 3.11 stdlib only) web interface that customers open on their
phone over the home Wi-Fi to look after the Pi that runs wavelens-bridge:

    http://wavelens.local:8080      (or http://<ip>:8080)

Pages:  /          status, Pi facts, update banner, quick actions
        /logs      bridge journal (last 300 lines, live polling, download)
        /settings  /etc/default/wavelens-bridge editor (comments preserved)
        /update    check / install / roll back via /usr/local/bin/wavelens-update
        /wifi      join a network, forget saved ones, rename the Pi
        /spotter   flash a Wavelens Spotter (ESP32) over USB with esptool
        /power     restart / reboot / shut down (each behind a confirm page)
        /account   change the admin password, log out

Security model: LAN only, plain HTTP, one admin password (PBKDF2-HMAC-SHA256, 200k rounds) kept
in /etc/wavelens/admin.json, an HttpOnly SameSite=Strict session cookie, CSRF tokens on every
form, and login rate limiting. Nothing here is reachable from the internet unless you forward a
port to it — don't. The Pi only ever talks out to GitHub, and only for updates you ask for.

Recovery: `sudo wavelens-admin --reset-password` (or delete /etc/wavelens/admin.json on the SD
card) and the next visit asks for a new password.

Runs as root under systemd (wavelens-admin.service): it edits system files and restarts services.
Every external command is run with a list argv (never a shell) and a timeout.
"""

import base64
import collections
import email.parser
import email.policy
import glob
import hashlib
import hmac
import html
import http.cookies
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------------------------
# Paths and constants (match install-service.sh / wavelens-bridge.service)
# --------------------------------------------------------------------------------------------
LIB_DIR = "/usr/local/lib/wavelens"
VERSION_FILE = os.path.join(LIB_DIR, "VERSION")
FW_DIR = os.path.join(LIB_DIR, "firmware")
FW_BIN = os.path.join(FW_DIR, "wavelens-esp32.bin")
FW_VERSION = os.path.join(FW_DIR, "VERSION")
FW_URL = "https://github.com/pmcbrien/wavelens/releases/download/firmware/wavelens-esp32.bin"
# one app image per chip; FW_BIN (classic ESP32) is the one every release has
FW_CHIPS = ("esp32", "esp32s3", "esp32c3", "esp32c5", "esp32c6")


def fw_bin_for(chip: str) -> str:
    return os.path.join(FW_DIR, f"wavelens-{chip}.bin")
FW_VERSION_URL = "https://github.com/pmcbrien/wavelens/releases/download/firmware/VERSION"
# An uploaded .bin waits here for the flash. Not /tmp: a fixed name there let any local user plant the file
# (or a symlink) that root then flashed or overwrote. A root-only directory on tmpfs; gone after a reboot.
UPLOAD_DIR = "/run/wavelens/admin-upload"
UPLOAD_BIN = os.path.join(UPLOAD_DIR, "upload.bin")
DEFAULTS_FILE = "/etc/default/wavelens-bridge"
ADMIN_JSON = "/etc/wavelens/admin.json"
STATUS_JSON = "/run/wavelens/status.json"
SPOTTER_ROLES = "/etc/wavelens/spotters.json"   # owner-chosen jobs for Spotters on USB, by port; the bridge re-reads it
SPOTTER_JOBS = [("auto", "Automatic"), ("ble", "Bluetooth only"), ("wifi:1,6,11", "Wi-Fi ch 1, 6, 11"), ("wifi:1", "Wi-Fi ch 1"),
                ("wifi:6", "Wi-Fi ch 6"), ("wifi:11", "Wi-Fi ch 11"), ("wifi:hop", "Wi-Fi, all channels"), ("both", "Wi-Fi + Bluetooth")]
UPDATE_JSON = "/var/lib/wavelens/update.json"
UPDATE_CLI = "/usr/local/bin/wavelens-update"
UPDATE_LOG = "/var/lib/wavelens/update.log"     # written by wavelens-update apply/rollback; survives this page restarting
UPDATE_RUN_UNIT = "wavelens-update-run"          # the transient unit wavelens-update runs apply/rollback in
PREV_DIR = "/usr/local/lib/wavelens.prev"        # the saved working version a rollback puts back
BRIDGE_UNIT = "wavelens-bridge"
ADMIN_UNIT = "wavelens-admin"
PORT = int(os.environ.get("WAVELENS_ADMIN_PORT", "8080"))

SESSION_TTL = 12 * 3600          # seconds
LOGIN_MAX_FAILS = 5              # per IP ...
LOGIN_WINDOW = 10 * 60           # ... per 10 minutes
MAX_FORM_BODY = 64 * 1024        # ordinary POST bodies
MAX_UPLOAD_BODY = 4 * 1024 * 1024 + 64 * 1024   # 4 MB .bin plus multipart overhead
NOISY = ('"t":"rf"', '"t":"spec"')   # high-volume journal lines hidden by default
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.I)
PBKDF2_ROUNDS = 200_000


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------
def esc(s):
    """HTML-escape anything (None becomes empty)."""
    return html.escape("" if s is None else str(s), quote=True)


def run(argv, timeout=20, stdin_text=None):
    """Run a command (list argv, no shell). Returns (returncode, combined output). Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           input=stdin_text, errors="replace")
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{argv[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 1, f"{argv[0]}: {e}"


def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def fmt_ts(ts):
    """Unix timestamp -> local 'YYYY-MM-DD HH:MM', or 'never'."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if ts else "never"
    except (TypeError, ValueError):
        return "never"


def fmt_dur(seconds):
    seconds = int(max(0, seconds))
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return (f"{d}d " if d else "") + f"{h}h {m:02d}m"


def hostname():
    return read_text("/etc/hostname").strip() or run(["hostname"])[1] or "wavelens"


LINK_STALE = 120   # s; the bridge rewrites status.json with every hello (every 15 s while a phone is linked)


def status_linked(st):
    """A phone is linked per status.json only if the bridge said so recently: a "linked" left behind by a
    bridge that stopped or crashed would otherwise show (and hold off auto-updates) forever."""
    try:
        return bool(st.get("linked")) and time.time() - float(st.get("at") or 0) < LINK_STALE
    except (TypeError, ValueError):
        return False


def host_ok(host_header):
    """Is the request's Host this Pi (its name, name.local, one of its IPs, or localhost)? Guards the first-run
    form against DNS rebinding: a web page on some other name that resolves to the Pi gets refused."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):                       # [IPv6]:port
        h = h[1:h.find("]")] if "]" in h else ""
    elif h.count(":") == 1:                     # name:port / IPv4:port
        h = h.split(":", 1)[0]
    h = h.rstrip(".")
    if not h:
        return False
    name = hostname().lower()
    allowed = {"localhost", "127.0.0.1", "::1", name, name + ".local"}
    rc, ips = run(["hostname", "-I"], timeout=5)
    if rc == 0:
        allowed.update(ip.lower() for ip in ips.split())
    # Avahi renames the Pi to name-2.local (and so on) when another device on the network already has name.local.
    return h in allowed or re.fullmatch(re.escape(name) + r"-\d{1,3}\.local", h) is not None


def installed_version():
    return read_text(VERSION_FILE).strip() or "unknown"


# --------------------------------------------------------------------------------------------
# Admin password store and sessions
# --------------------------------------------------------------------------------------------
def pw_hash(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex),
                               PBKDF2_ROUNDS).hex()


def admin_exists():
    return os.path.isfile(ADMIN_JSON)


def admin_set_password(password):
    """Write /etc/wavelens/admin.json (mode 0600) atomically."""
    salt = secrets.token_hex(16)
    data = {"salt": salt, "hash": pw_hash(password, salt), "rounds": PBKDF2_ROUNDS}
    os.makedirs(os.path.dirname(ADMIN_JSON), mode=0o700, exist_ok=True)
    tmp = ADMIN_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, ADMIN_JSON)


def admin_check_password(password):
    data = read_json(ADMIN_JSON) or {}
    try:
        return hmac.compare_digest(pw_hash(password, data["salt"]), data["hash"])
    except (KeyError, ValueError, TypeError):
        return False


class Sessions:
    """In-memory session table: token -> {expires, csrf, flash}. Lost on restart (fine: log in again)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.table = {}
        self.fails = collections.defaultdict(list)   # ip -> [timestamps of failed logins]
        self.setup = {}   # first-run setup form: cookie token -> {expires, csrf}; never a login session

    def new(self):
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.table[tok] = {"expires": time.time() + SESSION_TTL,
                               "csrf": secrets.token_urlsafe(24), "flash": None}
        return tok

    def get(self, tok):
        if not tok:
            return None
        with self.lock:
            s = self.table.get(tok)
            if s and s["expires"] < time.time():
                del self.table[tok]
                return None
            return s

    def new_setup(self):
        """A short-lived token pair for the first-run password form (cookie + hidden field)."""
        tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.setup = {k: v for k, v in self.setup.items() if v["expires"] > now}
            if len(self.setup) >= 256:          # someone is reloading in a loop: start over rather than grow
                self.setup.clear()
            self.setup[tok] = {"expires": now + 3600, "csrf": csrf}
        return tok, csrf

    def get_setup(self, tok):
        if not tok:
            return None
        with self.lock:
            s = self.setup.get(tok)
            return s if s and s["expires"] > time.time() else None

    def drop(self, tok):
        with self.lock:
            self.table.pop(tok, None)

    def drop_all(self):
        with self.lock:
            self.table.clear()

    def login_begin(self, ip):
        """Check the rate limit and count this attempt as a failure in one step, under the lock: checking first
        and recording after the (slow) password check let many parallel guesses all pass the check. Returns the
        attempt's timestamp, for login_ok() when the password is right, or None when the IP is blocked."""
        now = time.time()
        with self.lock:
            recent = [t for t in self.fails[ip] if now - t < LOGIN_WINDOW]
            if len(recent) >= LOGIN_MAX_FAILS:
                self.fails[ip] = recent
                return None
            recent.append(now)
            self.fails[ip] = recent
            return now

    def login_ok(self, ip, stamp):
        """The password was right: take back the failure login_begin() counted."""
        with self.lock:
            try:
                self.fails[ip].remove(stamp)
            except ValueError:
                pass


SESSIONS = Sessions()


# --------------------------------------------------------------------------------------------
# /etc/default/wavelens-bridge: safe KEY="value" rewriting that keeps every comment
# --------------------------------------------------------------------------------------------
KEY_RE = re.compile(r'^\s*(export\s+)?([A-Z_][A-Z0-9_]*)=(.*)$')


def defaults_read():
    """Return {KEY: value} for every KEY=... line (quotes removed, shell-style)."""
    out = {}
    for line in read_text(DEFAULTS_FILE).splitlines():
        m = KEY_RE.match(line)
        if not m or line.lstrip().startswith("#"):
            continue
        raw = m.group(3).strip()
        try:
            parts = shlex.split(raw, comments=True)
            out[m.group(2)] = " ".join(parts)
        except ValueError:
            out[m.group(2)] = raw.strip('"')
    return out


# Anything str.splitlines() (and so this file's own reader) or systemd would treat as the end of a line, plus
# NUL. Inside the value it would end KEY="… early and start a new line — e.g. a second WAVELENS_AUTO_UPDATE=1.
LINE_BREAKS_RE = re.compile("[\x00\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


def sh_quote(value):
    """Double-quote a value for the EnvironmentFile (systemd reads it, run-bridge.sh word-splits it). Every value
    here is one shell line, so line breaks become spaces; then \\ " $ ` are escaped. Always exactly one line."""
    value = LINE_BREAKS_RE.sub(" ", str(value))
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def defaults_write(updates):
    """Replace only the KEY= lines named in `updates`; comments and order untouched; append if missing."""
    text = read_text(DEFAULTS_FILE)
    lines = text.splitlines()
    seen = set()
    for i, line in enumerate(lines):
        m = KEY_RE.match(line)
        if m and not line.lstrip().startswith("#") and m.group(2) in updates:
            key = m.group(2)
            if key in seen:            # a duplicate definition: comment it out rather than keep two
                lines[i] = "# " + line
                continue
            lines[i] = f"{key}={sh_quote(updates[key])}"
            seen.add(key)
    for key, val in updates.items():
        if key not in seen:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"{key}={sh_quote(val)}")
    tmp = DEFAULTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, DEFAULTS_FILE)


def args_get_name(args):
    """The X in '--name X' (or --name=X) inside WAVELENS_ARGS, or ''."""
    try:
        parts = shlex.split(args)
    except ValueError:
        return ""
    for i, p in enumerate(parts):
        if p == "--name" and i + 1 < len(parts):
            return parts[i + 1]
        if p.startswith("--name="):
            return p[7:]
    return ""


def args_set_name(args, name):
    """Return WAVELENS_ARGS with any --name removed and '--name <name>' appended (if name given)."""
    try:
        parts = shlex.split(args)
    except ValueError:
        parts = args.split()
    out, skip = [], False
    for p in parts:
        if skip:
            skip = False
            continue
        if p == "--name":
            skip = True
            continue
        if p.startswith("--name="):
            continue
        out.append(p)
    if name:
        out += ["--name", name]
    return shlex.join(out)


def defaults_help():
    """Comment lines from the shipped example, for the 'what these mean' block."""
    for path in (DEFAULTS_FILE, os.path.join(LIB_DIR, "wavelens-bridge.default"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "wavelens-bridge.default")):
        txt = read_text(path)
        if txt:
            return "\n".join(l[1:].strip() for l in txt.splitlines() if l.startswith("#"))
    return ""


# --------------------------------------------------------------------------------------------
# Background jobs (update CLI, esptool) with a live ring-buffer log
# --------------------------------------------------------------------------------------------
class Job:
    """Runs one command at a time in a thread; stdout+stderr lines go to a ring buffer."""

    def __init__(self, name):
        self.name = name
        self.lock = threading.Lock()
        self.lines = collections.deque(maxlen=2000)
        self.proc = None
        self.running = False
        self.title = ""
        self.exit = None
        self.seq = 0   # bumps on every start so pollers can tell a new run from the old

    def start(self, title, argv, timeout=1800, on_kill=None):
        """on_kill: called after the job had to be killed for running too long (e.g. undo what its own
        cleanup would have done)."""
        with self.lock:
            if self.running:
                return False
            self.running, self.title, self.exit = True, title, None
            self.seq += 1
            self.lines.clear()
            self.lines.append(f"$ {shlex.join(argv)}")
        threading.Thread(target=self._run, args=(argv, timeout, on_kill), daemon=True).start()
        return True

    def _run(self, argv, timeout, on_kill=None):
        timer, killed = None, threading.Event()
        try:
            # its own process group, so the kill reaches what a /bin/sh wrapper started (esptool) too, and
            # the stdout pipe they hold closes
            self.proc = proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                text=True, errors="replace", bufsize=1, start_new_session=True)

            def kill():
                # on a timer, not on the next output line: a job that hangs silently is killed on time too
                killed.set()
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass

            timer = threading.Timer(timeout, kill)
            timer.daemon = True
            timer.start()
            for line in proc.stdout:
                self.lines.append(line.rstrip("\n"))
            rc = proc.wait()
            if killed.is_set():
                self.lines.append(f"killed: ran longer than {timeout}s")
                if on_kill:
                    try:
                        on_kill()
                    except Exception as e:  # noqa: BLE001
                        self.lines.append(f"cleanup after the kill failed: {e}")
        except FileNotFoundError:
            self.lines.append(f"{argv[0]}: not found")
            rc = 127
        except Exception as e:  # noqa: BLE001
            self.lines.append(f"error: {e}")
            rc = 1
        finally:
            if timer:
                timer.cancel()
        self.lines.append(f"done (exit 0)" if rc == 0 else f"failed (exit {rc})")
        with self.lock:
            self.exit, self.running = rc, False

    def snapshot(self):
        with self.lock:
            return {"running": self.running, "title": self.title, "exit": self.exit,
                    "seq": self.seq, "lines": list(self.lines)}


UPDATE_JOB = Job("update")
FLASH_JOB = Job("flash")
DOWNLOAD_LOCK = threading.Lock()


# --------------------------------------------------------------------------------------------
# System facts
# --------------------------------------------------------------------------------------------
def unit_state(unit):
    """('active'|'inactive'|'failed'|..., uptime string)."""
    _, state = run(["systemctl", "is-active", unit], timeout=5)
    _, ts = run(["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "--value", unit], timeout=5)
    up = ""
    try:
        usec = int(ts)
        if usec and state == "active":
            with open("/proc/uptime") as f:
                boot_s = float(f.read().split()[0])
            up = fmt_dur(boot_s - usec / 1e6)
    except (ValueError, OSError):
        pass
    return state or "unknown", up


def wifi_ssid():
    _, out = run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"], timeout=10)
    for line in out.splitlines():
        if line.startswith("yes:"):
            return line[4:]
    return ""


def pi_facts():
    f = {"hostname": hostname(), "ips": run(["hostname", "-I"], timeout=5)[1].split(),
         "ssid": wifi_ssid()}
    try:
        f["temp"] = f"{int(read_text('/sys/class/thermal/thermal_zone0/temp').strip()) / 1000:.1f} °C"
    except ValueError:
        f["temp"] = "n/a"
    try:
        du = shutil.disk_usage("/")
        f["disk"] = f"{du.free / 1e9:.1f} GB free of {du.total / 1e9:.1f} GB"
    except OSError:
        f["disk"] = "n/a"
    try:
        f["load"] = " ".join(f"{x:.2f}" for x in os.getloadavg())
        f["uptime"] = fmt_dur(float(read_text("/proc/uptime", "0").split()[0]))
    except (OSError, ValueError, IndexError):
        f["load"], f["uptime"] = "n/a", "n/a"
    return f


def journal_lines(n=300, noise=False):
    _, out = run(["journalctl", "-u", BRIDGE_UNIT, "--no-pager", "-n", str(n), "-o", "short-iso"], timeout=15)
    lines = out.splitlines()
    if not noise:
        lines = [l for l in lines if not any(k in l for k in NOISY)]
    return lines


# --------------------------------------------------------------------------------------------
# Is the bridge actually working? Plain-language diagnosis for people who will never open a terminal.
# --------------------------------------------------------------------------------------------
# (pattern in the bridge's journal, what it means, what to do) — first match wins, most specific first.
KNOWN_FAULTS = [
    (re.compile(r"unsupported operand type\(s\) for \||SyntaxError|ModuleNotFoundError|ImportError"),
     "This version of the scanner software doesn't run on this Pi.",
     "rollback"),
    (re.compile(r"No such file or directory: '/usr/local/lib/wavelens|run-bridge.sh: not found|can't open file"),
     "Some of the scanner software's files are missing — usually an update that was interrupted.",
     "reinstall"),
    (re.compile(r"usb_claim_interface|Failed to open rtlsdr|No supported devices found|usb_open error"),
     "A radio dongle isn't answering. It may have come loose, or another program is using it.",
     "replug"),
    (re.compile(r"org\.bluez|No Bluetooth adapter|Bluetooth.*(not available|failed)|hci0.*(down|not)", re.I),
     "The Pi's Bluetooth isn't working, so the phone can't connect.",
     "reboot"),
    (re.compile(r"Traceback \(most recent call last\)|Error:"),
     "The scanner software stopped with an error.",
     "rollback"),
]
FIXES = {
    "rollback": "Tap <b>Put the previous version back</b>. If there's no previous version, tap <b>Install the latest update</b> — "
                "fixes for problems like this are usually already out.",
    "reinstall": "Tap <b>Install the latest update</b>: it reinstalls every file and checks the result.",
    "replug": "Unplug each USB radio, wait five seconds, plug it back in. The scanner picks it up by itself within ten seconds.",
    "reboot": "Tap <b>Reboot</b>. If it comes back the same, power the Pi off for ten seconds.",
}


def unit_show(unit, *props):
    _, out = run(["systemctl", "show", unit, "--no-pager"] + [f"-p{p}" for p in props], timeout=5)
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        d[k] = v
    return d


def bridge_health():
    """{'ok': bool, 'looping': bool, 'restarts_5m': int, 'title', 'fix', 'detail'} — the Status page's verdict."""
    props = unit_show(BRIDGE_UNIT, "ActiveState", "SubState", "Result")
    _, recent = run(["journalctl", "-u", BRIDGE_UNIT, "--since", "-5min", "--no-pager", "-o", "cat"], timeout=15)
    lines = recent.splitlines()
    restarts = sum(1 for l in lines if "Scheduled restart job" in l or "Main process exited" in l)
    active = props.get("ActiveState") == "active"
    looping = restarts >= 3
    h = {"ok": active and not looping, "looping": looping, "restarts_5m": restarts,
         "state": props.get("ActiveState") or "unknown", "title": "", "fix": "", "detail": ""}
    if h["ok"]:
        return h
    text = "\n".join(lines[-400:])
    for rx, title, fix in KNOWN_FAULTS:
        m = rx.search(text)
        if m:
            h["title"], h["fix"] = title, FIXES[fix]
            # the line that matched, without the journal's own prefix
            for l in reversed(lines):
                if rx.search(l):
                    h["detail"] = l.strip()[:240]
                    break
            break
    if not h["title"]:
        h["title"] = ("The scanner software keeps stopping and restarting." if looping
                      else "The scanner software isn't running.")
        h["fix"] = "Tap <b>Restart the scanner</b>. If it stops again, tap <b>Put the previous version back</b>."
    return h


def update_running():
    _, st = run(["systemctl", "is-active", UPDATE_RUN_UNIT], timeout=5)
    return st in ("active", "activating")


def update_log_snapshot():
    """The live job if this page started one and is still here; otherwise the updater's own log file, which
    carries on even when the update restarts this page part-way through."""
    snap = UPDATE_JOB.snapshot()
    if snap["running"] and snap["lines"]:
        return snap
    try:
        with open(UPDATE_LOG, errors="replace") as f:
            lines = f.read().splitlines()[-2000:]
    except OSError:
        return snap
    if not lines:
        return snap
    running = update_running()
    result = (read_json(UPDATE_JSON) or {}).get("last_result") or ""
    failed = any(w in result for w in ("failed", "did not start", "would not start", "could not", "not running"))
    return {"running": running, "title": "update", "seq": snap["seq"],
            "exit": None if running else (1 if failed else 0), "lines": lines}


# --------------------------------------------------------------------------------------------
# HTML shell
# --------------------------------------------------------------------------------------------
CSS = """
:root{--bg:#0f1216;--card:#171b21;--line:#262c35;--fg:#e6e8eb;--mut:#8b93a1;--acc:#f2b705;--bad:#ff6b6b;--ok:#5fd38d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--acc)}header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line)}
header .brand{font-weight:700;letter-spacing:.3px}header .brand b{color:var(--acc)}header .meta{color:var(--mut);font-size:13px;text-align:right}
nav{display:flex;gap:4px;overflow-x:auto;padding:8px 12px;border-bottom:1px solid var(--line);-webkit-overflow-scrolling:touch}
nav a{white-space:nowrap;padding:8px 12px;border-radius:8px;text-decoration:none;color:var(--fg)}nav a.on{background:var(--acc);color:#111;font-weight:600}
main{max-width:860px;margin:0 auto;padding:16px}h1{font-size:22px;margin:4px 0 14px}h2{font-size:17px;margin:0 0 10px;color:var(--acc)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}
table{width:100%;border-collapse:collapse}td,th{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--mut);font-weight:500;width:38%}
label{display:block;color:var(--mut);font-size:14px;margin:10px 0 4px}input[type=text],input[type=password],textarea,select{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
textarea{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
.btn{display:inline-block;padding:10px 16px;border-radius:8px;border:1px solid var(--acc);background:var(--acc);color:#111;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;margin:6px 6px 0 0}
.btn.sec{background:transparent;color:var(--acc)}.btn.dng{background:var(--bad);border-color:var(--bad);color:#111}.btn[disabled]{opacity:.5;cursor:default}
.flash{background:#2a2f1a;border:1px solid var(--acc);color:var(--fg);padding:10px 14px;border-radius:8px;margin-bottom:16px}
.warn{background:#2d1d1d;border:1px solid var(--bad);padding:10px 14px;border-radius:8px;margin-bottom:16px}
.note{color:var(--mut);font-size:14px}.ok{color:var(--ok)}.bad{color:var(--bad)}.mut{color:var(--mut)}
pre{background:#0a0c0f;border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;max-height:60vh;font-size:12px;line-height:1.35;white-space:pre-wrap;word-break:break-all}
footer{color:var(--mut);font-size:12px;text-align:center;padding:24px 16px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}form.inline{display:inline}
"""

NAV = [("/", "Status"), ("/logs", "Logs"), ("/settings", "Settings"), ("/update", "Update"),
       ("/wifi", "Wi-Fi"), ("/spotter", "Spotter"), ("/power", "Power"), ("/account", "Account")]


def page(title, body, active="", nav=True, flash=None):
    tabs = "".join(f'<a href="{p}" class="{"on" if p == active else ""}">{t}</a>' for p, t in NAV) if nav else ""
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · Wavelens admin</title>
<style>{CSS}</style></head><body>
<header><div class="brand">Wave<b>lens</b> admin</div>
<div class="meta">{esc(hostname())}<br>v{esc(installed_version())}</div></header>
{f'<nav>{tabs}</nav>' if nav else ''}
<main><h1>{esc(title)}</h1>{flash_html}{body}</main>
<footer>Wavelens companion admin · receive-only · nothing leaves this Pi except updates you ask for</footer>
</body></html>"""


def csrf_ok(given, expected):
    """Constant-time token check. hmac.compare_digest on two str raises TypeError when either holds a
    non-ASCII character (a crafted form value), so compare the UTF-8 bytes instead."""
    if not isinstance(given, str) or not isinstance(expected, str) or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass"))


def csrf_field(sess):
    return f'<input type="hidden" name="csrf" value="{esc(sess["csrf"])}">'


# Tiny poller used by the logs, update and spotter pages. Fetches JSON {lines:[...]} every 2 s.
POLL_JS = """
<script>
(function(){
  var pre=document.getElementById('out'), url=document.getElementById('out').dataset.url;
  var live=document.getElementById('live'), noise=document.getElementById('noise'), st=document.getElementById('st');
  var last='';
  function tick(){
    if(live && !live.checked) return;
    var u=url+(noise&&noise.checked?'?noise=1':'');
    fetch(u,{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(j){
      var t=j.lines.join('\\n'); if(t!==last){last=t; pre.textContent=t; pre.scrollTop=pre.scrollHeight;}
      if(st){st.textContent=j.running?'running: '+j.title+' …':(j.exit===null||j.exit===undefined?'idle':(j.exit===0?'done (exit 0)':'failed (exit '+j.exit+')'));
             st.className=j.running?'mut':(j.exit===0?'ok':'bad');}
    }).catch(function(){});
  }
  tick(); setInterval(tick,2000); if(noise) noise.addEventListener('change',function(){last='';tick();});
})();
</script>"""


# --------------------------------------------------------------------------------------------
# The HTTP handler
# --------------------------------------------------------------------------------------------
# The app images go to the first app slot (app0 at 0x10000). A Spotter that took a Bluetooth update from the
# phone runs from the second slot (ota_1), and its otadata says so: flashing app0 alone left it booting the old
# firmware. Blanking otadata (0xe000, 8 KB in every partition table we build: min_spiffs, huge_app, default)
# in the same write makes the bootloader start app0 — the image just written. The merged images write it anyway.
OTADATA_OFFSET, OTADATA_SIZE = 0xE000, 0x2000


def otadata_reset():
    """['0xe000', <8 KB of 0xFF>]: extra write_flash arguments that reset the boot slot to app0."""
    blank = os.path.join(upload_dir(), "otadata-blank.bin")
    if not (os.path.isfile(blank) and os.path.getsize(blank) == OTADATA_SIZE):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(blank), prefix=".ota-")
        with os.fdopen(fd, "wb") as f:
            f.write(b"\xff" * OTADATA_SIZE)
        os.replace(tmp, blank)
    return [hex(OTADATA_OFFSET), blank]


SPOTTER_PAUSE = "/run/wavelens/spotter-pause"   # ports the bridge must leave alone (one per line) while we flash


def spotter_unpause(port):
    """Hand a port back to the bridge (what the flash wrapper does itself when esptool finishes)."""
    try:
        with open(SPOTTER_PAUSE, encoding="utf-8") as f:
            keep = [l for l in f.read().splitlines() if l != port]
    except FileNotFoundError:
        return
    with open(SPOTTER_PAUSE + ".n", "w", encoding="utf-8") as f:
        f.write("".join(l + "\n" for l in keep))
    os.replace(SPOTTER_PAUSE + ".n", SPOTTER_PAUSE)


def upload_dir():
    """UPLOAD_DIR, created root-only (0700); refuses a symlink or a directory someone else owns."""
    os.makedirs(UPLOAD_DIR, mode=0o700, exist_ok=True)
    st = os.lstat(UPLOAD_DIR)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise OSError(f"{UPLOAD_DIR} is not a private directory")
    os.chmod(UPLOAD_DIR, 0o700)
    return UPLOAD_DIR


def save_upload(data, name):
    """Write an uploaded image (and its original name, so a '…merged.bin' flashes at 0x0) atomically via
    private temp files; nothing half-written is left behind on failure."""
    d = upload_dir()
    for dest, payload in ((UPLOAD_BIN, data), (UPLOAD_BIN + ".name", name.encode("utf-8", "replace"))):
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".up-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class BadRequest(ValueError):
    """A malformed request (answered 400, not 413)."""


class Handler(BaseHTTPRequestHandler):
    server_version = "WavelensAdmin/1"
    protocol_version = "HTTP/1.1"
    timeout = 30   # seconds per socket read: a client that stops sending mid-body can't hold a thread forever

    # ---- plumbing --------------------------------------------------------------------------
    def log_message(self, fmt, *args):   # keep the journal quiet; errors still go through log_error
        if self.command == "POST":
            sys.stderr.write("%s %s\n" % (self.client_address[0], fmt % args))

    def send_html(self, body, status=200, cookie=None):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, text, filename):
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path, cookie=None):
        self.send_response(303)
        self.send_header("Location", path)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def session(self):
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        tok = c["wl_session"].value if "wl_session" in c else None
        return tok, SESSIONS.get(tok)

    @staticmethod
    def cookie(tok, expire=False):
        base = f"wl_session={tok}; Path=/; HttpOnly; SameSite=Strict"
        return base + ("; Max-Age=0" if expire else f"; Max-Age={SESSION_TTL}")

    def body_length(self, limit, too_big):
        """The request's Content-Length, checked: a negative, malformed or oversized one is refused (a -1
        passed the size check and rfile.read(-1) then read until the client closed the connection)."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("bad Content-Length") from None
        if n < 0:
            raise BadRequest("bad Content-Length")
        if n > limit:
            raise ValueError(too_big)
        return n

    def form(self, limit=MAX_FORM_BODY):
        """Parse a urlencoded POST body into {name: value} (first value wins)."""
        n = self.body_length(limit, "body too large")
        raw = self.rfile.read(n).decode(errors="replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def multipart(self):
        """Parse a multipart/form-data POST: returns ({field: value}, {field: (filename, bytes)})."""
        n = self.body_length(MAX_UPLOAD_BODY, "upload larger than 4 MB")
        raw = self.rfile.read(n)
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + self.headers.get("Content-Type", "").encode() + b"\r\n\r\n" + raw)
        fields, files = {}, {}
        for part in msg.iter_parts() if msg.is_multipart() else []:
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            if part.get_filename():
                files[name] = (part.get_filename(), payload)
            else:
                fields[name] = payload.decode(errors="replace")
        return fields, files

    def flash(self, sess, msg):
        sess["flash"] = msg

    def render(self, sess, title, body, active):
        f = sess.pop("flash", None) if sess else None
        self.send_html(page(title, body, active, flash=f))

    # ---- routing ---------------------------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path, q = url.path, urllib.parse.parse_qs(url.query)
        if not admin_exists():
            if not host_ok(self.headers.get("Host")):
                return self.bad_host()
            return self.page_setup()
        tok, sess = self.session()
        if path == "/login" or not sess:
            return self.page_login()
        routes = {"/": self.page_status, "/logs": self.page_logs, "/settings": self.page_settings,
                  "/update": self.page_update, "/wifi": self.page_wifi, "/spotter": self.page_spotter,
                  "/power": self.page_power, "/account": self.page_account}
        if path in routes:
            return routes[path](sess)
        if path.startswith("/power/"):
            return self.page_power_confirm(sess, path[7:])
        if path == "/api/logs":
            return self.send_json({"lines": journal_lines(300, noise="noise" in q)})
        if path == "/api/logs.txt":
            return self.send_text("\n".join(journal_lines(5000, noise=True)) + "\n", "wavelens-bridge.log")
        if path == "/api/update/log":
            return self.send_json(update_log_snapshot())
        if path == "/api/health":
            return self.send_json(bridge_health())
        if path == "/api/spotter/log":
            return self.send_json(FLASH_JOB.snapshot())
        self.send_html(page("Not found", "<p>No such page.</p>", nav=True), 404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if not admin_exists():
                if not host_ok(self.headers.get("Host")):
                    return self.bad_host()
                return self.post_setup()
            tok, sess = self.session()
            if path == "/login":
                return self.post_login()
            if not sess:
                return self.redirect("/login")
            if self.headers.get("Content-Type", "").startswith("multipart/form-data"):
                form, files = self.multipart()
            else:
                form, files = self.form(), {}
            if not csrf_ok(form.get("csrf"), sess["csrf"]):
                return self.send_html(page("Rejected", "<p>Form token mismatch — go back, reload the page and try again.</p>"), 403)
            handlers = {"/logout": self.post_logout, "/account": self.post_account, "/settings": self.post_settings,
                        "/update": self.post_update, "/wifi": self.post_wifi, "/spotter": self.post_spotter,
                        "/bridge/restart": self.post_bridge_restart}
            if path in handlers:
                return handlers[path](sess, form, files, tok)
            if path.startswith("/power/"):
                return self.post_power(sess, path[7:])
            self.send_html(page("Not found", "<p>No such action.</p>"), 404)
        except ValueError as e:
            self.close_connection = True   # the body may be unread; don't reuse this connection
            self.send_html(page("Rejected", f"<p>{esc(e)}</p>"), 400 if isinstance(e, BadRequest) else 413)

    # ---- first-run setup and login ---------------------------------------------------------
    def bad_host(self):
        self.close_connection = True
        name = esc(hostname())
        self.send_html(page("Rejected", f"<p>Open this page by the Pi's own address: <code>http://{name}.local:{PORT}</code> "
                                        "or its IP address.</p>", nav=False), 403)

    def setup_token(self):
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        tok = c["wl_setup"].value if "wl_setup" in c else None
        return tok, SESSIONS.get_setup(tok)

    def page_setup(self, err=""):
        tok, st = self.setup_token()
        cookie = None
        if not st:
            tok, csrf = SESSIONS.new_setup()
            cookie = f"wl_setup={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age=3600"
        else:
            csrf = st["csrf"]
        body = f"""<div class="card"><p>This Pi has no admin password yet. Choose one now; you'll need it every time you
open this page. If you forget it: <code>sudo wavelens-admin --reset-password</code> over SSH, or delete
<code>/etc/wavelens/admin.json</code> on the SD card.</p>
{f'<div class="warn">{esc(err)}</div>' if err else ''}
<form method="post" action="/"><input type="hidden" name="csrf" value="{esc(csrf)}">
<label>Password (at least 8 characters)</label><input type="password" name="p1" autocomplete="new-password">
<label>Type it again</label><input type="password" name="p2" autocomplete="new-password">
<button class="btn" type="submit">Set admin password</button></form></div>"""
        self.send_html(page("Set an admin password", body, nav=False), cookie=cookie)

    def post_setup(self):
        f = self.form()
        _, st = self.setup_token()
        if not st or not csrf_ok(f.get("csrf"), st["csrf"]):
            return self.page_setup("The form expired — try again.")
        p1, p2 = f.get("p1", ""), f.get("p2", "")
        if len(p1) < 8:
            return self.page_setup("Password must be at least 8 characters.")
        if p1 != p2:
            return self.page_setup("The two passwords don't match.")
        admin_set_password(p1)
        with SESSIONS.lock:
            SESSIONS.setup.clear()
        tok = SESSIONS.new()
        SESSIONS.get(tok)["flash"] = "Admin password set. You're logged in."
        self.redirect("/", cookie=self.cookie(tok))

    def page_login(self, err=""):
        body = f"""<div class="card">{f'<div class="warn">{esc(err)}</div>' if err else ''}
<form method="post" action="/login"><label>Admin password</label>
<input type="password" name="password" autocomplete="current-password" autofocus>
<button class="btn" type="submit">Log in</button></form>
<p class="note">Forgot it? <code>sudo wavelens-admin --reset-password</code> over SSH, or delete
<code>/etc/wavelens/admin.json</code> on the SD card, then reload this page.</p></div>"""
        self.send_html(page("Log in", body, nav=False))

    def post_login(self):
        ip = self.client_address[0]
        f = self.form()
        stamp = SESSIONS.login_begin(ip)
        if stamp is None:
            return self.page_login("Too many failed logins. Wait 10 minutes and try again.")
        if not admin_check_password(f.get("password", "")):
            time.sleep(0.5)                     # the failure is already counted
            return self.page_login("Wrong password.")
        SESSIONS.login_ok(ip, stamp)
        tok = SESSIONS.new()
        self.redirect("/", cookie=self.cookie(tok))

    def post_logout(self, sess, form, files, tok):
        SESSIONS.drop(tok)
        self.redirect("/login", cookie=self.cookie("x", expire=True))

    # ---- /account --------------------------------------------------------------------------
    def page_account(self, sess):
        body = f"""<div class="card"><h2>Change password</h2><form method="post" action="/account">{csrf_field(sess)}
<label>Current password</label><input type="password" name="old" autocomplete="current-password">
<label>New password (at least 8 characters)</label><input type="password" name="p1" autocomplete="new-password">
<label>Type it again</label><input type="password" name="p2" autocomplete="new-password">
<button class="btn" type="submit">Change password</button></form></div>
<div class="card"><h2>Log out</h2><form method="post" action="/logout">{csrf_field(sess)}<button class="btn sec" type="submit">Log out</button></form>
<p class="note">Sessions last 12 hours. Recovery if locked out: <code>sudo wavelens-admin --reset-password</code>.</p></div>"""
        self.render(sess, "Account", body, "/account")

    def post_account(self, sess, form, files, tok):
        if not admin_check_password(form.get("old", "")):
            self.flash(sess, "Current password is wrong.")
        elif len(form.get("p1", "")) < 8:
            self.flash(sess, "New password must be at least 8 characters.")
        elif form.get("p1") != form.get("p2"):
            self.flash(sess, "The two new passwords don't match.")
        else:
            admin_set_password(form["p1"])
            SESSIONS.drop_all()
            tok = SESSIONS.new()
            SESSIONS.get(tok)["flash"] = "Password changed. Other devices were logged out."
            return self.redirect("/account", cookie=self.cookie(tok))
        self.redirect("/account")

    # ---- / status --------------------------------------------------------------------------
    def page_status(self, sess):
        state, up = unit_state(BRIDGE_UNIT)
        st = read_json(STATUS_JSON) or {}
        facts = pi_facts()
        upd = st.get("update") or {}
        banner = ""
        health = bridge_health()
        if not health["ok"] and not update_running():
            can_rb = os.path.isdir(PREV_DIR)
            rb = (f'<form method="post" action="/update" class="inline">{csrf_field(sess)}<input type="hidden" name="do" value="rollback">'
                  f'<button class="btn" type="submit">Put the previous version back</button></form>') if can_rb else ""
            banner += f"""<div class="warn"><h2 style="color:var(--bad)">{esc(health['title'])}</h2>
<p>{health['fix']}</p>
{f'<p class="note">What it said: <code>{esc(health["detail"])}</code></p>' if health["detail"] else ""}
<div class="row">{rb}
<form method="post" action="/update" class="inline">{csrf_field(sess)}<input type="hidden" name="do" value="apply"><button class="btn {"sec" if can_rb else ""}" type="submit">Install the latest update</button></form>
<form method="post" action="/bridge/restart" class="inline">{csrf_field(sess)}<button class="btn sec" type="submit">Restart the scanner</button></form>
<a class="btn sec" href="/power/reboot">Reboot…</a>
<a class="btn sec" href="/api/logs.txt">Download log for support</a></div>
<p class="note">Updates check themselves: if a new version doesn't start, the previous one is put back automatically.
Support: wavelensapp@gmail.com — attach the downloaded log.</p></div>"""
        elif update_running():
            banner += '<div class="flash">An update is running → <a href="/update">watch it on the Update page</a>. The phone may lose the scanner for up to a minute.</div>'
        uj = read_json(UPDATE_JSON) or {}
        last = uj.get("last_result") or ""
        recent = time.time() - float(uj.get("checked_at") or 0) < 24 * 3600
        if recent and health["ok"] and any(w in last for w in ("put back", "rolled back")):
            banner += f'<div class="flash">Last update: {esc(last)}. The scanner is on the version that worked; nothing else needs doing.</div>'
        if upd.get("available"):
            banner = f'<div class="flash">Update <b>{esc(upd["available"])}</b> is available → <a href="/update">Update page</a></div>'
        state_html = (f'<span class="{"ok" if health["ok"] else "bad"}">{esc(state if not health["looping"] else "restarting over and over")}</span>'
                      + (f" · up {esc(up)}" if up and health["ok"] else ""))
        age = ""
        if st.get("at"):
            age = f'<tr><th>Reported</th><td>{int(time.time() - float(st["at"]))} s ago</td></tr>'
        missing = ""
        if st.get("hw_missing"):
            missing = '<div class="warn">Missing hardware: ' + ", ".join(esc(x) for x in st["hw_missing"]) + "</div>"
        linked = "" if not st else ('<span class="ok">phone connected</span>' if status_linked(st) else '<span class="mut">phone not connected</span>')
        bridge = f"""<table>
<tr><th>Service</th><td>{state_html}</td></tr>
<tr><th>Phone</th><td>{linked or '<span class="mut">no live status yet</span>'}</td></tr>
<tr><th>Radio name</th><td>{esc(st.get('name') or args_get_name(defaults_read().get('WAVELENS_ARGS', '')) or '—')}</td></tr>
<tr><th>Pair code</th><td>{esc(st.get('code') or '—')}</td></tr>
<tr><th>Hardware</th><td>{esc(st.get('hw_line') or '—')}</td></tr>
<tr><th>Bridge build</th><td>{esc(st.get('fw') or installed_version())}</td></tr>{age}</table>"""
        pi = f"""<table>
<tr><th>Hostname</th><td>{esc(facts['hostname'])} <span class="mut">(http://{esc(facts['hostname'])}.local:{PORT})</span></td></tr>
<tr><th>IP</th><td>{esc(' '.join(facts['ips']) or '—')}</td></tr>
<tr><th>Wi-Fi</th><td>{esc(facts['ssid'] or 'not connected')}</td></tr>
<tr><th>CPU temp</th><td>{esc(facts['temp'])}</td></tr>
<tr><th>Disk</th><td>{esc(facts['disk'])}</td></tr>
<tr><th>Load</th><td>{esc(facts['load'])}</td></tr>
<tr><th>Pi uptime</th><td>{esc(facts['uptime'])}</td></tr></table>"""
        body = f"""{banner}{missing}<div class="grid">
<div class="card"><h2>Bridge</h2>{bridge}</div><div class="card"><h2>This Pi</h2>{pi}</div></div>
<div class="card"><h2>Quick actions</h2><div class="row">
<form method="post" action="/bridge/restart" class="inline">{csrf_field(sess)}<button class="btn" type="submit">Restart bridge</button></form>
<a class="btn sec" href="/power/reboot">Reboot…</a><a class="btn sec" href="/logs">View logs</a></div></div>"""
        self.render(sess, "Status", body, "/")

    def post_bridge_restart(self, sess, form, files, tok):
        rc, out = run(["systemctl", "restart", BRIDGE_UNIT], timeout=30)
        self.flash(sess, "Bridge restarted." if rc == 0 else f"Restart failed: {out}")
        self.redirect("/")

    # ---- /logs -----------------------------------------------------------------------------
    def page_logs(self, sess):
        lines = journal_lines(300)
        body = f"""<div class="card"><div class="row">
<label style="margin:0"><input type="checkbox" id="live" checked> live (refresh every 2 s)</label>
<label style="margin:0"><input type="checkbox" id="noise"> include rf/spec lines (noisy)</label>
<a class="btn sec" href="/api/logs.txt">Download full log</a></div>
<pre id="out" data-url="/api/logs">{esc(chr(10).join(lines))}</pre>
<p class="note">Last 300 lines of <code>journalctl -u wavelens-bridge</code>. The download has the last 5000 lines including the noisy ones.</p></div>{POLL_JS}"""
        self.render(sess, "Logs", body, "/logs")

    # ---- /settings -------------------------------------------------------------------------
    def page_settings(self, sess):
        d = defaults_read()
        args = d.get("WAVELENS_ARGS", "")
        body = f"""<div class="card"><form method="post" action="/settings">{csrf_field(sess)}
<label>Radio name (what the app lists under Find; letters, digits, - and _; up to 20)</label>
<input type="text" name="name" value="{esc(args_get_name(args))}" placeholder="Wavelens-Car" maxlength="20">
<label>WAVELENS_AUTO</label><select name="auto"><option value="0" {"selected" if d.get("WAVELENS_AUTO", "0") != "1" else ""}>0 — the bridge assigns radios itself (default, hot-plug)</option>
<option value="1" {"selected" if d.get("WAVELENS_AUTO") == "1" else ""}>1 — plan in the shell at each start (older way)</option></select>
<label>WAVELENS_RTL433 (rtl_433 arguments; usually empty)</label><input type="text" name="rtl433" value="{esc(d.get('WAVELENS_RTL433', ''))}">
<label>WAVELENS_ARGS (extra bridge flags; the radio name above is kept in sync)</label>
<textarea name="args" rows="4">{esc(args)}</textarea>
<button class="btn" type="submit">Save and restart bridge</button></form></div>
<div class="card"><h2>What these mean</h2><p class="note">From the comments in <code>/etc/default/wavelens-bridge</code>:</p>
<pre style="max-height:40vh">{esc(defaults_help())}</pre></div>"""
        self.render(sess, "Settings", body, "/settings")

    def post_settings(self, sess, form, files, tok):
        name = form.get("name", "").strip()
        if name and not NAME_RE.match(name):
            self.flash(sess, "Radio name must be 1–20 characters: letters, digits, - or _.")
            return self.redirect("/settings")
        args = " ".join(form.get("args", "").split())   # collapse newlines; it is one shell line
        try:
            shlex.split(args)
            shlex.split(form.get("rtl433", ""))
        except ValueError as e:
            self.flash(sess, f"Unbalanced quotes: {e}")
            return self.redirect("/settings")
        args = args_set_name(args, name)
        defaults_write({"WAVELENS_AUTO": "1" if form.get("auto") == "1" else "0",
                        "WAVELENS_RTL433": " ".join(form.get("rtl433", "").split()),   # one shell line, like args
                        "WAVELENS_ARGS": args})
        rc, out = run(["systemctl", "restart", BRIDGE_UNIT], timeout=30)
        self.flash(sess, "Settings saved; bridge restarted." if rc == 0 else f"Saved, but restart failed: {out}")
        self.redirect("/settings")

    # ---- /update ---------------------------------------------------------------------------
    def page_update(self, sess):
        u = read_json(UPDATE_JSON) or {}
        auto = defaults_read().get("WAVELENS_AUTO_UPDATE", "0") == "1"
        snap = UPDATE_JOB.snapshot()
        dis = "disabled" if snap["running"] else ""
        avail = u.get("available")
        body = f"""<div class="card"><table>
<tr><th>Installed</th><td>{esc(u.get('installed') or installed_version())}{f' <span class="mut">({esc(u["commit"][:10])})</span>' if u.get('commit') else ''}</td></tr>
<tr><th>Available</th><td>{f'<b class="ok">{esc(avail)}</b>' if avail else '<span class="mut">none — you are up to date</span>'}</td></tr>
<tr><th>Last check</th><td>{esc(fmt_ts(u.get('checked_at')))}</td></tr>
<tr><th>Last result</th><td>{esc(u.get('last_result') or '—')}</td></tr></table>
<form method="post" action="/update" class="row">{csrf_field(sess)}
<button class="btn sec" name="do" value="check" {dis}>Check now</button>
<button class="btn" name="do" value="apply" {dis}>Install update</button>
<button class="btn sec" name="do" value="rollback" {dis}>Roll back to previous</button></form>
<p class="note">Updates are free for life, come from the public GitHub repo <code>pmcbrien/wavelens</code>, and need Wi-Fi.
Installing restarts the bridge for a few seconds.</p></div>
<div class="card"><form method="post" action="/update">{csrf_field(sess)}<input type="hidden" name="do" value="auto">
<label style="margin:0"><input type="checkbox" name="auto" value="1" {"checked" if auto else ""}> Install updates automatically overnight</label>
<button class="btn sec" type="submit">Save</button></form></div>
<div class="card"><h2>Progress <span id="st" class="mut"></span></h2><pre id="out" data-url="/api/update/log">{esc(chr(10).join(snap['lines']))}</pre></div>{POLL_JS}"""
        self.render(sess, "Update", body, "/update")

    def post_update(self, sess, form, files, tok):
        do = form.get("do", "")
        if do == "auto":
            defaults_write({"WAVELENS_AUTO_UPDATE": "1" if form.get("auto") == "1" else "0"})
            self.flash(sess, "Automatic overnight updates " + ("on." if form.get("auto") == "1" else "off."))
        elif do in ("check", "apply", "rollback"):
            if not os.access(UPDATE_CLI, os.X_OK):
                self.flash(sess, f"{UPDATE_CLI} is not installed on this Pi.")
            elif UPDATE_JOB.start(do, [UPDATE_CLI, do]):
                self.flash(sess, f"Started: wavelens-update {do}. Watch the progress box.")
            else:
                self.flash(sess, "An update task is already running.")
        self.redirect("/update")

    # ---- /wifi -----------------------------------------------------------------------------
    def wifi_scan(self):
        _, out = run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes"], timeout=30)
        best = {}
        for line in out.splitlines():
            parts = line.replace("\\:", "\x00").split(":")
            if len(parts) < 3:
                continue
            ssid, sig, sec = parts[0].replace("\x00", ":"), parts[1], ":".join(parts[2:])
            if not ssid:
                continue
            try:
                sig = int(sig)
            except ValueError:
                sig = 0
            if ssid not in best or best[ssid][0] < sig:
                best[ssid] = (sig, sec.replace("\x00", ":") or "open")
        return sorted(((s, v[0], v[1]) for s, v in best.items()), key=lambda x: -x[1])

    def wifi_saved(self):
        _, out = run(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"], timeout=10)
        return [l.rsplit(":", 1)[0].replace("\\:", ":") for l in out.splitlines() if l.endswith(":802-11-wireless")]

    def page_wifi(self, sess):
        cur = wifi_ssid()
        nets = "".join(f"<tr><td>{esc(s)}{' <span class=ok>(connected)</span>' if s == cur else ''}</td><td>{sig}%</td><td class=mut>{esc(sec)}</td></tr>"
                       for s, sig, sec in self.wifi_scan()) or "<tr><td colspan=3 class=mut>nothing found</td></tr>"
        saved = "".join(f"""<tr><td>{esc(n)}</td><td><form method="post" action="/wifi" class="inline">{csrf_field(sess)}
<input type="hidden" name="do" value="forget"><input type="hidden" name="con" value="{esc(n)}"><button class="btn sec" type="submit">Forget</button></form></td></tr>"""
                        for n in self.wifi_saved()) or "<tr><td colspan=2 class=mut>none</td></tr>"
        body = f"""<div class="warn">The Wi-Fi password you type here travels over your local network unencrypted (this page is plain HTTP on the LAN).
Do this on your home Wi-Fi, not on public Wi-Fi.</div>
<div class="grid"><div class="card"><h2>Networks nearby</h2><table><tr><th>SSID</th><th>Signal</th><th>Security</th></tr>{nets}</table></div>
<div class="card"><h2>Join a network</h2><form method="post" action="/wifi">{csrf_field(sess)}<input type="hidden" name="do" value="connect">
<label>Network name (SSID)</label><input type="text" name="ssid" value="" autocapitalize="off">
<label>Password (leave empty for an open network)</label><input type="password" name="pw" autocomplete="off">
<button class="btn" type="submit">Connect</button></form>
<p class="note">Currently connected: <b>{esc(cur or 'nothing')}</b>. If you switch networks this page will move to the Pi's new address.</p></div></div>
<div class="card"><h2>Saved networks</h2><table>{saved}</table></div>
<div class="card"><h2>Pi name</h2><form method="post" action="/wifi">{csrf_field(sess)}<input type="hidden" name="do" value="hostname">
<label>Hostname (letters, digits, hyphens)</label><input type="text" name="host" value="{esc(hostname())}" autocapitalize="off">
<button class="btn sec" type="submit">Rename</button></form>
<p class="note">After renaming, this page is at <b>http://&lt;name&gt;.local:{PORT}</b> (currently http://{esc(hostname())}.local:{PORT}).</p></div>"""
        self.render(sess, "Wi-Fi", body, "/wifi")

    def post_wifi(self, sess, form, files, tok):
        do = form.get("do")
        if do == "connect":
            ssid, pw = form.get("ssid", "").strip(), form.get("pw", "")
            if not ssid or len(ssid) > 32:
                self.flash(sess, "Enter the network name.")
            else:
                argv = ["nmcli", "dev", "wifi", "connect", ssid] + (["password", pw] if pw else [])
                rc, out = run(argv, timeout=60)
                self.flash(sess, f"Connected to {ssid}." if rc == 0 else f"Could not connect: {out.replace(pw, '••••') if pw else out}")
        elif do == "forget":
            con = form.get("con", "")
            if con in self.wifi_saved():
                rc, out = run(["nmcli", "con", "delete", con], timeout=20)
                self.flash(sess, f"Forgot {con}." if rc == 0 else f"Failed: {out}")
            else:
                self.flash(sess, "No such saved network.")
        elif do == "hostname":
            host = form.get("host", "").strip()
            if not HOST_RE.match(host):
                self.flash(sess, "Hostname: letters, digits and hyphens only, 1–63 characters.")
            else:
                rc, out = run(["hostnamectl", "set-hostname", host], timeout=20)
                if rc == 0:
                    self.rewrite_hosts(host)
                    run(["systemctl", "restart", "avahi-daemon"], timeout=20)
                    self.flash(sess, f"Renamed. This page is now at http://{host}.local:{PORT}")
                else:
                    self.flash(sess, f"Rename failed: {out}")
        self.redirect("/wifi")

    @staticmethod
    def rewrite_hosts(host):
        """Point the 127.0.1.1 line in /etc/hosts at the new name (sudo and avahi rely on it)."""
        lines = read_text("/etc/hosts").splitlines()
        done = False
        for i, l in enumerate(lines):
            if l.split() and l.split()[0] == "127.0.1.1":
                lines[i], done = f"127.0.1.1\t{host}", True
        if not done:
            lines.append(f"127.0.1.1\t{host}")
        with open("/etc/hosts", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    # ---- /spotter --------------------------------------------------------------------------
    @staticmethod
    def esptool_argv():
        """How to invoke esptool on this Pi, or None if it isn't installed."""
        for exe in ("esptool.py", "esptool"):
            p = shutil.which(exe)
            if p:
                return [p]
        if run([sys.executable, "-c", "import esptool"], timeout=10)[0] == 0:
            return [sys.executable, "-m", "esptool"]
        return None

    def page_spotter(self, sess):
        ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        tool = self.esptool_argv()
        snap = FLASH_JOB.snapshot()
        dis = "disabled" if snap["running"] else ""
        fw_ver = read_text(FW_VERSION).strip() or "unknown version"
        sources = []
        if os.path.isfile(FW_BIN):
            sources.append(("installed", f"Installed firmware {fw_ver} ({os.path.getsize(FW_BIN) // 1024} KB)"))
        if os.path.isfile(UPLOAD_BIN):
            sources.append(("upload", f"Uploaded file ({os.path.getsize(UPLOAD_BIN) // 1024} KB)"))
        src_html = "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in sources) or '<option value="">no firmware on the Pi yet</option>'
        port_html = "".join(f'<option value="{esc(p)}">{esc(p)}</option>' for p in ports) or '<option value="">no Spotter found on USB</option>'
        tool_html = ('<span class="ok">esptool found</span>' if tool else
                     '<span class="bad">esptool is not installed</span> — over SSH run <code>sudo apt install esptool</code> (or <code>pip install esptool</code>), then reload.')
        units = (read_json(STATUS_JSON) or {}).get("spotters") or []
        rows = ""
        for u in units:
            cur = "auto" if u.get("auto") else u.get("role", "auto")
            opts = "".join(f'<option value="{k}"{" selected" if k == cur else ""}>{esc(v)}</option>' for k, v in SPOTTER_JOBS)
            state = ('<span class="ok">working</span>' if u.get("ok") else
                     f'<span class="bad">{esc(u.get("msg") or "not answering")}</span>')
            rows += (f'<tr><th>{esc(u.get("u", "?"))}</th><td>{esc(u.get("name") or "ESP32")} {esc(u.get("ver") or "")}<br>'
                     f'{state} · {int(u.get("hits") or 0)} heard · now: {esc(u.get("role_label") or "")}{" (automatic)" if u.get("auto") else ""}</td>'
                     f'<td><form method="post" action="/spotter" class="inline">{csrf_field(sess)}<input type="hidden" name="do" value="role">'
                     f'<input type="hidden" name="id" value="{esc(u.get("id", ""))}"><select name="role" onchange="this.form.submit()">{opts}</select>'
                     f'<noscript><button class="btn sec" type="submit">Set</button></noscript></form></td></tr>')
        units_html = (f'<table>{rows}</table><p class="note">Spotters plugged into the Pi talk over the cable, not Bluetooth, and the Pi passes '
                      'what they hear to the phone. <b>Automatic</b> splits the work: one on Bluetooth, the others on the busy Wi-Fi channels '
                      '(1, 6, 11); a Spotter the phone holds over Bluetooth keeps Wi-Fi + Bluetooth on every channel. A job stays with the USB socket.</p>'
                      if units else '<p class="mut">None plugged in. Plug a Spotter into one of the Pi\'s USB ports: it is picked up within a few '
                      'seconds and the phone gets what it hears over the Pi\'s one Bluetooth link.</p>')
        body = f"""<div class="card"><h2>Spotters on this Pi</h2>{units_html}</div>
<div class="card"><h2>1 · Firmware</h2><table>
<tr><th>Installed file</th><td>{esc(fw_ver) if os.path.isfile(FW_BIN) else '<span class=mut>none</span>'}</td></tr>
<tr><th>Uploaded file</th><td>{'present' if os.path.isfile(UPLOAD_BIN) else '<span class=mut>none</span>'}</td></tr></table>
<div class="row"><form method="post" action="/spotter" class="inline">{csrf_field(sess)}<input type="hidden" name="do" value="download">
<button class="btn sec" type="submit" {dis}>Download latest from GitHub</button></form></div>
<form method="post" action="/spotter" enctype="multipart/form-data">{csrf_field(sess)}<input type="hidden" name="do" value="upload">
<label>…or upload a .bin (max 4 MB)</label><input type="file" name="bin" accept=".bin"><button class="btn sec" type="submit">Upload</button></form></div>
<div class="card"><h2>2 · Flash</h2><p>{tool_html}</p>
<form method="post" action="/spotter">{csrf_field(sess)}<input type="hidden" name="do" value="flash">
<label>Spotter USB port</label><select name="port">{port_html}</select>
<label>Firmware</label><select name="src">{src_html}</select>
<button class="btn" type="submit" {dis if tool and ports and sources else 'disabled'}>Flash Spotter</button></form>
<p class="note">Plug the Spotter into one of the Pi's USB ports and reload this page if it isn't listed. Some generic ESP32 boards need
the BOOT button held while flashing starts; the Spotter we sell doesn't. Flashing takes about a minute at 460800 baud; the Spotter
restarts by itself afterwards.</p></div>
<div class="card"><h2>Progress <span id="st" class="mut"></span></h2><pre id="out" data-url="/api/spotter/log">{esc(chr(10).join(snap['lines']))}</pre></div>{POLL_JS}"""
        self.render(sess, "Spotter", body, "/spotter")

    def post_spotter(self, sess, form, files, tok):
        do = form.get("do")
        if do == "role":
            uid, role = form.get("id", ""), form.get("role", "auto")
            if not uid or role not in dict(SPOTTER_JOBS) or "/" in uid:
                self.flash(sess, "Pick a job from the list.")
            else:
                roles = read_json(SPOTTER_ROLES) or {}
                if not isinstance(roles, dict):
                    roles = {}
                if role == "auto":
                    roles.pop(uid, None)
                else:
                    roles[uid] = role
                os.makedirs(os.path.dirname(SPOTTER_ROLES), exist_ok=True)
                with open(SPOTTER_ROLES + ".tmp", "w") as f:
                    json.dump(roles, f, indent=1)
                os.replace(SPOTTER_ROLES + ".tmp", SPOTTER_ROLES)
                self.flash(sess, f"Job set to {dict(SPOTTER_JOBS)[role]}. The Spotter switches within a few seconds.")
            return self.redirect("/spotter")
        if do == "download":
            threading.Thread(target=self.download_firmware, daemon=True).start()
            self.flash(sess, "Downloading firmware from GitHub; reload in a few seconds.")
        elif do == "upload":
            fname, data = files.get("bin", ("", b""))
            if not data:
                self.flash(sess, "No file received.")
            elif len(data) > 4 * 1024 * 1024:
                self.flash(sess, "File is larger than 4 MB.")
            else:
                try:
                    save_upload(data, os.path.basename(fname))
                    self.flash(sess, f"Uploaded {os.path.basename(fname)} ({len(data) // 1024} KB).")
                except OSError as e:
                    self.flash(sess, f"Could not save the upload: {e}")
        elif do == "flash":
            port, src = form.get("port", ""), form.get("src", "")
            if port not in glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"):
                self.flash(sess, "That USB port isn't there any more; reload and pick again.")
            elif src == "installed" and os.path.isfile(FW_BIN):
                # resolved at flash time: the job asks the board which chip it is and flashes wavelens-<chip>.bin
                path, name = "@installed", "wavelens-<chip>.bin"
            elif src == "upload" and os.path.isfile(UPLOAD_BIN):
                path, name = UPLOAD_BIN, read_text(UPLOAD_BIN + ".name").strip() or "upload.bin"
            else:
                self.flash(sess, "Pick a firmware file first.")
                path = None
            tool = self.esptool_argv()
            if path and not tool:
                self.flash(sess, "esptool is not installed: sudo apt install esptool")
            elif path:
                offset = "0x0" if "merged" in name.lower() else "0x10000"
                try:
                    ota = otadata_reset() if offset == "0x10000" else []
                except OSError as e:
                    self.flash(sess, f"Could not prepare the flash: {e}")
                    return self.redirect("/spotter")
                # The bridge drives Spotters plugged in here (spotters.py): ask it to let go of this port for the
                # flash, and take the request back afterwards whatever esptool's result.
                pause = ('p="$1"; shift; mkdir -p /run/wavelens; echo "$p" >> /run/wavelens/spotter-pause; sleep 2; "$@"; rc=$?; '
                         'grep -vxF "$p" /run/wavelens/spotter-pause > /run/wavelens/spotter-pause.n 2>/dev/null; '
                         'mv -f /run/wavelens/spotter-pause.n /run/wavelens/spotter-pause; exit $rc')
                if path == "@installed":
                    # ask the board what it is (esptool prints "Chip is ESP32-C6 …" / "Chip type: ESP32-C6 …"), then
                    # flash the image built for that chip. A chip this release has no image for stops with a message.
                    pick = ('c=$("$@" --port "$P" chip_id 2>&1 | grep -oE "ESP32-(S3|C3|C5|C6)" | head -1 | tr -d - | tr A-Z a-z); '
                            '[ -n "$c" ] || c=esp32; f="$D/wavelens-$c.bin"; '
                            'if [ ! -f "$f" ]; then echo "no Spotter firmware for $c on this Pi yet: download it, or use the browser flasher"; exit 2; fi; '
                            'echo "chip: $c -> $f"; "$@" --chip "$c" --port "$P" --baud 460800 write_flash $OTA 0x10000 "$f"')
                    inner = ["/bin/sh", "-c", pick, "pick"] + tool
                    argv = ["/bin/sh", "-c", pause, "sh", port, "env", f"P={port}", f"D={FW_DIR}",
                            "OTA=" + " ".join(ota)] + inner
                else:
                    argv = ["/bin/sh", "-c", pause, "sh", port] + tool + ["--chip", "auto", "--port", port, "--baud", "460800",
                                                                         "write_flash"] + ota + [offset, path]
                if FLASH_JOB.start("flash " + name, argv, timeout=600, on_kill=lambda: spotter_unpause(port)):
                    self.flash(sess, f"Flashing {name} at {offset}{' (and resetting the boot slot)' if offset == '0x10000' else ''}"
                                     f" on {port}. Watch the progress box.")
                else:
                    self.flash(sess, "A flash is already running.")
        self.redirect("/spotter")

    @staticmethod
    def download_firmware():
        """Fetch the latest Spotter firmware (every chip) + VERSION into FW_DIR. Logged to the flash box.
        All or nothing: everything goes to a staging folder first and only a complete set replaces the installed
        images and VERSION — a release missing one chip's image used to leave that chip's old .bin under the new
        VERSION, and the page then flashed stale firmware it called current."""
        if not DOWNLOAD_LOCK.acquire(blocking=False):
            return
        stage = None
        try:
            os.makedirs(FW_DIR, exist_ok=True)
            base = FW_URL.rsplit("/", 1)[0]
            FLASH_JOB.lines.append(f"downloading {base}/ (every chip's image)")
            stage = tempfile.mkdtemp(dir=FW_DIR, prefix=".dl-")
            # VERSION is fetched first and again at the end: the release is deleted and recreated on every
            # publish, so a download that straddles one could otherwise put old images under the new VERSION.
            want = [(FW_VERSION_URL, FW_VERSION)] + [(f"{base}/wavelens-{c}.bin", fw_bin_for(c)) for c in FW_CHIPS]
            got = []

            def fetch(url, tmp):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "wavelens-admin"})
                    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                        shutil.copyfileobj(r, f, 1 << 16)
                    if os.path.getsize(tmp) == 0:
                        raise OSError("empty file")
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError(f"no {os.path.basename(tmp)} in this release ({e}); "
                                       f"kept firmware {read_text(FW_VERSION).strip() or 'as it was'}") from None

            for url, dest in want:
                tmp = os.path.join(stage, os.path.basename(dest))
                fetch(url, tmp)
                got.append((tmp, dest))
            again = os.path.join(stage, "VERSION.check")
            fetch(FW_VERSION_URL, again)
            if read_text(again).strip() != read_text(got[0][0]).strip():
                raise RuntimeError("a new firmware release was published during the download; "
                                   f"kept firmware {read_text(FW_VERSION).strip() or 'as it was'} - try again")
            got.append(got.pop(0))           # install VERSION last, once every image is in
            for tmp, dest in got:            # VERSION is last in the list, so it only changes once every image is in
                os.replace(tmp, dest)
            FLASH_JOB.lines.append(f"downloaded firmware {read_text(FW_VERSION).strip() or '?'} ({os.path.getsize(FW_BIN) // 1024} KB)")
        except Exception as e:  # noqa: BLE001
            FLASH_JOB.lines.append(f"download failed: {e}")
        finally:
            if stage:
                shutil.rmtree(stage, ignore_errors=True)
            DOWNLOAD_LOCK.release()

    # ---- /power ----------------------------------------------------------------------------
    POWER = {"restart-bridge": ("Restart bridge", ["systemctl", "restart", BRIDGE_UNIT], "The phone loses the Spotter for a few seconds."),
             "restart-admin": ("Restart admin", ["systemctl", "restart", ADMIN_UNIT], "This page will reload after a moment; you'll need to log in again."),
             "reboot": ("Reboot", ["systemctl", "reboot"], "The Pi is back in about a minute."),
             "shutdown": ("Shut down", ["systemctl", "poweroff"], "Pull the power and plug it back in to start the Pi again.")}

    def page_power(self, sess):
        rows = "".join(f'<tr><td><b>{esc(t)}</b><br><span class="note">{esc(n)}</span></td><td><a class="btn sec" href="/power/{k}">{esc(t)}…</a></td></tr>'
                       for k, (t, _, n) in self.POWER.items())
        self.render(sess, "Power", f'<div class="card"><table>{rows}</table></div>', "/power")

    def page_power_confirm(self, sess, action):
        if action not in self.POWER:
            return self.send_html(page("Not found", "<p>No such action.</p>"), 404)
        title, _, note = self.POWER[action]
        body = f"""<div class="card"><p>Really <b>{esc(title.lower())}</b>? {esc(note)}</p>
<form method="post" action="/power/{action}">{csrf_field(sess)}<button class="btn dng" type="submit">Yes, {esc(title.lower())}</button>
<a class="btn sec" href="/power">Cancel</a></form></div>"""
        self.render(sess, title, body, "/power")

    def post_power(self, sess, action):
        if action not in self.POWER:
            return self.send_html(page("Not found", "<p>No such action.</p>"), 404)
        title, argv, note = self.POWER[action]
        if action in ("restart-admin", "reboot", "shutdown"):
            # Answer first, then act: the service that runs this page is about to go away.
            self.send_html(page(title, f'<div class="card"><p>{esc(title)} started. {esc(note)}</p><p><a href="/">Back to status</a></p></div>', "/power"))
            threading.Timer(1.0, lambda: run(argv, timeout=30)).start()
            return
        rc, out = run(argv, timeout=30)
        self.flash(sess, f"{title} done." if rc == 0 else f"{title} failed: {out}")
        self.redirect("/power")


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------
def main(argv):
    if "--reset-password" in argv:
        if os.path.isfile(ADMIN_JSON):
            os.remove(ADMIN_JSON)
            print(f"Removed {ADMIN_JSON}. The next visit to http://{hostname()}.local:{PORT} asks for a new password.")
            run(["systemctl", "restart", ADMIN_UNIT], timeout=20)   # drop live sessions too; harmless if not a service
        else:
            print(f"{ADMIN_JSON} does not exist; nothing to reset.")
        return 0
    if "-h" in argv or "--help" in argv:
        print("usage: wavelens-admin [--reset-password]\n  WAVELENS_ADMIN_PORT=8080  listening port")
        return 0
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"wavelens-admin listening on http://0.0.0.0:{PORT} (http://{hostname()}.local:{PORT})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
