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
second on a Pi 4 with 500 kHz bins. Every HackRF has a few spurs of its own (carriers it makes
itself, some very strong); find them once with the antenna unscrewed — `find_spurs.py` prints a
ready `--hackrf-mask 4770:4805,3215:3240` — and masked ranges feed the floor but are never reported. Every bin that rises 18 dB over its own rolling floor is
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

<details><summary><strong>Full band table</strong>, generated from <code>BANDS</code> in the bridge (<code>python3 companion/tools/band_table.py</code>)</summary>

<!-- bands:start (generated by companion/tools/band_table.py) -->
| Range | Reported as | Category | When |
|---|---|---|---|
| **Skipped outright** | | | |
| 2400–2500 MHz | 2.4 GHz Wi-Fi / BLE / microwave ovens |  | Not reported |
| 5150–5644 MHz | 5 GHz Wi-Fi (also airport weather radar 5600–5650) |  | Not reported |
| 5926–6000 MHz | Wi-Fi 6E |  | Not reported |
| 1215–1240 MHz | GPS L2 + long-range air-traffic radar bursts |  | Not reported |
| 1300–1390 MHz | long-range air-traffic and military radar |  | Not reported |
| 1400–1427 MHz | radio astronomy: protected, nothing should transmit |  | Not reported |
| **Narrow, specific** | | | |
| 121.4–121.6 MHz | Aviation emergency frequency 121.5 MHz (ELT beacon or guard call) | Unclassified | Each burst over the noise floor |
| 242.9–243.1 MHz | Military aviation emergency frequency 243.0 MHz (beacon or guard call) | Unclassified | Each burst over the noise floor |
| 406–406.1 MHz | 406 MHz distress beacon (EPIRB / PLB / ELT) | Unclassified | Each burst over the noise floor |
| 161.95–162.05 MHz | AIS: ship or boat position transponder nearby | Unclassified | Each burst over the noise floor |
| 144.37–144.41 MHz | APRS 144.39 MHz: amateur position beacon | Unclassified | Each burst over the noise floor |
| 462.54–462.74 MHz | Walkie-talkie nearby (FRS / GMRS) | Unclassified | Each burst over the noise floor |
| 467.54–467.74 MHz | Walkie-talkie nearby (FRS / GMRS) | Unclassified | Each burst over the noise floor |
| 978–978.5 MHz | Aircraft ADS-B (978 MHz UAT): small plane nearby | Unclassified | Each burst over the noise floor |
| 1029.5–1030.5 MHz | Secondary radar interrogation (1030 MHz): airport or military radar nearby | Unclassified | Each burst over the noise floor |
| 1089.5–1090.5 MHz | Aircraft transponder (1090 MHz, Mode S / ADS-B) nearby | Unclassified | Each burst over the noise floor |
| 26.96–27.41 MHz | CB radio transmitting nearby (trucks, CB users) | Vehicle | Each burst over the noise floor |
| **Unlicensed Part 15 / ISM devices** | | | |
| 314–316 MHz | 315 MHz burst (TPMS, key fob) | Vehicle | Each burst over the noise floor |
| 299–319 MHz | Key fob or garage remote (300–318 MHz) | Vehicle | Each burst over the noise floor |
| 389.5–390.5 MHz | Garage remote (390 MHz) | Vehicle | Each burst over the noise floor |
| 433.05–434.79 MHz | 433 MHz ISM burst (remotes, sensors, TPMS) | IoT / sensor | Each burst over the noise floor |
| 902–928 MHz | 900 MHz ISM burst (LoRa, telemetry, meters) | IoT / sensor | Bursts as shown; a carrier still there after 3 passes becomes “Steady 900 MHz carrier: meter collector, LoRa gateway, cordless phone — or a toll / RFID reader on the road” |
| 863–870 MHz | 868 MHz ISM burst (EU LoRa, sensors) | IoT / sensor | Each burst over the noise floor |
| 49.8–49.9 MHz | 49 MHz baby monitor, toy or old cordless phone | IoT / sensor | Each burst over the noise floor |
| 72–76 MHz | 72 MHz steady carrier: radio-control model (RC aircraft), or hearing-assist / paging | Drone / video link | Only when it's on two passes in a row |
| **Video and drone links** | | | |
| 5645–5849 MHz | 5.8 GHz steady carrier: FPV video, or a busy Wi-Fi access point | Drone / video link | Only when it's on 6 passes in a row (Wi-Fi comes and goes; analog video doesn't) |
| 5850–5925 MHz | 5.9 GHz burst: connected-vehicle radio (C-V2X roadside unit or vehicle) | Vehicle | Bursts as shown; a carrier still there after 3 passes becomes “5.9 GHz steady carrier: FPV video on a high channel, or Wi-Fi ch 169–177” |
| 5030–5091 MHz | Drone command-link band (5030–5091 MHz, aviation UAS control) | Drone / video link | Only when it's on two passes in a row |
| 5091–5150 MHz | Airport surface network (AeroMACS) | Network | Each burst over the noise floor |
| 1240–1300 MHz | 1.2 GHz carrier: video or telemetry link (also ham 23 cm) | Drone / video link | Only when it's on two passes in a row |
| 2025–2110 MHz | News / broadcast video link (2 GHz: ENG trucks, news helicopters) | Camera | Only when it's on 6 passes in a row (Wi-Fi comes and goes; analog video doesn't) |
| 4940–4990 MHz | 4.9 GHz public-safety link (camera / video backhaul, mesh) | Network | Only when it's on two passes in a row |
| **Aircraft** | | | |
| 4200–4400 MHz | Radio altimeter: aircraft flying low overhead (helicopter, plane on approach, large drone) | Unclassified | Each burst over the noise floor |
| 118–137 MHz | Aircraft or airport radio (VHF air band) | Unclassified | Each burst over the noise floor |
| 108–118 MHz | Aviation navigation beacon (VOR / ILS) | Unclassified | Background: once a minute per band, never an alert |
| 960–1215 MHz | Aviation navigation (DME / TACAN beacons) | Unclassified | Background: once a minute per band, never an alert |
| 225–400 MHz | Military / government UHF: military aviation, tactical radio, SATCOM | Unclassified | Each burst over the noise floor |
| 1435–1525 MHz | Flight-test / aeronautical telemetry (L-band) | Unclassified | Each burst over the noise floor |
| 2360–2395 MHz | Flight-test / aeronautical telemetry (S-band) | Unclassified | Each burst over the noise floor |
| 2200–2290 MHz | Federal telemetry: space operations, flight test, military UAV links | Unclassified | Each burst over the noise floor |
| **Phones and satellite terminals transmitting (uplinks** | | | |
| 663–698 MHz | Phone transmitting nearby (600 MHz LTE uplink) | Phone | Each burst over the noise floor |
| 699–716 MHz | Phone transmitting nearby (700 MHz LTE uplink) | Phone | Each burst over the noise floor |
| 777–787 MHz | Phone transmitting nearby (700 MHz LTE uplink) | Phone | Each burst over the noise floor |
| 788–798 MHz | Phone or public-safety LTE (FirstNet) transmitting nearby | Phone | Each burst over the noise floor |
| 799–805 MHz | Radio keying up nearby (700 MHz public-safety mobile) | Unclassified | Each burst over the noise floor |
| 806–815 MHz | Radio keying up nearby (800 MHz repeater input) | Unclassified | Each burst over the noise floor |
| 815–824 MHz | Phone transmitting nearby (LTE band 26 uplink) | Phone | Each burst over the noise floor |
| 824–849 MHz | Phone transmitting nearby (850 MHz cellular uplink) | Phone | Each burst over the noise floor |
| 1710–1780 MHz | Phone transmitting nearby (AWS LTE uplink) | Phone | Each burst over the noise floor |
| 1850–1920 MHz | Phone transmitting nearby (PCS uplink) | Phone | Each burst over the noise floor |
| 2000–2020 MHz | Phone transmitting nearby (AWS-4 uplink) | Phone | Each burst over the noise floor |
| 2305–2315 MHz | Phone transmitting nearby (WCS uplink) | Phone | Each burst over the noise floor |
| 1610–1626.5 MHz | Satellite phone or tracker transmitting nearby (Globalstar / Iridium) | Phone | Each burst over the noise floor |
| 1626.5–1660.5 MHz | Satellite terminal transmitting nearby (Inmarsat: maritime, aviation) | Phone | Each burst over the noise floor |
| **Two-way radio, by band** | | | |
| 156–162.05 MHz | Marine VHF radio (boats, harbors) | Unclassified | Each burst over the noise floor |
| 160.2–161.6 MHz | Railroad radio | Unclassified | Each burst over the noise floor |
| 162.4–162.55 MHz | NOAA Weather Radio | Unclassified | Background: once a minute per band, never an alert |
| 162.05–174 MHz | Federal government VHF | Unclassified | Each burst over the noise floor |
| 138–144 MHz | Federal / military VHF | Unclassified | Each burst over the noise floor |
| 148–150.8 MHz | Federal / military VHF | Unclassified | Each burst over the noise floor |
| 150.8–174 MHz | VHF two-way radio (public safety, business) | Unclassified | Each burst over the noise floor |
| 144–148 MHz | Amateur radio (2 m) | Unclassified | Each burst over the noise floor |
| 219–225 MHz | Amateur radio (1.25 m) / telemetry | Unclassified | Each burst over the noise floor |
| 30–50 MHz | VHF low-band two-way radio | Unclassified | Each burst over the noise floor |
| 50–54 MHz | Amateur radio (6 m) | Unclassified | Each burst over the noise floor |
| 400–406 MHz | Weather balloon (radiosonde) or federal telemetry | Unclassified | Each burst over the noise floor |
| 406.1–420 MHz | Federal government UHF | Unclassified | Each burst over the noise floor |
| 420–450 MHz | Amateur radio (70 cm) / military radar | Unclassified | Each burst over the noise floor |
| 450–470 MHz | UHF two-way radio (public safety, business) | Unclassified | Each burst over the noise floor |
| 608–614 MHz | Hospital medical telemetry (WMTS) | IoT / sensor | Each burst over the noise floor |
| 769–775 MHz | 700 MHz public-safety radio (base / repeater) | Unclassified | Background: once a minute per band, never an alert |
| 896–902 MHz | 900 MHz business radio / private LTE | Unclassified | Each burst over the noise floor |
| 935–941 MHz | 900 MHz business radio / private LTE | Unclassified | Background: once a minute per band, never an alert |
| 1390–1400 MHz | Hospital medical telemetry (WMTS) | IoT / sensor | Each burst over the noise floor |
| 1427–1435 MHz | Hospital medical telemetry (WMTS) | IoT / sensor | Each burst over the noise floor |
| 1780–1850 MHz | Federal / military (1780–1850 MHz: tactical links, satellite control) | Unclassified | Each burst over the noise floor |
| 4400–4940 MHz | Federal / military microwave (4.4–4.94 GHz) | Unclassified | Each burst over the noise floor |
| **Radars** | | | |
| 2700–2900 MHz | Airport or weather radar (S-band) | Unclassified | Background: once a minute per band, never an alert |
| 2900–3100 MHz | Ship radar (S-band) | Unclassified | Each burst over the noise floor |
| 3100–3450 MHz | Military radar (S-band) | Unclassified | Each burst over the noise floor |
| **Background** | | | |
| 1–1.7 MHz | AM broadcast | Unclassified | Background: once a minute per band, never an alert |
| 1.7–30 MHz | HF: shortwave, amateur, maritime, aviation | Unclassified | Background: once a minute per band, never an alert |
| 54–88 MHz | TV broadcast (VHF low) | Unclassified | Background: once a minute per band, never an alert |
| 88–108 MHz | FM broadcast | Unclassified | Background: once a minute per band, never an alert |
| 137–138 MHz | Weather / low-orbit satellite downlink | Unclassified | Background: once a minute per band, never an alert |
| 174–216 MHz | TV broadcast (VHF high) | Unclassified | Background: once a minute per band, never an alert |
| 470–608 MHz | TV broadcast (UHF) | Unclassified | Background: once a minute per band, never an alert |
| 614–652 MHz | Cell tower (600 MHz downlink) | Cell site | Background: once a minute per band, never an alert |
| 716–768 MHz | Cell tower (700 MHz downlink) | Cell site | Background: once a minute per band, never an alert |
| 851–869 MHz | 800 MHz tower output (regional, not nearby) | Unclassified | Background: once a minute per band, never an alert |
| 869–894 MHz | Cell tower (850 MHz downlink) | Cell site | Background: once a minute per band, never an alert |
| 929–932 MHz | Paging transmitter | Unclassified | Background: once a minute per band, never an alert |
| 941–960 MHz | Fixed point-to-point links / paging | Unclassified | Background: once a minute per band, never an alert |
| 1525–1559 MHz | Mobile satellite downlink (Inmarsat, Ligado) | Unclassified | Background: once a minute per band, never an alert |
| 1660.5–1710 MHz | Weather satellite / radiosonde downlink | Unclassified | Background: once a minute per band, never an alert |
| 1920–2000 MHz | Cell tower (PCS downlink) | Cell site | Background: once a minute per band, never an alert |
| 2110–2200 MHz | Cell tower (AWS downlink) | Cell site | Background: once a minute per band, never an alert |
| 2315–2345 MHz | Satellite radio (SiriusXM) and its street repeaters | Unclassified | Background: once a minute per band, never an alert |
| 2345–2360 MHz | Cell tower (WCS downlink) | Cell site | Background: once a minute per band, never an alert |
| 2496–2690 MHz | 5G (2.5 GHz band 41): tower or phone | Cell site | Background: once a minute per band, never an alert |
| 3450–3550 MHz | Cell tower (3.45 GHz 5G) | Cell site | Background: once a minute per band, never an alert |
| 3550–3700 MHz | CBRS: private LTE / 5G, fixed wireless | Network | Background: once a minute per band, never an alert |
| 3700–3980 MHz | Cell tower (C-band 5G) | Cell site | Background: once a minute per band, never an alert |
| 3980–4200 MHz | Satellite C-band downlink | Unclassified | Background: once a minute per band, never an alert |
<!-- bands:end -->

</details>

**Background**, at most once a minute per band (they're everywhere): AM / HF / FM / TV broadcast,
VOR / ILS / DME navigation beacons, NOAA Weather Radio, paging, every cell-tower downlink (600 MHz
through C-band, 2.5 GHz band 41, CBRS), satellite downlinks (Inmarsat, weather satellites,
SiriusXM) and airport / weather radar. **Skipped:** 2.4 GHz and 5 GHz Wi-Fi and Bluetooth, Wi-Fi 6E,
GPS L2 and long-range air-traffic radar (1215–1240, 1300–1390 MHz), and the radio-astronomy band
(1400–1427 MHz). Anything else is reported as "*NNN.N* MHz carrier".

**Broadband bursts.** When four or more unrelated bands rise within 2.5 s, the HackRF is overloading:
a strong transmitter beside it (the phone, the Pi's own Wi-Fi) or a radar's main beam sweeping past. The
bridge keeps hits within 5 dB of the strongest (the likely cause) and anything 30 dB over its floor, drops
the rest, and sends one status line at most once a minute. Reports are held 2.5 s to judge this. Only bands
that *rose* in the last 10 s take part: a bucket hot for longer (a spur, a steady carrier) is established,
passes untouched and is not counted, so permanent spurs can't make every window look like overload. A 1 MHz
bucket hot for 3 min straight is always-on: background, once a minute. Radar hits are one entry per radar band.
Lower the gain with `--hackrf-lna 16` (and `--hackrf-vga`) if it happens often.

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

**Parked for body cams & radios.** *More → Companion radio → Park for body cams & radios*
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
- **HackRF + PortaPack (Mayhem):** `lsusb` showing `1d50:6018` / `ttyACM0` instead of `1d50:6089` means it's in
  Mayhem's own mode; choose HackRF mode on the PortaPack screen. `Under-voltage detected!` in `dmesg` with three
  radios plugged in: use the 3 A supply or a powered hub.
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

with the licensee, call sign, service and place in the detail view, instead of "UHF carrier". Each
licensee also gets an **agency type** from its name and radio service — Fire / rescue, EMS / ambulance,
Hospital / medical, 911 / emergency management, Law enforcement, School / campus, Airport, Port / marine,
Transit / roads, Utility, Security company, Towing, Taxi, Delivery, Federal, Local government, else
Public safety or Business — and every licensed hit leads with it: `Fire / rescue: <County> Fire Rescue ·
WQAB123`, or `EMS / ambulance radio nearby · <licensee> · WQAB124` on a repeater input. Rebuild an older
pack to get agency types.

What a licence hit claims is kept to what the record supports, so it never raises a false alarm:
a repeater **input** (station class MO) is *Likely* a radio nearby; a base / repeater **output** is
heard for miles, so *Possible* and labelled "(regional)"; a **wide sweep bin** (HackRF, 500 kHz) with
several licensees in it names none — "Licensed radio nearby · N licensees in this 500 kHz (agency
types)", *Possible* — only the RTL-SDR's 12.5 kHz sweep names one; and a licence **never asserts a
device type** the app alerts on (body cam, camera, traffic sensor, drone, tool), even from an older
pack that stored one. Low-threat hints (vehicle, IoT) are kept for list colouring. Alerts are opt-in by
agency type (app: Companion radio → *Alert when one of these keys up nearby*), and only for repeater
inputs.
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
