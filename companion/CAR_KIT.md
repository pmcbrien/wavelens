# Wavelens companion: the car kit

A parts list for a Raspberry Pi companion that lives in a car and runs the full bridge: sensor
decoding, VHF/UHF/800 MHz sweeps, listening, trunk-tracking, and (optionally) the HackRF's 1 MHz–6 GHz
sweep. Everything is receive-only; nothing here transmits. Prices are rough US street prices as of
late 2026 and move around; the vendor links are category pages, not endorsements.

Two tiers. The **Scanner kit** does everything the Scanner tab needs (P25 / DMR trunking, Follow,
sensors). The **Full kit** adds the HackRF for the wideband sweep (video links, drones, 700/4.9 GHz
public-safety data, cell uplinks). Build the Scanner kit first; the HackRF bolts on later.

## Scanner kit (~$200–260)

| # | Part | What it does | Spec that matters | ~Price |
|---|---|---|---|---|
| 1 | **Raspberry Pi 4 (4 GB) or Pi 5 (4 GB)** | Runs the bridge, dsd-fme and the sweeps | A Pi 4 handles two RTL-SDRs + Bluetooth fine; a Pi 5 adds headroom for the HackRF sweep at 100 kHz bins. Not a Pi Zero: dsd-fme and three USB radios are too much. | $55–80 |
| 2 | **32 GB A2 microSD** (or a USB SSD for the Pi 5) | OS, dsd-fme build, RadioReference packs | Endurance-rated; a car cold-starts the Pi daily | $10–15 |
| 3 | **RTL-SDR dongle ×2** | Dongle A: rtl_433 sensors (TPMS, fobs, 315/433/915). Dongle B: sweep + listen + trunking | See *Which dongles* below. Both need **TCXO** (0.5 ppm or better): P25 needs the tuned frequency within ~1 kHz, and a cheap 28.8 MHz crystal drifts that much with the car's temperature swing. | $35–45 each |
| 4 | **Powered USB 3 hub, 4-port, 12 V-tolerant** | Two dongles + Bluetooth + (later) a HackRF is more than a Pi's USB can source | Externally powered (barrel or USB-C PD in). A Pi 4 browns out (`Under-voltage detected!` in `dmesg`) with two dongles and a HackRF on its own ports. | $20–30 |
| 5 | **12 V → 5 V / 5 A buck converter** with USB-C, or a USB-C PD car charger rated 5 V 5 A (Pi 5) / 5 V 3 A (Pi 4) | Clean power from the car | Hard-wired to an accessory (ignition-switched) circuit via an add-a-fuse tap, or a quality PD socket adapter. Cheap lighter plugs sag under load and reset the Pi over bumps. | $12–25 |
| 6 | **Scanner antenna, mag-mount, 25–1300 MHz** | Antenna for dongle B: public-safety 800 MHz, UHF, VHF | A wideband mobile scanner whip (the kind sold for Uniden/Whistler mobiles) with an SMA or BNC lead. Roof centre is best; a trunk-lid mount is fine. This is the part that decides whether trunking works — the stock 4 cm dongle stub is 15–20 dB down at 850 MHz. | $20–35 |
| 7 | **433/315/915 MHz antenna** for dongle A | TPMS, key fobs, sensors, 900 MHz ISM | A quarter-wave whip tuned for 433 MHz (~17 cm) on a small mag mount, or the dipole kit that ships with the RTL-SDR Blog dongle set to 17 cm per leg. Sensors are near-field (metres); this one can sit on the dash or rear deck. | $8–15 |
| 8 | **SMA adapters & 50 Ω coax** | Antennas → dongles | RTL-SDR dongles are SMA female; most scanner mounts are BNC or SMA male. Get an SMA-male-to-BNC-female and keep runs under 3 m (RG-58 loses ~1 dB per 3 m at 850 MHz; RG-316 pigtails are fine for 30 cm). | $8–12 |
| 9 | **GPS puck (USB, u-blox 7/8)** — optional | Position for the FCC/RadioReference ranking without sharing the phone's GPS | Any u-blox USB GPS; `gpsd` reads it. The phone can share its fix instead (Companion → Share position). | $15–20 |
| 10 | **Case with ventilation + heatsink or fan** | The Pi sits in a parked car in Florida | A Pi 4 throttles at 80 °C; a closed glovebox in July reaches 60 °C ambient. Vented case plus the official fan or a 30 mm fan on GPIO. | $10–20 |
| 11 | **Velcro / mounting tape, cable ties, a short USB-A extension** | Keeping it in one piece | Mount the hub and Pi where you can reach the SD card. | $5–10 |

### Which dongles

The lesson from this week: **the two dongles are not interchangeable, and the driver matters.**

- For **dongle B (sweep / listen / trunking)**: a plain **RTL2832U + R820T2 TCXO** dongle — RTL-SDR Blog V3, Nooelec NESDR SMArt / SMArTee, or similar. These run on the stock `librtlsdr` and reach 800 MHz fine. The Realtek "RTL2838UHIDIR" that came with the ADS-B kit is one of these, and it hears Pinellas's 800 MHz control channels at +35 dB.
- For **dongle A (rtl_433 sensors)**: anything with a TCXO. The **RTL-SDR Blog V4** (R828D tuner, built-in filters and bias tee) is a better radio on paper, but it **needs the rtl-sdr-blog driver** (`https://github.com/rtlsdrblog/rtl-sdr-blog`), not the Raspberry Pi OS `rtl-sdr` package — on the stock driver it shows `[R82XX] PLL not locked!`, tunes wrong, and `rtl_power` hangs. Until that's confirmed working on your Pi, give the V4 the sensor job (315/433/915 only) and keep the plain dongle on the sweeps. `pi/install-dsd.sh` pulls `librtlsdr-dev` from apt; if you install the blog driver, do it afterwards and rebuild dsd-fme.
- **Set serials once.** Cheap dongles all ship as `00000001`; `rtl_eeprom -d 0 -s 00000978` (etc.) gives each one a name so `WAVELENS_RTL433="-d :SERIAL …"` and `--rtl-power …@SERIAL` / `--listen-device SERIAL` pin roles that survive a reboot in a different USB order.

### Antennas, by band

| Band | Who | Antenna |
|---|---|---|
| 851–869 MHz | P25 / EDACS public safety (Pinellas, Hillsborough, SLERS) | Scanner mag-mount or an 800 MHz quarter-wave (8.4 cm) |
| 764–776 MHz | 700 MHz P25 | Same scanner antenna |
| 450–470 MHz | UHF business, DMR, hospitals, federal | Same scanner antenna |
| 150–174 MHz | VHF public safety, marine, rail, ham | Same scanner antenna (it's a compromise at VHF, but control channels aren't here) |
| 118–137 MHz | Air band | Same |
| 315 / 433 / 915 MHz | TPMS, fobs, sensors, LoRa | Short 433 whip on dongle A, near the car's own wheels is fine |
| 1090 MHz | ADS-B aircraft (`--dump1090`, needs a third dongle) | The 1090 stub that came with the ADS-B kit |

One wideband mobile scanner antenna on the roof does the first five rows. Don't split one antenna
between two dongles with a tee — a cheap splitter costs 3–6 dB and lets each dongle's LO leak into the
other; two antennas is the right answer.

## Full kit: add the HackRF (+$320–380)

| # | Part | Notes | ~Price |
|---|---|---|---|
| 12 | **HackRF One** (Great Scott Gadgets, or the PortaPack H2/H4 bundle) | `hackrf_sweep` 1 MHz–6 GHz in about a second. Receive-only use. The bridge handles a PortaPack in Mayhem mode by switching it to HackRF mode itself. Clones work but have more spurs; run `pi/find_spurs.py` either way and put the result in `--hackrf-mask`. | $300–350 (bare) / $380+ (PortaPack) |
| 13 | **Wideband antenna, 700 MHz–6 GHz** | The HackRF's job is the bands the RTL can't reach: 2.4 / 5.8 GHz video, 4.9 GHz public safety, 1.7–2.2 GHz cellular. A log-periodic or a dual-band 2.4/5.8 GHz "Wi-Fi" rubber duck on an SMA mag mount. The stock ANT500 telescopic is for the bench, not a roof. | $15–30 |
| 14 | **USB 3 cable, short, shielded** | The HackRF streams 20 MS/s; a long or cheap cable drops samples and the bridge's stall watchdog restarts it | $8 |
| 15 | **Spare powered-hub port** | Already in the Scanner kit; this is why it has four ports | — |

The HackRF sweep in a car finds: 5.8 GHz video (drones, FPV, wireless cameras), 2.4 GHz non-Wi-Fi
links, 4.9 GHz public-safety data, 1.2 GHz video, cell uplinks (a phone transmitting nearby), 700/800
MHz carriers as energy. It does **not** replace the RTL-SDR for listening: dsd-fme reads from `rtl_tcp`,
and trunk-tracking needs the RTL.

## Wiring

```
                  roof: scanner whip (25–1300 MHz)        roof/rear: 2.4/5.8 GHz duck (Full kit)
                           │ SMA                                   │ SMA
   dash: 433 whip          │                                       │
      │ SMA                │                                       │
      ▼                    ▼                                       ▼
 ┌─────────┐        ┌─────────┐                              ┌───────────┐
 │ RTL A   │        │ RTL B   │                              │ HackRF One│
 │ rtl_433 │        │ sweep + │                              │ hackrf_   │
 │ sensors │        │ listen +│                              │ sweep     │
 └────┬────┘        │ trunk   │                              └─────┬─────┘
      │ USB         └────┬────┘                                    │ USB 3
      ▼                  ▼                                         ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Powered USB 3 hub  ◄── 12 V (or 5 V 3 A) from the accessory circuit │
 └───────────────────────────────┬──────────────────────────────────────┘
                                 │ USB 3 (one cable to the Pi)
                      ┌──────────▼──────────┐
                      │ Raspberry Pi 4 / 5  │ ◄── 12 V → 5 V buck (USB-C), accessory-switched
                      │ wavelens-bridge     │
                      │ dsd-fme · rtl_power │
                      └──────────┬──────────┘
                                 │ Bluetooth LE (JSON + ADPCM audio, ~4 kB/s)
                      ┌──────────▼──────────┐
                      │ phone · Wavelens    │  Scanner tab · Spectrum analyzer · Alerts journal
                      └─────────────────────┘
```

Diagrams with the same layout: [diagrams/pi-hackrf-2sdr.svg](diagrams/pi-hackrf-2sdr.svg) and
[diagrams/vehicle-mount.svg](diagrams/vehicle-mount.svg).

## Install checklist

1. Flash Raspberry Pi OS Lite (64-bit). Enable SSH. `sudo apt install rtl-sdr rtl-433 bluez git python3`.
2. Set the dongle serials with `rtl_eeprom`, then `rtl_test -t` to confirm both show with the right SN.
3. `pi/install-dsd.sh` for digital voice (10 min build). If a V4 is in the kit, install the rtl-sdr-blog driver *after* this and rebuild dsd-fme; blacklist `dvb_usb_rtl28xxu`.
4. `pi/install-service.sh`, then edit `/etc/default/wavelens-bridge`: `WAVELENS_RTL433="-d :SERIAL_A -f 315M -f 433.92M -f 915M -H 30 -g 40"` and
   `WAVELENS_ARGS="--listen-device SERIAL_B --rtl-power 144M:174M:12.5k@SERIAL_B --rtl-power 440M:470M:12.5k@SERIAL_B --rtl-power 806M:824M:12.5k@SERIAL_B --rr-pack … --rr-county …"`, plus `--hackrf-sweep --hackrf-lna 16 --hackrf-bin 100000 --hackrf-mask …` for the Full kit.
5. Export your counties' RadioReference CSVs (frequencies, trunked sites, talkgroups) into `/usr/local/lib/wavelens/rr/` — the sites export is what lets the bridge lock a control channel by name, the talkgroups export is what names the calls.
6. Prove the 800 MHz antenna before trusting the hunt: `rtl_power -d SERIAL_B -f 851M:869M:12.5k -i 2 -e 6 -g 40 /tmp/p.csv` should show lines at +25 dB or more over the band. If the top line is +10, the antenna is wrong.
7. In the app: pair under More → Companion radio, open the Scanner tab, tap SCAN. The first lock takes 30–90 s (the hunt sweeps five bands); after that the control channel is cached and lock-on is immediate.

## Power and heat in a car

- **Accessory-switched power.** The Pi should die with the ignition. An SD card survives hard power-offs far better than a battery that's been drained flat by a Pi left running overnight. If you want it to keep running after the engine stops, add a 10 Ah USB power bank with pass-through charging between the buck converter and the Pi.
- **Cold-start order.** Hub powered before the Pi, or the Pi enumerates the radios as they appear and `rtl_433` may grab the wrong one. Powering both from the same accessory circuit with the hub on a shorter cable is enough in practice.
- **Heat.** Vented case, fan, and don't bury it under the seat with the dongles. The RTL-SDR's tuner drifts with temperature; TCXO dongles hold within 1 kHz, which is why the kit insists on them.
- **Ignition noise.** If the sweep shows a comb of carriers every few hundred kHz that appears with the engine, it's alternator/ignition noise on the power line: a ferrite on the 12 V lead and the USB cable, and ground the hub's shell to the chassis.

## What you do not need

- A second HackRF. One sweep covers it.
- An LNA on the scanner antenna. 800 MHz public safety is +30 dB at the dongle already; an LNA overloads it.
- An Airspy or SDRplay. dsd-fme reads `rtl_tcp`; the bridge is written for RTL-SDRs.
- A Pi with a touchscreen. The phone is the screen.
