# Wavelens

**See what's transmitting around you.** Wavelens is a passive radio scanner for Android. It listens to the Bluetooth, Wi‑Fi and cellular signals that nearby devices already broadcast, classifies them against an offline signature pack, and tells you in plain language what they are — item trackers, fixed cameras and licence‑plate readers, body‑worn cameras, drones, doorbells, phones, vehicles — and whether any of them is moving with you.

It never connects to, pairs with or transmits to anything. It has **no network access**: no account, no analytics, no uploads. Everything stays on the phone.

<p align="center">
  <a href="https://github.com/pmcbrien/wavelens/releases/latest/download/wavelens-debug.apk"><img alt="Download the APK" src="https://img.shields.io/badge/Download-Wavelens%20APK-2ea44f?style=for-the-badge&logo=android&logoColor=white"></a>
  &nbsp;
  <a href="https://github.com/pmcbrien/wavelens/releases/latest"><img alt="All releases" src="https://img.shields.io/github/v/release/pmcbrien/wavelens?style=for-the-badge&label=Latest%20release"></a>
</p>

> **Free APK, no sign-up.** Builds are published under **[Releases](https://github.com/pmcbrien/wavelens/releases/latest)** (right-hand sidebar on the repo page). Direct link: [wavelens-debug.apk](https://github.com/pmcbrien/wavelens/releases/latest/download/wavelens-debug.apk). Android 8.0+ (API 26).

<p align="center">
  <img src="docs/screenshots/live-icons-freq.png" width="30%" alt="Live list with device icons, signal and frequency">
  <img src="docs/screenshots/card-signal-adv-ring.png" width="30%" alt="Expanded card: signal history, advertising interval, RSSI ring">
  <img src="docs/screenshots/flagged-axon.png" width="30%" alt="A flagged body camera recognised by fingerprint">
</p>

*Screenshots are from real drives. GPS coordinates are blacked out; some are from earlier builds that still carried the working title "SIGINT Scope".*

---

## Beta testers wanted

Wavelens is heading to the Google Play Store. Before it can go live, Google requires a closed test with real users — and testers get the Play Store version **free, permanently**, including every update after launch.

To join:

1. Email **[wavelensapp@gmail.com](mailto:wavelensapp@gmail.com?subject=Wavelens%20beta%20tester)** with the **Google account address** you use on your Android phone (the one signed into the Play Store). Testers are added by Google account, so a Gmail or Google Workspace address is required.
2. You'll get an invite link back. Accept it, install from the Play Store, and use the app normally for a couple of weeks — driving is where it earns its keep.
3. Send bugs, false alerts and "what was that device?" captures to the same address or open an issue here. The CSV export on the Setup screen has everything needed to write a new signature rule.

No fixed commitment beyond actually using it. Twelve people is the minimum Google needs; more is better.

---

## Install

1. On your phone, open [github.com/pmcbrien/wavelens/releases/latest](https://github.com/pmcbrien/wavelens/releases/latest) and tap the `.apk` under **Assets** — or use the direct link above.
2. Open it. Android will ask you to allow installs from your browser — allow it once.
3. Open Wavelens, tap **SCAN**, read the short disclosure, and grant:
   - **Nearby devices / Bluetooth** — to hear BLE advertisements and classic discovery.
   - **Location (precise)** — Android requires it for any Bluetooth/Wi‑Fi scan results; Wavelens also uses it to geotag detections and to tell when something is moving with you.
   - **Notifications** — the persistent "scanning" notification and alerts.

The first scan takes a few seconds to populate. In a city you'll see hundreds of emitters within a minute; the Live screen hides the boring ones by default (see *Main screen* in Setup).

`wavelens-release.apk` is signed with the app's release key and will update cleanly to the Play Store version later. `wavelens-debug.apk` is the development build — same features, but it can't be upgraded over by the release build (uninstall first).

---

## What it finds

| Category | Examples | Colour |
|---|---|---|
| ALPR / fixed camera | Flock, Motorola/Vigilant, Genetec, Axis, Avigilon, Verkada | red |
| Acoustic sensor | ShotSpotter‑class gunshot sensors | red |
| Drone / UAS | DJI, Autel, Parrot, Skydio; any aircraft broadcasting Remote ID | red |
| Body cam / LE gear | Axon, Motorola/WatchGuard, Getac, in‑car LTE routers | red |
| Pentest / RF tool | Flipper Zero, HackRF, Ubertooth, Pwnagotchi, O.MG | red |
| Item tracker | AirTag, SmartTag, Tile, Chipolo, Find My / FMDN beacons | amber |
| Traffic / MAC sensor | Bluetooth travel‑time and occupancy sensors | amber |
| Camera / doorbell | Ring, Nest, Arlo, Wyze, Blink, Kuna, Eufy | amber |
| Vehicle / TPMS | BLE tyre sensors, OBD dongles, car head units | blue |
| Phone / tablet | Apple Continuity, Android, Windows | blue |
| Wearable / audio | watches, earbuds, hearing aids | blue |
| IoT sensor, beacon, AP / network gear | smart home, iBeacon/Eddystone, routers | green |
| Cell site | your serving and neighbour towers (RF tab) | grey |

Everything else is bucketed by manufacturer using the IEEE OUI and Bluetooth SIG company registries, which ship inside the app. Confidence is shown as **Possible / Likely / Confirmed** — a vendor prefix alone is *Possible*; a structured, self‑describing frame (Remote ID, Find My) is *Confirmed*.

<p align="center">
  <img src="docs/screenshots/live-le-filter.png" width="30%" alt="Live list filtered to Body cam / LE gear">
  <img src="docs/screenshots/drone-dji.png" width="30%" alt="A DJI device classified as drone">
  <img src="docs/screenshots/fastpair-tracker.png" width="30%" alt="Fast Pair earbuds shown with the weak-match note">
</p>

The rule that matched is always shown on the card with a one‑line reason, including when the match is weak ("also matches ordinary headphones, so treat as weak"). The classifier is a best‑effort guess from public signatures — look before you act on it.

---

## The screens

### Live
The main list. Strongest signal first, with a glyph for the device kind (🔔 doorbell, 🛞 TPMS, 🎥 body cam, 🚁 drone, 🏷 tracker, 📷 camera …), the current dBm, the last frequency heard (Wi‑Fi channel, BLE PHY, or cell band), an estimated distance, and badges: **NEW** highlight box, **FLAGGED**, **FOLLOWING**, **RIDING / PACING**, **MINE**, **WITH OWNER / SEPARATED** for trackers. Filter chips across the top narrow by category. Tap a card for details; alert‑worthy cards open expanded automatically.

<p align="center">
  <img src="docs/screenshots/card-expanded.png" width="30%" alt="Expanded card with match reason, SSID, channel, capabilities">
</p>

A card's details include first/last seen, packet count, signal now/best/average, advertising interval and whether the payload is static, the services and company IDs it advertises, the matched rules with their explanations, the raw manufacturer data, dwell time, and the peak‑signal fix. Buttons: **Flag**, **Mine**, **Ignore "<name>"**.

### HUD
A driving view: one giant band for the single most important emitter (flagged > following > riding with you > category threat), big chips, and a status strip (emitters heard, seconds since the last packet, scanner restarts). Tap anywhere to switch between *priority* and *strongest‑signal* ordering; long‑press the top band to flag it.

### Alerts
Every alert that fired, newest first, with a **Recurring devices** block at the top. The alert log is also written to disk as CSV as it happens, so a crash or battery death loses nothing. Export from Setup.

### Map
Trails of moving devices by name over a chosen time window, and **convoys** — groups of devices that keep turning up together.

### RF
Serving and neighbour cell sites with tech (LTE / 5G NR / WCDMA / GSM), PCI/CID, channel number and the downlink frequency it maps to, and signal strength. A 2G fallback on the serving cell is called out.

### Setup
Everything is configurable. Highlights below.

<p align="center">
  <img src="docs/screenshots/setup-alerts.png" width="30%" alt="Setup: alert categories, re-alert interval, main screen threshold">
</p>

---

## Features in depth

### Alerts while you drive
Distinct tones for trackers, cameras/ALPR, and body cams, plus spoken alerts ("Possible body cam, RAI‑SIGNAGE, close"), screen flash, torch strobe, vibration and a heads‑up notification — each one a toggle. Alerts can be forced to the phone speaker so they aren't swallowed by a car's Bluetooth audio. Pick which categories alert and how long before the same device re‑alerts. The scanner keeps running with the screen off; the screen stays on while the app is in front.

### Flag once, recognise forever
Phones and trackers rotate their Bluetooth address every few minutes, so a MAC is useless for "is that the same device?". **Flag** builds a weighted fingerprint from what *doesn't* change — vendor prefix, service UUIDs, manufacturer‑data prefix, service‑data prefix, name, Wi‑Fi cipher suite and channel, advertising‑interval class, static payload — and matches on that. A device scoring ≥ 4 with at least one identity‑bearing trait is shown **FLAGGED** again, address or no address. Every body‑cam, ALPR and acoustic alert is auto‑fingerprinted, so the second time you pass the same camera it says **Known** rather than *Possible*.

### Riding with you
A device that holds a steady signal while you cover ground is in or on your vehicle. Every track with enough packets keeps a signal‑strength ring; every 10 seconds it is scored against your own GPS motion. Steady (IQR ≤ 10 dB) over 400 / 1 500 / 5 000 m becomes **POSSIBLE / LIKELY / CONFIRMED**, with an **in vehicle** tier when it's strong and very flat; stop‑and‑go traffic or a 90° turn with the signal unchanged promotes it a level. Scanner stalls and restarts are treated as gaps, not as "device gone". Traffic jams and parking don't count — the window needs real path length and speed. Alerts fire at LIKELY. Tap **Mine** on your own earbuds, watch or car and they're fingerprinted and never alert. Scope the alerts to trackers only, everything but vehicle/phone/wearable, or everything.

### Following
Separate from riding: a device seen at three or more positions ≥ 120 m apart within four minutes is **FOLLOWING**; a separated Find My / FMDN tracker needs only two.

### Encounter history and baseline
An on‑device SQLite database records where (0.0025° cells) and when every device is heard, learns your home cell, and summarises each device as *places / days / sessions*. A device heard at home **and** across town (or in three places) on two or more days announces **Recurring device** once per session. Fixed emitters you've passed in the same spot three or more sessions go quiet, so the alerts stay meaningful. Retention 7–90 days, CSV export, clear.

### Peak signal across passes
The KML export places each device at the averaged peak‑signal position across every pass, which localises a fixed camera far better than any single drive‑by.

### Remote ID (drones)
ASTM F3411 / Open Drone ID on BLE is decoded: UAS ID, aircraft type, position, altitude, speed, heading, **operator location**, operator ID and self‑ID text. The voice alert gives the operator's distance ("Drone, operator 340 metres away"). KML places the aircraft at its reported altitude with the operator as a second placemark.

### Session record and replay
Setup → Sessions. Records every observation and GPS fix to a gzip NDJSON file while scanning (≈5–10 MB per hour in a city); replay feeds a recording back through the classifier with live packets muted, so a new signature pack can be tested against last week's drive. Export the raw file for analysis on a laptop.

### Scanner watchdog
Android's BLE scanner sometimes silently stops delivering. Wavelens watches the Bluetooth clock alone (Wi‑Fi and cells keep ticking through a BLE stall), cycles the radios in‑process when it goes quiet, and shows the restart count in the status line.

### Exports
CSV of all emitters, CSV of cell sites, KML for Google Earth, the alert log, the history database as CSV, session recordings, and the watchlist as JSON (import on another phone).

---

## Signature pack

The signature pack that ships inside the app: rules are AND‑matched on OUI prefixes, service UUIDs (with wildcards), manufacturer‑data prefixes, service‑data prefixes, names, SSIDs, Wi‑Fi capabilities and channel. Each rule carries a category, confidence, weight and a short explanation that is shown on the card. The pack is versioned and the version shows in the status line.

A pack can also be dropped on the phone as an override and reloaded from Setup without reinstalling the app. Found something the pack doesn't know? Open an issue with the capture row (the CSV export has everything needed) and what the device turned out to be.

---

## Privacy

- No network permission. Nothing leaves the phone except files you export yourself.
- Detections, alerts, history, fingerprints and recordings live in the app's private storage and are excluded from Android backup and device transfer.
- Full policy: [pmcbrien.github.io/wavelens](https://pmcbrien.github.io/wavelens/).

---


---

## Disclaimer

Wavelens only listens to transmissions that devices make in the clear, which is lawful in most jurisdictions, but check yours. Classification is a best‑effort guess from public signature data and can be wrong in both directions. It is a situational‑awareness tool; what you do with that awareness is on you.

© Patrick McBrien. Wavelens is free to download and use from this page; redistribution of the app or its signature pack is not permitted without permission.
