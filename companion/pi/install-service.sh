#!/bin/sh
# Installs the Wavelens bridge as a systemd service so it starts when the Pi boots.
# Run from this folder:  sudo ./install-service.sh
# Undo:                  sudo ./install-service.sh --remove
# Options (environment):
#   WAVELENS_HOSTNAME=name   the Pi's hostname, so the admin page is http://name.local:8080. Unset: a Pi still
#                            on the factory name "raspberrypi" becomes "wavelens"; a name somebody chose is kept.
#                            "keep" never renames.
#   WAVELENS_BT_AUTOPAIR=0   do not install the service that accepts Bluetooth pairing requests without a prompt
set -eu
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi

if [ "${1:-}" = "--remove" ]; then
    systemctl disable --now wavelens-bridge wavelens-admin wavelens-update.timer wavelens-bt-agent 2>/dev/null || true
    rm -f /etc/systemd/system/wavelens-bridge.service /etc/systemd/system/wavelens-admin.service \
          /etc/systemd/system/wavelens-bt-agent.service \
          /etc/systemd/system/wavelens-update.service /etc/systemd/system/wavelens-update.timer \
          /usr/local/bin/wavelens-update /usr/local/bin/wavelens-admin /usr/local/bin/wavelens-area
    rm -rf /usr/local/lib/wavelens
    systemctl daemon-reload
    echo "Removed. /etc/default/wavelens-bridge, the area setting and /var/lib/wavelens/area were kept."
    exit 0
fi

install -d /usr/local/lib/wavelens /usr/local/lib/wavelens/packs /usr/local/lib/wavelens/firmware /etc/wavelens /var/lib/wavelens
# The area folder: the licence pack wavelens-area downloads for the postcode set on this companion, and the
# owner's own RadioReference exports in rr/. The bridge reads it at start; nothing ships in it.
install -d /var/lib/wavelens/area /var/lib/wavelens/area/rr
install -m 0755 wavelens_bridge.py smartnet.py run-bridge.sh /usr/local/lib/wavelens/
install -m 0644 regions.py spotters.py area.py /usr/local/lib/wavelens/
install -m 0755 wavelens-area /usr/local/bin/wavelens-area
install -m 0644 ../../packs/??.json /usr/local/lib/wavelens/packs/ 2>/dev/null || true
install -m 0644 ../VERSION /usr/local/lib/wavelens/VERSION
install -m 0644 wavelens-bridge.default /usr/local/lib/wavelens/wavelens-bridge.default   # the admin page's help text
install -m 0644 wavelens-bridge.service /etc/systemd/system/
# Updates (wavelens-update: check / apply / rollback; a nightly timer checks, and installs only if
# WAVELENS_AUTO_UPDATE=1 in the settings file) and the admin page (http://<hostname>.local:8080).
install -m 0755 wavelens-update /usr/local/bin/wavelens-update
install -m 0644 wavelens-update.service wavelens-update.timer /etc/systemd/system/
( cd ../.. && pwd ) > /etc/wavelens/source          # the checkout wavelens-update pulls from
if [ -f wavelens_admin.py ]; then
    install -m 0755 wavelens_admin.py /usr/local/lib/wavelens/
    install -m 0755 wavelens-admin /usr/local/bin/wavelens-admin
    install -m 0644 wavelens-admin.service /etc/systemd/system/
fi
# Keep an existing settings file; only install the example the first time.
[ -f /etc/default/wavelens-bridge ] || install -m 0644 wavelens-bridge.default /etc/default/wavelens-bridge

# The SDR's kernel TV driver grabs the dongle before rtl_433 can.
[ -f /etc/modprobe.d/rtlsdr.conf ] || echo "blacklist dvb_usb_rtl28xxu" > /etc/modprobe.d/rtlsdr.conf

# The name the admin page is reached by. provision.sh sets it, but a hand install (git clone, then this
# script) used to leave a new Pi as "raspberrypi", and the http://wavelens.local:8080 in every instruction
# did not open.
WANT=${WAVELENS_HOSTNAME:-}
CUR=$(hostname)
if [ -z "$WANT" ] && [ "$CUR" = "raspberrypi" ]; then WANT=wavelens; fi
case "$WANT" in
    ""|keep|"$CUR") ;;
    -*|*-|*[!a-z0-9-]*)
        echo "WAVELENS_HOSTNAME must be lowercase letters, digits and hyphens: '$WANT' (the name stays $CUR)" ;;
    *)
        if [ "${#WANT}" -le 63 ] && command -v hostnamectl >/dev/null 2>&1 && hostnamectl set-hostname "$WANT"; then
            sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$WANT/" /etc/hosts
            grep -q "^127.0.1.1" /etc/hosts || printf '127.0.1.1\t%s\n' "$WANT" >> /etc/hosts
            echo "Hostname: $CUR -> $WANT"
        else
            echo "Could not set the hostname to '$WANT' (the name stays $CUR)"
        fi ;;
esac
# name.local only answers while avahi runs; restart it so a new name is announced now, not at the next boot
systemctl enable -q avahi-daemon 2>/dev/null || true
systemctl restart avahi-daemon 2>/dev/null || true

# Bluetooth pairing without a prompt (see wavelens-bt-agent.service). Needs bt-agent from the bluez-tools
# package, which provision.sh installs; without it the service is left out and pairing works as before.
if [ "${WAVELENS_BT_AUTOPAIR:-1}" != "0" ] && [ -f wavelens-bt-agent.service ]; then
    if command -v bt-agent >/dev/null 2>&1; then
        install -m 0644 wavelens-bt-agent.service /etc/systemd/system/
        BT_AGENT=1
    else
        echo "bt-agent not found (sudo apt install bluez-tools, then run this again): Bluetooth pairing without a prompt is off"
    fi
fi

systemctl daemon-reload
if [ "${BT_AGENT:-0}" = "1" ]; then systemctl enable -q --now wavelens-bt-agent || echo "wavelens-bt-agent did not start: journalctl -u wavelens-bt-agent"; fi
systemctl enable wavelens-bridge
systemctl enable --now wavelens-update.timer
[ -f /etc/systemd/system/wavelens-admin.service ] && systemctl enable --now wavelens-admin && systemctl restart wavelens-admin || true
# restart, not just start: on an update the service is already running the old copy
systemctl restart wavelens-bridge
sleep 3
systemctl --no-pager --lines=5 status wavelens-bridge || true
echo
echo "Settings: /etc/default/wavelens-bridge   Logs: journalctl -u wavelens-bridge -f"
echo "Admin page: http://$(hostname).local:8080   Updates: sudo wavelens-update check | apply | rollback"
echo "Area (local licence names): sudo wavelens-area set US 49749   (any country: COUNTRY POSTCODE)"
