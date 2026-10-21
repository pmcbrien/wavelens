# Wavelens

**See what's transmitting around you.** Wavelens is a passive radio scanner for Android. It listens to the Bluetooth, Wi‑Fi and cellular signals that nearby devices already broadcast, classifies them against an offline signature pack, and tells you in plain language what they are — item trackers, fixed cameras and licence‑plate readers, body‑worn cameras, drones, doorbells, phones, vehicles — and whether any of them is moving with you.

It never connects to, pairs with or transmits to anything. It has **no network access**: no account, no analytics, no uploads. Everything stays on the phone.

<p align="center">
  <a href="https://github.com/pmcbrien/wavelens/raw/main/wavelens.apk"><img alt="Download the APK" src="https://img.shields.io/badge/Download-Wavelens%20APK-2ea44f?style=for-the-badge&logo=android&logoColor=white"></a>
</p>

> **Free APK, no sign-up.** Tap the button above or download [`wavelens.apk`](https://github.com/pmcbrien/wavelens/raw/main/wavelens.apk) straight from this repo. Android 8.0+ (API 26).

<p align="center">
  <a href="https://github.com/pmcbrien/wavelens/raw/main/docs/promo/wavelens-promo.mp4"><img src="docs/promo/body-cam-alert.jpg" width="92%" alt="Night drive: Wavelens warns the driver of a body cam ahead"></a><br>
  <sub>Drone overhead, body cam ahead, a phone that keeps reappearing behind you — tone, voice and flash before you can see any of it. <a href="https://github.com/pmcbrien/wavelens/raw/main/docs/promo/wavelens-promo.mp4">Watch the 30-second spot</a>.</sub>
</p>

<p align="center">
  <img src="docs/screenshots/live-icons-freq.png" width="30%" alt="Live list with device icons, signal and frequency">
  <img src="docs/screenshots/card-signal-adv-ring.png" width="30%" alt="Expanded card: signal history, advertising interval, RSSI ring">
  <img src="docs/screenshots/tracker-with-owner.png" width="30%" alt="A Find My Device tracker, confirmed, still with its owner">
</p>

*Screenshots are from real drives; GPS coordinates are blacked out. A few are from earlier builds that still carried the working title "SIGINT Scope".*

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

1. On your phone, tap the download button at the top of this page (or [`wavelens.apk`](https://github.com/pmcbrien/wavelens/raw/main/wavelens.apk)).
2. Open it. Android will ask you to allow installs from your browser — allow it once.
3. Open Wavelens, tap **SCAN**, read the short disclosure, and grant:
   - **Nearby devices / Bluetooth** — to hear BLE advertisements and classic discovery.
   - **Location (precise)** — Android requires it for any Bluetooth/Wi‑Fi scan results; Wavelens also uses it to geotag detections and to tell when something is moving with you.
   - **Notifications** — the persistent "scanning" notification and alerts.

The first scan takes a few seconds to populate. In a city you'll see hundreds of emitters within a minute; the Live screen hides the boring ones by default (see *Main screen* in Setup).

The APK here is the current build; it is replaced in place whenever a new version ships, so re-download from this page to update. When the Play Store version arrives it is signed with a different key, so uninstall this one before installing from the store.

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
  <img src="docs/screenshots/flagged-axon.png" width="30%" alt="A flagged body camera recognised by fingerprint">
  <img src="docs/screenshots/drone-dji.png" width="30%" alt="A DJI device classified as drone">
</p>
<p align="center">
  <img src="docs/screenshots/fastpair-tracker.png" width="30%" alt="Fast Pair earbuds shown with the weak-match note">
  <img src="docs/screenshots/card-expanded.png" width="30%" alt="Expanded card with match reason, SSID, channel, capabilities">
</p>

The rule that matched is always shown on the card with a one‑line reason, including when the match is weak ("also matches ordinary headphones, so treat as weak"). The classifier is a best‑effort guess from public signatures — look before you act on it.

---

## The screens

### Live
The main list. Strongest signal first, with a glyph for the device kind (🔔 doorbell, 🛞 TPMS, 🎥 body cam, 🚁 drone, 🏷 tracker, 📷 camera …), the current dBm, the last frequency heard (Wi‑Fi channel, BLE PHY, or cell band), an estimated distance, and badges: **NEW** highlight box, **FLAGGED**, **FOLLOWING**, **RIDING / PACING**, **MINE**, **WITH OWNER / SEPARATED** for trackers. Filter chips across the top narrow by category. Tap a card for details; alert‑worthy cards open expanded automatically.

A card's details include first/last seen, packet count, signal now/best/average, advertising interval and whether the payload is static, the services and company IDs it advertises, the matched rules with their explanations, the raw manufacturer data, dwell time, and the peak‑signal fix. Buttons: **Flag**, **Mine**, **Ignore "<name>"**.

### Signal
What is close to you *right now*. Every device heard in the last 10 s / 30 s / 1 min / 3 min, strongest first, ranked by current signal with a small penalty per second since it was last heard. Each row has a large dBm block colour‑coded red (≥ −50, touching the phone) / amber (≥ −65) / green (≥ −80) / blue (weaker), a five‑bar meter, a rough distance hint, a level bar with a white peak‑hold tick, and the trend arrow. Rows for flagged, following, riding or high‑threat devices are tinted red. Use it to walk a car or a room: the top row is the thing you are closest to.

### HUD
A driving view: one giant band for the single most important emitter (flagged > following > riding with you > category threat), big chips, and a status strip (emitters heard, seconds since the last packet, scanner restarts). Tap anywhere to switch between *priority* and *strongest‑signal* ordering; long‑press the top band to flag it.

### Alerts
Every alert that fired, newest first, with a **Recurring devices** block at the top. The alert log is also written to disk as CSV as it happens, so a crash or battery death loses nothing. Export from Setup.

### Map
Trails of moving devices by name over a chosen time window, and **convoys** — groups of devices that keep turning up together.

### RF
Serving and neighbour cell sites with tech (LTE / 5G NR / WCDMA / GSM), PCI/CID, channel number and the downlink frequency it maps to, and signal strength. A 2G fallback on the serving cell is called out.

### More
Feature screens that don't need a tab of their own:

- **Routes** — learns the roads you drive regularly and the fixed emitters along them, then alerts on a *new* camera, sensor or AP on a regular route. See [Routes](#routes-new-on-your-route).
- **Tails** — the unified "is this device tailing me?" verdict with a map replay of your path against the device's hits and gaps. See [Tails](#tails).
- **Trips** — a report for every drive: duration, distance, cameras passed, trackers heard, anything that rode or followed, alerts raised; export as PDF or KML. See [Trips](#trips).
- **What is this?** — a plain‑English card for every category and every verdict: what the hardware is, who deploys it, what it can and can't see, how Wavelens recognises it, what to do, and the public sources. The same cards open from the **What is this?** button on any expanded device card.
- **Signature packs** — see the active and bundled pack versions, import an updated pack offline with a diff of what changed, share a single rule, import a rule someone sent you. See [Signature pack](#signature-pack).

### Setup
Every setting, in the order it appears on the screen. Defaults in **bold**.

<p align="center">
  <img src="docs/screenshots/setup-alerts.png" width="30%" alt="Setup: alert categories, re-alert interval, main screen threshold">
</p>

#### Radios
| Setting | Default | What it does |
|---|---|---|
| Classic Bluetooth discovery | **on** | Runs a BR/EDR inquiry every few minutes alongside BLE. Finds older hands‑free kits, some body cams and OBD dongles that don't advertise on BLE. Costs a little battery; the inquiry briefly pauses BLE results. |
| Wi‑Fi scanning | **on** | Reads nearby access points and client probes. This is how ALPR units, body cams and dash cams with a hotspot are found. Android throttles scans to four per two minutes unless *Developer options → Wi‑Fi scan throttling* is off. |
| Cellular neighbour cells | **on** | Logs the serving and neighbour cell sites for the RF tab and the cell CSV. Needs location permission. |

Radio changes take effect on the next **SCAN**.

#### Display
| Setting | Default | What it does |
|---|---|---|
| Keep screen on while open | **on** | Holds the screen on while the app is in front (a phone mount). Scanning continues with the screen off either way. |

#### Alerts
| Setting | Default | What it does |
|---|---|---|
| Alerts enabled | **on** | Master switch. Off, the app still classifies and logs everything; it just stays silent. |
| Tone | **on** | Distinct tones per class: tracker, camera/ALPR, body cam (a short siren). |
| Voice | **on** | Text‑to‑speech, e.g. "Possible body cam, close". |
| └ Speak device name | **on** | Appends the advertised name or vendor to the voice line. |
| └ Speak distance hint | **on** | Appends "close / nearby / far" from the signal strength. |
| Screen flash | **on** | Three red flashes over whatever is on screen. |
| Torch strobe | **on** | Pulses the camera LED — visible with the phone face‑down in a cup holder. |
| Vibrate | **on** | One 180 ms buzz per new alert. |
| Notification | **on** | A heads‑up notification with the alert text, so it shows over navigation apps. |
| Play on phone speaker (ignore car audio) | off | Routes tones and speech to the handset speaker even when the car's Bluetooth audio is connected, so an alert isn't swallowed by a paused podcast. |
| Quiet trackers that are with their owner | **on** | An AirTag/Find My device advertising "owner nearby" is someone's keys in the next lane; don't alert on it. Separated trackers are unaffected. |
| "Following you" alerts | off | Alerts when a device is heard at 5 spots ≥ 300 m apart over ≥ 12 minutes **and** dropped out of range for ≥ 3 minutes in between. Off by default because cars sharing the road pass the first test; the FOLLOWING tag still shows on the list and HUD either way. |
| "Riding with you" alerts for | **Trackers & unknowns** | Which categories a *riding with you* verdict may alert on: trackers/unknowns/beacons/IoT only; everything except vehicle, phone and wearable classes; or everything. Devices you mark **Mine** never alert. |
| Alert on these categories | **ALPR, Acoustic, Body cam / LE gear, Drone, Pentest tool, Item tracker** | The categories that raise an alert when first heard. Flagged, following and riding devices alert regardless. |
| Re‑alert same device after | **10 min** | How long a device stays quiet after it alerts. Registry‑identified hardware (body cams, ALPR, acoustic) is de‑duplicated by vendor prefix, so a camera's Wi‑Fi and BLE radios alert once, not twice. |
| Tracker must stay with you for | **3 min** | Find My, Find My Device and SmartThings Find frames are sent by earbuds, watches and powered‑off phones as well as tags, and a tag in the parked car beside you isn't news. A separated tracker alerts only after it has been with you this long — or at once if it rides or follows. 0 alerts immediately. |

#### Encounter history
| Setting | Default | What it does |
|---|---|---|
| Alert on recurring devices | **on** | Says "Recurring device" once per session for a device seen at home *and* elsewhere, or in three places, on two or more days. **Ack** silences it; **Mine** fingerprints it as yours. |
| Quiet known fixed emitters | **on** | A camera or AP you've passed in the same spot in three or more sessions is scenery: it's logged as *known fixed* rather than alerted. Does not apply to flagged, following, riding or separated devices. |
| Forget one‑off devices after | **30 days** (7–90) | Retention for devices heard in a single session. Recurring devices and fixed emitters are kept. |

Also here: the count of known fixed emitters at your current location, recurring devices pending review, **Export history CSV**, **Clear history**.

#### Scanner health
| Setting | Default | What it does |
|---|---|---|
| Auto‑restart on stall | **on** | Android can stop delivering BLE results without reporting an error. The watchdog watches the Bluetooth clock alone and cycles the radios in‑process when nothing arrives. If a restart brings nothing back it waits twice as long before the next (up to 8×), and it holds off entirely for a minute after Android refuses a scan. |
| Restart after silence of | **45 s** (20–300) | The silence that counts as a stall. |
| Restart scan now | — | Manual cycle. |

If the status line says *Location Services is off*, turn it on: Android withholds all Bluetooth scan results without it, and no restart can help.

#### Main screen
| Setting | Default | What it does |
|---|---|---|
| What holds the list | **Trackers +** | Threat threshold for the Live list: Everything / Phones + / Trackers + / LE only. Anything at or above it stays for the live window; everything else shows while new or still transmitting, then drops off. Flagged and following devices always stay. |
| Highlight new devices for | **20 s** (0–120) | Amber border on a device the first time it's heard. |
| Auto‑expand details on interesting hits | **on** | Alert‑worthy cards open with their details showing. |
| Keep quiet devices for | **30 s** (5–300) | How long a below‑threshold device lingers after its last packet. |
| Live window (above threshold) | **5 min** (0–60) | How long an above‑threshold device stays after its last packet. 0 = forever. |
| Ignore weaker than | **−100 dBm** (−110…−40) | Floor for both the list and alerts. Raise it to −85 in a dense car park to see only what's near. |

#### Sessions
| Setting | Default | What it does |
|---|---|---|
| Record drives to disk | off | Writes every observation and GPS fix to a gzip NDJSON file while scanning (≈ 5–10 MB per hour in a city). |
| Keep newest sessions | **20** (1–200) | Oldest recordings are deleted when a new one starts. |
| Marker | — | Drops a timestamped note into the recording ("passed the white van"). |

Each recording can be **exported**, **replayed at 1× or 10×** (live packets are muted and the recording is fed through the classifier — test a new signature pack against last week's drive) or **deleted**.

#### Export
**CSV** of all emitters · **KML** for Google Earth (each device at its averaged peak‑signal position; drones at altitude with the operator as a second placemark) · **Cells** CSV · **Unknowns** report (unclassified devices with everything needed to write a rule) · **Alert log** CSV (written to disk as alerts happen, so a crash loses nothing) · **Clear alert log**.

#### Watchlist rules
Every device you've **Flagged** or marked **Mine**, with the fingerprint traits it matches on. Remove one, or **Remove all rules and clear flags**. The watchlist can be exported as JSON and imported on another phone.

#### Ignored names
Name fragments hidden from the list entirely, added with the **Ignore** button on a device card. Matched as case‑insensitive substrings, so "Ring Setup" hides the whole family.

#### Signatures
**Import pack** (JSON) replaces the bundled signature pack; **Reset** goes back to it. The richer flow with a diff of what changed is under **More → Signature packs**.

#### Debug
**Clear all tracks** empties the live table. **Test alert** fires a synthetic body‑cam alert so you can check tone, voice, flash, torch and speaker routing without waiting for a real one.

#### More → Routes, Tails, Trips
| Setting | Default | What it does |
|---|---|---|
| Routes → New‑on‑route alerts | **on** | Alert when a fixed‑type device with a public address first appears in a grid cell you've driven in ≥ 4 sessions on ≥ 3 days. |
| Routes → Forget routes | — | Wipes the learned route cells and baseline. |
| Tails → Tail alerts | **on** | Alert once per session when a device reaches the TAIL verdict. |
| Trips → Auto‑record raw sessions | off | Starts the Sessions recorder with every scan so the full packet log exists for each trip. |
| Trips → Keep | **50** (1–500) | Trip reports to keep; oldest are deleted. |

---

## Features in depth

### Alerts while you drive
Distinct tones for trackers, cameras/ALPR, and body cams, plus spoken alerts ("Possible body cam, RAI‑SIGNAGE, close"), screen flash, torch strobe, vibration and a heads‑up notification — each one a toggle. Alerts can be forced to the phone speaker so they aren't swallowed by a car's Bluetooth audio. Pick which categories alert and how long before the same device re‑alerts. The scanner keeps running with the screen off; the screen stays on while the app is in front.

### Flag once, recognise forever
Phones and trackers rotate their Bluetooth address every few minutes, so a MAC is useless for "is that the same device?". **Flag** builds a weighted fingerprint from what *doesn't* change — vendor prefix, service UUIDs, manufacturer‑data prefix, service‑data prefix, name, Wi‑Fi cipher suite and channel, advertising‑interval class, static payload — and matches on that. A device scoring ≥ 4 with at least one identity‑bearing trait is shown **FLAGGED** again, address or no address. Every body‑cam, ALPR and acoustic alert is auto‑fingerprinted, so the second time you pass the same camera it says **Known** rather than *Possible*.

### Riding with you
A device that holds a steady signal while you cover ground is in or on your vehicle. Every track with enough packets keeps a signal‑strength ring; every 10 seconds it is scored against your own GPS motion. Steady (IQR ≤ 10 dB) over 400 / 1 500 / 5 000 m becomes **POSSIBLE / LIKELY / CONFIRMED**, with an **in vehicle** tier when it's strong and very flat; stop‑and‑go traffic or a 90° turn with the signal unchanged promotes it a level. Scanner stalls and restarts are treated as gaps, not as "device gone". Traffic jams and parking don't count — the window needs real path length and speed. Alerts fire at LIKELY. Tap **Mine** on your own earbuds, watch or car and they're fingerprinted and never alert. Scope the alerts to trackers only, everything but vehicle/phone/wearable, or everything.

### Following
Separate from riding: a device heard at five or more positions ≥ 300 m apart over at least 12 minutes **that also dropped out of range for three minutes or more and came back** is **FOLLOWING**. The drop‑out is what separates a tail from the car in the next lane, which never leaves range. A separated Find My / FMDN tracker needs only two positions and two minutes. The tag always shows; alerting on it is a toggle, off by default.

### Routes (new on your route)
More → Routes. The app grids the world into ~250 m cells and counts the sessions and days you've been in each one. A cell visited in four or more sessions on three or more days is part of a regular route. Every fixed‑type device with a public address (ALPR, acoustic, traffic sensor, camera, AP, body‑cam vendor hardware) is filed under the cell where it was strongest. When one appears in a regular cell — and in none of the eight neighbouring cells — that it has never been filed in before, you get **New on your route** once per session. A fresh ALPR pole on your commute is the point. Cards show *known on this route* or *new on a regular route (cell visited N times)*.

### Tails
More → Tails. One verdict that combines the following heuristic and the riding‑with‑you score with the thing that matters most: re‑acquisition. For every device the app keeps the segments where it was in range and the gaps between them. **TAIL** = following, or riding with ≥ 2 re‑acquisitions. **WATCH** = three spots, one re‑acquisition and six minutes, or riding with one re‑acquisition. The card carries a narrative ("Appeared behind you 3 times across 22 min; dropped out twice, longest 4 min; 6 spots over 5.1 km") and opens a replay: your own path in grey, the device's hits as dots coloured by signal, gaps dashed, and a scrubber to step through time. **Share evidence** writes the narrative plus a CSV of every fix.

### Trips
More → Trips. Every scan session becomes a trip: duration, distance, top speed, cameras and sensors passed (with names and where), body‑cam encounters, trackers heard and their verdicts, drones, every alert, and the five most notable devices. The in‑progress trip is shown while you drive. **Export PDF** for a readable report, **Export KML** for the path and notable devices in Google Earth.

### Encounter history and baseline
An on‑device SQLite database records where (0.0025° cells) and when every device is heard, learns your home cell, and summarises each device as *places / days / sessions*. A device heard at home **and** across town (or in three places) on two or more days announces **Recurring device** once per session. Fixed emitters you've passed in the same spot three or more sessions go quiet, so the alerts stay meaningful. Retention 7–90 days, CSV export, clear.

### Peak signal across passes
The KML export places each device at the averaged peak‑signal position across every pass, which localises a fixed camera far better than any single drive‑by.

### Remote ID (drones)
ASTM F3411 / Open Drone ID on BLE is decoded: UAS ID, aircraft type, position, altitude, speed, heading, **operator location**, operator ID and self‑ID text. The voice alert gives the operator's distance ("Drone, operator 340 metres away"). KML places the aircraft at its reported altitude with the operator as a second placemark.

### Session record and replay
Setup → Sessions. Records every observation and GPS fix to a gzip NDJSON file while scanning (≈5–10 MB per hour in a city); replay feeds a recording back through the classifier with live packets muted, so a new signature pack can be tested against last week's drive. Export the raw file for analysis on a laptop.

### Scanner watchdog
Android's BLE scanner sometimes silently stops delivering. Wavelens watches the Bluetooth clock alone (Wi‑Fi and cells keep ticking through a BLE stall), cycles the radios in‑process when it goes quiet, and shows the restart count in the status line. It respects Android's limit of five scan starts per 30 seconds, backs off after a restart that brings nothing back, logs the reason when Android refuses a scan, and tells you when Location Services being off is the real cause.

### Exports
CSV of all emitters, CSV of cell sites, KML for Google Earth, the alert log, the history database as CSV, session recordings, and the watchlist as JSON (import on another phone).

---

## Community map — contribute what you find

Wavelens users can pool the location of **fixed** surveillance infrastructure — plate readers, traffic sensors, acoustic sensors — into one shared map, `community/emitters.geojson`, that anyone can load into the app.

**What gets shared.** Only emitters in those three categories, heard in the same spot on three or more separate drives, positioned by the averaged peak and rounded to about 100 m. No trips, no addresses or MACs (entries are keyed by a one‑way hash), no timestamps finer than a day, and nothing from the camera/doorbell, network, body‑cam, vehicle, phone or wearable classes — those are either mobile or someone's home.

**How to contribute.**
1. Setup → **Community map → Preview share pack**. Untick anything you'd rather keep to yourself, then **Export**.
2. Open a [Contribute emitters](../../issues/new?template=contribute.yml) issue and drag the `.geojson` file in.
3. A bot validates it (categories, rounding, no identifiers), merges it, and opens a pull request. Once reviewed, it's on the map.

**How to use it.** Download [`community/emitters.geojson`](community/emitters.geojson) to your phone, then Setup → **Community map → Import community map**. Those emitters become part of your known‑fixed baseline immediately: they stop alerting as "new" near home, and when you drive somewhere another contributor has mapped, you inherit their catalogue.

The app never uploads anything; every step above is a file you choose to share. Contributions are CC0.

---

## Signature pack

The signature pack that ships inside the app: rules are AND‑matched on OUI prefixes, service UUIDs (with wildcards), manufacturer‑data prefixes, service‑data prefixes, names, SSIDs, Wi‑Fi capabilities and channel. Each rule carries a category, confidence, weight and a short explanation that is shown on the card. The pack is versioned and the version shows in the status line.

A pack can also be dropped on the phone as an override and reloaded from Setup without reinstalling the app. **More → Signature packs** shows the active and bundled pack versions, imports a pack with a diff (added / removed / changed rules, "12 new ALPR vendors"), refuses an older pack unless you confirm the downgrade, and lets you share one rule as JSON or import a rule someone sent you into a user overlay that wins over the pack by id. The rule schema is documented in [docs/SIGNATURES.md](docs/SIGNATURES.md). Found something the pack doesn't know? Open an issue with the capture row (the CSV export has everything needed) and what the device turned out to be.

---

## Privacy

- No network permission. Nothing leaves the phone except files you export yourself.
- Detections, alerts, history, fingerprints and recordings live in the app's private storage and are excluded from Android backup and device transfer.
- Full policy: [docs/index.html](docs/index.html) (also at [pmcbrien.github.io/wavelens](https://pmcbrien.github.io/wavelens/)).

---


---

## Disclaimer

Wavelens only listens to transmissions that devices make in the clear, which is lawful in most jurisdictions, but check yours. Classification is a best‑effort guess from public signature data and can be wrong in both directions. It is a situational‑awareness tool; what you do with that awareness is on you.

© Patrick McBrien. Wavelens is free to download and use from this page; redistribution of the app or its signature pack is not permitted without permission.
