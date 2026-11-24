#!/usr/bin/env python3
"""Accept Bluetooth pairing requests on a Wavelens Scanner without anyone confirming a code.

A Scanner usually has no screen, and when it does (Raspberry Pi OS with the desktop) the panel's own
Bluetooth agent pops up "confirm the pairing code" on a monitor nobody is looking at, and the phone's
pairing waits and fails. This agent answers for the Pi instead, as a headless accessory does: capability
"NoInputNoOutput". An LE pairing confirmation is accepted (that is what keeps the phone free of a prompt);
legacy PIN / passkey pairing is refused, and a service is authorised only if it is the Wavelens service, so a
nearby computer that pairs silently gets no keyboard (HID), audio or serial profile out of it. main.conf also
puts the controller in LE-only mode (wavelens_bt_config.py), which leaves classic profiles nothing to use.

It also asks BlueZ to make it the default agent again every few seconds. The desktop panel registers its
agent at login and takes the default; asking again puts this one back in front, so the prompt never shows.

Installed by install-service.sh as wavelens-bt-agent.service (WAVELENS_BT_AUTOPAIR=0 leaves it out; turn it
off with  sudo systemctl disable --now wavelens-bt-agent). Receive-only like the rest of the Scanner: it only
answers BlueZ's questions. Needs python3-dbus and python3-gi, which the bridge already uses.
"""
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

AGENT_PATH = "/org/wavelens/btagent"
CAPABILITY = "NoInputNoOutput"
REASSERT_S = 5          # how often to ask to be the default agent again (cheap: one D-Bus call)
# The only service a Wavelens link uses ("Wavelens" in hex, see companion/README.md). Every other profile a
# paired device asks for — HID input above all (CVE-2023-45866 class keystroke injection) — is rejected.
ALLOWED_UUIDS = {"57617665-6c65-6e73-0100-000000000001"}


def log(msg: str) -> None:
    print(f"bt-agent: {msg}", file=sys.stderr, flush=True)


class Rejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class Agent(dbus.service.Object):
    """org.bluez.Agent1: say yes to everything a pairing asks, with no code to show or type."""

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        log("released by BlueZ")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        if str(uuid).lower() in ALLOWED_UUIDS:
            log(f"service {uuid} authorised for {device}")
            return
        log(f"service {uuid} refused for {device} (not the Wavelens service)")
        raise Rejected("only the Wavelens service is offered")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        log(f"legacy PIN pairing refused for {device}")
        raise Rejected("legacy PIN pairing is not offered")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        log(f"passkey entry refused for {device}")
        raise Rejected("passkey entry is not offered")

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log(f"pairing with {device} confirmed (code {int(passkey):06d})")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log(f"pairing with {device} authorised")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        log("pairing cancelled by the other side")


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    Agent(bus, AGENT_PATH)
    manager = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
    state = {"registered": False, "default": False}

    def assert_default() -> bool:
        try:
            if not state["registered"]:
                manager.RegisterAgent(AGENT_PATH, CAPABILITY)
                state["registered"] = True
                log(f"registered ({CAPABILITY})")
            manager.RequestDefaultAgent(AGENT_PATH)
            if not state["default"]:
                log("default agent: pairing requests are accepted without a prompt")
                state["default"] = True
        except dbus.DBusException as e:
            name = e.get_dbus_name() or ""
            if "AlreadyExists" in name:
                state["registered"] = True
            elif "DoesNotExist" in name or "ServiceUnknown" in name or "NoReply" in name:
                # bluetoothd restarted: register again on the next pass
                state["registered"] = state["default"] = False
            else:
                log(f"{name}: {e.get_dbus_message()}")
        return True                      # keep the timer

    # bluetoothd restarting drops every agent: register again as soon as it is back
    bus.add_signal_receiver(lambda name, old, new: (state.update(registered=False, default=False),
                                                     new and assert_default()),
                            signal_name="NameOwnerChanged", dbus_interface="org.freedesktop.DBus",
                            arg0="org.bluez")
    assert_default()
    GLib.timeout_add_seconds(REASSERT_S, assert_default)
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
