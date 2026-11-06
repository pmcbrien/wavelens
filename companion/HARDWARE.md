# Hardware compatibility: what works, what doesn't, and why

The short list of parts that give no trouble, and the longer list of what each part has to be. Everything here
comes from hardware that has actually run the bridge; where something failed, it says how. The units we sell
(Spotter, Scanner, Full Spectrum) are built from the "known good" column.

## At a glance

| Part | Known good | Works with care | Avoid |
|---|---|---|---|
| Phone | Pixel 6+, Samsung Galaxy S21+/A5x+, any Android 12+ phone with Bluetooth 5 that leaves background apps alone | Older Android 8–11 phones (BLE audio is choppier: 23-byte MTU fallbacks); OnePlus / Xiaomi / Huawei / Oppo after turning battery optimisation off for Wavelens | Phones without Bluetooth LE (pre-2013), Android below 8.0, "lite" Android forks that kill background services |
| Scanner computer | Raspberry Pi 4 (4 GB), Pi 5 (4 GB) | Pi 4 (2 GB): sensors + one sweep fine, two decoders tight; Pi 3B+: sensors only, no digital voice | Pi Zero / Zero 2 W, Pi 3B, anything with one USB port and no Bluetooth 4.2+ |
| RTL-SDR dongle | RTL-SDR Blog **V3** (R820T2, TCXO, metal case); Nooelec NESDR SMArt / SMArTee | RTL-SDR Blog **V4** (R828D) *only for the sensor job*, until the rtl-sdr-blog driver is proven on your Pi; generic RTL2838 / R820T "DVB-T" sticks with a TCXO | Dongles with a plain 28.8 MHz crystal (drift kills P25); E4000 / FC0012 tuners; "RTL2832U R820T2 1 ppm" no-name sticks that turn out to be 50 ppm |
| Wideband sweeper | HackRF One (and a PortaPack in HackRF mode) | HackRF clones with a bad clock (set `--hackrf-ppm`) | Anything that needs its own closed driver (SDRplay, Airspy are fine radios but not supported by the bridge today) |
| Spotter board | ESP32-WROOM-32**U** dev board (u.FL socket) with a **CP2102** or CH9102 USB-serial chip | ESP32-WROOM-32 (PCB antenna, no socket): works, hears less; CH340 boards: fine once the driver is on (Windows); ESP32-S3; ESP32-C3 / C6 (single core: hear a little less); ESP32-C5 (also hears 5 GHz Wi-Fi) | ESP32-S2 (no Bluetooth), ESP32-H2 and C2 (no Wi-Fi sniffing / too little RAM), ESP8266 |
| USB hub | Powered USB 3.0 hub with its own 2–3 A supply (12 V-tolerant for the car) | Powered USB 2.0 hub: fine for dongles, not for a HackRF | Unpowered hubs of any kind |
| Pi power | Official 5 V 3 A USB-C (Pi 4) / 5 A (Pi 5); in the car a 12 V → 5 V buck converter rated 5 A, or a quality USB-C PD socket adapter | A good phone charger rated 3 A with a short cable | Lighter-socket chargers rated under 3 A, long thin USB-C cables, any "2.4 A" adapter |
| Storage | Samsung PRO Endurance / SanDisk High Endurance microSD 32 GB; USB SSD on a Pi 5 | Any A2 card from a known brand | No-name cards, anything that came free with something |
| Antennas | Wideband mag-mount scanner whip (25–1300 MHz) on SMA for the scanner dongle; 17 cm quarter-wave whip for 433 MHz; 2.4 GHz panel/Yagi on the Spotter | The dipole kit in the RTL-SDR Blog box, legs set to 17 cm | The 4–7 cm stub antennas that ship with ADS-B kits (1090 MHz only) |
| Cables | Short (≤ 1 m) USB data cables; RG-316 pigtails under 30 cm; RG-58 runs under 3 m | SMA → BNC adapters (one, not a chain) | Charge-only USB cables; adapter chains; 10 m coax to the roof |

## Phones

What the app needs: **Android 8.0+**, Bluetooth LE for the companions, Wi-Fi and location for its own scanning. What makes the difference in practice:

- **Bluetooth 5 and MTU.** The scanner audio (8 kHz ADPCM over BLE) needs the phone to grant a 247-byte MTU and high connection priority. Every Pixel and Samsung from 2019 on does. Phones stuck at the 23-byte default (some Android 8–9 handsets) still get hits and alerts, but voice audio stutters.
- **Background survival.** The scan runs as a foreground service with a notification; Android itself allows that. What kills it is the maker's "battery optimiser": OnePlus, Xiaomi/Redmi/Poco, Huawei/Honor, Oppo/Realme/Vivo and some Motorola builds stop foreground services after a few minutes unless the app is exempted. On those: Settings → Battery → Wavelens → *Unrestricted* (or *Don't optimise*), and on Xiaomi also *Autostart*. dontkillmyapp.com has the per-maker steps. Pixel and Samsung (with "Put unused apps to sleep" off for Wavelens) need nothing.
- **Two companions.** Holding a Pi link and a Spotter link at once is normal BLE; every Android 10+ phone we've tried does it. A phone that already streams to Bluetooth earbuds and a watch is holding four links — still fine, but audio may stutter on the oldest chipsets.
- **Wi-Fi scanning throttle.** Android 9+ limits an app to four Wi-Fi scans in two minutes unless *Developer options → Wi-Fi scan throttling* is off. The app lives with the limit; a Spotter doesn't have it, which is one reason to add one.
- **GPS.** Any phone GPS; a car mount by the windscreen gets fixes faster than a pocket.
- **Not supported:** iPhones (an iOS build is a separate project), Android TV/Auto head units, and phones without location services.

## The scanner computer

- **Raspberry Pi 4, 4 GB** is the reference. It runs two RTL-SDRs, the Bluetooth link and dsd-fme with headroom; a HackRF sweep on top is where it gets busy (the sweep runs in a forked worker and pauses during digital voice for that reason).
- **Pi 5 (4 GB)** has the headroom for HackRF + two dongles + a decoder without pauses, and boots from a USB SSD. Needs the 5 A supply and an active cooler.
- **Pi 4, 2 GB** works for sensors + one sweep + listening; a second decoder swaps. **Pi 3B+** runs rtl_433 sensors and the Bluetooth link; dsd-fme is too slow for real-time P25 on it. **Pi Zero 2 W** has one USB port, no room, and is not supported.
- **Other Linux boxes** (an Intel fanless mini PC, an old laptop) run everything, given a Bluetooth adapter that does the LE *peripheral* role under BlueZ (Intel AX200/AX210 and CSR8510 dongles do) — or, better, a Spotter on USB acting as the Bluetooth radio, which is planned.
- **Thermal:** a Pi 4 throttles at 80 °C and a closed glovebox in summer reaches 60 °C ambient. Vented case plus heatsink or a 30 mm fan. The Scanner we ship is in a vented case with a fan.
- **Power:** `Under-voltage detected!` in `dmesg` is the Pi telling you the supply or cable is the problem. 5 V 3 A at the Pi, over a short cable, from a supply that doesn't sag — a hard-wired buck converter on an ignition-switched circuit in a car; the official supply at home.
- **Storage:** an endurance-rated card. Cars cold-start the Pi daily and cut power without a shutdown; cheap cards corrupt within months. The admin page's Reboot/Shut down buttons help; a read-only root filesystem helps more (planned in `provision.sh`).

## RTL-SDR dongles

Two jobs, and they're different:

- **Sensor dongle** (rtl_433: tyre sensors, fobs, remotes, 315/433/915 MHz). Any RTL2832U dongle with a TCXO. The **RTL-SDR Blog V4** is a good choice *here* once the rtl-sdr-blog driver is installed; on the stock `rtl-sdr` package it logs `[R82XX] PLL not locked!`, tunes wrong and hangs `rtl_power`, so until that driver is proven on your Pi keep the V4 on sensors only.
- **Scanner dongle** (sweeps, listening, trunking, 800 MHz public safety). A plain **RTL2832U + R820T2 with TCXO** on the stock driver: RTL-SDR Blog **V3**, Nooelec SMArt / SMArTee, or the generic "RTL2838UHIDIR" sticks. P25 needs the tuned frequency within about 1 kHz: a **TCXO (≤ 1 ppm)** is mandatory; a 28.8 MHz crystal drifts further than that between a cold morning and a hot afternoon. Set `--rtl-ppm` if a dongle's offset is known.
- **Bias tee:** only the V3/V4 have one; the bridge never turns it on (nothing we sell needs powering).
- **Serials:** every cheap dongle ships as `00000001`. `rtl_eeprom -d 0 -s 00000002` once per dongle, then replug; the bridge's auto-plan is by what's on the bus, but pinned roles (`@SERIAL`) need distinct serials.
- **Two dongles, one Pi:** a dongle draws ~300 mA; two on the Pi's own ports is fine. Add a HackRF (500 mA, spikes higher) or an ESP32 and you need the powered hub.
- **One program per dongle.** PiAware/dump1090/readsb hold a dongle open; `usb_claim_interface error -6` means something else has it.

## HackRF

- **HackRF One** for the 1 MHz–6 GHz energy sweep: 2.4/5.8 GHz video links, 4.9 GHz, cellular uplinks. It does **not** replace the RTL-SDR: listening and trunking stay on the dongle (dsd-fme reads `rtl_tcp`).
- **PortaPack** owners: put it in HackRF mode on the PortaPack screen (`lsusb` must show `1d50:6089`, not Mayhem's `1d50:6018`); the bridge switches it over itself when it finds the console port.
- Always on a **powered hub**, with a wideband (700 MHz–6 GHz) antenna of its own; the 800 MHz whip tells it nothing above 1.3 GHz.
- Clones: fine if the clock is decent; `--hackrf-ppm` corrects a fixed offset. `--hackrf-lna 16 --hackrf-bin 100000` are sane car settings.

## USB hubs

The single most common cause of "a dongle vanished" / "rtl_433 can't open device" on a Pi is an unpowered hub, or a powered one running on the Pi's 5 V.

- **Powered**, with its own supply rated **2–3 A**. USB 3.0 so each port is rated 900 mA (USB 2.0 ports are 500 mA, enough for dongles but marginal for a HackRF).
- **Four ports**: dongle, dongle, HackRF, spare (Spotter on USB, or a GPS puck).
- In a car: a hub that takes **9–36 V in** (barrel) wired to the same ignition-switched circuit as the Pi, or a 5 V 4 A buck feeding it. Not the Pi's USB ports as the source — that is the brown-out.
- Budget: Pi 4 USB ports supply 1.2 A **total** across all four. Two dongles (600 mA) + Bluetooth (on-board, no USB) is inside it; two dongles + ESP32 (150 mA) + a HackRF (500 mA+) is not.
- Skip hubs with per-port switches and "charging" ports: they renegotiate on load and drop devices.

## Spotter (ESP32) boards

- **ESP32-WROOM-32U** dev board (the *-U* has a u.FL socket for an external antenna; that is the whole point) with a **CP2102** / CP2102N or **CH9102** USB-serial chip. Flashes without holding BOOT; shows up as `/dev/ttyUSB0` on the Pi and as a COM port on Windows without extra drivers (CH9102 may want one on older Windows).
- **CH340** boards work; Windows needs the WCH driver, and some need BOOT held while flashing.
- **ESP32-WROOM-32** with the PCB antenna: works, hears less, no antenna upgrade path.
- **ESP32-S3**: supported (`[env:esp32s3]`), dual core like the classic, works the same.
- **ESP32-C3, ESP32-C6** (C3 SuperMini, XIAO ESP32C3 / C6, C6-DevKitC-1): supported from firmware 2026.11.06 (`[env:esp32c3]`, `[env:esp32c6]`). One core runs both the Wi-Fi sniffer and the Bluetooth scan (the BLE scan window drops to 60%), so they catch a little less than a classic ESP32 in a busy street. Pick a board with an antenna socket if you want range.
- **ESP32-C5** (C5-DevKitC-1): supported from 2026.11.06 (`[env:esp32c5]`), single core, and the only board that also hears **5 GHz** Wi-Fi: by default it hops the 13 2.4 GHz channels and 25 5 GHz channels; `{"c":"band","v":"2g"|"5g"|"all"}` narrows it, and its hello adds a `5g` cap.
- The C3 / C5 / C6 builds are new (Arduino core 3.x on the pioarduino platform): they build and install, and are being proven on testers' boards.
- **Not supported:** ESP32-S2 (no Bluetooth, so no link to the phone), ESP32-H2 and C2 (no Wi-Fi sniffing / too little memory), ESP8266.
- Every hello carries `"chip"` (`esp32`, `esp32s3`, `esp32c3`, `esp32c5`, `esp32c6`); the app and the Pi only push a board the image built for its chip. The browser flasher reads the chip itself. On C3 / C5 / C6 dev kits with two USB ports use the one marked USB (Serial is the chip's own USB).
- 4 MB flash minimum (every WROOM-32 has it); the OTA partition scheme needs it.
- Power: any USB port, 150 mA. A charge-only cable is fine for running it; a data cable is needed only to flash from a computer.
- Antenna: a 2.4 GHz panel or Yagi on a u.FL → SMA pigtail; the stealth stub we fit on the Spotter hears about a block further than a phone and doesn't look like anything.

## Antennas and cable

- **Scanner dongle:** wideband mobile scanner whip, 25–1300 MHz, mag-mount, roof centre. This part decides whether trunking works; the 4 cm stubs from ADS-B kits are deaf at 800 MHz. The roof antenna kit we sell is this antenna on a 3 m SMA lead plus sealant.
- **Sensor dongle:** 17 cm quarter-wave for 433 MHz (or the Blog dipole kit at 17 cm per leg). Sensors are near-field; dash or rear deck is fine.
- **HackRF:** a 700 MHz–6 GHz wideband (log-periodic or discone-style) antenna.
- **Spotter:** 2.4 GHz directional (panel or Yagi) for range in one direction, or the stub for all-round.
- **Coax:** RG-58 loses about 1 dB per 3 m at 850 MHz; RG-316 pigtails are fine for 30 cm. One adapter at most; SMA female on the dongles, so the lead wants SMA male.
- **Yagi:** the 9-element we sell is for the RTL-SDR (scanner bands); a 2.4 GHz Yagi is a different part, for the Spotter.

## Known-bad combinations, from the bench

| Symptom | Cause | Fix |
|---|---|---|
| Trunking never locks; `rtl_power` shows nothing above VHF | V4 dongle got the scanner role on the stock driver | Give the V4 the sensor job or install the rtl-sdr-blog driver; plain R820T2 on the scanner job |
| Dongle disappears after minutes, `usb_claim_interface -6` | Unpowered hub, or a HackRF on the Pi's own ports | Powered hub with its own supply |
| `Under-voltage detected!` | Supply/cable | 3 A supply, short cable, buck converter in the car |
| P25 decodes then garbles as the car warms up | Crystal dongle drifting | TCXO dongle; `--rtl-ppm` |
| Audio stutters on the phone | 23-byte MTU phone, or four Bluetooth links on an old chipset | Newer phone; drop the earbuds while listening |
| App stops scanning after 5 minutes | Maker battery optimiser | Exempt Wavelens (dontkillmyapp.com) |
| Spotter won't flash | Charge-only cable, or BOOT needed | Data cable; hold BOOT on CH340 boards |
| Pi won't boot after a week in the car | Cheap SD card | Endurance card; shut down from the admin page before cutting power when you can |

Questions or a part not listed: wavelensapp@gmail.com, or open an issue on the public repo with `lsusb` and `journalctl -u wavelens-bridge -n 100` output.
