#!/bin/bash
# Started by wavelens-bridge.service. Reads its settings from /etc/default/wavelens-bridge.
#
#   WAVELENS_RTL433   rtl_433 arguments; empty = no RTL-SDR decoding
#   WAVELENS_ARGS     extra bridge flags, e.g. --hackrf-sweep --name Wavelens-Car
#
# Receive only: nothing started here transmits.
set -eu

BRIDGE=/usr/local/lib/wavelens/wavelens_bridge.py

# The last run couldn't register its Bluetooth advert. On some Pi images BlueZ stays stuck that way
# until it is restarted, so restart it before trying again (this service only Wants bluetooth, so
# that doesn't stop us).
MARK=/run/wavelens-advert-failed
if [ -e "$MARK" ]; then
    echo "bridge: last advert failed; restarting bluetooth first"
    rm -f "$MARK"
    systemctl restart bluetooth || true
    sleep 4
fi

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

# WAVELENS_AUTO=1: the bridge looks at what is plugged in (rtl_test, hackrf_info) and assigns roles —
# sensors, VHF/UHF sweeps, a listening dongle, the HackRF sweep — by how many radios there are.
# WAVELENS_ARGS then carries only the extras (masks, packs, name); WAVELENS_RTL433 only rtl_433's
# frequencies and gain (the -d is added here). Replug a dongle and restart: the plan is redone.
if [ "${WAVELENS_AUTO:-0}" = "1" ]; then
    eval "$(python3 $BRIDGE --plan)"
    WAVELENS_ARGS="${PLAN_ARGS:-} ${WAVELENS_ARGS:-}"
    if [ -n "${PLAN_RTL433_DEV:-}" ]; then
        WAVELENS_RTL433="$PLAN_RTL433_DEV ${WAVELENS_RTL433:--f 315M -f 433.92M -f 915M -H 30 -g 40}"
    else
        WAVELENS_RTL433=""
    fi
fi

# shellcheck disable=SC2086  # word splitting of the argument strings is intended
if [ -n "${WAVELENS_RTL433:-}" ]; then
    # The bridge is the main process and reads rtl_433 through process substitution, so if the
    # bridge exits the service exits (systemd then stops rtl_433 too), and if rtl_433 exits the
    # bridge sees end-of-input and exits. Either way systemd restarts both together.
    # -F log: without it rtl_433 hides its errors ("can't open device", "usb_claim_interface"), so a
    # dongle that has dropped off the bus looked like "stopped sending" with no reason in the journal.
    # Log lines go to stderr (the journal); JSON stays on stdout for the bridge.
    exec python3 -u $BRIDGE --rtl433 ${WAVELENS_ARGS:-} < <(exec rtl_433 -F log -F json $WAVELENS_RTL433)
else
    exec python3 -u $BRIDGE ${WAVELENS_ARGS:-}
fi
