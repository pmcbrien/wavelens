# Keeping a Wavelens companion up to date

Updates are free for life, for the Spotter (ESP32) and the Scanner (Raspberry Pi) alike, whether you bought
one from us or built your own from this folder. Nothing phones home: a companion only reaches the network
when you ask it to update, and the phone app never does (it has no network permission).

One version number covers everything: `companion/VERSION` (a date, e.g. `2026.11.03`). The Pi bridge, the
ESP32 firmware and the app's bundled firmware all report it, so "is it current?" is one comparison. The app's
Companion screen shows the companion's version next to its name.

## Spotter (ESP32): three ways

| Way | Needs | How |
|---|---|---|
| **From the phone** (the usual way) | nothing but the app | *More → Companion radio*. When the app carries a newer firmware for the Spotter's chip than the Spotter reports, an **Update firmware** button appears (the app carries one image per chip: classic ESP32, S3, C3, C5, C6; the Spotter's hello says which it is). Tap it; the image goes over Bluetooth in about a minute, the Spotter reboots into it and reconnects by itself. Don't unplug it while the bar is moving. |
| **From a browser** | a computer with Chrome or Edge, a data USB cable | https://pmcbrien.github.io/wavelens/flash/ — plug in, click *Install*, pick the port. It reads the chip and installs the matching build. Also the way to put Wavelens on a blank ESP32 board, and the way to bring a Spotter with pre-2026.11 firmware (no Bluetooth updates yet) up to date once. |
| **From a Scanner Pi** | a Scanner, a USB cable | Plug the Spotter into the Pi's USB, open the Pi's admin page (`http://wavelens.local:8080` → *Spotter*), *Flash*. The Pi keeps every chip's current image from its own updates, asks the board which chip it is, and flashes the matching one. |

Command line, if you'd rather: `pip install esptool`, then
`esptool.py --chip esp32 write_flash 0x0 wavelens-esp32-merged.bin` (image on the
[firmware release](https://github.com/pmcbrien/wavelens/releases/tag/firmware)). For another chip swap both names:
`--chip esp32c6` and `wavelens-esp32c6-merged.bin`, and so on.

How the Bluetooth update is safe: the firmware has two slots (OTA partitions). The new image is written to
the spare slot and verified before the board switches to it; if it ever fails to boot, the bootloader
falls back to the one that was running. A cancelled or interrupted update leaves the old firmware in
place.

Where the firmware comes from: every change to `companion/esp32/` on `main` is built by CI
(`.github/workflows/firmware.yml`) and published as the rolling **firmware** release on this repo, plus a
`firmware-<version>` tag you can go back to. The Android build downloads every chip's image into the app, so a
new app release carries the newest firmware for each board.

## Scanner (Raspberry Pi): four ways

The Pi updates itself from this repository with `wavelens-update`, over its own Wi-Fi. Join it to your home
Wi-Fi once (admin page → *Wi-Fi*, or `nmtui` at the console).

| Way | How |
|---|---|
| **From the phone** | *More → Companion radio*: **Check for updates**, then **Install update** when one is waiting. The Pi reports "update available" in its hello, so the app shows it without asking. About a minute; the bridge restarts and the phone reconnects. **Roll back** puts the previous version back. |
| **From the admin page** | `http://wavelens.local:8080` → *Update*: the same three buttons with the live log, plus a switch for **automatic overnight updates**. |
| **Automatically** | A systemd timer (`wavelens-update.timer`) checks every night between 03:00 and 04:00 and 10 minutes after every boot. It only *installs* when `WAVELENS_AUTO_UPDATE=1` is set in `/etc/default/wavelens-bridge` (the admin page's switch), and never while a phone is connected. |
| **At the console** | `sudo wavelens-update check` / `apply` / `rollback` / `status`. The old `companion/pi/update.sh` still works and now calls the same tool. |

### An update can't leave the Scanner dead

What an update does, in order, and what happens if any step goes wrong:

1. **Save the working version.** The current install (and the updater, admin page and service files it would
   replace) is copied to `/usr/local/lib/wavelens.prev` — but only if the bridge is actually running right
   now. A Pi that is already broken keeps its earlier saved copy, so the fallback is always a version that worked.
2. **Fetch and install** the new bridge, admin page, region packs, `VERSION` and the Spotter firmware image.
   If installing fails part-way, the saved version goes straight back.
3. **Try it before switching.** The new bridge must load on this Pi's Python. If it doesn't — the 4 Nov
   case, code that needed Python 3.10 on a Pi with 3.9 — the saved version goes back and the running bridge
   is never interrupted.
4. **Restart and watch.** The bridge must stay up without systemd having to restart it, and get as far as
   advertising to the phone, within about 25 seconds. If it crashes or keeps restarting, the saved version
   goes back.
5. **Check the rollback too.** After putting the saved version back, the same health check runs again.

Started from the phone or the admin page, the update runs in a systemd unit of its own
(`wavelens-update-run`), so restarting the bridge or the admin page can't cut it off half-way. The result —
"updated to X", or "update to X did not start, so Y was put back — <the error>" — is in
`/var/lib/wavelens/update.json`, shown on the admin page's Status and Update pages and on the phone's
Companion screen. The step-by-step log is `/var/lib/wavelens/update.log`.

Your settings (`/etc/default/wavelens-bridge`), RadioReference packs and the admin password are never touched.

CI (`.github/workflows/pi.yml`) loads the bridge on Python 3.9, 3.11 and 3.13 and runs the updater against
a fake systemd — a good update, one that won't load, one that crash-loops, a rollback, and an update from an
already-broken install — before anything reaches a Pi.

## A fresh Pi, the way we ship them

`companion/pi/provision.sh` (run with sudo on a fresh Raspberry Pi OS) installs the packages, clones the public repo to `/opt/wavelens`, sets the hostname to `wavelens`, and runs `install-service.sh` — bridge, updater, nightly timer and admin page in one go. Digital voice is the separate `install-dsd.sh` (longer build, patent notice).

## Everything else on the Pi

The operating system and the radio tools (`rtl_433`, `rtl-sdr`, `dsd-fme`) are not touched by
`wavelens-update`; `sudo apt update && sudo apt full-upgrade` once in a while is the usual Raspberry Pi OS
advice, and the admin page's *Power → Reboot* afterwards.

## For the person releasing

1. Change what you need under `companion/`, bump `companion/VERSION` to today's date (add `.1`, `.2` for a
   second release the same day), push to `main`. Check that **Pi companion** and **Spotter firmware** are green.
2. CI mirrors `companion/` to the public repo (`android.yml`), builds the ESP32 image and publishes the
   firmware release (`firmware.yml`), and the next app build bundles it.
3. Every Pi that checks tonight sees the new version; phones see it the next time they connect to one.
   The ESP32 image reaches phones with the next app release on Google Play.
