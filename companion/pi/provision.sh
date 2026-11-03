#!/bin/bash
# provision.sh — turn a fresh Raspberry Pi OS (Bookworm, Lite or Desktop) into a Wavelens Scanner, the way the
# units we ship are set up. Idempotent: run it again after an SD-card reimage, or to repair.
#
#   curl -fsSL https://raw.githubusercontent.com/pmcbrien/wavelens/main/companion/pi/provision.sh | sudo bash
#   (or from a checkout:  sudo companion/pi/provision.sh)
#
# What it does: apt packages (rtl-sdr, rtl_433, BlueZ, NetworkManager, avahi, esptool, git), the Python bridge
# dependency (bluezero), a checkout of the public repo at /opt/wavelens, hostname "wavelens" (so the admin page is
# http://wavelens.local:8080), install-service.sh (bridge + updater + nightly timer + admin page), and the kernel
# TV-driver blacklist. Digital voice (dsd-fme) is a separate, longer build with a patent notice: install-dsd.sh.
# Everything here is receive-only. Needs internet on the Pi while it runs.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
SRC=${WAVELENS_SRC:-/opt/wavelens}
REPO=https://github.com/pmcbrien/wavelens
HOST=${WAVELENS_HOSTNAME:-wavelens}

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git python3 python3-pip python3-dbus python3-gi rtl-sdr rtl-433 bluez bluez-tools \
    network-manager avahi-daemon esptool curl multimon-ng sox || \
apt-get install -y -q git python3 python3-pip python3-dbus python3-gi rtl-sdr rtl-433 bluez network-manager \
    avahi-daemon curl
# esptool from apt is missing on some images; pip is fine for a tool
command -v esptool.py >/dev/null 2>&1 || command -v esptool >/dev/null 2>&1 || pip3 install -q --break-system-packages esptool || true
python3 -c "import bluezero" 2>/dev/null || pip3 install -q --break-system-packages bluezero

echo "== checkout at $SRC"
if [ -d "$SRC/.git" ]; then
    git -C "$SRC" fetch -q origin main && git -C "$SRC" reset -q --hard origin/main
else
    git clone -q "$REPO" "$SRC"
fi
git -C "$SRC" config core.fileMode false
git config --global --add safe.directory "$SRC" 2>/dev/null || true

echo "== hostname $HOST (admin page at http://$HOST.local:8080)"
if [ "$(hostname)" != "$HOST" ]; then
    hostnamectl set-hostname "$HOST"
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOST/" /etc/hosts
    grep -q "^127.0.1.1" /etc/hosts || echo -e "127.0.1.1\t$HOST" >> /etc/hosts
fi
systemctl enable -q --now avahi-daemon bluetooth NetworkManager 2>/dev/null || true

echo "== bridge, updater, admin page"
chmod +x "$SRC"/companion/pi/*.sh "$SRC"/companion/pi/wavelens-update "$SRC"/companion/pi/wavelens-admin 2>/dev/null || true
(cd "$SRC/companion/pi" && ./install-service.sh)
mkdir -p /etc/wavelens && echo "$SRC" > /etc/wavelens/source
/usr/local/bin/wavelens-update check || true

echo
echo "Done. Version $(cat /usr/local/lib/wavelens/VERSION 2>/dev/null || echo ?)."
echo "  admin page:  http://$HOST.local:8080  (set the password on first visit)"
echo "  bridge log:  journalctl -u wavelens-bridge -f"
echo "  digital voice (P25/DMR):  sudo $SRC/companion/pi/install-dsd.sh   (10 min build, read its notice)"
echo "  pair it:     Wavelens app → More → Companion radio → Find → Pair"
