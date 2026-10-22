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
| Pi / carputer + **HackRF** | hackrf_sweep: carriers anywhere from 1 MHz to 6 GHz — 5.8 GHz video, 1.2 GHz links, 900 MHz LoRa, 433 / 315 MHz bursts — as "something is transmitting here" | No | Same script, +$300 |
| Carputer (any Linux box) | All of the above at once, as many radios as it has USB ports | No | Same script |
| Any SDR build + [local FCC pack](#local-fcc-license-pack--who-owns-that-carrier) | Licensed carriers named: which agency or business holds the frequency, and how far its site is | — | One script run per area |
| Arduino | Only with a BLE module (HM-10) *and* a radio module; an ESP32 does both for less | — | Not recommended; see [diagrams](diagrams/) |

Wiring and antenna diagrams: [diagrams/](diagrams/) — overview, ESP32, Pi + SDR, carputer, Arduino, vehicle mounting.

### What the SDRs do and don't add

- **RTL-SDR** (24–1766 MHz): decoding, via `rtl_433`. It names things: a Schrader TPMS sensor, an Acurite weather station, a garage remote. Ceiling is 1.7 GHz, so no Wi-Fi, BLE or 5.8 GHz.
- **HackRF** (1 MHz–6 GHz): energy, via `hackrf_sweep`. It reaches the bands the RTL-SDR can't, but a sweep sees *carriers*, not packets: "30 dB over the floor at 5745 MHz" could be a video link or a Wi-Fi access point on channel 149. The bridge labels these honestly and the app shows them as *Possible*. It does not demodulate Wi-Fi usefully (a $15 monitor-mode adapter does that better) and cannot tell a camera's LTE modem from the phones around you. **Receive only** — the bridge never transmits, and neither should you.
- **ESP32 + CC1101 / SX1276**: a subset of rtl_433's decoders on a $5 module, tuned to one frequency at a time. The no-Pi way to hear TPMS and 433 MHz.
- **ADS-B** (`dump1090`, `readsb`): aircraft overhead with callsign, altitude and position in the detail view. Unclassified on purpose — an airliner is not a drone.

## Pairing

1. Power the companion. It advertises the Wavelens service.
2. In the app: More → Companion radio → turn on **Use a companion radio**.
3. Start a scan. The phone's own BLE scan hears the companion, lists it, and — if none
   is chosen — adopts it automatically. It stays connected for the session and reconnects
   if the link drops.
4. Pick where it listens: radio (Wi-Fi / BLE / both / other RF), Wi-Fi band, channels
   (hop or park). The choice is sent on every reconnect.

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
{"t":"hello","fw":"wavelens-esp32 0.1","name":"Wavelens ESP32","caps":["wifi","ble","rf"]}
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
{"c":"mode","v":"both"}              // wifi | ble | both | rf
{"c":"band","v":"2g"}                // 2g | 5g | all     (say so in a status if the hardware can't)
{"c":"ch","v":"hop"}                 // hop every channel of the band
{"c":"ch","v":[1,6,11]}              // or park on these
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

What this does and does not tell you: a licensed public-safety frequency carrying traffic
means that agency's radios are in range — a dispatcher, a cruiser, a repeater on a tower
ten miles off — not that one is watching you. Unlicensed bands (Wi-Fi, BLE, 433 / 915 MHz
ISM, 5.8 GHz video) have no license records; the app's own signature pack covers those.

## What a companion does not fix

A directional antenna adds gain where it points and loses it everywhere else. It makes a
fixed pole audible from further out and a device riding with you audible all the time; it
does not keep an oncoming car at highway speed in range any longer. The companion makes the
app hear more and hear it sooner. It does not change the physics in
[Does it work?](https://wavelensapp.us/results.html).
