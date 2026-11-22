#!/bin/bash
# Build and install APCO25 (companion/apco25 in this repository) on a Raspberry Pi, so the Wavelens bridge
# decodes trunked P25 systems with its CQPSK demodulator instead of dsd-fme. On simulcast systems (several
# towers sending the same signal: most county radio systems) it is clearly the better decoder. dsd-fme stays
# installed and is used for everything else (DMR, NXDN, EDACS, conventional digital) and whenever APCO25
# cannot start. Takes a few minutes on a Pi 5 (GNU Radio comes from apt). Needs internet on the Pi. Re-run it
# to update. GPL-3.0: see companion/apco25/LICENSE and NOTICE.md.
#
# APCO25 decodes P25 voice with an IMBE vocoder, which is covered by patents in some countries. Check that
# building and using it is allowed where you are.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
SRCDIR=${APCO25_SRC:-$HERE/../apco25}
DEST=/opt/apco25
[ -f "$SRCDIR/CMakeLists.txt" ] || { echo "APCO25 source not found at $SRCDIR (update the Wavelens checkout)"; exit 1; }
if [ -t 0 ] && [ -z "${WAVELENS_YES:-}" ]; then
    printf '\nAPCO25 decodes P25 voice with an IMBE vocoder; that codec is patented in some countries.\n'
    read -r -p "Is building it allowed where you are? [y/N] " ok
    [ "${ok,,}" = "y" ] || { echo "not building"; exit 2; }
fi
sudo apt-get update
sudo apt-get install -y gnuradio gnuradio-dev gr-osmosdr librtlsdr-dev libuhd-dev libhackrf-dev liborc-0.4-dev \
    cmake build-essential pkg-config python3-pybind11 python3-numpy python3-waitress python3-requests \
    libsndfile1-dev libspdlog-dev rsync
sudo mkdir -p "$DEST"
# the source tree is copied over; the build folder is kept, so a re-run rebuilds only what changed
sudo rsync -a --delete --exclude build "$SRCDIR"/ "$DEST"/
cd "$DEST"
sudo mkdir -p build && cd build
sudo cmake ../ >/dev/null
sudo nice -n 10 make -j"$(nproc)"
sudo make install >/dev/null && sudo ldconfig
test -f "$DEST/op25/gr-op25_repeater/apps/rx.py"
echo
echo "APCO25 installed. The bridge uses it for trunked P25 from its next start: sudo systemctl restart wavelens-bridge"
echo "(WAVELENS_TRUNK_DECODER=dsd-fme in /etc/default/wavelens-bridge goes back to dsd-fme.)"
