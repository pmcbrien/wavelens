#!/bin/bash
# provision.sh — turn a fresh Raspberry Pi OS (Bookworm, Lite or Desktop) into a Wavelens Scanner, the way the
# units we ship are set up. Idempotent: run it again after an SD-card reimage, or to repair.
#
#   curl -fsSL https://raw.githubusercontent.com/pmcbrien/wavelens/main/companion/pi/provision.sh | sudo bash
#   (or from a checkout:  sudo companion/pi/provision.sh)
#   The page at https://pmcbrien.github.io/wavelens/pi/ builds this command with the options below filled in.
#
# Options (environment; with the one-liner:  ... | sudo env WAVELENS_HOSTNAME=car bash):
#   WAVELENS_HOSTNAME=name     the Pi's hostname, so the admin page is http://name.local:8080 (default wavelens)
#   WAVELENS_RTLSDR_BLOG=auto  the RTL-SDR Blog driver, which an RTL-SDR Blog V4 dongle needs on an OS whose own
#                              rtl-sdr package is older than 2.0 (Bookworm): auto = build it when a V4 is plugged
#                              in (default), 1 = build it anyway, 0 = never. Adds about five minutes.
#   WAVELENS_SRC=/opt/wavelens where the checkout goes
#   WAVELENS_BT_AUTOPAIR=1     accept a phone's or computer's Bluetooth pairing request without a prompt (the Pi
#                              has no screen to confirm on). 0 leaves that off.
#   WAVELENS_COUNTRY=US        where it will be used: a two-letter country and a postcode (WAVELENS_ZIP works
#   WAVELENS_POSTCODE=49749    too). Downloads the local licence names for that place, where a public licence
#                              database exists (today the US), so hits read "Example County Road Commission"
#                              rather than "151 MHz carrier". Any time later: sudo wavelens-area set US 49749,
#                              or the admin page's Area tab. Without it the Scanner loads no local data.
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
BLOG=${WAVELENS_RTLSDR_BLOG:-auto}
POSTCODE=${WAVELENS_POSTCODE:-${WAVELENS_ZIP:-}}
COUNTRY=${WAVELENS_COUNTRY:-}
WARNINGS=""
warn() { echo "  !! $1"; WARNINGS="$WARNINGS
  - $1"; }

case "$HOST" in
    ""|-*|*-|*[!a-z0-9-]*) echo "WAVELENS_HOSTNAME must be lowercase letters, digits and hyphens: '$HOST'"; exit 1 ;;
esac
case "$BLOG" in auto|0|1) ;; *) echo "WAVELENS_RTLSDR_BLOG must be auto, 0 or 1: '$BLOG'"; exit 1 ;; esac
case "$COUNTRY" in ""|[A-Za-z][A-Za-z]) ;; *) echo "WAVELENS_COUNTRY must be a two-letter country code: '$COUNTRY'"; exit 1 ;; esac
case "$POSTCODE" in *[!A-Za-z0-9\ -]*) echo "WAVELENS_POSTCODE must be letters, digits, spaces and hyphens: '$POSTCODE'"; exit 1 ;; esac

# RTL-SDR dongles on the USB bus, read from sysfs so it works before any package is installed.
rtl_dongles() {
    for d in /sys/bus/usb/devices/*; do
        [ -r "$d/idVendor" ] && [ -r "$d/idProduct" ] || continue
        [ "$(cat "$d/idVendor")" = "0bda" ] || continue
        case "$(cat "$d/idProduct")" in 2832|2838) echo "$(cat "$d/product" 2>/dev/null || echo RTL-SDR)" ;; esac
    done
}

echo "== checking this computer"
MODEL="unknown computer"
[ -r /proc/device-tree/model ] && MODEL=$(tr -d '\0' < /proc/device-tree/model)
OSNAME=$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown OS}")
echo "  $MODEL, $OSNAME, $(uname -m)"
command -v apt-get >/dev/null 2>&1 || { echo "This needs Raspberry Pi OS (or another Debian) with apt."; exit 1; }
case "$MODEL" in
    *"Raspberry Pi Zero"*|*"Raspberry Pi 3 Model B Rev"*|*"Raspberry Pi 2"*|*"Raspberry Pi Model"*)
        warn "$MODEL is not enough for a Scanner (see companion/HARDWARE.md); a Pi 4 or Pi 5 is what works." ;;
    *"Raspberry Pi"*) ;;
    *) warn "This is not a Raspberry Pi. The bridge may still run, but it is only tested on a Pi." ;;
esac
# A Pi 5 on a supply that didn't negotiate 5 A holds its four USB ports to 600 mA between them.
PSU=/proc/device-tree/chosen/power/max_current
if [ -r "$PSU" ]; then
    HEX=$(od -An -tx1 "$PSU" 2>/dev/null | tr -d ' \n')
    case "$HEX" in ""|*[!0-9a-f]*) MA=0 ;; *) MA=$((16#$HEX)) ;; esac
    if [ "$MA" -gt 0 ] && [ "$MA" -lt 5000 ]; then
        warn "Power supply offers ${MA} mA: this Pi limits USB to 600 mA in total. More than one dongle needs the 27 W (5 A) supply or a powered hub."
    fi
fi
if command -v curl >/dev/null 2>&1 && ! curl -sS -m 15 -o /dev/null https://github.com; then
    echo "No internet from this Pi (github.com unreachable). Connect it and run this again."; exit 1
fi

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git python3 python3-pip python3-dbus python3-gi rtl-sdr rtl-433 uhubctl bluez bluez-tools \
    network-manager avahi-daemon esptool curl multimon-ng sox || \
apt-get install -y -q git python3 python3-pip python3-dbus python3-gi rtl-sdr rtl-433 uhubctl bluez network-manager \
    avahi-daemon curl
# esptool from apt is missing on some images; pip is fine for a tool
command -v esptool.py >/dev/null 2>&1 || command -v esptool >/dev/null 2>&1 || pip3 install -q --break-system-packages esptool || true
python3 -c "import bluezero" 2>/dev/null || pip3 install -q --break-system-packages bluezero

# The RTL-SDR Blog V4 has an R828D tuner. rtl-sdr 2.0 and later drive it; the 0.6 that Bookworm ships shows
# "[R82XX] PLL not locked!", tunes wrong and rtl_power hangs. There, build the RTL-SDR Blog fork as Debian packages
# that replace librtlsdr0 / rtl-sdr in place, so rtl_433 and everything else linked against librtlsdr picks it up.
# A failed build is reported and the rest of the install carries on with the stock driver.
blog_driver() {
    cur=$(dpkg-query -W -f='${Version}' rtl-sdr 2>/dev/null || echo 0)
    case "$cur" in *rtlsdrblog*) echo "  RTL-SDR Blog driver already installed ($cur)"; return 0 ;; esac
    if dpkg --compare-versions "$cur" ge 2.0; then
        echo "  rtl-sdr $cur already drives the RTL-SDR Blog V4; nothing to build"; return 0
    fi
    echo "  building the RTL-SDR Blog driver (replaces rtl-sdr $cur; about five minutes)"
    apt-get install -y -q build-essential cmake pkg-config debhelper libusb-1.0-0-dev || return 1
    work=$(mktemp -d)
    git clone -q --depth 1 https://github.com/rtlsdrblog/rtl-sdr-blog "$work/rtl-sdr-blog" || { rm -rf "$work"; return 1; }
    if ! (cd "$work/rtl-sdr-blog" && dpkg-buildpackage -b --no-sign > "$work/build.log" 2>&1); then
        tail -n 20 "$work/build.log"; rm -rf "$work"; return 1
    fi
    apt-mark unhold librtlsdr0 librtlsdr-dev rtl-sdr >/dev/null 2>&1 || true
    dpkg -i "$work"/librtlsdr0_*.deb "$work"/librtlsdr-dev_*.deb "$work"/rtl-sdr_*.deb || { rm -rf "$work"; return 1; }
    # Otherwise the next apt upgrade could put the stock packages back.
    apt-mark hold librtlsdr0 librtlsdr-dev rtl-sdr >/dev/null
    rm -rf "$work"
    echo "  RTL-SDR Blog driver installed ($(dpkg-query -W -f='${Version}' rtl-sdr))"
}
echo "== RTL-SDR driver"
V4=0; rtl_dongles | grep -q "Blog V4" && V4=1
if [ "$BLOG" = "1" ] || { [ "$BLOG" = "auto" ] && [ "$V4" = "1" ]; }; then
    blog_driver || warn "The RTL-SDR Blog driver did not build; the stock driver is still in place. A Blog V4 dongle will not tune right until it does: run this again, or see companion/HARDWARE.md."
elif [ "$V4" = "1" ]; then
    warn "An RTL-SDR Blog V4 is plugged in and its driver was skipped (WAVELENS_RTLSDR_BLOG=0)."
else
    echo "  stock rtl-sdr $(dpkg-query -W -f='${Version}' rtl-sdr 2>/dev/null || echo '?') (no RTL-SDR Blog V4 plugged in)"
fi

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

if [ -n "$POSTCODE" ]; then
    echo "== area ${COUNTRY:-US} $POSTCODE"
    # the bridge is restarted by the tool once the data is in place; a failure here is not a failed install
    /usr/local/bin/wavelens-area set --country "${COUNTRY:-US}" --postcode "$POSTCODE" \
        || warn "The area was not set. Try again when the Pi is online: sudo wavelens-area set ${COUNTRY:-US} $POSTCODE"
fi

echo
echo "== radios"
FOUND=$(rtl_dongles)
if [ -n "$FOUND" ]; then
    echo "$FOUND" | sed 's/^/  RTL-SDR: /'
else
    echo "  no RTL-SDR plugged in yet. Plug one in any time: the bridge picks it up by itself."
fi
bluetoothctl show 2>/dev/null | grep -q "Controller" || warn "No Bluetooth adapter found; the app links to the Scanner over Bluetooth."
if [ -n "$WARNINGS" ]; then
    echo
    echo "Worth a look:$WARNINGS"
fi
echo
echo "Done. Version $(cat /usr/local/lib/wavelens/VERSION 2>/dev/null || echo ?)."
echo "  admin page:  http://$HOST.local:8080  (set the password on first visit)"
echo "  bridge log:  journalctl -u wavelens-bridge -f"
[ -n "$POSTCODE" ] || echo "  area:        sudo wavelens-area set US 49749   (COUNTRY POSTCODE; or the admin page's Area tab)"
echo "  digital voice (P25/DMR):  sudo $SRC/companion/pi/install-dsd.sh   (10 min build, read its notice)"
echo "  pair it:     Wavelens app → More → Companion radio → Find → Pair"
