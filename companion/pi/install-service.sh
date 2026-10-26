#!/bin/sh
# Installs the Wavelens bridge as a systemd service so it starts when the Pi boots.
# Run from this folder:  sudo ./install-service.sh
# Undo:                  sudo ./install-service.sh --remove
set -eu
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi

if [ "${1:-}" = "--remove" ]; then
    systemctl disable --now wavelens-bridge 2>/dev/null || true
    rm -f /etc/systemd/system/wavelens-bridge.service
    rm -rf /usr/local/lib/wavelens
    systemctl daemon-reload
    echo "Removed. /etc/default/wavelens-bridge was kept."
    exit 0
fi

install -d /usr/local/lib/wavelens
install -m 0755 wavelens_bridge.py run-bridge.sh /usr/local/lib/wavelens/
install -m 0644 wavelens-bridge.service /etc/systemd/system/
# Keep an existing settings file; only install the example the first time.
[ -f /etc/default/wavelens-bridge ] || install -m 0644 wavelens-bridge.default /etc/default/wavelens-bridge

# The SDR's kernel TV driver grabs the dongle before rtl_433 can.
[ -f /etc/modprobe.d/rtlsdr.conf ] || echo "blacklist dvb_usb_rtl28xxu" > /etc/modprobe.d/rtlsdr.conf

systemctl daemon-reload
systemctl enable wavelens-bridge
# restart, not just start: on an update the service is already running the old copy
systemctl restart wavelens-bridge
sleep 3
systemctl --no-pager --lines=5 status wavelens-bridge || true
echo
echo "Settings: /etc/default/wavelens-bridge   Logs: journalctl -u wavelens-bridge -f"
