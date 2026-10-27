#!/bin/bash
# Started by wavelens-bridge.service. Reads its settings from /etc/default/wavelens-bridge.
#
#   WAVELENS_RTL433   rtl_433 arguments; empty = no RTL-SDR decoding
#   WAVELENS_ARGS     extra bridge flags, e.g. --hackrf-sweep --name Wavelens-Car
#
# Receive only: nothing started here transmits.
set -eu

BRIDGE=/usr/local/lib/wavelens/wavelens_bridge.py

# Wait for the Bluetooth adapter to come up after boot (up to ~30 s).
i=0
while [ $i -lt 30 ]; do
    if bluetoothctl show 2>/dev/null | grep -q "Powered: yes"; then break; fi
    bluetoothctl power on >/dev/null 2>&1 || true
    i=$((i + 1)); sleep 1
done

# Clear any LE advertising a previous run left on the controller (killed before it could withdraw
# it); otherwise BlueZ refuses the new advert. hciconfig is deprecated but present on Raspberry Pi OS.
if command -v hciconfig >/dev/null 2>&1; then hciconfig hci0 noleadv >/dev/null 2>&1 || true; fi

# shellcheck disable=SC2086  # word splitting of the argument strings is intended
if [ -n "${WAVELENS_RTL433:-}" ]; then
    # The bridge is the main process and reads rtl_433 through process substitution, so if the
    # bridge exits the service exits (systemd then stops rtl_433 too), and if rtl_433 exits the
    # bridge sees end-of-input and exits. Either way systemd restarts both together.
    exec python3 -u $BRIDGE --rtl433 ${WAVELENS_ARGS:-} < <(exec rtl_433 -F json $WAVELENS_RTL433)
else
    exec python3 -u $BRIDGE ${WAVELENS_ARGS:-}
fi
