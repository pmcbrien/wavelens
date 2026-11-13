#!/bin/bash
# Build and install OP25 (boatbod's fork) on a Raspberry Pi, so the Wavelens bridge decodes trunked P25
# systems with OP25's CQPSK demodulator instead of dsd-fme. On simulcast systems (several towers sending
# the same signal: most county radio systems) it is clearly the better decoder. dsd-fme stays installed
# and is used for everything else (DMR, NXDN, EDACS, conventional digital) and whenever OP25 cannot start.
# Takes a few minutes on a Pi 5 (GNU Radio comes from apt). Needs internet on the Pi. Re-run it to update.
#
# OP25's vocoder implements the P25 IMBE codec, which is covered by patents in some countries. Check that
# building and using it is allowed where you are.
set -e
OP25_REF=${OP25_REF:-1dbf51d4d8d4b09df617c7af094f43ecbedbee3a}   # tested on a Pi 5, Debian 13, GNU Radio 3.10
if [ -t 0 ] && [ -z "${WAVELENS_YES:-}" ]; then
    printf '\nOP25 decodes P25 voice with an IMBE vocoder; that codec is patented in some countries.\n'
    read -r -p "Is building it allowed where you are? [y/N] " ok
    [ "${ok,,}" = "y" ] || { echo "not building"; exit 2; }
fi
sudo apt-get update
sudo apt-get install -y gnuradio gnuradio-dev gr-osmosdr librtlsdr-dev libuhd-dev libhackrf-dev liborc-0.4-dev \
    cmake git build-essential pkg-config python3-pybind11 python3-numpy python3-waitress python3-requests \
    libsndfile1-dev libspdlog-dev
sudo mkdir -p /opt
if [ -d /opt/op25/.git ]; then
    sudo git -C /opt/op25 fetch -q --depth 1 origin "$OP25_REF" && sudo git -C /opt/op25 checkout -q FETCH_HEAD
else
    sudo git clone -q https://github.com/boatbod/op25 /opt/op25 && sudo git -C /opt/op25 checkout -q "$OP25_REF"
fi
cd /opt/op25
sudo mkdir -p build && cd build
sudo cmake ../ >/dev/null
sudo nice -n 10 make -j"$(nproc)"
sudo make install >/dev/null && sudo ldconfig
test -f /opt/op25/op25/gr-op25_repeater/apps/rx.py
echo
echo "OP25 installed. The bridge uses it for trunked P25 from its next start: sudo systemctl restart wavelens-bridge"
echo "(WAVELENS_TRUNK_DECODER=dsd-fme in /etc/default/wavelens-bridge goes back to dsd-fme.)"
