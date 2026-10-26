# Wavelens companion radios

A phone can't choose which Wi-Fi channel it listens on, can't hear 433/915 MHz or 5.8 GHz
at all, and has an antenna the size of a paperclip pointed wherever the phone happens to
be. A companion fixes all three: a second radio, with a real antenna, that does the
listening and hands the results to Wavelens over Bluetooth. The app treats what it hears
exactly like its own packets — same signatures, alerts, history, map, tails and Rule hits —
and can tell the companion which band or channels to park on.

Nothing changes about privacy: the link is BLE only, the app still has no network
permission, and nothing the companion hears leaves the phone.

| Build | Hears | Honours band/channel commands | Effort |
|---|---|---|---|
| [ESP32](esp32/) (S3 / C3 / classic, **-U variant** for an external antenna) | 2.4 GHz Wi-Fi beacons + BLE adverts · optional RX5808 → 5.8 GHz video carriers · optional CC1101 / SX1276 → rtl_433 decoders at 433 / 315 MHz (TPMS, fobs, sensors) | Channels yes; 5 GHz only on an ESP32-C5 | Flash a sketch, $10–30 |
| [Raspberry Pi + RTL-SDR](pi/) | rtl_433 (hundreds of sub-GHz decoders) · rtl_power band sweeps below 1.7 GHz · dump1090 ADS-B aircraft · monitor-mode USB Wi-Fi via Kismet | No (relays what the tools give it) | A Python script, $60–120 |
| Pi / carputer + **HackRF** | hackrf_sweep: the whole 1 MHz–6 GHz range in about a second, every carrier labelled by band ([what it labels](#hackrf-what-the-full-sweep-labels)) — video and drone links, low aircraft, military air, toll readers, phones and sat phones, 4.9 GHz public-safety links — as "something is transmitting here" | No | Same script, +$300 |
| Carputer (any Linux box) | All of the above at once, as many radios as it has USB ports | No | Same script |
| Any SDR build + [local FCC pack](#local-fcc-license-pack--who-owns-that-carrier) | Licensed carriers named: which agency or business holds the frequency, and how far its site is | — | One script run per area |
| Arduino | Only with a BLE module (HM-10) *and* a radio module; an ESP32 does both for less | — | Not recommended; see [diagrams](diagrams/) |

Wiring and antenna diagrams: [diagrams/](diagrams/) — overview, ESP32, Pi + SDR, [Pi + HackRF + two RTL-SDRs](diagrams/pi-hackrf-2sdr.svg), carputer, Arduino, vehicle mounting.

### What the SDRs do and don't add

- **RTL-SDR** (24–1766 MHz): decoding, via `rtl_433`. It names things: a Schrader TPMS sensor, an Acurite weather station, a garage remote. Ceiling is 1.7 GHz, so no Wi-Fi, BLE or 5.8 GHz.
- **HackRF** (1 MHz–6 GHz): energy, via `hackrf_sweep`. It reaches the bands the RTL-SDR can't, but a sweep sees *carriers*, not packets: "30 dB over the floor at 5745 MHz" could be a video link or a Wi-Fi access point on channel 149. The bridge labels these honestly and the app shows them as *Possible*. It does not demodulate Wi-Fi usefully (a $15 monitor-mode adapter does that better) and cannot tell a camera's LTE modem from the phones around you. **Receive only** — the bridge never transmits, and neither should you.
- **ESP32 + CC1101 / SX1276**: a subset of rtl_433's decoders on a $5 module, tuned to one frequency at a time. The no-Pi way to hear TPMS and 433 MHz.
- **ADS-B** (`dump1090`, `readsb`): aircraft overhead with callsign, altitude and position in the detail view. Unclassified on purpose — an airliner is not a drone.

## HackRF: what the full sweep labels

`--hackrf-sweep` sweeps 1 MHz–6 GHz by default (`--hackrf-range 1:6000`), about one full pass a
second on a Pi 4 with 500 kHz bins. Every bin that rises 18 dB over its own rolling floor is
labelled from the US frequency allocation table. **Energy only:** a sweep shows that something is
on the air, never what it says, so every label is *Possible*, nothing is demodulated, and no radio
IDs are decoded. With a [local FCC pack](#local-fcc-license-pack--who-owns-that-carrier) loaded, a
licensed frequency gets the licensee's name instead.

| Band | Label | Category |
|---|---|---|
| 4200–4400 MHz | Radio altimeter: aircraft flying low overhead (helicopter, plane on approach, large drone) | UNKNOWN |
| 118–137 MHz · 121.5 | Aircraft or airport radio · aviation emergency frequency (ELT / guard) | UNKNOWN |
| 225–400 MHz · 243.0 | Military / government UHF (military aviation, tactical, SATCOM) · military emergency frequency | UNKNOWN |
| 978 · 1030 · 1090 MHz | ADS-B UAT · secondary radar interrogation · aircraft transponder | UNKNOWN |
| 1435–1525 · 2200–2290 · 2360–2395 MHz | Flight-test / aeronautical / federal telemetry (incl. military UAV links) | UNKNOWN |
| 5645–5849 MHz | 5.8 GHz carrier (FPV video, or Wi-Fi ch 149+) — two passes in a row | DRONE |
| 5850–5925 MHz | **Burst:** connected-vehicle radio (C-V2X roadside unit or vehicle) · **steady:** FPV video on a high channel | VEHICLE / DRONE |
| 5030–5091 MHz | Drone command-link band (aviation UAS control) — two passes | DRONE |
| 1240–1300 MHz | 1.2 GHz video or telemetry link — two passes | DRONE |
| 2025–2110 MHz | News / broadcast video link (ENG trucks, news helicopters) — two passes | CAMERA |
| 4940–4990 MHz | 4.9 GHz public-safety link (camera / video backhaul, mesh) — two passes | NETWORK |
| 902–928 MHz | **Burst:** LoRa, telemetry, meters · **steady:** toll gantry or RFID / gate reader | IOT / TRAFFIC_SENSOR |
| 300–318 · 390 · 433 · 868 · 49 · 72 MHz | Key fobs, garage remotes, TPMS, ISM sensors, baby monitors, RC models | VEHICLE / IOT / DRONE |
| 600 / 700 / 800 / 850 MHz, AWS, PCS (1850–1920), AWS-4, WCS uplinks | Phone transmitting nearby (very often your own) | PHONE |
| 1610–1626.5 · 1626.5–1660.5 MHz | Satellite phone or tracker (Globalstar / Iridium) · Inmarsat terminal transmitting | PHONE |
| 462 / 467 MHz | Walkie-talkie nearby (FRS / GMRS) | UNKNOWN |
| 799–805 · 806–815 MHz | Radio keying up nearby (700 / 800 MHz public-safety mobile, repeater input) | UNKNOWN |
| 161.975 / 162.025 · 156–162 · 160–161.6 MHz | AIS ship transponder · marine VHF · railroad radio | UNKNOWN |
| 406.0–406.1 MHz | 406 MHz distress beacon (EPIRB / PLB / ELT) | UNKNOWN |
| 144.39 · amateur bands | APRS beacon · 6 m, 2 m, 1.25 m, 70 cm, 23 cm amateur | UNKNOWN |
| Other VHF / UHF land mobile, federal VHF / UHF, 1780–1850, 4.4–4.94 GHz | Two-way radio by band · federal / military | UNKNOWN |
| 2.9–3.1 · 3.1–3.45 GHz | Ship radar · military radar (S-band) | UNKNOWN |
| 1559–1610 · 1164–1189 MHz | Possible GPS jammer (L1 or L5) — a wide rise on two passes; that band's floor rises slowly so a jammer riding with you stays visible | TOOL |
| 608–614 · 1390–1400 · 1427–1435 MHz | Hospital medical telemetry (WMTS) | IOT |

**Background**, at most once a minute per band (they're everywhere): AM / HF / FM / TV broadcast,
VOR / ILS / DME navigation beacons, NOAA Weather Radio, paging, every cell-tower downlink (600 MHz
through C-band, 2.5 GHz band 41, CBRS), satellite downlinks (Inmarsat, weather satellites,
SiriusXM) and airport / weather radar. **Skipped:** 2.4 GHz and 5 GHz Wi-Fi and Bluetooth, Wi-Fi 6E,
GPS L2 and long-range air-traffic radar (1215–1240, 1300–1390 MHz), and the radio-astronomy band
(1400–1427 MHz). Anything else is reported as "*NNN.N* MHz carrier".

**Steady vs burst.** In 902–928 and 5850–5925 MHz the same band means two things, so the bridge
waits: a carrier still over the floor on three passes in a row gets the steady label (toll reader,
FPV video); one that comes and goes gets the burst label (LoRa, V2X), reported when it drops.

**What a sweep catches, and what it doesn't.** Each frequency is looked at for a few milliseconds
per pass. Continuous signals — video links, radio altimeters, toll readers, a keyed walkie-talkie —
show on every pass; short, rare bursts (a key fob, a tire sensor, a single V2X message) are mostly
missed, which is why an RTL-SDR stays parked on 315 / 433 / 915 MHz with `rtl_433`. The HackRF has
no front-end filter, so a very strong nearby tower can put a spur elsewhere in the sweep; the
rolling floor absorbs one that stays. Out of reach entirely: radar guns (10.5 / 24 / 35 GHz),
Starlink (above 10 GHz), and anything that would need decoding to tell apart — a sweep cannot tell
a fake cell tower from a real one, so the bridge never claims to.

**Parked for body cams & radios.** *More → Tools → Companion radio → Park for body cams & radios*
trades breadth for speed. An ESP32 keeps BLE on and parks Wi-Fi on channels 1 · 6 · 11 (each
revisited every 0.75 s instead of every 3.25 s), which is where body-cam and in-car-video hotspots
and the BLE advertisements of body cams and radio accessories are caught. A HackRF bridge stops the
full sweep and stays on **796–816 MHz** — 700 MHz public-safety mobile and 800 MHz repeater inputs,
where handhelds and car radios transmit — so a radio keying up nearby shows on the pass it happens
(milliseconds) rather than within a second. Energy only: it says a radio transmitted nearby, never
whose or what. Change the slice with `--hackrf-park-range 450:470` (for example, where your county's
radios are UHF). Everything else, drones and 5.8 GHz video included, goes quiet while parked.

A Pi with all three receivers: [diagrams/pi-hackrf-2sdr.svg](diagrams/pi-hackrf-2sdr.svg). Put them
on a **powered USB hub**; a Pi 4 cannot supply a HackRF and two RTL-SDRs on its own.

## Raspberry Pi: start at boot

To run the bridge headless whenever the car powers the Pi, install it as a service from `pi/`:

```
cd wavelens/companion/pi
sudo ./install-service.sh
```

It copies the bridge to `/usr/local/lib/wavelens`, installs `wavelens-bridge.service`, blacklists the
kernel TV driver that grabs RTL-SDR dongles, and starts it. Which radios it feeds is set in
`/etc/default/wavelens-bridge` — by default rtl_433 hopping 315 / 433.92 / 915 MHz; add
`--hackrf-sweep` or `--dump1090 …` to `WAVELENS_ARGS`. After editing,
`sudo systemctl restart wavelens-bridge`. Logs: `journalctl -u wavelens-bridge -f`.
Remove with `sudo ./install-service.sh --remove`. It waits for the Bluetooth adapter at boot and
restarts itself if a radio is unplugged.

### Two RTL-SDRs, older cards, and other things learned on real hardware

The full step-by-step guide, with every command, is on the website:
[wavelensapp.us/companion.html#pi-setup](https://wavelensapp.us/companion.html#pi-setup). The short version of what tends to trip people up:

- **Two dongles:** pin each by serial so they can't swap after a reboot. `rtl_test -t` lists serials
  (`No E4000 tuner found` and `PLL not locked` there are normal for R820T dongles). Decoder:
  `WAVELENS_RTL433="-d :SERIAL_A -f 433.92M -f 315M -H 15 -g 40"`; sweep:
  `WAVELENS_ARGS="--rtl-power 902M:928M:25k --rtl-power 1240M:1300M:100k --rtl-power 1559M:1610M:100k --rtl-power-device SERIAL_B --rtl-power-dwell 6"`
  — 900 MHz ISM / LoRa, 1.2 GHz video links, and the GPS band, where a strong signal is labelled a possible
  GPS jammer (that band's noise floor is kept from creeping up so a jammer riding with you stays visible).
  Several `--rtl-power` ranges take turns on one dongle (`--rtl-power-dwell`, default 15 s).
  Duplicate serials: `rtl_eeprom -d 1 -s 00000002`, then replug.
- **Whole range on two dongles:** `WAVELENS_RTL433=""` and
  `WAVELENS_ARGS="--rtl-power 24M:895M:250k@SERIAL_A --rtl-power 895M:1766M:250k@SERIAL_B"` — `@SERIAL` pins a range
  to a dongle and each dongle sweeps in parallel, a full pass of its half roughly every 14 s. Catches steady
  carriers; misses short bursts (fobs, TPMS, brief key-ups) and names nothing. Phone uplink bands are labelled as phones.
- **Antennas:** dongles sold for aircraft come with ~7 cm 1090/978 MHz antennas; use ~17 cm elements for 433 MHz.
- **One program per dongle:** PiAware/dump1090 holds one. `sudo systemctl disable --now piaware dump1090-fa dump978-fa` frees them
  (serial 00000978 is usually dump978's; `usb_claim_interface error -6` means a dongle is held).
- **FCC pack:** add `--fcc-pack` only once the file exists; a missing pack makes the service restart in a loop.
- **Raspbian Buster (old PiAware cards):** comment out dead third-party apt sources
  (`sudo sed -i 's|^deb .*flightaware.*|#&|' /etc/apt/sources.list /etc/apt/sources.list.d/*.list`); drop
  `--break-system-packages` from pip; build rtl_433 from source (`git clone https://github.com/merbanan/rtl_433`,
  `cmake`, `make`, `sudo make install`); don't run `apt autoremove` (it can remove `rtl-sdr`).
- **`dpkg: … files list file for package 'X' is missing final newline`:** `sudo mv /var/lib/dpkg/info/X.list ~/`,
  `sudo apt install --reinstall -y X`, `sudo dpkg --configure -a`, `sudo dpkg --audit`. Several damaged
  packages means the SD card is failing.
- **No Bluetooth (`bluetoothctl show` empty):** check for a `disable-bt` overlay in `/boot/config.txt`, that
  `bluez`, `pi-bluetooth` and `bluez-firmware` are installed, that `hciuart` and `bluetooth` are enabled, and
  `systemctl status hciuart` / `dmesg | grep -iE "hci|bluetooth|brcm"`.
- **Update:** `cd ~/wavelens && git config core.fileMode false && git pull && cd companion/pi && sudo ./install-service.sh`
  (settings are kept). Without `core.fileMode false`, the `chmod +x` from setup counts as a local edit and blocks the pull
  (`git stash` clears it). `unrecognized arguments` in the log means the installed bridge is older than your settings:
  update, then rerun `install-service.sh`.

## Pairing

1. Power the companion near the phone. It blinks twice, then advertises the Wavelens
   service as **Wavelens-XXXX** — XXXX is the end of its Bluetooth address, printed on
   the serial console at boot (`… advertising as Wavelens-3F2A (pair code 3F2A)`).
2. In the app: **More → Companion radio**. The screen starts looking on its own; tap
   **Find** to look again. No scan session is needed — it runs a short scan filtered to
   the Wavelens service, so the companion shows up within a second or two, strongest
   first, with its code.
3. Tap **Pair** on yours. It connects immediately; **Blink LED** flashes the board's LED
   (or the Pi's activity LED) so you know you picked the right box, and **Test link**
   shows the round-trip time.
4. That's it. From then on it connects whenever a scan runs or the screen opens, and
   after a drop it reconnects in the background (OS auto-connect with 3 s → 60 s
   backoff), so a companion powered by the car's accessory socket just comes back.
5. Pick where it listens: radio (Wi-Fi / BLE / both / other RF), Wi-Fi band, channels
   (hop or park). The choice is sent on every reconnect.

The link is Bluetooth only, on purpose: the app holds no INTERNET permission, and any
Wi-Fi socket — even to a box on the dash — would need one. **Not showing up?** on the
same screen covers the usual causes (held by another app, Bluetooth permission, error
133) and has a pair-by-address field.

## Line protocol

Transport: BLE GATT. The companion is the peripheral, the phone the central.

| | UUID | Direction | Properties |
|---|---|---|---|
| Service | `57617665-6c65-6e73-0100-000000000001` | | ("Wavelens" in hex) |
| TX | `57617665-6c65-6e73-0100-000000000002` | companion → phone | notify |
| RX | `57617665-6c65-6e73-0100-000000000003` | phone → companion | write, write-without-response |

Payload: UTF-8 JSON objects, **one per line, newline-terminated**. A notification may
carry part of a line or several lines; the phone reassembles on `\n`. The phone requests
an MTU of 247; assume 20-byte chunks if it fails.

### Companion → phone

```jsonc
{"t":"hello","fw":"wavelens-esp32 0.2","name":"Wavelens-3F2A","code":"3F2A","caps":["wifi","ble","rf"]}
{"t":"status","msg":"parked on 1 channel","band":"all","mode":"both","ch":6}

// Wi-Fi AP / hotspot (beacon or probe response). id = BSSID. ch or f (MHz); the phone derives the other.
{"t":"wifi","id":"b4:1e:52:12:34:56","n":"Flock-1a2b3c","r":-71,"ch":6,"src":"esp32","x":{"frame":"beacon"}}

// BLE advertisement. id = address; pub = public (not randomized) address.
// svc = service UUIDs (16-bit as 4 hex chars, or full 128-bit); mfg = {companyIdHex: payloadHex}
{"t":"ble","id":"c8:3f:26:aa:bb:cc","n":"Penguin-0123456789","r":-84,"pub":true,
 "svc":["fd44"],"mfg":{"09c8":"0102a1b2"},"src":"esp32"}

// Anything else: a decoded sub-GHz device, a 5.8 GHz carrier, an SDR hit.
// cat/conf are the companion's own guess (a Category name; POSSIBLE or LIKELY). The phone
// uses them only when its signature pack has no opinion, and never above Likely.
{"t":"rf","id":"Schrader/1a2b3c4d","n":"Schrader TPMS","f":315,"r":-62,
 "cat":"VEHICLE","conf":"LIKELY","src":"rtl433","x":{"pressure_kPa":"231"}}
```

Fields: `t` type (required) · `id` identifier (required; a MAC for wifi/ble) · `n` name or
SSID · `r` RSSI dBm (default −100) · `f` MHz · `ch` Wi-Fi channel · `pub` public address ·
`svc` · `mfg` · `cat`/`conf` hint · `src` which radio heard it (shown in the app as
"via …") · `x` free extras, copied into the device's detail view as-is.

Rate-limit per address on the companion (about one report per second per device is plenty).
The phone keeps RSSI history, so repeats are useful; floods are not.

### Phone → companion

```jsonc
{"c":"ping"}                         // reply with hello + status
{"c":"blink","v":5}                  // identify: flash the LED for v seconds (1–30)
{"c":"mode","v":"both"}              // wifi | ble | both | rf
{"c":"band","v":"2g"}                // 2g | 5g | all     (say so in a status if the hardware can't)
{"c":"ch","v":"hop"}                 // hop every channel of the band
{"c":"ch","v":[1,6,11]}              // or park on these
{"c":"park","v":"le"}                // body cams & radios preset (the app also sends mode/band/ch for it);
                                     // a HackRF bridge parks on 796–816 MHz. "off" = sweep everything again
{"c":"pos","lat":27.9,"lon":-82.8}   // opt-in only: the phone's fix, every 10 s, so the companion can rank
                                     // licensed sites by distance. lat null = sharing turned off; forget it.
```

Unknown commands are ignored. Every command may be answered with a `status` line.

## Categories the phone understands

`ALPR`, `ACOUSTIC`, `DRONE`, `TOOL`, `LE_GEAR`, `TRACKER`, `TRAFFIC_SENSOR`, `CAMERA`,
`VEHICLE`, `PHONE`, `WEARABLE`, `IOT`, `BEACON`, `NETWORK`, `CELL_SITE`, `UNKNOWN`.

## Local FCC license pack — who owns that carrier

Everything a HackRF or RTL-SDR hears below about 1 GHz that is not an unlicensed ISM band is
on a frequency somebody holds an FCC license for, with a name, a call sign and usually a
site. [`tools/fcc_local_pack.py`](tools/fcc_local_pack.py) pulls those out of the FCC
Universal Licensing System bulk files for one area — a state and some counties, or a radius
around a point — into a small JSON pack. Hand it to the bridge with `--fcc-pack` and a sweep
hit at 453.21 MHz comes through as

```
Licensed: <County> Sheriff's Office · WQAB123 · public safety     (LE_GEAR · Likely)
```

with the licensee, call sign, service and place in the detail view, instead of "UHF carrier".
The category hint comes from the licensee's name and radio service (police / sheriff →
LE gear; DOT / transit → traffic sensor; towing / taxi → vehicle; utilities → IoT; everything
else unclassified but named). Decoded `rtl_433` hits with no known model get the same
treatment on their frequency.

```
mkdir uls && cd uls && curl -O https://data.fcc.gov/download/pub/uls/complete/l_LMpriv.zip && cd ..
./tools/fcc_local_pack.py --zip uls/l_LMpriv.zip --state FL --county Pinellas --county Hillsborough -o fcc-local.json
./pi/wavelens_bridge.py --hackrf-sweep --fcc-pack fcc-local.json
```

Rebuild the pack when you move; the bulk files are refreshed weekly. With **Share my
position with the companion** on in the app (off by default), the bridge ranks licensees
sharing a frequency by the distance to their nearest licensed site and reports that distance;
the position lives only in the bridge's memory.

Repeater inputs: most licensed systems are pairs — the tower transmits on one frequency, handhelds and
mobiles transmit up to it on another. The pack keeps each frequency's FCC station class, and a hit on a
mobile (`MO…`) frequency — or anywhere in 806–815 MHz (815–849 MHz is phone uplink and is labelled as a phone) — comes through as *Radio transmitting nearby ·
<licensee> repeater input*, because a handheld on an input is only audible from close by. Tower outputs
keep the plain *Licensed:* label.

Check your state's law before sweeping public-safety bands from a vehicle; some restrict it (Florida
s. 843.16, with an exemption for licensed amateur operators).

What this does and does not tell you: a licensed public-safety frequency carrying traffic
means that agency's radios are in range — a dispatcher, a cruiser, a repeater on a tower
ten miles off — not that one is watching you. Unlicensed bands (Wi-Fi, BLE, 433 / 915 MHz
ISM, 5.8 GHz video) have no license records; the app's own signature pack covers those.

## Range and warning time, simulated

[`tools/range_sim.py`](tools/range_sim.py) is a link-budget model — log-distance path loss,
stated antenna gains, receiver sensitivities, a radio-horizon cap — with every assumption
in the file so you can change the antenna or the speed and rerun it. These are **median
ranges with line of sight in the beam**, not measurements; reliable range (nine passes in
ten) is roughly half. The Rule hits table in the app is what replaces them with real numbers.

![Simulated detection range, phone vs companion radios](diagrams/range-sim.svg)

| Target | Receiver | Median range | Warning, you at 45 mph | at 70 mph | Oncoming, both at 70 |
|---|---|---|---|---|---|
| BLE beacon on a pole (battery pack, accessory radio) | phone | 58 m | 2.2 s | 1.1 s | 0.2 s |
| BLE beacon on a pole (battery pack, accessory radio) | esp32 | 405 m | 19 s | 12 s | 5.6 s |
| Wi-Fi hotspot / AP (body cam, setup network) | phone | 231 m | 11 s | 6.7 s | 3.0 s |
| Wi-Fi hotspot / AP (body cam, setup network) | esp32 | 1.9 km | 96 s | 61 s | 30 s |
| TPMS sensor / key fob, 315-433 MHz | rtlsdr | 337 m | 16 s | 10.0 s | 4.6 s |
| Remote / sensor / LoRa node, 433-915 MHz | rtlsdr | 1.3 km | 65 s | 41 s | 20 s |
| 5.8 GHz FPV video, 200 mW, drone in the air | rx5808 | 4.7 km | 232 s | 149 s | 74 s |
| 5.8 GHz FPV video, 200 mW (HackRF sweep) | hackrf | 1.1 km | 55 s | 35 s | 16 s |
| Licensed handheld, 5 W | rtlsdr | 10 km (horizon) | regional — not a warning | | |
| Licensed mobile, 25 W | rtlsdr | 11 km (horizon) | regional — not a warning | | |
| Licensed repeater on a tower, 100 W | rtlsdr | 37 km (horizon) | regional — not a warning | | |

Medians with line of sight in the beam; reliable range is roughly half. Riding with you or following you: unlimited — the closing speed is zero. A fixed site you have passed before: 10–15 s from a 300 m history geofence at any speed, with no radio needed (planned).

What the table says in one line: a companion with a directional antenna turns a fixed pole
from "alert as you pass it" into "alert a block or two ahead," and makes sub-GHz and
5.8 GHz audible at all. It does not rescue the oncoming-at-highway-speed case; nothing
that has to hear the device first can. Road mode (first-packet alerts) gives back the
seconds the app used to spend waiting for a second packet.

## What a companion does not fix

A directional antenna adds gain where it points and loses it everywhere else. It makes a
fixed pole audible from further out and a device riding with you audible all the time; it
does not keep an oncoming car at highway speed in range any longer. The companion makes the
app hear more and hear it sooner. It does not change the physics in
[Does it work?](https://wavelensapp.us/results.html).
