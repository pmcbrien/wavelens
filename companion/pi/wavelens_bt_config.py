#!/usr/bin/env python3
"""Set BlueZ's /etc/bluetooth/main.conf so an iPhone or iPad links to the Pi with no pairing prompt.

The Wavelens link needs no pairing: the bridge's characteristics ask for no encryption. What made iOS show
"Bluetooth Pairing Request" anyway was bluetoothd itself. As soon as an iPad connects, bluetoothd's GATT
client reads the iPad's own services (it does this for every device that connects), some of those need
encryption, and iOS answers by asking its user to pair. With the GATT client off, bluetoothd never touches
the iPad's services, so nothing asks for encryption and iOS connects silently, like any BLE accessory.

JustWorksRepairing = always keeps a pairing that does happen (a computer pairing from its Bluetooth settings,
an iPad that forgot the Pi and pairs again) prompt-free on the Pi too, alongside wavelens-bt-agent. Whether the
Pi is pairable at all stays with the agent and the admin page's switch: nothing here turns pairing on.

Nothing on the Pi uses it as a Bluetooth central (Spotters plug in over USB), so the GATT client is not needed.

Run by install-service.sh:  python3 wavelens_bt_config.py [/etc/bluetooth/main.conf]
Prints "changed" when it rewrote the file (bluetoothd needs a restart then), "unchanged" otherwise.
Only the keys below are touched; comments and every other setting stay as they are.
"""
import re
import sys

SETTINGS = {
    "General": {"JustWorksRepairing": "always"},
    "GATT": {"Client": "false"},
}

_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def apply(text, settings=SETTINGS):
    """Return text with each key set in its section: an existing line (commented out or not) is replaced,
    a missing key is added at the end of its section, a missing section is appended."""
    lines = text.splitlines()
    out, section, done = [], None, {s: set() for s in settings}

    def flush(sec):
        # keys of the section just left that were not found: add them before the next section starts
        if sec in settings:
            for k, v in settings[sec].items():
                if k not in done[sec]:
                    while out and out[-1].strip() == "":
                        out.pop()
                    out.append(f"{k} = {v}")
                    out.append("")
                    done[sec].add(k)

    for line in lines:
        m = _SECTION.match(line)
        if m:
            flush(section)
            section = m.group(1).strip()
            out.append(line)
            continue
        if section in settings:
            km = re.match(r"^\s*#?\s*([A-Za-z]+)\s*=", line)
            if km and km.group(1) in settings[section] and km.group(1) not in done[section]:
                k = km.group(1)
                out.append(f"{k} = {settings[section][k]}")
                done[section].add(k)
                continue
        out.append(line)
    flush(section)
    for sec, keys in settings.items():
        missing = [k for k in keys if k not in done[sec]]
        if missing:
            if out and out[-1].strip():
                out.append("")
            out.append(f"[{sec}]")
            out.extend(f"{k} = {keys[k]}" for k in missing)
    return "\n".join(out).rstrip("\n") + "\n"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/etc/bluetooth/main.conf"
    try:
        with open(path) as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    new = apply(old)
    if new == old:
        print("unchanged")
        return
    import os
    if old and not os.path.exists(path + ".wavelens-bak"):       # keep the original, not the last edit
        with open(path + ".wavelens-bak", "w") as f:
            f.write(old)
    with open(path, "w") as f:
        f.write(new)
    print("changed")


if __name__ == "__main__":
    main()
