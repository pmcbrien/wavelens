#!/bin/bash
# End-to-end test of wavelens-update against a fake systemd: a good update, an update whose bridge won't load,
# an update whose bridge crash-loops after the restart, and an explicit rollback. In every failure case the
# Pi must end up on the last working version with the bridge running.
#
# Needs root (it installs into /usr/local/lib/wavelens and /etc/systemd/system like the real thing) — run it
# on a CI runner or in a throwaway container, never on a real Scanner:
#   sudo companion/pi/tests/test_update.sh
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PI=$(cd "$HERE/.." && pwd)
[ "$(id -u)" -eq 0 ] || { echo "run as root (CI or a container)"; exit 2; }
[ -e /usr/local/lib/wavelens/.real ] && { echo "refusing: this looks like a real Scanner"; exit 2; }

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
FAKE=$T/fake; BIN=$T/bin; mkdir -p "$FAKE" "$BIN"
: > "$FAKE/journal"; echo 0 > "$FAKE/restarts"; echo ok > "$FAKE/mode"

# ---- fake systemd: restart decides from the installed bridge whether it runs or crash-loops
cat > "$BIN/systemctl" <<EOF
#!/bin/bash
F=$FAKE; LIB=/usr/local/lib/wavelens
args=("\$@"); unit=""; for a in "\$@"; do case "\$a" in wavelens-*) unit=\$a;; esac; done
case "\$1" in
  restart)
    if [ "\$unit" = wavelens-bridge ]; then
      if grep -q "FAKE_CRASH" "\$LIB/wavelens_bridge.py"; then echo crash > \$F/mode
        echo "Traceback (most recent call last):" >> \$F/journal; echo "RuntimeError: fake crash" >> \$F/journal
      else echo ok > \$F/mode; echo "bridge: advertising as Wavelens-Pi-TEST" >> \$F/journal; fi
    fi ;;
  is-active)
    [ "\$unit" = wavelens-bridge ] || exit 3
    [ "\$(cat \$F/mode)" = ok ] && { [ "\${args[1]}" = -q ] || echo active; exit 0; }
    [ "\${args[1]}" = -q ] || echo activating; exit 3 ;;
  is-enabled) exit 1 ;;
  show)   # NRestarts: a crash-looping bridge restarts every time anyone looks
    if [ "\$(cat \$F/mode)" = crash ]; then echo \$(( \$(cat \$F/restarts) + 1 )) > \$F/restarts
      echo "Scheduled restart job" >> \$F/journal; fi
    cat \$F/restarts ;;
  *) exit 0 ;;
esac
EOF
printf '#!/bin/bash\ncat %s/journal\n' "$FAKE" > "$BIN/journalctl"
printf '#!/bin/bash\nexit 0\n' > "$BIN/sleep"          # health checks wait in real seconds; not here
printf '#!/bin/bash\nexit 0\n' > "$BIN/curl"           # no firmware download
chmod +x "$BIN"/*

# ---- an "origin" repo the updater pulls from, and the Pi's checkout of it
ORIGIN=$T/origin.git; WORK=$T/work; SRC=$T/src
git init -q --bare -b main "$ORIGIN"
git init -q -b main "$WORK"
mkdir -p "$WORK/companion/pi" "$WORK/packs"
cp "$PI"/wavelens_bridge.py "$PI"/smartnet.py "$PI"/run-bridge.sh "$PI"/regions.py "$PI"/spotters.py "$PI"/wavelens_admin.py \
   "$PI"/wavelens-admin "$PI"/wavelens-update "$PI"/wavelens-bridge.default "$PI"/*.service "$PI"/*.timer "$WORK/companion/pi/"
release() {   # release <version> [ok|noload|crash]
    echo "$1" > "$WORK/companion/VERSION"
    cp "$PI/wavelens_bridge.py" "$WORK/companion/pi/wavelens_bridge.py"
    case "${2:-ok}" in
      noload) sed -i '0,/^import /s//import wavelens_module_that_does_not_exist\nimport /' "$WORK/companion/pi/wavelens_bridge.py" ;;
      crash)  echo "# FAKE_CRASH" >> "$WORK/companion/pi/wavelens_bridge.py" ;;
    esac
    git -C "$WORK" add -A && git -C "$WORK" -c user.name=t -c user.email=t@example.com commit -qm "$1" && git -C "$WORK" push -q "$ORIGIN" main
}
git -C "$WORK" remote add origin "$ORIGIN"

U() { env PATH="$BIN:$PATH" WAVELENS_SRC="$SRC" WAVELENS_UPDATE_DETACHED=1 /usr/local/bin/wavelens-update "$@"; }
installed() { cat /usr/local/lib/wavelens/VERSION; }
result() { python3 -c "import json;print(json.load(open('/var/lib/wavelens/update.json'))['last_result'])"; }
fail() { echo "FAIL: $*"; echo "--- update.log"; cat /var/lib/wavelens/update.log 2>/dev/null; exit 1; }
pass() { echo "ok   $*"; }

# ---- install v1 the way install-service.sh would (files only)
release 1.0
git clone -q "$ORIGIN" "$SRC"
mkdir -p /usr/local/lib/wavelens /etc/systemd/system /var/lib/wavelens
rm -rf /usr/local/lib/wavelens.prev
install -m 0755 "$SRC/companion/pi/wavelens-update" /usr/local/bin/wavelens-update
cp "$SRC"/companion/pi/*.py "$SRC"/companion/pi/run-bridge.sh /usr/local/lib/wavelens/
cp "$SRC/companion/VERSION" /usr/local/lib/wavelens/VERSION

# 1. a good update installs and reports it
release 1.1
U apply >/dev/null 2>&1 || fail "good update returned an error"
[ "$(installed)" = 1.1 ] || fail "good update: installed $(installed), wanted 1.1"
[ "$(cat "$FAKE/mode")" = ok ] || fail "good update: bridge not running"
pass "good update 1.0 -> 1.1"

# 2. an update whose bridge won't even load is caught before the restart and the saved version is put back
release 1.2 noload
U apply >/dev/null 2>&1 && fail "non-loading update reported success"
[ "$(installed)" = 1.1 ] || fail "non-loading update: on $(installed), wanted 1.1 back"
[ "$(cat "$FAKE/mode")" = ok ] || fail "non-loading update: bridge left dead"
result | grep -q "would not start" || fail "non-loading update: result says '$(result)'"
pass "update that won't load -> stays on 1.1 ($(result))"

# 3. an update that crash-loops after the restart is rolled back by the health check
release 1.3 crash
U apply >/dev/null 2>&1 && fail "crash-looping update reported success"
[ "$(installed)" = 1.1 ] || fail "crash-looping update: on $(installed), wanted 1.1 back"
[ "$(cat "$FAKE/mode")" = ok ] || fail "crash-looping update: bridge left dead"
result | grep -q "did not start" || fail "crash-looping update: result says '$(result)'"
pass "update that crash-loops -> rolled back to 1.1 ($(result))"

# 4. the fix comes out; it installs over the good version
release 1.4
U apply >/dev/null 2>&1 || fail "fixed update returned an error"
[ "$(installed)" = 1.4 ] || fail "fixed update: installed $(installed)"
pass "next good update 1.1 -> 1.4"

# 5. an explicit rollback goes back to 1.1 and checks the bridge came up
U rollback >/dev/null 2>&1 || fail "rollback returned an error"
[ "$(installed)" = 1.1 ] || fail "rollback: on $(installed), wanted 1.1"
[ "$(cat "$FAKE/mode")" = ok ] || fail "rollback: bridge not running"
pass "rollback 1.4 -> 1.1"

# 6. a Pi already stuck on a broken version (tonight's case) must not save the broken one as the fallback
release 1.5 crash
U apply >/dev/null 2>&1 || true                            # fails, rolls back to 1.1, keeps 1.1 saved
echo "# FAKE_CRASH" >> /usr/local/lib/wavelens/wavelens_bridge.py; echo crash > "$FAKE/mode"
release 1.6
U apply >/dev/null 2>&1 || fail "update from a broken install returned an error"
[ "$(installed)" = 1.6 ] || fail "update from a broken install: installed $(installed)"
grep -q FAKE_CRASH /usr/local/lib/wavelens.prev/wavelens_bridge.py && fail "broken install was saved as the fallback"
pass "broken install -> 1.6, fallback kept the working copy"

echo "all updater tests passed"
