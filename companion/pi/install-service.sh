#!/bin/sh
# Installs the Wavelens bridge as a systemd service so it starts when the Pi boots.
# Run from this folder:  sudo ./install-service.sh
# Undo:                  sudo ./install-service.sh --remove
set -eu
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi

if [ "${1:-}" = "--remove" ]; then
    systemctl disable --now wavelens-bridge wavelens-admin wavelens-update.timer 2>/dev/null || true
    rm -f /etc/systemd/system/wavelens-bridge.service /etc/systemd/system/wavelens-admin.service \
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

systemctl daemon-reload
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
