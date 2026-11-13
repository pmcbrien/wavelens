#!/usr/bin/env python3
"""Wavelens Scanner — companion admin web UI for the Raspberry Pi.

A small, dependency-free (Python 3.11 stdlib only) web interface that customers open on their
phone over the home Wi-Fi to look after the Pi that runs wavelens-bridge:

    http://wavelens.local:8080      (or http://<ip>:8080)

Pages:  /          status, Pi facts, update banner, quick actions
        /logs      bridge journal (last 300 lines, live polling, download)
        /settings  /etc/default/wavelens-bridge editor (comments preserved), and the Bluetooth / Wi-Fi
                   set-up / SSH switches (POST /security)
        /frequencies  your own frequency list: add, edit, lock out, import, export, backups (freqdb.py)
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
import csv
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import freqdb                    # the owner's frequency list (companion/pi/freqdb.py)
except ImportError:                  # an install that predates it: the Frequencies tab says to update
    freqdb = None

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
MAX_RR_FILE = 16 * 1024 * 1024   # one RadioReference export (a nationwide one runs to a few MB)
MAX_RR_FILES = 40                # exports kept in area/rr/
MAX_RR_BODY = 2 * MAX_RR_FILE + 64 * 1024   # one upload of several files
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


SESSIONS_FILE = "/run/wavelens/admin-sessions.json"   # root-only, tmpfs: survives an admin restart, not a reboot


class Sessions:
    """Session table: token -> {expires, csrf, flash}. Kept in a root-only file on tmpfs as well, so the restart
    an update does part-way through doesn't log the owner out of the page that is showing its progress."""

    def __init__(self, path=None):
        self.lock = threading.Lock()
        self.path = path
        self.table = {}
        if path:
            try:
                st = os.lstat(path)
                if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077:
                    with open(path) as f:
                        now = time.time()
                        self.table = {k: {"expires": float(v["expires"]), "csrf": str(v["csrf"]), "flash": None}
                                      for k, v in json.load(f).items() if float(v["expires"]) > now}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.table = {}
        self.fails = collections.defaultdict(list)   # ip -> [timestamps of failed logins]
        self.setup = {}   # first-run setup form: cookie token -> {expires, csrf}; never a login session

    def new(self):
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.table[tok] = {"expires": time.time() + SESSION_TTL,
                               "csrf": secrets.token_urlsafe(24), "flash": None}
            self._save()
        return tok

    def _save(self):
        """Under self.lock. Best effort: a failed write only means a log-in after the next restart."""
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({k: {"expires": v["expires"], "csrf": v["csrf"]} for k, v in self.table.items()}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

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
            self._save()

    def drop_all(self):
        with self.lock:
            self.table.clear()
            self._save()

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


SESSIONS = Sessions(SESSIONS_FILE if os.geteuid() == 0 else None)


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


# Scanner settings the Settings page edits as form fields; each is one flag inside WAVELENS_ARGS.
# flag → (default as the bridge has it, validator returning the cleaned value or None)
def _int_in(lo, hi):
    def check(v):
        try:
            n = int(str(v).strip())
        except ValueError:
            return None
        return str(n) if lo <= n <= hi else None
    return check


def _one_of(*choices):
    return lambda v: v if v in choices else None


SCANNER_FLAGS = {
    "--listen-gain": ("40", _int_in(0, 50)),
    "--listen-ppm": ("", _int_in(-200, 200)),          # "" = not set: the bridge uses --rtl-ppm
    "--tg-unlisted": ("mute", _one_of("mute", "play")),
    "--tg-interop": ("other", _one_of("other", "police")),
}


def args_get_flag(args, flag):
    """The value of '--flag X' (or --flag=X) inside WAVELENS_ARGS, or None when it is not there."""
    try:
        parts = shlex.split(args)
    except ValueError:
        return None
    for i, p in enumerate(parts):
        if p == flag and i + 1 < len(parts):
            return parts[i + 1]
        if p.startswith(flag + "="):
            return p[len(flag) + 1:]
    return None


def args_set_flag(args, flag, value):
    """WAVELENS_ARGS with any '--flag …' removed and '--flag value' appended; value None or '' removes it."""
    try:
        parts = shlex.split(args)
    except ValueError:
        parts = args.split()
    out, skip = [], False
    for p in parts:
        if skip:
            skip = False
            continue
        if p == flag:
            skip = True
            continue
        if p.startswith(flag + "="):
            continue
        out.append(p)
    if value not in (None, ""):
        out += [flag, str(value)]
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
# Bluetooth and remote access (Settings tab): the switches for what a nearby phone or the home network can do
# --------------------------------------------------------------------------------------------
BT_AGENT_UNIT = "wavelens-bt-agent"
SSH_UNITS = ("ssh", "sshd")          # Raspberry Pi OS / Debian call it ssh; sshd is an alias on some images
OFF_WORDS = ("0", "no", "off", "false")


def env_on(d, key, default="1"):
    """The bridge's own reading of an on/off key in /etc/default/wavelens-bridge."""
    return str(d.get(key, default)).strip().lower() not in OFF_WORDS


def unit_enabled(unit):
    """'on', 'off' or 'missing' for a systemd unit: enabled at boot counts as on."""
    rc, out = run(["systemctl", "is-enabled", unit], timeout=10)
    word = (out.splitlines() or [""])[-1].strip()
    if word in ("enabled", "enabled-runtime", "alias", "static", "indirect"):
        return "on"
    if word in ("disabled", "masked", "masked-runtime"):
        return "off"
    return "missing"


def ssh_unit():
    """The SSH server's unit name on this Pi, or None if none is installed."""
    for u in SSH_UNITS:
        if unit_enabled(u) != "missing":
            return u
    return None


def unit_set(unit, on):
    """Enable and start, or disable and stop, a unit. Returns (ok, output)."""
    rc, out = run(["systemctl", "enable" if on else "disable", "--now", unit], timeout=30)
    return rc == 0, out


def security_state():
    """What the Settings tab's 'Bluetooth and remote access' card shows."""
    d = defaults_read()
    ssh = ssh_unit()
    return {"wifi_setup": env_on(d, "WAVELENS_WIFI_SETUP"),
            "app_control": env_on(d, "WAVELENS_APP_CONTROL"),
            "bt_autopair": unit_enabled(BT_AGENT_UNIT),      # on / off / missing
            "ssh": unit_enabled(ssh) if ssh else "missing",
            "ssh_unit": ssh}


def security_apply(form, cur):
    """Apply the card's form against the current state. Only what changed is touched.
    Returns (messages, restart_bridge)."""
    msgs, restart, env = [], False, {}
    want = {k: form.get(k) == "1" for k in ("wifi_setup", "app_control", "bt_autopair", "ssh")}
    if want["wifi_setup"] != cur["wifi_setup"]:
        env["WAVELENS_WIFI_SETUP"] = "1" if want["wifi_setup"] else "0"
        msgs.append("Wi-Fi set-up from the app " + ("on" if want["wifi_setup"] else "off") + ".")
    if want["app_control"] != cur["app_control"]:
        env["WAVELENS_APP_CONTROL"] = "1" if want["app_control"] else "0"
        msgs.append("Updates and area from the app " + ("on" if want["app_control"] else "off") + ".")
    if env:
        defaults_write(env)
        restart = True
    if cur["bt_autopair"] != "missing" and want["bt_autopair"] != (cur["bt_autopair"] == "on"):
        ok, out = unit_set(BT_AGENT_UNIT, want["bt_autopair"])
        if ok and not want["bt_autopair"]:
            run(["bluetoothctl", "pairable", "off"], timeout=10)   # the agent turned it on; don't leave it on
        msgs.append(("Pairing without a prompt " + ("on" if want["bt_autopair"] else "off") + ".") if ok
                    else f"Pairing setting failed: {out}")
    if cur["ssh"] != "missing" and want["ssh"] != (cur["ssh"] == "on"):
        ok, out = unit_set(cur["ssh_unit"], want["ssh"])
        if ok and not want["ssh"] and unit_enabled("ssh.socket") == "on":
            unit_set("ssh.socket", False)     # newer Debian can start sshd from a socket instead
        msgs.append(("SSH login " + ("on" if want["ssh"] else "off") + ".") if ok else f"SSH setting failed: {out}")
    return msgs, restart


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
        self.started = 0.0

    def start(self, title, argv, timeout=1800, on_kill=None):
        """on_kill: called after the job had to be killed for running too long (e.g. undo what its own
        cleanup would have done)."""
        with self.lock:
            if self.running:
                return False
            self.running, self.title, self.exit = True, title, None
            self.started = time.time()
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
                    "seq": self.seq, "started": self.started, "lines": list(self.lines)}


UPDATE_JOB = Job("update")
FLASH_JOB = Job("flash")
AREA_JOB = Job("area")
AREA_TOOL = os.path.join(LIB_DIR, "area.py")
AREA_FILE = "/etc/wavelens/area.json"
AREA_DIR = "/var/lib/wavelens/area"
COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
POSTCODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{0,11}$")
RR_DIR = os.path.join(AREA_DIR, "rr")
RR_KINDS = {0: "trunked sites / talkgroups", 1: "county frequencies", 2: "statewide frequencies", 3: "nationwide frequencies"}
RR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ ()-]{0,75}\.csv$", re.I)


def rr_kind(data):
    """What kind of RadioReference export these bytes are, by the header row: the same rules as area.rr_kind
    (which decides what the bridge loads; a test holds the two together). 9 = not one the bridge reads."""
    try:
        first = data[:8192].decode("utf-8-sig").splitlines()[0]
        head = [h.strip() for h in next(csv.reader([first]), [])]
    except (UnicodeDecodeError, IndexError, csv.Error):
        return 9
    if ("Frequencies" in head and ("Site Dec" in head or "Site Hex" in head)) or ("Decimal" in head and "Alpha Tag" in head):
        return 0
    if "Frequency Output" not in head:
        return 9
    if "Nationwide Agency" in head:
        return 3
    return 2 if "County" in head else 1


def rr_safe_name(name):
    """The file name an upload is stored under: its last path part (a browser may send a whole path), with
    anything but letters, digits, space and . _ ( ) - made an underscore. "" when no .csv name is left."""
    base = re.split(r"[\\/]", name or "")[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._ ()-]", "_", base).lstrip(". ")
    return base if RR_NAME_RE.match(base) else ""


def rr_dir():
    """RR_DIR, created if missing; refuses a symlink (the page runs as root and writes what it is sent)."""
    os.makedirs(RR_DIR, mode=0o755, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(RR_DIR).st_mode):
        raise OSError(f"{RR_DIR} is not a directory")
    return RR_DIR


def rr_list():
    """[{name, kind, label, rows, size, mtime}] for the exports in RR_DIR, by name."""
    out = []
    try:
        names = sorted(n for n in os.listdir(RR_DIR) if n.lower().endswith(".csv"))
    except OSError:
        return out
    for n in names:
        path = os.path.join(RR_DIR, n)
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                continue
            with open(path, "rb") as f:
                data = f.read(MAX_RR_FILE)
        except OSError:
            continue
        k = rr_kind(data)
        out.append({"name": n, "kind": k, "label": RR_KINDS.get(k, "not a RadioReference export: ignored"),
                    "rows": max(0, len(data.splitlines()) - 1), "size": st.st_size, "mtime": st.st_mtime})
    return out


def rr_save(name, data):
    """Check one uploaded export and store it in RR_DIR, whole or not at all. Returns (ok, message)."""
    shown = re.split(r"[\\/]", name or "")[-1][:80] or "(no name)"
    safe = rr_safe_name(name)
    if not safe:
        return False, f"{shown}: not a .csv file."
    if not data.strip():
        return False, f"{shown}: the file is empty."
    if len(data) > MAX_RR_FILE:
        return False, f"{shown}: larger than {MAX_RR_FILE // (1024 * 1024)} MB."
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False, f"{shown}: not a text file (RadioReference exports are UTF-8 CSV)."
    k = rr_kind(data)
    if k == 9:
        return False, (f"{shown}: not a RadioReference export the Scanner reads. It takes the CSV downloads of a "
                       "county's, state's or the nationwide frequencies, a trunked system's sites, and its talkgroups.")
    d = rr_dir()
    dest = os.path.join(d, safe)
    replacing = os.path.lexists(dest)
    if replacing and not stat.S_ISREG(os.lstat(dest).st_mode):
        return False, f"{shown}: something that is not a file already has that name."
    if not replacing and len([n for n in os.listdir(d) if n.lower().endswith(".csv")]) >= MAX_RR_FILES:
        return False, f"{shown}: there are already {MAX_RR_FILES} exports here; delete one first."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".up-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True, f"{safe}: {RR_KINDS[k]}{' (replaced)' if replacing else ''}."


def rr_delete(name):
    """Delete one export by the name the page listed. Returns (ok, message)."""
    if name not in [e["name"] for e in rr_list()]:
        return False, "No such export."
    try:
        os.unlink(os.path.join(RR_DIR, name))
    except OSError as e:
        return False, f"Could not delete {name}: {e.strerror}."
    return True, f"{name} deleted."
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
    # A data file the settings name (--fcc-pack, --rr-pack) that is not there. Bridges from 2026.11.10.8 skip
    # it and start; older ones stop here. Before the next entry, which used to claim it for itself and send
    # people to reinstall software that was all present.
    (re.compile(r"No such file or directory: '[^']+\.(?:json|csv)'"),
     "The scanner's settings name a data file that isn't on this Pi.",
     "settings"),
    (re.compile(r"No such file or directory: '/usr/local/lib/wavelens/[^']*\.(?:py|sh)'|run-bridge.sh: not found|can't open file"),
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
    "settings": "Copy that file to the path shown below, or open <b>Settings</b> and remove the <code>--fcc-pack</code> / "
                "<code>--rr-pack</code> entry that names it. Then tap <b>Restart the scanner</b>. Installing an update will not help: "
                "no software is missing.",
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
    started = 0.0
    m = re.match(r"update: (apply|rollback) started (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", lines[0])
    if m:
        try:
            started = time.mktime(time.strptime(m.group(2), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            pass
    return {"running": running, "title": m.group(1) if m else "update", "seq": snap["seq"], "started": started,
            "exit": None if running else (1 if failed else 0), "lines": lines}


def update_status_json():
    """/api/update/log: the progress lines plus what the Update page's header shows."""
    snap = update_log_snapshot()
    u = read_json(UPDATE_JSON) or {}
    snap.update(now=time.time(), installed=u.get("installed") or installed_version(), available=u.get("available"),
                last_result=u.get("last_result") or "", checked_at=u.get("checked_at"),
                can_rollback=os.path.isdir(PREV_DIR), prev=read_text(os.path.join(PREV_DIR, "VERSION")).strip())
    return snap


# --------------------------------------------------------------------------------------------
# What is plugged in and how the Pi itself is doing: the Status page's diagram, the Diagnostics page
# and the support report. Read from sysfs, /proc and vcgencmd; nothing here opens a radio, so the
# bridge is never disturbed.
# --------------------------------------------------------------------------------------------
USB_KINDS = {   # (vendor, product or "*") -> (kind, label)
    ("0bda", "2838"): ("rtl", "RTL-SDR"), ("0bda", "2832"): ("rtl", "RTL-SDR"),
    ("1d50", "6089"): ("hackrf", "HackRF One"), ("1d50", "cc15"): ("hackrf", "HackRF (rad1o)"),
    ("10c4", "ea60"): ("esp32", "ESP32 (CP210x serial)"), ("1a86", "7523"): ("esp32", "ESP32 (CH340 serial)"),
    ("1a86", "55d4"): ("esp32", "ESP32 (CH9102 serial)"), ("303a", "*"): ("esp32", "ESP32 (native USB)"),
    ("0403", "6001"): ("serial", "FTDI serial"), ("0403", "6015"): ("serial", "FTDI serial"),
    ("1546", "*"): ("gps", "u-blox GPS"), ("067b", "2303"): ("gps", "PL2303 serial / GPS"),
    ("0bda", "8179"): ("wifi", "Realtek Wi-Fi"), ("0bda", "8812"): ("wifi", "Realtek Wi-Fi"),
    ("0e8d", "7961"): ("wifi", "MediaTek Wi-Fi"), ("148f", "*"): ("wifi", "Ralink / MediaTek Wi-Fi"),
}
_CACHE = {}


def cached(key, ttl, fn):
    """fn() at most once every ttl seconds (the Status page polls; nmcli and vcgencmd are not free)."""
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.time(), val)
    return val


def _sys(path, default=""):
    return read_text(path, default).strip()


def vcgencmd(*args):
    if not shutil.which("vcgencmd"):
        return None
    rc, out = run(["vcgencmd", *args], timeout=5)
    return out if rc == 0 else None


THROTTLE_BITS = [(0, "under-voltage now"), (1, "CPU speed capped now"), (2, "throttled now"), (3, "temperature limit now"),
                 (16, "under-voltage since boot"), (17, "speed capped since boot"), (18, "throttled since boot"),
                 (19, "temperature limit since boot")]


def throttled():
    """{'raw', 'flags': [...], 'now': bool, 'ever': bool} or None when vcgencmd is not there."""
    out = vcgencmd("get_throttled")
    if not out or "=" not in out:
        return None
    try:
        v = int(out.split("=", 1)[1], 16)
    except ValueError:
        return None
    flags = [t for b, t in THROTTLE_BITS if v >> b & 1]
    return {"raw": f"0x{v:x}", "flags": flags, "now": bool(v & 0xF), "ever": bool(v & 0xF0000),
            "undervolt": bool(v & 0x10001)}


def power_rails():
    """Voltages and currents. A Pi 5 reports every rail of its PMIC (pmic_read_adc); older Pis only the core."""
    rails = {}
    out = vcgencmd("pmic_read_adc") or ""
    for line in out.splitlines():
        m = re.match(r"\s*(\S+)\s+(volt|current)\(\d+\)=([\d.]+)", line)
        if m:
            rails.setdefault(m.group(1).rsplit("_", 1)[0], {})["V" if m.group(2) == "volt" else "A"] = float(m.group(3))
    if not rails:
        for part in ("core", "sdram_c", "sdram_i", "sdram_p"):
            o = vcgencmd("measure_volts", part) or ""
            m = re.search(r"([\d.]+)V", o)
            if m:
                rails[part.upper()] = {"V": float(m.group(1))}
    return rails


def psu_max_ma():
    """The supply's current limit a Pi 5 negotiated (5000 with the official 27 W supply), or None."""
    try:
        with open("/proc/device-tree/chosen/power/max_current", "rb") as f:
            b = f.read(4)
        return int.from_bytes(b, "big") if len(b) == 4 else None
    except OSError:
        return None


def meminfo():
    d = {}
    for line in read_text("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        try:
            d[k] = int(v.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    return d


def cpu_percent():
    """Whole-CPU busy % over ~0.2 s."""
    def snap():
        try:
            f = [int(x) for x in read_text("/proc/stat").splitlines()[0].split()[1:]]
            return sum(f), f[3] + (f[4] if len(f) > 4 else 0)
        except (ValueError, IndexError):
            return 0, 0
    t0, i0 = snap()
    time.sleep(0.2)
    t1, i1 = snap()
    return round(100.0 * (1 - (i1 - i0) / (t1 - t0)), 1) if t1 > t0 else None


def top_processes(n=6):
    rc, out = run(["ps", "-eo", "pid,pcpu,pmem,rss,comm", "--sort=-pcpu", "--no-headers"], timeout=5)
    rows = []
    for line in out.splitlines()[:n]:
        p = line.split(None, 4)
        if len(p) == 5:
            rows.append({"pid": p[0], "cpu": p[1], "mem": p[2], "rss_mb": round(int(p[3]) / 1024, 1) if p[3].isdigit() else 0,
                         "name": p[4]})
    return rows


def system_facts():
    m = meminfo()
    try:
        temp = int(_sys("/sys/class/thermal/thermal_zone0/temp")) / 1000
    except ValueError:
        temp = None
    fan = None
    for f in glob.glob("/sys/devices/platform/cooling_fan/hwmon/*/fan1_input"):
        try:
            fan = int(_sys(f, "0"))
        except ValueError:
            pass
    try:
        cur = int(_sys("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "0")) // 1000
        mx = int(_sys("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "0")) // 1000
    except ValueError:
        cur = mx = 0
    try:
        du = shutil.disk_usage("/")
        disk = {"free": du.free, "total": du.total}
    except OSError:
        disk = {}
    _, root = run(["findmnt", "-n", "-o", "SOURCE,OPTIONS", "/"], timeout=5)
    try:
        load = os.getloadavg()
    except OSError:
        load = (0, 0, 0)
    return {"model": _sys("/proc/device-tree/model").rstrip("\x00") or "Linux computer",
            "cores": os.cpu_count() or 1, "load": [round(x, 2) for x in load], "cpu_pct": cpu_percent(),
            "mhz": cur, "mhz_max": mx, "temp": temp, "fan_rpm": fan,
            "mem_total": m.get("MemTotal", 0), "mem_avail": m.get("MemAvailable", 0),
            "swap_total": m.get("SwapTotal", 0), "swap_free": m.get("SwapFree", 0),
            "disk": disk, "root": root.split()[0] if root else "", "root_ro": bool(root) and "ro" in root.split()[-1].split(","),
            "uptime": float((read_text("/proc/uptime", "0").split() or ["0"])[0]),
            "os": next((l.split("=", 1)[1].strip('"') for l in read_text("/etc/os-release").splitlines()
                        if l.startswith("PRETTY_NAME=")), ""),
            "kernel": os.uname().release, "python": sys.version.split()[0],
            "throttled": throttled(), "rails": power_rails(), "psu_ma": psu_max_ma()}


def usb_devices():
    """Every USB device but the root hubs: bus/port address, ids, names, speed, the power it asked for, its tty."""
    out = []
    for d in sorted(glob.glob("/sys/bus/usb/devices/*")):
        name = os.path.basename(d)
        if not re.match(r"^\d+-[\d.]+$", name):
            continue                                   # root hubs (usb1) and interfaces (1-1:1.0)
        vid, pid = _sys(d + "/idVendor"), _sys(d + "/idProduct")
        if not vid:
            continue
        kind, label = USB_KINDS.get((vid, pid)) or USB_KINDS.get((vid, "*")) or (
            ("hub", "USB hub") if _sys(d + "/bDeviceClass") == "09" else ("other", ""))
        ttys = sorted({os.path.basename(t) for t in glob.glob(d + "/*/tty*") + glob.glob(d + "/*/tty/tty*")
                       if os.path.basename(t).startswith("tty") and os.path.basename(t) != "tty"})
        try:
            ma = int(re.sub(r"\D", "", _sys(d + "/bMaxPower", "0")) or 0)
        except ValueError:
            ma = 0
        drv = ""
        for i in glob.glob(d + f"/{name}:*/driver"):
            drv = os.path.basename(os.path.realpath(i))
            break
        out.append({"port": name, "bus": _sys(d + "/busnum"), "dev": _sys(d + "/devnum"), "id": f"{vid}:{pid}",
                    "kind": kind, "label": label, "maker": _sys(d + "/manufacturer"), "product": _sys(d + "/product"),
                    "serial": _sys(d + "/serial"), "speed": _sys(d + "/speed"), "ma": ma, "tty": ttys, "driver": drv})
    return out


def bluetooth_info():
    out = {"present": bool(glob.glob("/sys/class/bluetooth/hci*")), "addr": "", "powered": None, "name": ""}
    if not out["present"] or not shutil.which("bluetoothctl"):
        return out
    _, txt = run(["bluetoothctl", "show"], timeout=5)
    m = re.search(r"Controller ([0-9A-F:]{17})", txt)
    out["addr"] = m.group(1) if m else ""
    m = re.search(r"Powered: (yes|no)", txt)
    out["powered"] = (m.group(1) == "yes") if m else None
    m = re.search(r"\n\s*Alias: (.+)", txt)
    out["name"] = m.group(1).strip() if m else ""
    return out


def network_info():
    nets = []
    _, js = run(["ip", "-j", "addr"], timeout=5)
    try:
        addrs = json.loads(js or "[]")
    except ValueError:
        addrs = []
    for a in addrs:
        n = a.get("ifname", "")
        if n == "lo" or n.startswith(("docker", "veth", "br-")):
            continue
        nets.append({"name": n, "mac": a.get("address", ""), "up": a.get("operstate") == "UP",
                     "ipv4": [x["local"] for x in a.get("addr_info", []) if x.get("family") == "inet"],
                     "wifi": os.path.isdir(f"/sys/class/net/{n}/wireless")})
    _, w = run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,FREQ,DEVICE", "dev", "wifi"], timeout=8)
    for line in w.splitlines():
        p = line.replace("\\:", "\x00").split(":")
        if len(p) >= 5 and p[0] == "yes":
            for n in nets:
                if n["name"] == p[4]:
                    n.update(ssid=p[1].replace("\x00", ":"), signal=int(p[2]) if p[2].isdigit() else None, freq=p[3])
    return nets


def radio_roles(st):
    """serial -> role, from the bridge's status file (newer bridges), else parsed from its hardware line."""
    roles = {}
    for r in ((st.get("hw") or {}).get("rtl") or []):
        roles[r.get("serial") or ""] = r.get("role") or ""
    if not roles:
        m = re.search(r"RTL-SDR \(([^)]*)\)", st.get("hw_line") or "")
        for part in (m.group(1).split(", ") if m else []):
            sn, _, role = part.rpartition(": ")
            roles[sn] = role
    return roles


ROLE_WORDS = {"sweep": "scanning the bands", "listen": "listening (audio)", "sensors": "sensor decoder (rtl_433)",
              "free": "idle", "busy": "in use", "dump1090": "ADS-B (dump1090)", "readsb": "ADS-B (readsb)"}


def devices_snapshot():
    """Everything the Status diagram draws, in one JSON-able dict."""
    st = read_json(STATUS_JSON) or {}
    sysf = cached("sys", 4, system_facts)
    usb = cached("usb", 4, usb_devices)
    roles = radio_roles(st)
    spot = {s.get("id") or "": s for s in (st.get("spotters") or [])}
    spot_list = list(spot.values())
    for u in usb:
        if u["kind"] == "rtl":
            r = roles.get(u["serial"], "")
            u["role"] = ROLE_WORDS.get(r, r) or "not used by the scanner"
        elif u["kind"] == "esp32":
            s = spot.get(u["serial"]) or spot.get(u["port"]) or next(
                (x for x in spot_list if x.get("u") and (x["u"] == u["port"] or x["u"] in u["tty"])), None)
            if s:
                u["role"] = f"Spotter {s.get('name') or ''} · {s.get('role_label') or s.get('msg') or ''}".strip()
                u["ok"] = bool(s.get("ok"))
                u["ver"] = s.get("ver") or ""
    psu = sysf.get("psu_ma")
    # a Pi 5 gives its USB ports 600 mA in all unless the supply said it can do 5 A (or usb_max_current_enable=1)
    usb_limit = None
    if psu is not None:
        usb_limit = 1600 if psu >= 5000 or "usb_max_current_enable=1" in (vcgencmd("get_config", "usb_max_current_enable") or "") else 600
    return {"at": int(time.time()), "host": hostname(), "version": installed_version(), "sys": sysf, "usb": usb,
            "usb_ma": sum(u["ma"] for u in usb if u["kind"] != "hub"), "usb_limit_ma": usb_limit,
            "bt": cached("bt", 15, bluetooth_info), "net": cached("net", 10, network_info),
            "bridge": {"state": unit_state(BRIDGE_UNIT)[0], "linked": status_linked(st) if st else False,
                       "name": st.get("name") or "", "code": st.get("code") or "", "fw": st.get("fw") or "",
                       "missing": st.get("hw_missing") or [], "at": st.get("at")}}


# ---- diagnostics: each check is (id, title, level ok|warn|bad|info, what it found, what to do)
def _check(cid, title, level, found, fix=""):
    return {"id": cid, "title": title, "level": level, "found": found, "fix": fix}


def gb(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def bridge_rtl_opened():
    """How many RTL-SDRs the bridge said it found at its last start (its "auto: … N RTL-SDR(s)" line)."""
    _, out = run(["journalctl", "-u", BRIDGE_UNIT, "-b", "--no-pager", "-o", "cat", "-n", "20000"], timeout=15)
    found = re.findall(r"auto: HackRF .*?(\d+) RTL-SDR\(s\)", out)
    return int(found[-1]) if found else None


def kernel_events():
    """Problems the kernel logged this boot: USB devices dropping off, SD card errors, out-of-memory kills."""
    _, out = run(["journalctl", "-k", "-b", "--no-pager", "-o", "short-monotonic", "-n", "4000"], timeout=15)
    ev = {"usb_drop": [], "mmc": [], "oom": [], "voltage": []}
    for l in out.splitlines():
        if "USB disconnect" in l:
            ev["usb_drop"].append(l)
        elif re.search(r"mmc\d.*(error|timeout)|I/O error|EXT4-fs error", l):
            ev["mmc"].append(l)
        elif "Out of memory" in l or "oom-kill" in l:
            ev["oom"].append(l)
        elif "Undervoltage" in l or "under-voltage" in l.lower():
            ev["voltage"].append(l)
    return ev


def internet_ok():
    try:
        req = urllib.request.Request("https://github.com/", method="HEAD", headers={"User-Agent": "wavelens-admin"})
        with urllib.request.urlopen(req, timeout=8):   # noqa: S310 - fixed https URL
            return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(getattr(e, "reason", e))[:120]


def run_diagnostics(deep=False):
    """The Diagnostics page's checks, worst first. deep=True adds the slow ones (internet reachability)."""
    snap = devices_snapshot()
    s, st = snap["sys"], read_json(STATUS_JSON) or {}
    checks = []
    h = bridge_health()
    checks.append(_check("bridge", "Scanner software", "ok" if h["ok"] else "bad",
                         f"running ({unit_state(BRIDGE_UNIT)[1] or 'just started'})" if h["ok"] else h["title"],
                         "" if h["ok"] else re.sub(r"<[^>]+>", "", h["fix"])))
    t = s.get("throttled")
    if t is None:
        checks.append(_check("power", "Power supply", "info", "this computer doesn't report its power"))
    elif t["undervolt"]:
        checks.append(_check("power", "Power supply", "bad", "under-voltage " + ("right now" if t["now"] else "since boot")
                             + f" ({t['raw']})", "Use a stronger supply and a short, thick cable: 5 V / 5 A for a Pi 5, "
                             "5 V / 3 A for a Pi 4. Radio dongles drop out when the voltage sags; software can't fix it."))
    elif t["flags"]:
        checks.append(_check("power", "Power and heat", "warn", ", ".join(t["flags"]), "Usually heat: give the Pi a fan or a heatsink case."))
    else:
        v = (s.get("rails") or {}).get("EXT5V", {}).get("V")
        checks.append(_check("power", "Power supply", "ok", "no under-voltage since boot" + (f" · input {v:.2f} V" if v else "")))
    if snap.get("usb_limit_ma") and snap["usb_ma"] > snap["usb_limit_ma"]:
        checks.append(_check("usbpower", "USB power budget", "warn",
                             f"devices ask for {snap['usb_ma']} mA, the Pi allows {snap['usb_limit_ma']} mA on its USB ports",
                             "Use the official 27 W (5 V / 5 A) supply, or a powered USB hub for the dongles."))
    temp = s.get("temp")
    if temp is not None:
        lvl = "bad" if temp >= 80 else "warn" if temp >= 70 else "ok"
        checks.append(_check("temp", "CPU temperature", lvl, f"{temp:.1f} °C" + (f" · fan {s['fan_rpm']} rpm" if s.get("fan_rpm") else ""),
                             "" if lvl == "ok" else "Above 80 °C the Pi slows itself down. Add a fan, keep it out of direct sun."))
    cpu = s.get("cpu_pct")
    load1 = s["load"][0] if s.get("load") else 0
    lvl = "warn" if load1 > s["cores"] * 1.5 else "ok"
    checks.append(_check("cpu", "CPU", lvl, f"{cpu if cpu is not None else '?'} % busy · load {load1} on {s['cores']} cores · "
                         f"{s.get('mhz')} of {s.get('mhz_max')} MHz",
                         "" if lvl == "ok" else "Something is using a lot of CPU: see the busiest programs below."))
    if s.get("mem_total"):
        pct = 100 * (1 - s["mem_avail"] / s["mem_total"])
        lvl = "bad" if pct > 92 else "warn" if pct > 80 else "ok"
        checks.append(_check("mem", "Memory", lvl, f"{pct:.0f} % used · {gb(s['mem_avail'])} free of {gb(s['mem_total'])}"
                             + (f" · swap {gb(s['swap_total'] - s['swap_free'])} used" if s.get("swap_total") else ""),
                             "" if lvl == "ok" else "Restart the scanner; if it climbs again, send a support log."))
    d = s.get("disk") or {}
    if d:
        lvl = "bad" if d["free"] < 0.5e9 else "warn" if d["free"] < 2e9 else "ok"
        checks.append(_check("disk", "Storage", lvl, f"{gb(d['free'])} free of {gb(d['total'])} on {s.get('root') or '/'}",
                             "" if lvl == "ok" else "Free space: delete old files, or use a bigger card. Updates need about 200 MB."))
    if s.get("root_ro"):
        checks.append(_check("ro", "Storage", "bad", "the card is mounted read-only", "The SD card has errors: back up your settings and replace it."))
    ev = cached("kev", 30, kernel_events)
    if ev["mmc"]:
        checks.append(_check("mmc", "SD card", "bad", f"{len(ev['mmc'])} card errors this boot", "Replace the SD card soon."))
    if ev["oom"]:
        checks.append(_check("oom", "Memory", "bad", f"a program was killed for lack of memory ({len(ev['oom'])}×)", "Restart the scanner and send a support log."))
    rtls = [u for u in snap["usb"] if u["kind"] == "rtl"]
    opened = bridge_rtl_opened()
    if not rtls:
        checks.append(_check("radios", "Radios", "bad", "no RTL-SDR on the USB bus", "Plug the dongles in (straight into the Pi, or a powered hub)."))
    elif opened is not None and opened < len(rtls):
        checks.append(_check("radios", "Radios", "warn", f"{len(rtls)} RTL-SDR on USB, the scanner opened {opened} at its last start",
                             "Restart the scanner. If one still won't open, check the power first, then reboot."))
    else:
        checks.append(_check("radios", "Radios", "ok", f"{len(rtls)} RTL-SDR on USB" + (f", all {opened} opened" if opened else "")))
    drops = [l for l in ev["usb_drop"]]
    if drops:
        checks.append(_check("usbdrop", "USB dropouts", "warn" if len(drops) < 5 else "bad",
                             f"{len(drops)} USB device disconnect(s) this boot", "Loose cable, or not enough power. Check the Power line above."))
    _, mods = run(["lsmod"], timeout=5)
    if re.search(r"^dvb_usb_rtl28xxu\b", mods, re.M):
        checks.append(_check("dvb", "TV tuner driver", "warn", "the kernel's DVB-T driver is loaded and can take a dongle",
                             "Reboot (it is blocked from the next start)."))
    for m in snap["bridge"]["missing"]:
        checks.append(_check("hw", "Hardware", "warn", m))
    bt = snap["bt"]
    if not bt["present"]:
        checks.append(_check("bt", "Bluetooth", "bad", "no Bluetooth adapter", "Reboot. The phone needs Bluetooth to connect."))
    elif bt["powered"] is False:
        checks.append(_check("bt", "Bluetooth", "bad", "adapter is off", "Reboot."))
    else:
        checks.append(_check("bt", "Bluetooth", "ok", f"adapter {bt['addr'] or 'on'}" +
                             (" · phone connected" if snap["bridge"]["linked"] else " · no phone connected")))
    wifi = next((n for n in snap["net"] if n.get("ssid")), None)
    if wifi:
        sig = wifi.get("signal")
        checks.append(_check("wifi", "Wi-Fi", "warn" if sig is not None and sig < 35 else "ok",
                             f"{wifi['ssid']} · signal {sig if sig is not None else '?'} % · {', '.join(wifi['ipv4']) or 'no address'}",
                             "Weak signal: move the Pi closer to the router." if sig is not None and sig < 35 else ""))
    elif any(n["up"] and n["ipv4"] for n in snap["net"]):
        checks.append(_check("wifi", "Network", "ok", "wired: " + ", ".join(ip for n in snap["net"] for ip in n["ipv4"])))
    else:
        checks.append(_check("wifi", "Network", "info", "not on a network (fine in a car)", "Updates and area downloads need Wi-Fi."))
    if deep:
        ok, why = internet_ok()
        checks.append(_check("internet", "Internet (for updates)", "ok" if ok else "warn",
                             "GitHub reachable" if ok else f"GitHub not reachable ({why})",
                             "" if ok else "Updates and area downloads won't work until the Pi is online."))
    _, ntp = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=5)
    if ntp.strip() == "no":
        checks.append(_check("clock", "Clock", "info", "not synchronised (no network time yet)", "Times in logs may be off until the Pi is online."))
    a = read_json(AREA_FILE) or {}
    if a.get("country"):
        checks.append(_check("area", "Area", "ok", f"{a.get('country')} {a.get('postcode') or ''} · {a.get('licences', 0)} licensed frequencies"
                             + (" · follow me on" if a.get("follow") else "")))
    else:
        checks.append(_check("area", "Area", "warn", "no area set: carriers are named by band only", "Set it on the Area page."))
    if not shutil.which("dsd-fme"):
        checks.append(_check("dsd", "Digital voice", "info", "dsd-fme is not installed: P25 / DMR calls can't be heard",
                             "Ask support, or run companion/pi/install-dsd.sh."))
    else:
        checks.append(_check("dsd", "Digital voice", "ok", "dsd-fme installed"))
    uj = read_json(UPDATE_JSON) or {}
    if uj.get("available"):
        checks.append(_check("update", "Software", "warn", f"update {uj['available']} is available (installed {installed_version()})", "Install it on the Update page."))
    else:
        checks.append(_check("update", "Software", "ok", f"{installed_version()}" + (f" · {uj.get('last_result')}" if uj.get("last_result") else "")))
    for p in (st.get("problems") or [])[:4]:
        checks.append(_check("data", "Local data", "warn", p))
    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    checks.sort(key=lambda c: order[c["level"]])
    return {"at": int(time.time()), "checks": checks, "snap": snap, "top": top_processes(),
            "events": {k: v[-5:] for k, v in ev.items()},
            "summary": {k: sum(1 for c in checks if c["level"] == k) for k in order}}


def support_report():
    """Plain text for support: the checks, the hardware and the last bridge lines. No passwords, no Wi-Fi keys."""
    d = run_diagnostics(deep=True)
    out = [f"Wavelens support report · {time.strftime('%Y-%m-%d %H:%M:%S')} · {d['snap']['host']} · v{d['snap']['version']}", ""]
    for c in d["checks"]:
        out.append(f"[{c['level'].upper():4}] {c['title']}: {c['found']}" + (f"  → {c['fix']}" if c["fix"] else ""))
    s = d["snap"]["sys"]
    out += ["", f"{s['model']} · {s['os']} · kernel {s['kernel']} · Python {s['python']}",
            "rails: " + ", ".join(k + "".join(f" {v[u]:.3f}{u}" for u in ("V", "A") if u in v) for k, v in s["rails"].items()),
            f"supply limit: {s.get('psu_ma')} mA · USB asks {d['snap']['usb_ma']} mA of {d['snap']['usb_limit_ma']}", "", "USB:"]
    for u in d["snap"]["usb"]:
        out.append(f"  {u['port']:8} bus {u['bus']} dev {u['dev']} {u['id']} {u['label'] or u['product']} "
                   f"sn={u['serial'] or '-'} {u['speed']} Mb/s {u['ma']} mA {' '.join(u['tty'])} {u.get('role', '')}")
    out += ["", "Busiest programs:"] + [f"  {p['cpu']:>5}% cpu {p['rss_mb']:>7} MB {p['name']}" for p in d["top"]]
    out += ["", "Kernel events this boot:"] + [f"  {l}" for v in d["events"].values() for l in v]
    out += ["", "Bridge log (last 400 lines):"] + journal_lines(400)
    return "\n".join(out) + "\n"


def recent_events(n=10):
    """The bridge's important lines lately, for the Status page: radios, area, updates, errors."""
    keep = re.compile(r"auto: |advertising as|area|RadioReference overlay|FCC pack loaded|hardware|update|error|Error|"
                      r"Traceback|left out|connected|disconnected", re.I)
    return [l for l in journal_lines(600) if keep.search(l)][-n:]


def update_history(n=10):
    out = []
    for line in read_text("/var/lib/wavelens/update-history.jsonl").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return list(reversed(out))


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
input[type=number],input[type=file]{padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit;max-width:100%}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}table.list th{width:auto;white-space:nowrap}table.list td{white-space:nowrap}table.list td.wrap{white-space:normal;min-width:160px}
td.num{font-variant-numeric:tabular-nums;font-family:ui-monospace,Menlo,Consolas,monospace}
.pill{display:inline-block;padding:1px 8px;border:1px solid var(--line);border-radius:999px;font-size:12px;color:var(--mut)}.pill.lock{border-color:var(--bad);color:var(--bad)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:0 14px}.check{display:flex;align-items:center;gap:8px;color:var(--fg);font-size:16px}
.check input{width:20px;height:20px}.row.end{align-items:flex-end}.row.end>div{flex:1 1 140px}
.spill{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;border:1px solid var(--line);font-size:14px;font-weight:600}
.spill.okp{border-color:var(--ok);color:var(--ok)}.spill.warnp{border-color:var(--acc);color:var(--acc)}.spill.badp{border-color:var(--bad);color:var(--bad)}
.spill.act{border-color:#7fb2ff;color:#7fb2ff}
.spin{display:inline-block;width:12px;height:12px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:sp 0.8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
ol.steps{list-style:none;padding:0;margin:12px 0}ol.steps li{display:flex;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line)}
ol.steps .mk{display:inline-flex;width:24px;height:24px;border-radius:50%;align-items:center;justify-content:center;border:1px solid var(--line);flex:none;font-size:13px}
ol.steps .done .mk{background:var(--ok);border-color:var(--ok);color:#111}ol.steps .fail .mk{background:var(--bad);border-color:var(--bad);color:#111}
ol.steps .act{color:#7fb2ff;font-weight:600}ol.steps .act .mk{border-color:#7fb2ff}ol.steps .todo,ol.steps .skip{color:var(--mut)}ol.steps .skip{text-decoration:line-through}
.res{padding:12px 14px;border-radius:8px;margin-top:10px}.res.ok{background:#16281e;border:1px solid var(--ok)}.res.warn{background:#2a2f1a;border:1px solid var(--acc)}
.res.bad{background:#2d1d1d;border:1px solid var(--bad)}
#diagram{width:100%;min-height:200px}#diagram svg{width:100%;height:auto;display:block;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
ul.checks{list-style:none;padding:0;margin:0}ul.checks li{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)}
ul.checks .ic{flex:none;width:22px;height:22px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#111}
ul.checks .ok .ic{background:var(--ok)}ul.checks .warn .ic{background:var(--acc)}ul.checks .bad .ic{background:var(--bad)}ul.checks .info .ic{background:#7fb2ff}
.meter{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin-top:4px}.meter b{display:block;height:100%;background:var(--ok)}
.meter b.w{background:var(--acc)}.meter b.x{background:var(--bad)}
.stat{font-size:22px;font-weight:700}.small{font-size:13px}
.tbl{overflow-x:auto}.tbl table{font-size:13px;min-width:560px}.tbl th{width:auto}
"""

NAV = [("/", "Status"), ("/diagnostics", "Diagnostics"), ("/logs", "Logs"), ("/settings", "Settings"), ("/area", "Area"), ("/frequencies", "Frequencies"), ("/update", "Update"),
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



UPDATE_JS = """
<script>
(function(){
  var STEPS={
    apply:[['fetch','Check GitHub for the new version',/checking GitHub|updating \\S+ -> /],
           ['save','Save the working version to fall back to',/runs well before saving|saved a copy|keeping the saved copy/],
           ['install','Download and install the new files',/unpacking|installing the new files|updater changed/],
           ['verify','Check it loads on this Pi',/loads on this Pi/],
           ['fw','Spotter firmware for the USB flasher',/Spotter firmware/],
           ['restart','Restart the scanner',/restarting the scanner/],
           ['health','Confirm the scanner is running',/waiting for the scanner/],
           ['done','Finished',/done: .* is running|already on |is ahead of the published/]],
    rollback:[['restore','Put the saved version back',/putting the saved version/],
              ['health','Confirm the scanner is running',/waiting for the scanner/],
              ['done','Finished',/rolled back to/]],
    check:[['fetch','Ask GitHub for the latest version',/checking GitHub|wavelens-update check/],
           ['done','Compare with this Pi',/installed .* available|up to date|is ahead of the published/]]};
  var u=window.UPD, base=0, seen=0, fails=0, $=function(i){return document.getElementById(i);};
  function action(j){
    var l=(j.lines||[]).join('\\n');
    if(/wavelens-update rollback|rollback started/.test(l)||j.title==='rollback') return 'rollback';
    if(/wavelens-update check/.test(l)||j.title==='check') return 'check';
    return 'apply';
  }
  function clean(l){return l.replace(/^update: /,'').replace(/^\\$ .*/,'');}
  function fmt(s){s=Math.max(0,Math.round(s));return (s>=60?Math.floor(s/60)+' min ':'')+(s%60)+' s';}
  function upto(lines, steps){ var last=-1; lines.forEach(function(l){ steps.forEach(function(s,k){ if(s[2].test(l)&&k>last) last=k; }); }); return last; }
  function render(j){
    var lines=j.lines||[], act=action(j), all=lines.join('\\n');
    var ended=!j.running && j.exit!==null && j.exit!==undefined;
    var good=ended && (j.exit===0 || (act==='check' && j.exit===10));
    var items=[], rb=-1;
    lines.forEach(function(l,i){ if(rb<0 && act==='apply' && /putting the saved version back/.test(l)) rb=i; });
    function add(steps, last, failLast){
      steps.forEach(function(s,k){
        var c = k<last ? 'done' : k===last ? (ended ? (failLast?'fail':(good?'done':'fail')) : (failLast?'fail':'act')) : (ended?'skip':'todo');
        items.push([c, s[1]]);
      });
    }
    if(rb>=0){
      var before=STEPS.apply.slice(0, Math.max(1, upto(lines.slice(0,rb), STEPS.apply)+1));
      add(before, before.length-1, true);
      var after=lines.slice(rb), back=/rolled back to/.test(after.join('\\n')), alive=/rolled back to .*the bridge is running/.test(after.join('\\n'));
      items.push([back||ended?'done':'act', 'Put the saved version back']);
      items.push([alive?'done':(back||ended)?'fail':(/waiting for the scanner/.test(after.join('\\n'))?'act':'todo'), 'Confirm the saved version is running']);
    } else if(lines.length){
      var st=STEPS[act], last=upto(lines, st);
      if(good) last=st.length-1;
      add(st, Math.max(last,0), false);
    }
    var mk={done:'✓',fail:'✕',act:'<span class="spin"></span>',todo:'○',skip:'–'};
    $('steps').innerHTML=items.length?items.map(function(it){return '<li class="'+it[0]+'"><span class="mk">'+mk[it[0]]+'</span>'+it[1]+'</li>';}).join('')
      :'<li class="mut">Nothing running. Start a check or an install above.</li>';
    var msg=''; for(var i=lines.length-1;i>=0;i--){ var c=clean(lines[i]); if(c && !/^done \\(exit|^failed \\(exit/.test(c)){msg=c;break;} }
    $('now').textContent=j.running&&msg?('Now: '+msg):'';
    var out=$('out'); if(out.textContent!==all){out.textContent=all;out.scrollTop=out.scrollHeight;}
    base=j.started?(j.now-j.started):0; seen=Date.now();
    $('elapsed').textContent=j.started&&lines.length?(j.running?'running for ':'took ')+fmt(base):'';
    var r=$('result');
    if(ended && lines.length){
      var kind=rb>=0?'warn':(good?'ok':'bad'), text=act==='check'?msg:(j.last_result||msg);
      r.className='res '+kind; r.style.display='block';
      r.innerHTML='<b>'+{ok:'✓',warn:'↺',bad:'✕'}[kind]+'</b> '+esc(text)+
        (kind==='warn'?'<div class="note">The scanner is back on the version that worked. Nothing else needs doing; a fix usually follows in the next update.</div>':'')+
        (kind==='bad'?'<div class="note">Open <a href="/diagnostics">Diagnostics</a>, download the support report and send it to wavelensapp@gmail.com.</div>':'');
    } else r.style.display='none';
    var pill=$('upill');
    if(j.running){pill.className='spill act';pill.innerHTML='<span class="spin"></span> '+({apply:'Updating…',rollback:'Rolling back…',check:'Checking…'})[act];}
    else if(j.available){pill.className='spill warnp';pill.textContent='Update '+j.available+' available';}
    else if(ended && !good){pill.className='spill badp';pill.textContent=rb>=0?'Last update was put back':'Last action failed';}
    else if(ended && rb>=0){pill.className='spill warnp';pill.textContent='Last update was put back';}
    else {pill.className='spill okp';pill.textContent='Up to date';}
    $('vinst').innerHTML='<b>'+esc(j.installed||'')+'</b>'+(j.prev?' <span class="mut">(saved fallback: '+esc(j.prev)+')</span>':'');
    $('vavail').innerHTML=j.available?'<b class="ok">'+esc(j.available)+'</b>':'<span class="mut">none — up to date</span>';
    $('vlast').textContent=j.last_result||'—';
    document.querySelectorAll('#uform button').forEach(function(b){ if(b.value!=='rollback'||j.can_rollback) b.disabled=!!j.running; });
  }
  function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  var timer=null, running=false;
  function tick(){
    fetch('/api/update/log',{credentials:'same-origin',cache:'no-store'}).then(function(r){
      var ct=r.headers.get('content-type')||'';
      if(ct.indexOf('json')<0) throw 'login';
      return r.json();
    }).then(function(j){ fails=0; $('conn').style.display='none'; running=j.running; render(j); })
    .catch(function(e){
      fails++; var c=$('conn'); c.style.display='block';
      if(e==='login') c.innerHTML='Your login ended. <a href="/login">Log in again</a> to keep watching — the update carries on regardless.';
      else c.textContent=(running?'The admin page is restarting with the new version (normal near the end of an update). Reconnecting… ':'Can\\'t reach the Pi. Retrying… ')+(fails>1?'('+fails+')':'');
    }).then(function(){ clearTimeout(timer); timer=setTimeout(tick, running||fails?1000:4000); });
  }
  setInterval(function(){ if(running && base){ $('elapsed').textContent='running for '+fmt(base+(Date.now()-seen)/1000); }},1000);
  render(u); running=u.running; tick();
})();
</script>"""


DIAGRAM_JS = r"""
<script>
(function(){
  var NS='http://www.w3.org/2000/svg';
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function gbs(n){return n>=1e9?(n/1e9).toFixed(1)+' GB':(n/1e6).toFixed(0)+' MB';}
  function clip(t,n){t=String(t||'');return t.length>n?t.slice(0,n-1)+'…':t;}
  var COL={ok:'#5fd38d',warn:'#f2b705',bad:'#ff6b6b',off:'#5a6270',info:'#7fb2ff'};
  function speedLabel(sp){sp=+sp||0;return sp>=10000?'USB 3.2':sp>=5000?'USB 3':sp>=480?'USB 2.0':sp>=12?'USB 1.1':'USB';}
  function rail(d,k){return d.sys.rails&&d.sys.rails[k]?d.sys.rails[k]:null;}
  function nodes(d){
    var s=d.sys, L=[], R=[], t=s.throttled;
    var ext=rail(d,'EXT5V'), core=rail(d,'VDD_CORE')||rail(d,'CORE');
    var pst=t?(t.undervolt?'bad':(t.flags.length?'warn':'ok')):'info';
    var pl=[];
    if(ext&&ext.V) pl.push('Input '+ext.V.toFixed(2)+' V');
    if(s.psu_ma) pl.push('Supply limit '+(s.psu_ma/1000).toFixed(1)+' A');
    if(d.usb_limit_ma) pl.push('USB asks '+d.usb_ma+' of '+d.usb_limit_ma+' mA'); else if(d.usb_ma) pl.push('USB asks '+d.usb_ma+' mA');
    pl.push(t?(t.flags.length?t.flags[0]:'No under-voltage since boot'):'Not reported');
    if(d.usb_limit_ma && d.usb_ma>d.usb_limit_ma && pst==='ok') pst='warn';
    L.push({t:'Power supply',l:pl,st:pst,edge:'5 V'+(ext&&ext.A?' · '+ext.A.toFixed(2)+' A':''),icon:'⚡'});
    var b=d.bridge, bt=d.bt;
    L.push({t:'Phone (Wavelens app)',icon:'📱',st:b.linked?'ok':'off',dash:!b.linked,
            l:[b.linked?'Connected':'Not connected',b.name?'Radio name '+b.name:'',b.code?'Pair code '+b.code:'',bt.addr?'Pi BT '+bt.addr:(bt.present?'':'No Bluetooth adapter')],
            edge:'Bluetooth LE'});
    (d.net||[]).forEach(function(n){
      if(!n.up && !n.wifi) return;
      var w=n.wifi, ghz=n.freq?(parseInt(n.freq)>=5000?' 5 GHz':' 2.4 GHz'):'';
      L.push({t:(w?'Wi-Fi ':'Ethernet ')+n.name,icon:w?'📶':'🔌',st:n.ipv4.length?((n.signal!=null&&n.signal<35)?'warn':'ok'):'off',dash:!n.ipv4.length,
              l:[w?(n.ssid?n.ssid+(n.signal!=null?' · '+n.signal+' %':''):'Not connected'):(n.up?'Link up':'No cable'),n.ipv4.join(', ')||'No address','MAC '+n.mac],
              edge:w?'Wi-Fi'+ghz:'Ethernet'});
    });
    (d.usb||[]).filter(function(u){return u.kind!=='hub';}).forEach(function(u){
      var st=u.kind==='rtl'?(/not used|idle/.test(u.role||'')?'warn':'ok'):u.kind==='esp32'?(u.ok===false?'warn':'ok'):'info';
      R.push({t:(u.label||u.product||u.id),icon:{rtl:'📡',hackrf:'📡',esp32:'🛰',gps:'🧭',wifi:'📶'}[u.kind]||'🔧',st:st,
              l:[u.role||u.product||'',u.serial?'Serial '+u.serial:'', 'Port '+u.port+' · bus '+u.bus+' dev '+u.dev,
                 (u.speed?u.speed+' Mb/s · ':'')+u.ma+' mA'+(u.tty.length?' · /dev/'+u.tty.join(' /dev/'):''), u.ver?'Firmware '+u.ver:''],
              edge:speedLabel(u.speed)+' · '+u.port, full:u});
    });
    if(!R.length) R.push({t:'No USB radios',icon:'⚠',st:'bad',dash:true,l:['Plug the RTL-SDR dongles in'],edge:'USB'});
    var tmp=s.temp, memu=s.mem_total?100*(1-s.mem_avail/s.mem_total):0;
    var pist=(t&&t.undervolt)||tmp>=80?'bad':(tmp>=70||(t&&t.flags.length)||memu>90?'warn':'ok');
    var pi={t:clip((s.model||'Pi').replace(/ Model ([A-Z]) Rev [\d.]+$/,' $1').replace(/ Rev [\d.]+$/,''),30),icon:'',st:pist,big:true,
      l:[d.host+'.local','Wavelens v'+d.version,'Scanner '+(b.state==='active'?'running':b.state),
         'CPU '+(s.cpu_pct!=null?s.cpu_pct+' %':'?')+' · '+(s.mhz||'?')+' MHz',
         'Temp '+(tmp!=null?tmp.toFixed(1)+' °C':'?')+(s.fan_rpm?' · fan '+s.fan_rpm+' rpm':''),
         'RAM '+memu.toFixed(0)+' % of '+gbs(s.mem_total),
         s.disk&&s.disk.total?'Disk '+gbs(s.disk.free)+' free':'',
         core&&core.V?'Core '+core.V.toFixed(3)+' V':'',
         'Up '+Math.floor(s.uptime/86400)+' d '+Math.floor(s.uptime%86400/3600)+' h']};
    [pi].concat(L,R).forEach(function(n){n.l=n.l.filter(function(x){return x;});});
    return {L:L,R:R,pi:pi};
  }
  function el(tag,a,parent){var e=document.createElementNS(NS,tag);for(var k in a)e.setAttribute(k,a[k]);if(parent)parent.appendChild(e);return e;}
  function box(svg,n,x,y,w){
    var h=34+n.l.length*16, g=el('g',{},svg), c=COL[n.st]||COL.off;
    var tt=el('title',{},g); tt.textContent=n.t+'\n'+n.l.join('\n');
    el('rect',{x:x,y:y,width:w,height:h,rx:10,fill:'#171b21',stroke:c,'stroke-width':n.big?2.5:1.5,'stroke-dasharray':n.dash?'5 4':''},g);
    el('rect',{x:x,y:y,width:6,height:h,rx:3,fill:c},g);
    var tx=el('text',{x:x+16,y:y+21,fill:'#e6e8eb','font-size':n.big?15:13,'font-weight':700},g);
    tx.textContent=(n.icon?n.icon+' ':'')+clip(n.t,n.big?28:30);
    var dot=el('circle',{cx:x+w-14,cy:y+16,r:5,fill:c},g);
    n.l.forEach(function(line,i){var t=el('text',{x:x+16,y:y+40+i*16,fill:i===0&&!n.big?'#e6e8eb':'#8b93a1','font-size':12},g);t.textContent=clip(line,n.big?34:36);});
    n.h=h; n.x=x; n.y=y; n.w=w; return h;
  }
  function link(svg,x1,y1,x2,y2,n,vertical){
    var c=COL[n.st]||COL.off, p;
    if(vertical) p='M'+x1+' '+y1+' C'+x1+' '+(y1+y2)/2+' '+x2+' '+(y1+y2)/2+' '+x2+' '+y2;
    else p='M'+x1+' '+y1+' C'+(x1+x2)/2+' '+y1+' '+(x1+x2)/2+' '+y2+' '+x2+' '+y2;
    el('path',{d:p,fill:'none',stroke:c,'stroke-width':2,'stroke-dasharray':n.dash?'6 5':'', opacity:.9},svg);
    if(n.st==='ok' && !n.dash){ var dot=el('circle',{r:3,fill:c},svg); var an=el('animateMotion',{dur:'2.4s',repeatCount:'indefinite',path:p},dot); }
    if(n.edge){ var mx=(x1+x2)/2, my=(y1+y2)/2, w=n.edge.length*6.2+12;
      el('rect',{x:mx-w/2,y:my-9,width:w,height:17,rx:8,fill:'#0f1216',stroke:'#262c35'},svg);
      var t=el('text',{x:mx,y:my+4,'text-anchor':'middle',fill:'#8b93a1','font-size':11},svg); t.textContent=n.edge; }
  }
  function draw(d){
    var host=document.getElementById('diagram'); if(!host) return;
    var N=nodes(d), narrow=host.clientWidth<700, svg=document.createElementNS(NS,'svg'), gap=14;
    svg.setAttribute('role','img'); svg.setAttribute('aria-label','Connected devices');
    if(!narrow){
      var W=980, cw=270, px=355, pw=270;
      var hL=N.L.reduce(function(a,n){return a+34+n.l.length*16+gap;},0)-gap;
      var hR=N.R.reduce(function(a,n){return a+34+n.l.length*16+gap;},0)-gap;
      var hP=34+N.pi.l.length*16, H=Math.max(hL,hR,hP)+20;
      svg.setAttribute('viewBox','0 0 '+W+' '+H);
      var py=(H-hP)/2; box(svg,N.pi,px,py,pw);
      var y=(H-hL)/2; N.L.forEach(function(n,i){ var h=34+n.l.length*16; n.y0=y; y+=h+gap; });
      var y2=(H-hR)/2; N.R.forEach(function(n){ var h=34+n.l.length*16; n.y0=y2; y2+=h+gap; });
      N.L.forEach(function(n,i){ var py1=py+24+(hP-36)*(i+0.5)/N.L.length; link(svg,10+cw,n.y0+(34+n.l.length*16)/2,px,py1,n); });
      N.R.forEach(function(n,i){ var py1=py+24+(hP-36)*(i+0.5)/N.R.length; link(svg,px+pw,py1,W-10-cw,n.y0+(34+n.l.length*16)/2,n); });
      N.L.forEach(function(n){box(svg,n,10,n.y0,cw);}); N.R.forEach(function(n){box(svg,n,W-10-cw,n.y0,cw);});
      box(svg,N.pi,px,py,pw);
    } else {
      var W2=360, x=30, w=320, yy=6, all=N.L.concat([N.pi],N.R), pos=[];
      all.forEach(function(n){ pos.push(yy); yy+=34+n.l.length*16+gap+10; });
      svg.setAttribute('viewBox','0 0 '+W2+' '+yy);
      var pi_i=N.L.length, piy=pos[pi_i], pih=34+N.pi.l.length*16;
      all.forEach(function(n,i){ if(i===pi_i) return; var h=34+n.l.length*16, ny=pos[i]+h/2, ty=i<pi_i?piy+12+i*8:piy+pih-12-(i-pi_i-1)*8;
        var c=COL[n.st]||COL.off; el('path',{d:'M'+x+' '+ny+' H14 V'+ty+' H'+x,fill:'none',stroke:c,'stroke-width':2,'stroke-dasharray':n.dash?'6 5':''},svg); });
      all.forEach(function(n,i){ box(svg,n,x,pos[i],w); if(n.edge&&i!==pi_i){ var t=el('text',{x:x+w-8,y:pos[i]+(34+n.l.length*16)-6,'text-anchor':'end',fill:'#5a6270','font-size':10},svg); t.textContent=n.edge; } });
    }
    host.innerHTML=''; host.appendChild(svg);
    var at=document.getElementById('dg-at'); if(at) at.textContent='updated '+new Date(d.at*1000).toLocaleTimeString();
    var tb=document.getElementById('usb-table');
    if(tb){ tb.innerHTML='<tr><th>Device</th><th>Port · bus/dev</th><th>ID · serial</th><th>Speed · power</th><th>Doing</th></tr>'+
      (d.usb||[]).map(function(u){return '<tr><td>'+esc(u.label||u.product||u.kind)+'<br><span class="mut">'+esc(u.maker+' '+u.product)+'</span></td><td>'+esc(u.port)+'<br><span class="mut">'+esc(u.bus+'/'+u.dev)+'</span></td><td>'+esc(u.id)+'<br><span class="mut">'+esc(u.serial||'—')+'</span></td><td>'+esc(u.speed)+' Mb/s<br><span class="mut">'+esc(u.ma)+' mA'+(u.tty.length?' · '+esc(u.tty.join(' ')):'')+'</span></td><td>'+esc(u.role||u.driver||'—')+'</td></tr>';}).join(''); }
    var rt=document.getElementById('rail-table');
    if(rt){ var r=d.sys.rails||{}, ks=Object.keys(r).sort();
      rt.innerHTML=ks.length?'<tr><th>Rail</th><th>Voltage</th><th>Current</th></tr>'+ks.map(function(k){return '<tr><td>'+esc(k)+'</td><td>'+(r[k].V!=null?r[k].V.toFixed(3)+' V':'—')+'</td><td>'+(r[k].A!=null?(r[k].A*1000).toFixed(0)+' mA':'—')+'</td></tr>';}).join(''):'<tr><td class="mut">This Pi does not report its rails.</td></tr>'; }
  }
  var last=null;
  function tick(){ fetch('/api/devices',{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.json();}).then(function(d){last=d;draw(d);}).catch(function(){
    var at=document.getElementById('dg-at'); if(at) at.textContent='not updating — is the Pi still on?'; }); }
  tick(); setInterval(tick,5000); window.addEventListener('resize',function(){ if(last) draw(last); });
})();
</script>"""

CHECKS_JS = r"""
<script>
(function(){
  var ICON={ok:'✓',warn:'!',bad:'✕',info:'i'};
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  window.renderChecks=function(d, host, full){
    var c=d.checks.filter(function(x){return full||x.level!=='ok';});
    var sm=d.summary, head='<div class="row" style="margin-bottom:8px">'+
      (sm.bad?'<span class="spill badp">'+sm.bad+' problem'+(sm.bad>1?'s':'')+'</span>':'')+
      (sm.warn?'<span class="spill warnp">'+sm.warn+' warning'+(sm.warn>1?'s':'')+'</span>':'')+
      (!sm.bad&&!sm.warn?'<span class="spill okp">All checks passed</span>':'')+
      '<span class="spill">'+sm.ok+' OK</span></div>';
    host.innerHTML=head+(c.length?'<ul class="checks">'+c.map(function(x){return '<li class="'+x.level+'"><span class="ic">'+ICON[x.level]+'</span><div><b>'+esc(x.title)+'</b> — '+esc(x.found)+
      (x.fix&&x.level!=='ok'?'<div class="note">'+esc(x.fix)+'</div>':'')+'</div></li>';}).join('')+'</ul>':'<p class="note">Nothing needs attention.</p>');
  };
  var h=document.getElementById('checks');
  if(h && !h.dataset.manual){
    var go=function(){ fetch('/api/diagnostics',{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.json();}).then(function(d){window.renderChecks(d,h,false);}).catch(function(){}); };
    go(); setInterval(go,30000);
  }
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
        if getattr(self, "command", None) == "POST":   # a request that timed out before it was read has no command
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

    def send_download(self, text, filename, ctype):
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
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

    def multipart(self, limit=MAX_UPLOAD_BODY):
        """Parse a multipart/form-data POST: returns ({field: value}, {field: (filename, bytes)}). A field
        sent several files (<input multiple>) has them all under field + "[]", as a list."""
        n = self.body_length(limit, f"upload larger than {limit // (1024 * 1024)} MB")
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
                files.setdefault(name, (part.get_filename(), payload))
                files.setdefault(name + "[]", []).append((part.get_filename(), payload))
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
        routes = {"/": self.page_status, "/diagnostics": self.page_diag, "/logs": self.page_logs, "/settings": self.page_settings,
                  "/area": self.page_area, "/update": self.page_update, "/wifi": self.page_wifi, "/spotter": self.page_spotter,
                  "/power": self.page_power, "/account": self.page_account}
        if path in routes:
            return routes[path](sess)
        if path.startswith("/power/"):
            return self.page_power_confirm(sess, path[7:])
        if path == "/frequencies" or path.startswith("/frequencies/"):
            return self.get_frequencies(sess, path, {k: v[0] for k, v in q.items()})
        if path == "/api/logs":
            return self.send_json({"lines": journal_lines(300, noise="noise" in q)})
        if path == "/api/logs.txt":
            return self.send_text("\n".join(journal_lines(5000, noise=True)) + "\n", "wavelens-bridge.log")
        if path == "/api/update/log":
            return self.send_json(update_status_json())
        if path == "/api/devices":
            return self.send_json(devices_snapshot())
        if path == "/api/diagnostics":
            d = run_diagnostics(deep="deep" in q)
            if "deep" not in q:
                d.pop("snap", None)
            return self.send_json(d)
        if path == "/api/report.txt":
            return self.send_text(support_report(), f"wavelens-report-{time.strftime('%Y%m%d-%H%M')}.txt")
        if path == "/api/health":
            return self.send_json(bridge_health())
        if path == "/api/spotter/log":
            return self.send_json(FLASH_JOB.snapshot())
        if path == "/api/area/log":
            return self.send_json(AREA_JOB.snapshot())
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
                form, files = self.multipart(MAX_RR_BODY if path == "/area" else MAX_UPLOAD_BODY)
            else:
                form, files = self.form(), {}
            if not csrf_ok(form.get("csrf"), sess["csrf"]):
                return self.send_html(page("Rejected", "<p>Form token mismatch — go back, reload the page and try again.</p>"), 403)
            handlers = {"/logout": self.post_logout, "/account": self.post_account, "/settings": self.post_settings, "/security": self.post_security,
                        "/area": self.post_area, "/update": self.post_update, "/wifi": self.post_wifi, "/spotter": self.post_spotter,
                        "/bridge/restart": self.post_bridge_restart, "/frequencies": self.post_frequencies}
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
        if st.get("problems") and health["ok"]:
            missing += ('<div class="warn">Running without some local data: ' + "; ".join(esc(x) for x in st["problems"][:6])
                        + '. Copy the file to that path, or remove its entry in <a href="/settings">Settings</a>, then restart the scanner.</div>')
        if st.get("hw_missing"):
            missing += '<div class="warn">Missing hardware: ' + ", ".join(esc(x) for x in st["hw_missing"]) + "</div>"
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
        events = recent_events(10)
        body = f"""{banner}{missing}
<div class="card"><div class="row" style="justify-content:space-between"><h2 style="margin:0">Connected devices</h2>
<span class="note" id="dg-at">loading…</span></div>
<div id="diagram"></div>
<p class="note">Live, every 5 s. Green: working · amber: needs a look · red: problem · dashed: not connected. Hover or tap a box for its details.</p>
<details><summary class="note" style="cursor:pointer">USB addresses, serials and power</summary><div class="tbl"><table id="usb-table"></table></div></details>
<details><summary class="note" style="cursor:pointer">Voltages and currents</summary><div class="tbl"><table id="rail-table"></table></div></details></div>
<div class="card"><div class="row" style="justify-content:space-between"><h2 style="margin:0">Checks</h2><a class="note" href="/diagnostics">Full diagnostics →</a></div>
<div id="checks" style="margin-top:10px"><span class="note">running…</span></div></div>
<div class="grid">
<div class="card"><h2>Bridge</h2>{bridge}</div><div class="card"><h2>This Pi</h2>{pi}</div></div>
<div class="card"><h2>Recent events</h2>{('<pre style="max-height:260px">' + esc(chr(10).join(events)) + '</pre>') if events else '<p class="note">Nothing notable since the scanner started.</p>'}
<a class="note" href="/logs">All logs →</a></div>
<div class="card"><h2>Quick actions</h2><div class="row">
<form method="post" action="/bridge/restart" class="inline">{csrf_field(sess)}<button class="btn" type="submit">Restart bridge</button></form>
<a class="btn sec" href="/power/reboot">Reboot…</a><a class="btn sec" href="/diagnostics">Run diagnostics</a>
<a class="btn sec" href="/api/report.txt">Download support report</a></div></div>{DIAGRAM_JS}{CHECKS_JS}"""
        self.render(sess, "Status", body, "/")

    # ---- /diagnostics ---------------------------------------------------------------------
    def page_diag(self, sess):
        body = """<div class="card"><div class="row" style="justify-content:space-between">
<div class="row"><button class="btn" id="run">Run the test</button><a class="btn sec" href="/api/report.txt">Download support report</a></div>
<span class="note" id="dat"></span></div>
<p class="note">Checks power, heat, CPU, memory, storage and the SD card, the radios on USB, Bluetooth, Wi-Fi and the internet
connection updates use, the area data and the software. Nothing is changed and the scanner keeps running. The support report
has the results, the hardware list and the last bridge lines, and no passwords or Wi-Fi keys.</p></div>
<div class="card"><h2>Results</h2><div id="checks" data-manual="1"><span class="note">running…</span></div></div>
<div class="grid" id="tiles"></div>
<div class="card"><h2>Busiest programs</h2><div class="tbl"><table id="top"></table></div></div>
<div class="card"><h2>Kernel events this boot</h2><pre id="kev" style="max-height:240px">…</pre>
<p class="note">USB disconnects (a dongle dropping off), SD card errors, out-of-memory kills and under-voltage warnings.</p></div>
""" + CHECKS_JS + r"""<script>
(function(){
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function gbs(n){return n>=1e9?(n/1e9).toFixed(1)+' GB':(n/1e6).toFixed(0)+' MB';}
  function meter(p){p=Math.max(0,Math.min(100,p));return '<div class="meter"><b class="'+(p>90?'x':p>75?'w':'')+'" style="width:'+p+'%"></b></div>';}
  function tile(t,v,sub,p){return '<div class="card"><div class="note">'+t+'</div><div class="stat">'+v+'</div>'+(p!=null?meter(p):'')+'<div class="note small">'+(sub||'')+'</div></div>';}
  var btn=document.getElementById('run');
  function go(){
    btn.disabled=true; btn.innerHTML='<span class="spin"></span> Testing…';
    fetch('/api/diagnostics?deep=1',{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
      window.renderChecks(d, document.getElementById('checks'), true);
      var s=d.snap.sys, m=s.mem_total?100*(1-s.mem_avail/s.mem_total):0, dk=s.disk||{}, du=dk.total?100*(1-dk.free/dk.total):0;
      var ext=(s.rails||{}).EXT5V;
      document.getElementById('tiles').innerHTML=
        tile('CPU',(s.cpu_pct!=null?s.cpu_pct:'?')+' %','load '+s.load.join(' / ')+' · '+s.cores+' cores · '+s.mhz+' / '+s.mhz_max+' MHz',s.cpu_pct)+
        tile('Memory',m.toFixed(0)+' %',gbs(s.mem_avail)+' free of '+gbs(s.mem_total)+(s.swap_total?' · swap '+gbs(s.swap_total-s.swap_free)+' used':' · no swap'),m)+
        tile('Temperature',s.temp!=null?s.temp.toFixed(1)+' °C':'?',(s.fan_rpm?'fan '+s.fan_rpm+' rpm · ':'')+'slows down at 80 °C',s.temp!=null?s.temp/85*100:null)+
        tile('Storage',dk.free?gbs(dk.free)+' free':'?',(dk.total?'of '+gbs(dk.total)+' · ':'')+esc(s.root),du)+
        tile('Power',ext&&ext.V?ext.V.toFixed(2)+' V':(s.throttled?(s.throttled.undervolt?'Low':'OK'):'n/a'),
             (s.psu_ma?'supply limit '+(s.psu_ma/1000).toFixed(1)+' A · ':'')+(s.throttled?'get_throttled '+s.throttled.raw:''),null)+
        tile('System',esc(s.model.replace(/ Rev [\d.]+$/,'')),esc(s.os)+' · kernel '+esc(s.kernel)+' · Python '+esc(s.python)+' · up '+Math.floor(s.uptime/3600)+' h',null);
      document.getElementById('top').innerHTML='<tr><th>Program</th><th>CPU %</th><th>Memory</th><th>PID</th></tr>'+d.top.map(function(p){
        return '<tr><td>'+esc(p.name)+'</td><td>'+esc(p.cpu)+'</td><td>'+esc(p.rss_mb)+' MB ('+esc(p.mem)+' %)</td><td class="mut">'+esc(p.pid)+'</td></tr>';}).join('');
      var ev=[]; Object.keys(d.events).forEach(function(k){ev=ev.concat(d.events[k]);});
      document.getElementById('kev').textContent=ev.length?ev.join('\n'):'None.';
      document.getElementById('dat').textContent='tested '+new Date(d.at*1000).toLocaleTimeString();
    }).catch(function(){ document.getElementById('checks').innerHTML='<div class="warn">The test did not finish. Reload the page and try again.</div>'; })
    .then(function(){ btn.disabled=false; btn.textContent='Run the test again'; });
  }
  btn.addEventListener('click',go); go();
})();
</script>"""
        self.render(sess, "Diagnostics", body, "/diagnostics")

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
        sc = {flag: (args_get_flag(args, flag) if args_get_flag(args, flag) is not None else default)
              for flag, (default, _) in SCANNER_FLAGS.items()}
        body = f"""<div class="card"><form method="post" action="/settings">{csrf_field(sess)}
<label>Radio name (what the app lists under Find; letters, digits, - and _; up to 20)</label>
<input type="text" name="name" value="{esc(args_get_name(args))}" placeholder="Wavelens-Car" maxlength="20">
<h2>Scanner</h2>
<label>Listening radio gain, dB (0 = automatic; default 40). Lower it if digital voice breaks up near strong transmitters</label>
<input type="number" name="listen_gain" min="0" max="50" step="1" value="{esc(sc['--listen-gain'])}">
<label>Listening radio frequency correction, ppm (empty = same as the sweep's; a TCXO dongle needs 0)</label>
<input type="number" name="listen_ppm" min="-200" max="200" step="1" value="{esc(sc['--listen-ppm'])}" placeholder="0">
<label>With POLICE, FIRE or EMS chosen in the app: talkgroups your list does not name</label>
<select name="tg_unlisted"><option value="mute" {"selected" if sc['--tg-unlisted'] != "play" else ""}>Muted (default)</option>
<option value="play" {"selected" if sc['--tg-unlisted'] == "play" else ""}>Played</option></select>
<label>Interop and Emergency Ops talkgroups (police, fire and EMS all use them)</label>
<select name="tg_interop"><option value="other" {"selected" if sc['--tg-interop'] != "police" else ""}>Count as OTHER (default)</option>
<option value="police" {"selected" if sc['--tg-interop'] == "police" else ""}>Count as POLICE</option></select>
<h2>Advanced</h2>
<label>WAVELENS_AUTO</label><select name="auto"><option value="0" {"selected" if d.get("WAVELENS_AUTO", "0") != "1" else ""}>0 — the bridge assigns radios itself (default, hot-plug)</option>
<option value="1" {"selected" if d.get("WAVELENS_AUTO") == "1" else ""}>1 — plan in the shell at each start (older way)</option></select>
<label>WAVELENS_RTL433 (rtl_433 arguments; usually empty)</label><input type="text" name="rtl433" value="{esc(d.get('WAVELENS_RTL433', ''))}">
<label>WAVELENS_ARGS (extra bridge flags; the radio name and the Scanner fields above are kept in sync)</label>
<textarea name="args" rows="4">{esc(args)}</textarea>
<button class="btn" type="submit">Save and restart bridge</button></form></div>
{self.security_card(sess)}
<div class="card"><h2>What these mean</h2><p class="note">From the comments in <code>/etc/default/wavelens-bridge</code>:</p>
<pre style="max-height:40vh">{esc(defaults_help())}</pre></div>"""
        self.render(sess, "Settings", body, "/settings")

    def security_card(self, sess):
        st = security_state()

        def row(key, label, note, state):
            if state == "missing":
                return f'<label>{label}</label><p class="note">Not installed on this Pi.</p>'
            on = state is True or state == "on"
            return (f'<label>{label}</label><select name="{key}">'
                    f'<option value="1" {"selected" if on else ""}>On</option>'
                    f'<option value="0" {"" if on else "selected"}>Off</option></select>'
                    f'<p class="note">{note}</p>')

        return f"""<div class="card"><h2>Bluetooth and remote access</h2>
<p class="note">Commands from the app come over Bluetooth from whichever phone is connected. Once this Scanner is on
your Wi-Fi and set up, you can turn off what you don't use. This page keeps working either way.</p>
<form method="post" action="/security">{csrf_field(sess)}
{row("wifi_setup", "Wi-Fi set-up from the app", "The app can list networks and join this Scanner to one. Needed only for first set-up; the Wi-Fi tab here does the same.", st["wifi_setup"])}
{row("app_control", "Updates and area from the app", "The app's Check / Install / Roll back buttons and its Area row. The Update and Area tabs here keep working.", st["app_control"])}
{row("bt_autopair", "Accept Bluetooth pairing without a prompt", "Lets a phone or computer that asks to pair do so without confirming on the Pi. The app doesn't need it to connect.", st["bt_autopair"])}
{row("ssh", "SSH login", "Command-line login from your home network. Change the shipped password first if you keep it on.", st["ssh"])}
<button class="btn" type="submit">Save</button></form></div>"""

    def post_security(self, sess, form, files, tok):
        msgs, restart = security_apply(form, security_state())
        if restart:
            rc, out = run(["systemctl", "restart", BRIDGE_UNIT], timeout=30)
            msgs.append("Bridge restarted." if rc == 0 else f"Bridge restart failed: {out}")
        self.flash(sess, " ".join(msgs) or "Nothing changed.")
        self.redirect("/settings")

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
        # The Scanner fields: each is one flag in the same line. A value equal to the bridge's default
        # is left out, so the line stays short and a later change of default still applies.
        for flag, field in (("--listen-gain", "listen_gain"), ("--listen-ppm", "listen_ppm"),
                            ("--tg-unlisted", "tg_unlisted"), ("--tg-interop", "tg_interop")):
            if field not in form:
                continue                         # an older form (or a script) that does not send it: leave the flag alone
            default, check = SCANNER_FLAGS[flag]
            raw = form.get(field, "").strip()
            val = check(raw) if raw else ""
            if val is None:
                self.flash(sess, f"{flag[2:]}: {raw!r} is not a value the scanner accepts.")
                return self.redirect("/settings")
            args = args_set_flag(args, flag, "" if val == default else val)
        defaults_write({"WAVELENS_AUTO": "1" if form.get("auto") == "1" else "0",
                        "WAVELENS_RTL433": " ".join(form.get("rtl433", "").split()),   # one shell line, like args
                        "WAVELENS_ARGS": args})
        rc, out = run(["systemctl", "restart", BRIDGE_UNIT], timeout=30)
        self.flash(sess, "Settings saved; bridge restarted." if rc == 0 else f"Saved, but restart failed: {out}")
        self.redirect("/settings")

    # ---- /area -----------------------------------------------------------------------------
    def page_area(self, sess):
        """Where this Scanner is used. The work is done by area.py (wavelens-area): this page shows what it
        stored and runs it."""
        a = read_json(AREA_FILE) or {}
        snap = AREA_JOB.snapshot()
        dis = "disabled" if snap["running"] else ""
        regions = a.get("regions") or {}
        districts = "; ".join(f"{r or '—'}: {', '.join(d) or '—'}" for r, d in regions.items()) or "—"
        rr = rr_list()
        if rr:
            rr_rows = "".join(
                f"""<tr><td><b>{esc(e['name'])}</b><br><span class="mut">{esc(e['label'])} · {e['rows']} rows · {fmt_ts(e['mtime'])}</span></td>
<td style="text-align:right"><form method="post" action="/area" onsubmit="return confirm('Delete this export from the Scanner?')">{csrf_field(sess)}
<input type="hidden" name="name" value="{esc(e['name'])}"><button class="btn sec" name="do" value="rr_delete">Delete</button></form></td></tr>"""
                for e in rr)
            rr_table = f"<table>{rr_rows}</table>"
        else:
            rr_table = '<p class="mut">None yet.</p>'
        fcc_path = os.path.join(AREA_DIR, "fcc-local.json")
        try:
            fcc_size = os.path.getsize(fcc_path)
            fcc_db = (f"<b>{fcc_size / 1e6:.1f} MB</b> on this Pi · {int(a.get('licences') or 0):,} records · "
                      f"<span class=\"mut\">{esc(fcc_path)}</span>")
        except OSError:
            fcc_db = '<span class="mut">not downloaded</span>'
        follow = bool(a.get("follow"))
        fst = (read_json(STATUS_JSON) or {}).get("area_follow") or {}
        follow_state = ""
        if follow:
            bits = []
            if fst.get("km") is not None:
                bits.append(f"phone is {fst['km']:g} km from the area's centre")
            if fst.get("msg"):
                bits.append(esc(fst["msg"]) + (f" ({esc(fmt_ts(fst['at']))})" if fst.get("at") else ""))
            follow_state = "<p class=\"note\">" + (" · ".join(bits) or "on; the scanner checks the phone's position every minute") + "</p>"
        if a:
            where = " ".join(x for x in (a.get("country", ""), a.get("postcode", "")) if x)
            place = ", ".join(x for x in (a.get("district", ""), a.get("region", "")) if x)
            table = f"""<table>
<tr><th>Area</th><td><b>{esc(where)}</b>{f' <span class="mut">({esc(place)})</span>' if place else ''}</td></tr>
<tr><th>Position, radius</th><td>{esc(a.get('lat'))}, {esc(a.get('lon'))} · {esc(a.get('radius_km', 50))} km</td></tr>
<tr><th>Districts in range</th><td>{esc(districts)}</td></tr>
<tr><th>Licensed frequencies</th><td>{f"<b>{int(a['licences']):,}</b>" if a.get('licences') else '<span class="mut">none: no public licence data for this country yet</span>'}</td></tr>
<tr><th>FCC database</th><td>{fcc_db}</td></tr>
<tr><th>Data of</th><td>{esc(a.get('data_built') or '—')} <span class="mut">(set {esc(a.get('updated') or '—')})</span></td></tr></table>"""
        else:
            table = '<p><b>No area set.</b> The Scanner works without one; carriers are then named by band only.</p>'
        body = f"""<div class="card">{table}
<form method="post" action="/area">{csrf_field(sess)}
<div class="row"><div><label>Country (two letters)</label><input type="text" name="country" value="{esc(a.get('country') or 'US')}" maxlength="2" size="4" style="width:5em"></div>
<div><label>Postcode / ZIP</label><input type="text" name="postcode" value="{esc(a.get('postcode') or '')}" maxlength="12" placeholder="49749"></div>
<div><label>Radius (km)</label><input type="number" name="radius" value="{esc(int(a.get('radius_km') or 50))}" min="5" max="300" style="width:6em"></div></div>
<div class="row"><button class="btn" name="do" value="set" {dis}>Set area and download</button>
<button class="btn sec" name="do" value="refresh" {dis}>Check for newer data</button>
<button class="btn sec" name="do" value="clear" {dis}>Clear</button></div></form>
<p class="note">Any country: a two-letter code (US, CA, AU, GB, DE …) and a postcode. The Scanner downloads the public licence
records around that place and names what it hears from them. Licence records exist where a country publishes them, today the
United States (FCC). Needs Wi-Fi; restarts the bridge for a few seconds. Postcode positions: GeoNames, CC BY 4.0.</p></div>
<div class="card"><h2>Follow me</h2>
<form method="post" action="/area">{csrf_field(sess)}<input type="hidden" name="do" value="follow">
<label style="margin:0;color:var(--fg)"><input type="checkbox" name="follow" value="1" {"checked" if follow else ""} {"" if a else "disabled"}>
Move the area with me as I travel</label>
<button class="btn sec" type="submit" {"" if a else "disabled"}>Save</button></form>{follow_state}
<p class="note">For a car or a backpack. When the phone shares its position with the Scanner (Android: More → Companion radio → <i>Share my position with the companion</i>; iPhone: Scanner → <i>Share position with the Scanner</i>)
and you have travelled out of the middle of the area (about {max(15, int(0.4 * float(a.get('radius_km') or 50)))} km for this radius),
the Scanner re-centres the area on you and swaps in the local licences without restarting, so the phone stays connected.
At most once every 10 minutes. It needs an internet connection at that moment (phone hotspot or Wi-Fi); without one it keeps
the area it has and tries again later. {"Set an area first." if not a else ""}</p></div>
<div class="card"><h2>Your own RadioReference exports</h2>
{rr_table}
<form method="post" action="/area" enctype="multipart/form-data">{csrf_field(sess)}<input type="hidden" name="do" value="rr_upload">
<label>Add exports (.csv, several at once is fine)</label><input type="file" name="rr" accept=".csv,text/csv" multiple required>
<div class="row"><button class="btn">Upload</button></div></form>
<p class="note">The CSV downloads from your own RadioReference account: a county's, a state's or the nationwide frequencies, a trunked
system's sites, and its talkgroups. The Scanner tells them apart by their headers, so the file names don't matter; a file with
the same name replaces the old one. Uploading or deleting restarts the bridge for a few seconds so the change is used.
The files stay on this Scanner (<code>{esc(RR_DIR)}</code>) and are never sent anywhere.</p></div>
<div class="card"><h2>Progress <span id="st" class="mut"></span></h2><pre id="out" data-url="/api/area/log">{esc(chr(10).join(snap['lines']))}</pre></div>{POLL_JS}"""
        self.render(sess, "Area", body, "/area")

    def post_area(self, sess, form, files, tok):
        do = form.get("do", "")
        if do in ("rr_upload", "rr_delete"):
            return self.post_area_rr(sess, do, form, files)
        if do == "follow":
            on = form.get("follow") == "1"
            a = read_json(AREA_FILE) or {}
            if not a:
                self.flash(sess, "Set an area first.")
            else:
                a["follow"] = on
                try:
                    tmp = AREA_FILE + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(a, f, separators=(",", ":"), ensure_ascii=False)
                    os.chmod(tmp, 0o644)
                    os.replace(tmp, AREA_FILE)
                    self.flash(sess, "Follow me is on: the area moves with the phone's position." if on else "Follow me is off.")
                except OSError as e:
                    self.flash(sess, f"Could not save: {e}")
            return self.redirect("/area")
        if not os.path.isfile(AREA_TOOL):
            self.flash(sess, "The area tool is not installed on this Pi: update the Scanner first.")
            return self.redirect("/area")
        argv = [sys.executable or "/usr/bin/python3", AREA_TOOL]
        if do == "set":
            country, postcode = form.get("country", "").strip().upper(), " ".join(form.get("postcode", "").upper().split())
            try:
                radius = float(form.get("radius", "50"))
            except ValueError:
                radius = -1.0
            # checked here as well as in the tool: these go on a command line (as arguments, never through a shell)
            if not COUNTRY_RE.match(country) or not POSTCODE_RE.match(postcode) or not 5 <= radius <= 300:
                self.flash(sess, "Country is two letters, the postcode letters and digits, the radius 5 to 300 km.")
                return self.redirect("/area")
            argv += ["set", "--country", country, "--postcode", postcode, "--radius-km", f"{radius:g}"]
        elif do == "refresh":
            argv += ["refresh"]
        elif do == "clear":
            argv += ["clear"]
        else:
            return self.redirect("/area")
        if AREA_JOB.start(f"area {do}", argv, timeout=900):
            self.flash(sess, "Started. Watch the progress box; reload the page when it says done.")
        else:
            self.flash(sess, "An area task is already running.")
        self.redirect("/area")

    def post_area_rr(self, sess, do, form, files):
        """Upload or delete the owner's RadioReference exports, then restart the bridge so it reads them."""
        if do == "rr_upload":
            sent = [(n, d) for n, d in files.get("rr[]", []) if n]
            if not sent:
                self.flash(sess, "Choose one or more .csv files first.")
                return self.redirect("/area")
            results = []
            for name, data in sent:
                try:
                    results.append(rr_save(name, data))
                except OSError as e:
                    results.append((False, f"{name}: could not be saved ({e.strerror or e})."))
        else:
            results = [rr_delete(form.get("name", ""))]
        msg = " ".join(m for _, m in results)
        if any(ok for ok, _ in results):
            rc, out = run(["systemctl", "restart", BRIDGE_UNIT], timeout=30)
            msg += " Bridge restarted." if rc == 0 else f" The bridge did not restart ({out}); restart it from Power."
        self.flash(sess, msg)
        self.redirect("/area")

    # ---- /frequencies ----------------------------------------------------------------------
    # The owner's own frequency list. All the rules (what a valid channel is, the file formats, backups,
    # locking) live in freqdb.py; this is the pages. The bridge re-reads the list by itself when it changes.
    FREQ_PER_PAGE = 100
    FREQ_FILTERS = ("q", "group", "mode", "show", "sort", "page")
    FREQ_SORTS = {"freq": "Frequency", "name": "Name", "group": "Group", "new": "Newest change"}

    @staticmethod
    def freq_qs(d, **over):
        """The list's filter settings as a query string (only the known ones, so a form can't steer a redirect)."""
        keep = {k: str(d.get(k) or "") for k in Handler.FREQ_FILTERS}
        keep.update({k: str(v) for k, v in over.items()})
        keep = {k: v[:80] for k, v in keep.items() if v}
        return ("?" + urllib.parse.urlencode(keep)) if keep else ""

    @staticmethod
    def freq_options(pairs, chosen):
        return "".join(f'<option value="{esc(v)}"{" selected" if v == chosen else ""}>{esc(t)}</option>' for v, t in pairs)

    @staticmethod
    def freq_groups(channels):
        return sorted({c.get("group") or "" for c in channels} - {""}, key=str.lower)

    def get_frequencies(self, sess, path, q):
        if freqdb is None:
            return self.render(sess, "Frequencies", '<div class="warn">This page needs a newer version of the scanner '
                               'software. <a href="/update">Install the latest update</a>, then come back.</div>', "/frequencies")
        store = freqdb.Store()
        if path == "/frequencies/schema":
            return self.send_download(json.dumps(freqdb.SCHEMA, indent=1) + "\n", "wavelens-frequencies.schema.json", "application/json")
        if path == "/frequencies/backups":
            return self.page_freq_backups(sess, store)
        if path == "/frequencies/backup":
            try:
                return self.send_download(read_text(store.backup_path(q.get("name", ""))), "wavelens-frequencies-backup.json", "application/json")
            except freqdb.StoreError:
                return self.send_html(page("Not found", "<p>No such backup.</p>"), 404)
        try:
            doc = store.load()
        except freqdb.StoreError as e:
            return self.render(sess, "Frequencies", f'<div class="warn"><b>{esc(e)}</b></div>'
                               '<p><a class="btn" href="/frequencies/backups">Backups</a></p>', "/frequencies")
        if path == "/frequencies":
            return self.page_freq_list(sess, doc, q)
        if path == "/frequencies/edit":
            ch = next((c for c in doc["channels"] if c.get("id") == q.get("id")), None) if q.get("id") else None
            if q.get("id") and ch is None:
                self.flash(sess, "That channel is no longer in the list.")
                return self.redirect("/frequencies")
            return self.page_freq_edit(sess, doc, self.freq_form_values(ch), ch, q)
        if path == "/frequencies/import":
            return self.page_freq_import(sess, doc)
        if path == "/frequencies/export":
            fmt = q.get("fmt", "")
            if fmt not in freqdb.EXPORT_FORMATS:
                return self.send_html(page("Not found", "<p>No such format.</p>"), 404)
            chans = [c for c in doc["channels"] if not q.get("group") or c.get("group") == q["group"]]
            _, filename, ctype = freqdb.EXPORT_FORMATS[fmt]
            return self.send_download(freqdb.export(chans, fmt), filename, ctype)
        self.send_html(page("Not found", "<p>No such page.</p>"), 404)

    @staticmethod
    def freq_tone_text(c):
        kind, tone = c.get("tone_type") or "", c.get("tone") or ""
        if not kind:
            return ""
        return {"ctcss": f"{tone} Hz", "dcs": f"DCS {tone}", "nac": f"NAC {tone}", "cc": f"CC {tone}", "ran": f"RAN {tone}"}[kind]

    def page_freq_list(self, sess, doc, q):
        chans = doc["channels"]
        groups = self.freq_groups(chans)
        needle = (q.get("q") or "").strip().lower()
        shown = []
        for c in chans:
            if q.get("group") and (c.get("group") or "") != q["group"]:
                continue
            if q.get("mode") and c.get("mode") != q["mode"]:
                continue
            if q.get("show") == "locked" and not c.get("lockout") or q.get("show") == "open" and c.get("lockout"):
                continue
            if needle and needle not in " ".join([c["name"], freqdb.mhz(c["freq_hz"]), c.get("group", ""), c.get("service", ""),
                                                  c.get("notes", ""), " ".join(c.get("tags") or [])]).lower():
                continue
            shown.append(c)
        sort = q.get("sort") if q.get("sort") in self.FREQ_SORTS else "freq"
        if sort == "name":
            shown.sort(key=lambda c: c["name"].lower())
        elif sort == "group":
            shown.sort(key=lambda c: ((c.get("group") or "~").lower(), c["freq_hz"]))
        elif sort == "new":
            shown.sort(key=lambda c: c.get("updated", ""), reverse=True)
        pages = max(1, -(-len(shown) // self.FREQ_PER_PAGE))
        try:
            pg = min(max(int(q.get("page") or 1), 1), pages)
        except ValueError:
            pg = 1
        rows = []
        for c in shown[(pg - 1) * self.FREQ_PER_PAGE: pg * self.FREQ_PER_PAGE]:
            out_of_range = not freqdb.RTL_HZ[0] <= c["freq_hz"] <= freqdb.RTL_HZ[1]
            pills = ('<span class="pill lock">locked out</span> ' if c.get("lockout") else "") + \
                    ('<span class="pill" title="Outside what the kit\'s radios tune: kept for reference">reference</span>' if out_of_range else "")
            pair = f'<br><span class="mut">in {esc(freqdb.mhz(c["input_hz"]))}</span>' if c.get("input_hz") else ""
            edit = f'/frequencies/edit{self.freq_qs(q, id=c["id"])}'
            rows.append(f'<tr><td><input type="checkbox" name="id_{esc(c["id"])}" value="1" aria-label="select {esc(c["name"])}"></td>'
                        f'<td class="num"><a href="{esc(edit)}">{esc(freqdb.mhz(c["freq_hz"]))}</a>{pair}</td>'
                        f'<td class="wrap"><a href="{esc(edit)}">{esc(c["name"])}</a> {pills}</td>'
                        f'<td>{esc(freqdb.MODE_NAMES.get(c.get("mode"), "Auto"))}</td><td>{esc(self.freq_tone_text(c))}</td>'
                        f'<td class="wrap">{esc(c.get("group", ""))}</td></tr>')
        locked = sum(1 for c in chans if c.get("lockout"))
        hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(q.get(k) or "")}">' for k in self.FREQ_FILTERS if q.get(k))
        group_opts = self.freq_options([("", "All groups")] + [(g, g) for g in groups], q.get("group") or "")
        mode_opts = self.freq_options([("", "All modes")] + [(m, freqdb.MODE_NAMES[m]) for m in freqdb.MODES], q.get("mode") or "")
        show_opts = self.freq_options([("", "All"), ("locked", "Locked out"), ("open", "Not locked out")], q.get("show") or "")
        sort_opts = self.freq_options(list(self.FREQ_SORTS.items()), sort)
        pager = ""
        if pages > 1:
            prev_ = f'<a class="btn sec" href="/frequencies{esc(self.freq_qs(q, page=pg - 1))}">Previous</a>' if pg > 1 else ""
            next_ = f'<a class="btn sec" href="/frequencies{esc(self.freq_qs(q, page=pg + 1))}">Next</a>' if pg < pages else ""
            pager = f'<div class="row">{prev_}<span class="mut">page {pg} of {pages}</span>{next_}</div>'
        if not chans:
            table = ('<p><b>No channels yet.</b> Add one, or import a list you already keep: a spreadsheet saved as CSV, '
                     'a radio programming file, SDR bookmarks or your own RadioReference export.</p>')
        elif not shown:
            table = '<p class="mut">Nothing matches. <a href="/frequencies">Show everything</a></p>'
        else:
            table = f"""<form method="post" action="/frequencies">{csrf_field(sess)}{hidden}
<div class="scroll"><table class="list"><tr><th><input type="checkbox" id="all" aria-label="select all on this page"></th>
<th>MHz</th><th>Name</th><th>Mode</th><th>Tone / code</th><th>Group</th></tr>{''.join(rows)}</table></div>{pager}
<div class="row end"><div><label>With the ticked channels</label><select name="act">
<option value="lock">Lock out (Follow skips them)</option><option value="unlock">Clear lockout</option>
<option value="group">Move to the group named here</option><option value="delete">Delete</option></select></div>
<div><label>Group name (for a move)</label><input type="text" name="to_group" maxlength="40" list="groups"></div>
<div><label class="check"><input type="checkbox" name="sure" value="1"> Yes, delete them</label></div>
<div><button class="btn" name="do" value="bulk">Apply</button></div></div></form>
<script>document.getElementById('all').addEventListener('change',function(e){{
var b=document.querySelectorAll('input[name^="id_"]');for(var i=0;i<b.length;i++)b[i].checked=e.target.checked;}});</script>"""
        fmt_opts = self.freq_options([(k, v[0]) for k, v in freqdb.EXPORT_FORMATS.items()], "wavelens")
        body = f"""<div class="card"><p><b>{len(chans)}</b> channel{'' if len(chans) == 1 else 's'} · <b>{locked}</b> locked out · <b>{len(groups)}</b> group{'' if len(groups) == 1 else 's'}
{f'<span class="mut">· showing {len(shown)}</span>' if len(shown) != len(chans) else ''}</p>
<div class="row"><a class="btn" href="/frequencies/edit{esc(self.freq_qs(q))}">Add a channel</a><a class="btn sec" href="/frequencies/import">Import</a>
<a class="btn sec" href="/frequencies/backups">Backups</a></div>
<p class="note">Your own list, kept on this Scanner. A hit on one of these frequencies is named from here before any licence
record, Listen and Follow tune it in the mode you set, and Follow never stops on a channel you lock out. A change is in use
within a few seconds, with no restart.</p></div>
<div class="card"><form method="get" action="/frequencies"><div class="row end">
<div><label>Search</label><input type="text" name="q" value="{esc(q.get('q') or '')}" maxlength="80" placeholder="name, MHz, notes"></div>
<div><label>Group</label><select name="group">{group_opts}</select></div>
<div><label>Mode</label><select name="mode">{mode_opts}</select></div>
<div><label>Show</label><select name="show">{show_opts}</select></div>
<div><label>Sort by</label><select name="sort">{sort_opts}</select></div>
<div><button class="btn sec">Filter</button></div></div></form>
<datalist id="groups">{''.join(f'<option value="{esc(g)}">' for g in groups)}</datalist>{table}</div>
<div class="card"><h2>Export</h2><form method="get" action="/frequencies/export"><div class="row end">
<div><label>Format</label><select name="fmt">{fmt_opts}</select></div>
<div><label>Which channels</label><select name="group">{self.freq_options([("", "The whole list")] + [(g, g) for g in groups], "")}</select></div>
<div><button class="btn sec"{' disabled' if not chans else ''}>Download</button></div></div></form>
<p class="note">The list is yours to take anywhere. The Wavelens file keeps everything and is the one to keep as a copy; its layout is
published as a <a href="/frequencies/schema">JSON Schema</a>. The other formats carry what they have room for (a bookmark file has
no tones, for one).</p></div>"""
        self.render(sess, "Frequencies", body, "/frequencies")

    @staticmethod
    def freq_form_values(ch):
        """A stored channel as the edit form's text fields (a new channel when ch is None)."""
        if ch is None:
            return {"mode": "auto"}
        bw = ch.get("bandwidth_hz")
        return {"id": ch["id"], "stamp": ch.get("updated", ""), "name": ch["name"], "freq": freqdb.mhz(ch["freq_hz"]),
                "input": freqdb.mhz(ch["input_hz"]) if ch.get("input_hz") else "", "mode": ch.get("mode", "auto"),
                "tone_type": ch.get("tone_type", ""), "tone": ch.get("tone", ""), "emission": ch.get("emission", ""),
                "bandwidth": f"{bw / 1000:g}" if bw else "", "group": ch.get("group", ""), "service": ch.get("service", ""),
                "tags": ", ".join(ch.get("tags") or []), "lockout": "1" if ch.get("lockout") else "",
                "notes": ch.get("notes", ""), "lat": "" if ch.get("lat") is None else ch["lat"],
                "lon": "" if ch.get("lon") is None else ch["lon"]}

    def page_freq_edit(self, sess, doc, v, ch, q, errors=""):
        new = not v.get("id")
        hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(q.get(k) or "")}">' for k in self.FREQ_FILTERS if q.get(k))
        err = f'<div class="warn"><b>Not saved.</b> {esc(errors)}</div>' if errors else ""
        tone_opts = self.freq_options([(t, freqdb.TONE_NAMES[t]) for t in freqdb.TONE_TYPES], v.get("tone_type") or "")
        mode_opts = self.freq_options([(m, freqdb.MODE_NAMES[m] + ("" if m in freqdb.LISTEN_MODES or m == "auto" else " (kept for export; heard as Auto)"))
                                       for m in freqdb.MODES], v.get("mode") or "auto")
        service_opts = self.freq_options([(s, s or "Not set") for s in freqdb.SERVICES] +
                                         ([(v["service"], v["service"])] if v.get("service") and v["service"] not in freqdb.SERVICES else []),
                                         v.get("service") or "")
        delete = "" if new else f"""<div class="card"><h2>Delete</h2><form method="post" action="/frequencies">{csrf_field(sess)}{hidden}
<input type="hidden" name="id" value="{esc(v['id'])}"><label class="check"><input type="checkbox" name="sure" value="1"> Yes, delete this channel</label>
<button class="btn dng" name="do" value="delete">Delete</button></form>
<p class="note">A deleted channel can be put back from Backups.</p></div>"""
        when = f'<p class="note">Added {esc((ch or {}).get("created", "")[:10])} · last changed {esc((ch or {}).get("updated", "")[:16].replace("T", " "))} UTC · from {esc((ch or {}).get("source", "manual"))}</p>' if ch else ""
        body = f"""{err}<div class="card"><form method="post" action="/frequencies">{csrf_field(sess)}{hidden}
<input type="hidden" name="id" value="{esc(v.get('id') or '')}"><input type="hidden" name="stamp" value="{esc(v.get('stamp') or '')}">
<label>Name</label><input type="text" name="name" value="{esc(v.get('name') or '')}" maxlength="64" required placeholder="PCSO Dispatch North">
<div class="cols">
<div><label>Frequency (MHz)</label><input type="text" name="freq" value="{esc(v.get('freq') or '')}" inputmode="decimal" required placeholder="155.475"></div>
<div><label>Repeater input (MHz), if any</label><input type="text" name="input" value="{esc(v.get('input') or '')}" inputmode="decimal" placeholder="empty for simplex"></div>
<div><label>Mode</label><select name="mode">{mode_opts}</select></div>
<div><label>Tone or code</label><select name="tone_type">{tone_opts}</select></div>
<div><label>Its value</label><input type="text" name="tone" value="{esc(v.get('tone') or '')}" maxlength="12" list="ctcss" placeholder="127.3 · 023 · 293"></div>
<div><label>Group</label><input type="text" name="group" value="{esc(v.get('group') or '')}" maxlength="40" list="groups" placeholder="County fire"></div>
<div><label>Who uses it</label><select name="service">{service_opts}</select></div>
<div><label>Tags (commas between)</label><input type="text" name="tags" value="{esc(v.get('tags') or '')}" maxlength="200"></div>
</div>
<label class="check" style="margin-top:14px"><input type="checkbox" name="lockout" value="1"{' checked' if v.get('lockout') else ''}> Locked out: Follow never stops on it (a pager, a busy repeater, a data channel)</label>
<label>Notes</label><input type="text" name="notes" value="{esc(v.get('notes') or '')}" maxlength="500">
<details style="margin-top:12px"{' open' if any(v.get(k) not in (None, '') for k in ('emission', 'bandwidth', 'lat', 'lon')) else ''}><summary class="mut">Technical details (all optional)</summary>
<div class="cols">
<div><label>Emission designator (ITU)</label><input type="text" name="emission" value="{esc(v.get('emission') or '')}" maxlength="9" placeholder="11K2F3E"></div>
<div><label>Bandwidth (kHz)</label><input type="text" name="bandwidth" value="{esc(v.get('bandwidth') or '')}" inputmode="decimal" placeholder="12.5"></div>
<div><label>Latitude</label><input type="text" name="lat" value="{esc(v.get('lat'))}" inputmode="decimal" placeholder="27.8760"></div>
<div><label>Longitude</label><input type="text" name="lon" value="{esc(v.get('lon'))}" inputmode="decimal" placeholder="-82.8510"></div>
</div><p class="note">The emission designator is the code on a licence (11K2F3E is 11.2 kHz analog FM voice, 8K10F1E is P25 voice). Give it and
the bandwidth is read from it. The position is the transmitter's, in decimal degrees (WGS 84).</p></details>
<datalist id="ctcss">{''.join(f'<option value="{t}">' for t in freqdb.CTCSS)}</datalist>
<datalist id="groups">{''.join(f'<option value="{esc(g)}">' for g in self.freq_groups(doc['channels']))}</datalist>
<div class="row"><button class="btn" name="do" value="save">Save</button><a class="btn sec" href="/frequencies{esc(self.freq_qs(q))}">Cancel</a></div>
</form>{when}</div>{delete}"""
        self.render(sess, "Add a channel" if new else "Edit channel", body, "/frequencies")

    def page_freq_import(self, sess, doc):
        got = sess.get("freq_import")
        if got:
            have = {freqdb.dedupe_key(c) for c in doc["channels"]}
            dup = sum(1 for c in got["channels"] if freqdb.dedupe_key(c) in have)
            sample = "".join(f'<tr><td class="num">{esc(freqdb.mhz(c["freq_hz"]))}</td><td class="wrap">{esc(c["name"])}</td>'
                             f'<td>{esc(freqdb.MODE_NAMES.get(c["mode"], "Auto"))}</td><td>{esc(self.freq_tone_text(c))}</td>'
                             f'<td class="wrap">{esc(c.get("group", ""))}</td></tr>' for c in got["channels"][:15])
            problems = ""
            if got["problems"]:
                problems = (f'<div class="warn"><b>{len(got["problems"])} row{"" if len(got["problems"]) == 1 else "s"} left out</b> '
                            f'(the rest are fine to import):<pre>{esc(chr(10).join(got["problems"][:60]))}</pre></div>')
            body = f"""<div class="card"><h2>Check before importing</h2>
<table><tr><th>File</th><td>{esc(got['name'])} <span class="mut">read as {esc(freqdb.IMPORT_FORMATS[got['format']])}</span></td></tr>
<tr><th>Channels readable</th><td><b>{len(got['channels'])}</b>{f' <span class="mut">({got["repeats"]} repeated in the file, counted once)</span>' if got['repeats'] else ''}</td></tr>
<tr><th>New to your list</th><td><b>{len(got['channels']) - dup}</b></td></tr>
<tr><th>Already in your list</th><td>{dup} <span class="mut">(same frequency and tone)</span></td></tr></table>{problems}
<div class="scroll"><table class="list"><tr><th>MHz</th><th>Name</th><th>Mode</th><th>Tone / code</th><th>Group</th></tr>{sample}</table></div>
{f'<p class="mut">… and {len(got["channels"]) - 15} more</p>' if len(got['channels']) > 15 else ''}
<form method="post" action="/frequencies">{csrf_field(sess)}
<label>What to do with channels already in your list</label><select name="mode">
<option value="add">Keep mine, add only the new ones</option><option value="update">Overwrite mine with the file's</option>
<option value="replace">Replace my whole list with this file</option></select>
<label>Put every imported channel in this group (empty keeps the file's own)</label><input type="text" name="to_group" maxlength="40">
<div class="row"><button class="btn" name="do" value="import_apply"{' disabled' if not got['channels'] else ''}>Import</button>
<button class="btn sec" name="do" value="import_cancel">Cancel</button></div></form>
<p class="note">Nothing has changed yet. The list as it is now is kept as a backup when you import.</p></div>"""
            return self.render(sess, "Import", body, "/frequencies")
        fmts = "".join(f"<tr><th>{esc(t)}</th><td>{esc(d)}</td></tr>" for t, d in (
            ("Wavelens frequency list", "the JSON this page exports: everything comes back as it was"),
            ("Plain CSV", "any spreadsheet saved as CSV with a header row. Needs a frequency column (frequency, freq, mhz or frequency_mhz); "
                          "name, mode, tone, group, notes, input, lockout, lat and lon are read when present"),
            ("CHIRP CSV", "the generic CSV radio programming tools exchange: name, frequency, offset, tone or DCS, mode, skip"),
            ("Gqrx bookmarks", "a bookmarks.csv: frequency, name, modulation, bandwidth; the first tag becomes the group"),
            ("SDR++ frequency manager", "an exported list or the whole frequency manager file: name, frequency, bandwidth, mode"),
            ("RadioReference CSV", "your own export of a county's or agency's conventional frequencies. Trunked sites and talkgroups "
                                   "belong on the Area page instead")))
        body = f"""<div class="card"><form method="post" action="/frequencies" enctype="multipart/form-data">{csrf_field(sess)}
<label>A file from your phone or computer (up to 4 MB)</label><input type="file" name="file" required>
<div class="row"><button class="btn" name="do" value="import_upload">Read the file</button><a class="btn sec" href="/frequencies">Back</a></div></form>
<p class="note">The kind of file is worked out from what is in it. You see what was read, and what was left out and why, before
anything is added.</p></div>
<div class="card"><h2>Files this page reads</h2><table>{fmts}</table></div>"""
        self.render(sess, "Import", body, "/frequencies")

    def page_freq_backups(self, sess, store):
        rows = "".join(
            f'<tr><td>{esc(b["when"][:16].replace("T", " "))} UTC</td><td>{esc("?" if b["channels"] is None else b["channels"])}</td>'
            f'<td><a href="/frequencies/backup?{esc(urllib.parse.urlencode({"name": b["name"]}))}">Download</a></td>'
            f'<td><form class="inline" method="post" action="/frequencies">{csrf_field(sess)}<input type="hidden" name="name" value="{esc(b["name"])}">'
            f'<button class="btn sec" name="do" value="restore" style="margin:0">Put back</button></form></td></tr>' for b in store.backups())
        table = (f'<div class="scroll"><table class="list"><tr><th>Saved over at</th><th>Channels</th><th></th><th></th></tr>{rows}</table></div>'
                 if rows else '<p class="mut">No backups yet: one is kept each time the list changes.</p>')
        body = f"""<div class="card">{table}
<p class="note">Every change keeps the list it replaced, the last {freqdb.KEEP_BACKUPS} of them. Putting one back keeps today's
list as a backup too, so it can be undone. Backups stay on this Scanner (<code>{esc(store.backup_dir)}</code>); for a copy that
survives a lost SD card, export the list and keep the file.</p>
<a class="btn sec" href="/frequencies">Back to the list</a></div>
<div class="card"><h2>Start again</h2><form method="post" action="/frequencies">{csrf_field(sess)}
<label class="check"><input type="checkbox" name="sure" value="1"> Yes, remove every channel</label>
<button class="btn dng" name="do" value="clear">Empty the list</button></form>
<p class="note">Also the way out if the list file is damaged. The emptied list is kept as a backup.</p></div>"""
        self.render(sess, "Frequency backups", body, "/frequencies")

    def post_frequencies(self, sess, form, files, tok):
        if freqdb is None:
            return self.redirect("/frequencies")
        store, do = freqdb.Store(), form.get("do", "")
        back = "/frequencies" + self.freq_qs(form)
        try:
            if do == "save":
                return self.post_freq_save(sess, form, store, back)
            if do == "delete":
                if form.get("sure") != "1":
                    self.flash(sess, "Tick the box to confirm, then press Delete.")
                    return self.redirect(f'/frequencies/edit{self.freq_qs(form, id=form.get("id", ""))}')

                def drop(doc):
                    n = len(doc["channels"])
                    doc["channels"] = [c for c in doc["channels"] if c.get("id") != form.get("id")]
                    return n - len(doc["channels"])
                self.flash(sess, "Deleted." if store.edit(drop) else "That channel was already gone.")
                return self.redirect(back)
            if do == "bulk":
                ids = {k[3:] for k in form if k.startswith("id_")}
                act, to_group = form.get("act", ""), freqdb.text(form.get("to_group"), 40)
                if not ids:
                    self.flash(sess, "Tick the channels first.")
                elif act == "delete" and form.get("sure") != "1":
                    self.flash(sess, "Nothing deleted: tick “Yes, delete them” as well.")
                elif act == "group" and not to_group:
                    self.flash(sess, "Type the group name to move them to.")
                elif act in ("lock", "unlock", "group", "delete"):
                    def bulk(doc):
                        hit = [c for c in doc["channels"] if c.get("id") in ids]
                        if act == "delete":
                            doc["channels"] = [c for c in doc["channels"] if c.get("id") not in ids]
                        for c in hit:
                            if act in ("lock", "unlock"):
                                c["lockout"] = act == "lock"
                            elif act == "group":
                                c["group"] = to_group
                            c["updated"] = freqdb.now_iso()
                        return len(hit)
                    n = store.edit(bulk)
                    what = {"lock": "locked out", "unlock": "cleared", "group": f"moved to {to_group}", "delete": "deleted"}[act]
                    self.flash(sess, f"{n} channel{'' if n == 1 else 's'} {what}.")
                return self.redirect(back)
            if do == "import_upload":
                name, data = files.get("file") or ("", b"")
                if not data:
                    self.flash(sess, "Choose a file first.")
                    return self.redirect("/frequencies/import")
                try:
                    got = freqdb.parse_import(data)
                except ValueError as e:
                    self.flash(sess, f"Not imported: {e}.")
                    return self.redirect("/frequencies/import")
                got["name"] = freqdb.text(os.path.basename(name.replace("\\", "/")), 80) or "file"
                sess["freq_import"] = got
                return self.redirect("/frequencies/import")
            if do == "import_cancel":
                sess.pop("freq_import", None)
                return self.redirect("/frequencies")
            if do == "import_apply":
                got = sess.pop("freq_import", None)
                if not got:
                    self.flash(sess, "That import has expired: read the file again.")
                    return self.redirect("/frequencies/import")
                mode = form.get("mode") if form.get("mode") in ("add", "update", "replace") else "add"
                to_group = freqdb.text(form.get("to_group"), 40)
                if to_group:
                    for c in got["channels"]:
                        c["group"] = to_group
                n = store.edit(lambda doc: freqdb.merge(doc, got["channels"], mode), recover=mode == "replace")
                self.flash(sess, f"Imported from {got['name']}: {n['added']} added, {n['updated']} overwritten, {n['skipped']} already in your list.")
                return self.redirect("/frequencies")
            if do == "restore":
                n = store.restore(form.get("name", ""))
                self.flash(sess, f"Put back: {n} channel{'' if n == 1 else 's'}. The list it replaced is now the newest backup.")
                return self.redirect("/frequencies")
            if do == "clear":
                if form.get("sure") != "1":
                    self.flash(sess, "Nothing removed: tick the box to confirm.")
                    return self.redirect("/frequencies/backups")
                store.edit(lambda doc: doc.update(channels=[]), recover=True)
                self.flash(sess, "The list is empty. What it held is the newest backup.")
                return self.redirect("/frequencies")
        except freqdb.StoreError as e:
            self.flash(sess, f"Not done: {e}")
            return self.redirect("/frequencies/backups" if do in ("restore", "clear") else back)
        self.redirect("/frequencies")

    def post_freq_save(self, sess, form, store, back):
        cid = form.get("id", "")
        v = {k: form.get(k, "") for k in ("name", "freq", "input", "mode", "tone_type", "tone", "emission", "bandwidth", "group",
                                          "service", "tags", "lockout", "notes", "lat", "lon")}
        v.update(id=cid, stamp=form.get("stamp", ""))
        problem = {"text": ""}

        def save(doc):
            old = next((c for c in doc["channels"] if c.get("id") == cid), None) if cid else None
            if cid and old is None:
                problem["text"] = "this channel was deleted in another window. Saving now would add it again: press Save once more to do that."
                v["id"] = v["stamp"] = ""
                raise ValueError
            if old is not None and v["stamp"] and old.get("updated") != v["stamp"]:
                problem["text"] = "this channel was changed in another window since you opened it. Check the values below and save again."
                v["stamp"] = old.get("updated", "")
                raise ValueError
            raw = dict(v, freq_hz=v["freq"], input_hz=v["input"], lockout=v["lockout"] == "1", source=(old or {}).get("source", "manual"))
            raw.pop("bandwidth_hz", None)
            if v["bandwidth"].strip():
                try:
                    raw["bandwidth_hz"] = str(int(freqdb.decimal.Decimal(v["bandwidth"].replace(",", ".").strip()) * 1000))
                except (ArithmeticError, ValueError):
                    raw["bandwidth_hz"] = "x"            # clean_channel words the complaint
            try:
                ch = freqdb.clean_channel(raw, old)
            except ValueError as e:
                problem["text"] = str(e)
                raise
            twin = next((c for c in doc["channels"] if c is not old and freqdb.dedupe_key(c) == freqdb.dedupe_key(ch)), None)
            if twin is not None:
                problem["text"] = (f"{freqdb.mhz(ch['freq_hz'])} MHz with that tone is already in your list as “{twin['name']}”. "
                                   "Edit that one, or give this one its own tone or code.")
                raise ValueError
            if old is None:
                doc["channels"].append(ch)
            else:
                doc["channels"][doc["channels"].index(old)] = ch
            return ch
        try:
            ch = store.edit(save)
        except ValueError:
            try:
                doc = store.load()
            except freqdb.StoreError:
                doc = {"channels": []}
            current = next((c for c in doc["channels"] if c.get("id") == v["id"]), None) if v["id"] else None
            return self.page_freq_edit(sess, doc, v, current, form, errors=problem["text"])
        self.flash(sess, f"Saved {ch['name']} on {freqdb.mhz(ch['freq_hz'])} MHz.")
        self.redirect(back)

    # ---- /update ---------------------------------------------------------------------------
    def page_update(self, sess):
        u = update_status_json()
        auto = defaults_read().get("WAVELENS_AUTO_UPDATE", "0") == "1"
        dis = "disabled" if u["running"] else ""
        hist = update_history(10)
        hist_rows = "".join(
            f'<tr><td class="mut" style="white-space:nowrap">{esc(fmt_ts(h.get("at")))}</td><td>{esc(h.get("result") or "")}</td></tr>'
            for h in hist) or '<tr><td class="mut">No updates recorded yet.</td></tr>'
        prev = f' <span class="mut">(saved fallback: {esc(u["prev"])})</span>' if u["prev"] else ""
        body = f"""<div class="card"><div class="row" style="justify-content:space-between">
<div><div id="upill" class="spill">…</div></div>
<div class="note" id="checked">Last check: {esc(fmt_ts(u.get('checked_at')))}</div></div>
<table style="margin-top:10px">
<tr><th>Installed</th><td id="vinst"><b>{esc(u['installed'])}</b>{prev}</td></tr>
<tr><th>Available</th><td id="vavail">{f'<b class="ok">{esc(u["available"])}</b>' if u["available"] else '<span class="mut">none — up to date</span>'}</td></tr>
<tr><th>Last result</th><td id="vlast">{esc(u['last_result'] or '—')}</td></tr></table>
<form method="post" action="/update" class="row" id="uform">{csrf_field(sess)}
<button class="btn sec" name="do" value="check" {dis}>Check now</button>
<button class="btn" name="do" value="apply" {dis}>Install update</button>
<button class="btn sec" name="do" value="rollback" {dis} {'' if u['can_rollback'] else 'disabled title="No saved version yet"'}
 onclick="return confirm('Put the previous version back?')">Roll back to previous</button></form>
<p class="note">Updates are free for life and come from the public GitHub repo <code>pmcbrien/wavelens</code> (the Pi needs Wi-Fi).
An install takes one to three minutes: the scanner and this page restart near the end and this page reconnects by itself.
If the new version doesn't start, the saved one is put back automatically.</p></div>

<div class="card" id="pcard"><div class="row" style="justify-content:space-between"><h2 style="margin:0">Progress</h2>
<span class="note" id="elapsed"></span></div>
<div id="conn" class="flash" style="display:none;margin:10px 0 0"></div>
<ol class="steps" id="steps"><li class="mut">Nothing running. Start a check or an install above.</li></ol>
<div id="now" class="note"></div>
<div id="result" style="display:none"></div>
<details style="margin-top:10px"><summary class="note" style="cursor:pointer">Show the full log</summary>
<pre id="out">{esc(chr(10).join(u['lines']))}</pre></details></div>

<div class="card"><h2>Automatic updates</h2><form method="post" action="/update">{csrf_field(sess)}<input type="hidden" name="do" value="auto">
<label style="margin:0"><input type="checkbox" name="auto" value="1" {"checked" if auto else ""}> Install updates automatically overnight
(around 3 am, never while a phone is connected)</label>
<button class="btn sec" type="submit">Save</button></form></div>
<div class="card"><h2>History</h2><table>{hist_rows}</table></div>
<script>window.UPD={json.dumps(u)};</script>{UPDATE_JS}"""
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
                self.flash(sess, {"check": "Checking for an update …", "apply": "Installing the update — follow each step below.",
                                  "rollback": "Putting the previous version back — follow each step below."}[do])
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
