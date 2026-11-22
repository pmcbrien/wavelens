#!/bin/bash
# Build and install dsd-fme (Digital Speech Decoder, Florida Man Edition) and mbelib on a Raspberry Pi,
# so the Wavelens bridge can play DMR, P25 phase 1, NXDN and D-STAR voice through the phone.
# Takes about ten minutes on a Pi 4. Needs internet on the Pi (apt + GitHub). Re-run it to update.
#
# mbelib (companion/vendor/mbelib) implements voice codecs that are covered by patents in some countries. Its authors ask you to
# read https://github.com/lwvmobile/mbelib#readme before building it. This script will not proceed
# until you confirm you have.
set -e
printf '\nmbelib patent notice: https://github.com/lwvmobile/mbelib#readme\n'
if [ -n "${WAVELENS_YES:-}" ]; then
    echo "(accepted: WAVELENS_YES is set, e.g. by provision.sh with WAVELENS_DIGITAL_VOICE=1)"
else
    read -r -p "Have you read it, and is building it allowed where you are? [y/N] " ok
    [ "${ok,,}" = "y" ] || { echo "not building"; exit 2; }
fi

sudo apt-get update
sudo apt-get install -y libpulse-dev libsndfile1-dev libfftw3-dev liblapack-dev socat libusb-1.0-0-dev \
    libncurses5 libncurses5-dev libncursesw5-dev rtl-sdr librtlsdr-dev cmake git wget make build-essential \
    libcodec2-dev 2>/dev/null || sudo apt-get install -y libpulse-dev libsndfile1-dev libfftw3-dev liblapack-dev \
    socat libusb-1.0-0-dev libncurses-dev rtl-sdr librtlsdr-dev cmake git wget make build-essential libcodec2-dev
# Pager data (POCSAG / FLEX) on analog channels: the bridge uses multimon-ng when it is present.
sudo apt-get install -y multimon-ng || true

# Built from the copies in companion/vendor (pinned, tested versions), not from their projects' repositories.
VENDOR=$(cd "$(dirname "$0")/../vendor" && pwd)
[ -f "$VENDOR/mbelib/CMakeLists.txt" ] && [ -f "$VENDOR/dsd-fme/CMakeLists.txt" ] \
    || { echo "companion/vendor/mbelib or dsd-fme missing (update the Wavelens checkout)"; exit 1; }
work=$(mktemp -d)
cp -a "$VENDOR/mbelib" "$VENDOR/dsd-fme" "$work/"
cd "$work"
mkdir mbelib/build && cd mbelib/build && cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig
cd "$work"
mkdir dsd-fme/build && cd dsd-fme/build && cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig
cd / && rm -rf "$work"
echo
echo "dsd-fme installed: $(command -v dsd-fme)"
echo "Nothing else to configure: the bridge finds it on PATH. Restart the bridge if it is running:"
echo "  sudo systemctl restart wavelens-bridge"
