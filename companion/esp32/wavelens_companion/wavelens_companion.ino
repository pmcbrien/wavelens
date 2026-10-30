/*
 * Wavelens companion — ESP32 firmware
 *
 * A second radio for the Wavelens Android app. The ESP32 sniffs Wi-Fi management frames
 * (beacons, probe responses) and BLE advertisements, and pushes every hit to the phone as
 * one JSON line over a BLE GATT notify characteristic. The phone can tell it which Wi-Fi
 * channels to park on. Optionally: an RX5808 5.8 GHz receiver sweeps for analog video carriers
 * (FPV / older camera links); a CC1101 / SX1276 decodes 433/315 MHz sensors and, with HAS_SPECTRUM,
 * draws the app's spectrum and reports carriers; an SA818 FM module makes it an analog scanner for
 * the app's Scanner tab (Listen, Follow, scan list, audio over BLE). What it cannot do is digital
 * voice or trunking — that is the Raspberry Pi bridge. Nothing here ever touches the internet.
 *
 * Protocol: see ../../README.md (newline-delimited JSON, service 57617665-6c65-6e73-0100-…).
 *
 * Boards: ESP32-WROOM-32U / ESP32-S3-WROOM-1U / ESP32-C3 — the "U" variants have a u.FL
 * antenna connector, which is the whole point if you want a directional antenna. Classic
 * ESP32 / S3 / C3 are 2.4 GHz only; the 5 GHz band command is honoured only on an ESP32-C5.
 *
 * Libraries (Arduino IDE → Library Manager):
 *   - NimBLE-Arduino 2.x  (h2zero)
 *   - rtl_433_ESP (NorthernMan54) + RadioLib + ArduinoJson, only with HAS_SUBGHZ. rtl_433_ESP is
 *     configured by build flags (-DRF_SX1276 or -DRF_CC1101 and the module's pins), so it is a
 *     PlatformIO build; the platformio.ini next to this file has a working example.
 * Board package: esp32 by Espressif 3.x. Partition scheme: anything with ≥ 1.5 MB app.
 *
 * Status: written against the NimBLE 2.x and ESP-IDF 5 APIs as documented; not yet
 * compiled on hardware by the author. Expect to fix a signature or two on first build.
 */

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <WiFi.h>
#include <algorithm>
#include "esp_wifi.h"
#include "esp_mac.h"

static void candAdd(float mhz);               // a carrier the spectrum sweep found: Follow may tune it (SA818)
static void subghzInit();

// ------------------------------------------------------------------ config

#define FW_NAME     "wavelens-esp32 0.3"
#ifndef LED_PIN
#define LED_PIN     2            // on-board LED on most DevKit boards; set -1 if yours has none
#endif
#define SVC_UUID    "57617665-6c65-6e73-0100-000000000001"
#define TX_UUID     "57617665-6c65-6e73-0100-000000000002"
#define RX_UUID     "57617665-6c65-6e73-0100-000000000003"
#define AU_UUID     "57617665-6c65-6e73-0100-000000000004"   // audio frames while listening (same as the Pi bridge)

// #define HAS_RX5808           // uncomment if an RX5808 5.8 GHz module is wired (see below)
// #define HAS_SUBGHZ           // uncomment for a CC1101 / SX1276 sub-GHz receiver via the rtl_433_ESP library (see below)
// #define HAS_SPECTRUM         // uncomment for RSSI spectrum sweeps on that same SX1276 / CC1101 (RadioLib; see below)
// #define HAS_SA818_U          // uncomment for an SA818-U / DRA818U 400–480 MHz FM receiver module (analog scanner; see below)
// #define HAS_SA818_V          // uncomment for an SA818-V / DRA818V 134–174 MHz module (a second UART; both can be fitted)

// SA818 wiring (classic ESP32 pins; change for your board). The module's audio out goes through a
// 1 µF capacitor into a 10k/10k divider to a 1.65 V midpoint, then into the ADC pin. SQ is the module's
// squelch-detect output: low while a carrier is open. PD (power down) and PTT are tied high (receive only).
#define SA818U_TX    17         // ESP32 → module RXD
#define SA818U_RX    16         // module TXD → ESP32
#define SA818U_SQ    4          // module SQ → ESP32 (input, active low)
#define SA818U_AUDIO 36         // ADC1_CH0 (VP): module AF out via the divider
#define SA818V_TX    25
#define SA818V_RX    26
#define SA818V_SQ    27
#define SA818V_AUDIO 39         // ADC1_CH3 (VN)

// Spectrum reach of the sub-GHz chip (HAS_SPECTRUM): SX1276 433-variant 410–525; 868/915-variant 862–1020;
// CC1101 300–348 / 387–464 / 779–928 (give it the one range you care about).
#ifndef SPEC_LO
#define SPEC_LO 410.0
#endif
#ifndef SPEC_HI
#define SPEC_HI 525.0
#endif
#define _STR2(x) #x
#define _STR(x) _STR2(x)
#ifdef HAS_SPECTRUM
#define SPEC_RANGE_STR _STR(SPEC_LO) "," _STR(SPEC_HI)
#else
#define SPEC_RANGE_STR ""
#endif
#define RX5808_DATA  23         // RX5808 CH1 (SPI data)
#define RX5808_LE    22         // RX5808 CH2 (latch enable)
#define RX5808_CLK   21         // RX5808 CH3 (SPI clock)
#define RX5808_RSSI  34         // RX5808 RSSI, analog, through a 10k/10k divider if > 3.3 V

static const uint32_t HOP_MS        = 250;    // dwell per channel when hopping
static const uint32_t WIFI_RATE_MS  = 1500;   // per-BSSID report rate
static const uint32_t BLE_RATE_MS   = 1000;   // per-address report rate
static const int      RSSI_FLOOR    = -100;   // report everything; the phone decides

// ------------------------------------------------------------------ state

enum Mode { M_BOTH, M_WIFI, M_BLE, M_RF };
static Mode     mode = M_BOTH;
static char     band[8] = "all";
static bool     hopping = true;
static char     devName[24] = "Wavelens-0000";   // "Wavelens-" + last 4 hex of the BT address
static char     pairCode[5] = "0000";
static uint32_t blinkUntil = 0;
static uint8_t  chList[32];
static uint8_t  chCount = 0;
static uint8_t  chIdx = 0;
static uint32_t lastHop = 0;
static uint8_t  curCh = 1;

static const uint8_t CH_24[] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13};

static NimBLEServer*         server = nullptr;
static NimBLECharacteristic* txChar = nullptr;
static NimBLECharacteristic* rxChar = nullptr;
static volatile bool         linked = false;
static uint16_t              mtu = 23;

// Output ring: the Wi-Fi callback runs in the Wi-Fi task, so lines are queued and
// drained from loop() where it is safe to notify.
#define QN 24
#define QL 240
static char     q[QN][QL];
static volatile uint8_t qh = 0, qt = 0;

static bool enqueue(const char* s) {
  uint8_t n = (qh + 1) % QN;
  if (n == qt) return false;                  // full: drop; the next packet will come
  strncpy(q[qh], s, QL - 1); q[qh][QL - 1] = 0;
  qh = n;
  return true;
}

static void drain() {
  while (qt != qh) {
    if (linked && txChar) {
      const char* s = q[qt];
      size_t len = strlen(s);
      size_t chunk = mtu > 3 ? mtu - 3 : 20;
      for (size_t i = 0; i < len; i += chunk) {
        size_t n = min(chunk, len - i);
        txChar->setValue((const uint8_t*)(s + i), n);
        txChar->notify();
        delay(2);
      }
    }
    qt = (qt + 1) % QN;
  }
}

// Per-address rate limit so a chatty beacon does not flood the link.
struct Seen { uint8_t mac[6]; uint32_t t; };
static Seen seenW[96], seenB[96];

static bool rateOk(Seen* tbl, const uint8_t* mac, uint32_t now, uint32_t minMs) {
  int freeSlot = -1, oldest = 0;
  for (int i = 0; i < 96; i++) {
    if (tbl[i].t == 0) { if (freeSlot < 0) freeSlot = i; continue; }
    if (memcmp(tbl[i].mac, mac, 6) == 0) {
      if (now - tbl[i].t < minMs) return false;
      tbl[i].t = now; return true;
    }
    if (tbl[i].t < tbl[oldest].t) oldest = i;
  }
  int s = freeSlot >= 0 ? freeSlot : oldest;
  memcpy(tbl[s].mac, mac, 6); tbl[s].t = now;
  return true;
}

// Minimal JSON string escape into dst (ASCII only; anything else becomes '?').
static void jesc(const uint8_t* s, size_t n, char* dst, size_t cap) {
  size_t j = 0;
  for (size_t i = 0; i < n && j + 2 < cap; i++) {
    uint8_t c = s[i];
    if (c == '"' || c == '\\') { dst[j++] = '\\'; dst[j++] = c; }
    else if (c < 0x20 || c > 0x7e) dst[j++] = '?';
    else dst[j++] = c;
  }
  dst[j] = 0;
}

static void macStr(const uint8_t* m, char* out) {
  snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
}

static void status(const char* msg) {
  char line[QL];
  snprintf(line, sizeof line, "{\"t\":\"status\",\"msg\":\"%s\",\"band\":\"%s\",\"mode\":\"%s\",\"ch\":%d}\n",
           msg, band, mode == M_BOTH ? "both" : mode == M_WIFI ? "wifi" : mode == M_BLE ? "ble" : "rf", curCh);
  enqueue(line);
}

static void hello() {
  char line[QL];
  snprintf(line, sizeof line,
           "{\"t\":\"hello\",\"fw\":\"%s\",\"name\":\"%s\",\"code\":\"%s\",\"caps\":[\"wifi\",\"ble\"%s%s%s%s]%s}\n",
           FW_NAME, devName, pairCode,
#if defined(HAS_RX5808) || defined(HAS_SPECTRUM)
           ",\"rf\"",
#else
           "",
#endif
#ifdef HAS_SUBGHZ
           ",\"subghz\"",
#else
           "",
#endif
#ifdef HAS_SPECTRUM
           ",\"sweep\"",       // the phone's Spectrum / Scanner strips ask with {"c":"spec"}
#else
           "",
#endif
#if defined(HAS_SA818_U) || defined(HAS_SA818_V)
           ",\"listen\",\"scan\"",   // analog FM only: no "trunk" (the Pi bridge advertises that)
#else
           "",
#endif
#ifdef HAS_SPECTRUM
           ",\"spec\":[" SPEC_RANGE_STR "]"   // the sweep's reach in MHz, so the app picks windows it can draw
#else
           ""
#endif
  );
  enqueue(line);
}

// ------------------------------------------------------------------ Wi-Fi sniffer

static void setChannel(uint8_t ch) {
  curCh = ch;
  esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
}

static void applyChannels() {
  if (hopping || chCount == 0) {
    // hop: every channel for the band (2.4 GHz only on classic ESP32 / S3 / C3)
    chCount = sizeof CH_24;
    memcpy(chList, CH_24, chCount);
  }
  chIdx = 0;
  setChannel(chList[0]);
}

static void IRAM_ATTR sniff(void* buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;
  const wifi_promiscuous_pkt_t* p = (const wifi_promiscuous_pkt_t*)buf;
  const uint8_t* f = p->payload;
  int len = p->rx_ctrl.sig_len;
  if (len < 36) return;
  uint8_t subtype = (f[0] >> 4) & 0x0F;
  // 8 = beacon, 5 = probe response: both are an AP or hotspot announcing itself.
  if (subtype != 8 && subtype != 5) return;
  const uint8_t* bssid = f + 16;              // addr3
  uint32_t now = millis();
  if (!rateOk(seenW, bssid, now, WIFI_RATE_MS)) return;

  // SSID: tagged parameters start after the 24-byte header + 12 bytes of fixed fields.
  char ssid[66] = "";
  int i = 36;
  while (i + 2 <= len) {
    uint8_t tag = f[i], tl = f[i + 1];
    if (i + 2 + tl > len) break;
    if (tag == 0) { jesc(f + i + 2, tl > 32 ? 32 : tl, ssid, sizeof ssid); break; }
    i += 2 + tl;
  }
  char mac[18]; macStr(bssid, mac);
  char line[QL];
  snprintf(line, sizeof line,
           "{\"t\":\"wifi\",\"id\":\"%s\",\"n\":\"%s\",\"r\":%d,\"ch\":%d,\"src\":\"esp32\",\"x\":{\"frame\":\"%s\"}}\n",
           mac, ssid, p->rx_ctrl.rssi, p->rx_ctrl.channel, subtype == 8 ? "beacon" : "probe-resp");
  enqueue(line);
}

static void wifiStart() {
  WiFi.mode(WIFI_MODE_STA);
  WiFi.disconnect(true);
  esp_wifi_set_promiscuous(false);
  wifi_promiscuous_filter_t filt = { .filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT };
  esp_wifi_set_promiscuous_filter(&filt);
  esp_wifi_set_promiscuous_rx_cb(&sniff);
  esp_wifi_set_promiscuous(true);
  applyChannels();
}

static void wifiStop() { esp_wifi_set_promiscuous(false); }

static void wifiTick(uint32_t now) {
  if (mode == M_BLE || mode == M_RF) return;
  if (chCount <= 1) return;
  if (now - lastHop < HOP_MS) return;
  lastHop = now;
  chIdx = (chIdx + 1) % chCount;
  setChannel(chList[chIdx]);
}

// ------------------------------------------------------------------ BLE scanner (relay)

class ScanCB : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* d) override {
    if (mode == M_WIFI || mode == M_RF) return;
    uint32_t now = millis();
    const uint8_t* raw = d->getAddress().getBase()->val;      // NimBLE keeps it little-endian
    uint8_t mac[6]; for (int i = 0; i < 6; i++) mac[i] = raw[5 - i];
    if (!rateOk(seenB, mac, now, BLE_RATE_MS)) return;
    if (d->getRSSI() < RSSI_FLOOR) return;

    char name[66] = "";
    std::string nm = d->getName();
    if (!nm.empty()) jesc((const uint8_t*)nm.data(), nm.size(), name, sizeof name);

    char svc[160] = "";
    int sc = d->getServiceUUIDCount();
    for (int i = 0; i < sc && i < 4; i++) {
      std::string u = d->getServiceUUID(i).toString();
      size_t used = strlen(svc);
      snprintf(svc + used, sizeof svc - used, "%s\"%s\"", i ? "," : "", u.c_str());
    }

    char mfg[80] = "";
    std::string md = d->getManufacturerData();
    if (md.size() >= 2) {
      uint16_t cid = (uint8_t)md[0] | ((uint8_t)md[1] << 8);
      char hex[48]; size_t j = 0;
      for (size_t i = 2; i < md.size() && j + 2 < sizeof hex; i++) { snprintf(hex + j, 3, "%02x", (uint8_t)md[i]); j += 2; }
      hex[j] = 0;
      snprintf(mfg, sizeof mfg, "\"%04x\":\"%s\"", cid, hex);
    }

    char ms[18]; macStr(mac, ms);
    char line[QL];
    snprintf(line, sizeof line,
             "{\"t\":\"ble\",\"id\":\"%s\",\"n\":\"%s\",\"r\":%d,\"pub\":%s,\"svc\":[%s],\"mfg\":{%s},\"src\":\"esp32\"}\n",
             ms, name, d->getRSSI(), d->getAddress().getType() == BLE_ADDR_PUBLIC ? "true" : "false", svc, mfg);
    enqueue(line);
  }
};
static ScanCB scanCb;

static void bleScanStart() {
  NimBLEScan* s = NimBLEDevice::getScan();
  s->setScanCallbacks(&scanCb, false);
  s->setActiveScan(true);
  s->setInterval(100);
  s->setWindow(99);
  s->setMaxResults(0);           // do not store; we stream
  s->setDuplicateFilter(false);  // repeats are the point: RSSI over time
  s->start(0, false, true);      // forever
}

// ------------------------------------------------------------------ RX5808 5.8 GHz sweep (optional)

#ifdef HAS_RX5808
static void rxWriteBit(bool b) {
  digitalWrite(RX5808_DATA, b); delayMicroseconds(2);
  digitalWrite(RX5808_CLK, 1);  delayMicroseconds(2);
  digitalWrite(RX5808_CLK, 0);  delayMicroseconds(2);
}
static void rxSetFreq(uint16_t mhz) {
  // Register 0x1: N (7 bits) and A (5 bits) from the synthesizer formula f = 2*(32N + A) + 479.
  uint16_t n = (mhz - 479) / 2;
  uint16_t reg = ((n / 32) << 7) | (n % 32);
  digitalWrite(RX5808_LE, 0);
  for (int i = 0; i < 4; i++) rxWriteBit((1 >> i) & 1);       // address 0x1
  rxWriteBit(1);                                             // write
  for (int i = 0; i < 20; i++) rxWriteBit((reg >> i) & 1);  // 20 data bits, LSB first
  digitalWrite(RX5808_LE, 1); delayMicroseconds(2);
  digitalWrite(RX5808_LE, 0);
}
static uint16_t sweepMhz = 5645; static uint32_t lastSweep = 0; static int sweepRaw = 0;
static void rfInit() {
  pinMode(RX5808_DATA, OUTPUT); pinMode(RX5808_LE, OUTPUT); pinMode(RX5808_CLK, OUTPUT);
  digitalWrite(RX5808_LE, 0); digitalWrite(RX5808_CLK, 0);
  rxSetFreq(sweepMhz);
}
static void rfTick(uint32_t now) {
  if (mode != M_BOTH && mode != M_RF) return;
  if (now - lastSweep < 40) return;             // the PLL needs ~30 ms to settle
  lastSweep = now;
  int raw = analogRead(RX5808_RSSI);            // 0..4095
  // Rough: the RX5808 RSSI line sits ~0.5 V quiet and ~1.1 V on a strong carrier.
  int dbm = map(constrain(raw, 600, 1400), 600, 1400, -95, -35);
  if (dbm > -75) {
    char line[QL];
    snprintf(line, sizeof line,
             "{\"t\":\"rf\",\"id\":\"5g8/%u\",\"n\":\"5.8 GHz video carrier\",\"f\":%u,\"r\":%d,"
             "\"cat\":\"DRONE\",\"conf\":\"POSSIBLE\",\"src\":\"rx5808\",\"x\":{\"raw\":%d}}\n",
             sweepMhz, sweepMhz, dbm, raw);
    enqueue(line);
  }
  sweepMhz += 5; if (sweepMhz > 5945) sweepMhz = 5645;
  rxSetFreq(sweepMhz);
}
#else
static void rfInit() {}
static void rfTick(uint32_t) {}
#endif

// ------------------------------------------------------------------ sub-GHz via rtl_433_ESP (optional)
//
// A CC1101 or SX1276/SX1278 module gives the ESP32 a good part of what rtl_433 decodes on a
// Pi — TPMS, key fobs, weather and door sensors — on 433.92 or 315 MHz (one at a time; the
// receiver is tuned, not swept). rtl_433_ESP hands back the same JSON rtl_433 would; this
// turns it into a Wavelens rf line. Pins and the radio type come from build flags, see
// platformio.ini.

#ifdef HAS_SUBGHZ
#include <rtl_433_ESP.h>
#ifndef SUBGHZ_MHZ
#define SUBGHZ_MHZ 433.92
#endif
static rtl_433_ESP subghz;
static char subghzBuf[1024];

static void subghzCb(char* json) {
  // Pull model / id / rssi out of rtl_433's JSON with string ops; no parser needed for three fields.
  char model[40] = "rtl_433"; char id[32] = ""; int rssi = -70;
  const char* m = strstr(json, "\"model\":\""); if (m) { m += 9; size_t n = strcspn(m, "\""); if (n >= sizeof model) n = sizeof model - 1; memcpy(model, m, n); model[n] = 0; }
  const char* i = strstr(json, "\"id\":");       if (i) { i += 5; while (*i == ' ' || *i == '"') i++; size_t n = strcspn(i, ",}\""); if (n >= sizeof id) n = sizeof id - 1; memcpy(id, i, n); id[n] = 0; }
  const char* r = strstr(json, "\"rssi\":");     if (r) rssi = atoi(r + 7);
  bool tpms = strcasestr(model, "tpms") != nullptr || strstr(json, "pressure") != nullptr;
  char mj[48]; jesc((const uint8_t*)model, strlen(model), mj, sizeof mj);
  char line[QL];
  snprintf(line, sizeof line,
           "{\"t\":\"rf\",\"id\":\"%s/%s\",\"n\":\"%s\",\"f\":%d,\"r\":%d,\"cat\":\"%s\",\"conf\":\"LIKELY\",\"src\":\"rtl433esp\"}\n",
           mj, id, mj, (int)SUBGHZ_MHZ, rssi, tpms ? "VEHICLE" : "IOT");
  enqueue(line);
}

static void subghzInit() {
  subghz.initReceiver(RF_MODULE_RECEIVER_GPIO, SUBGHZ_MHZ);
  subghz.setCallback(subghzCb, subghzBuf, sizeof subghzBuf);
  subghz.enableReceiver();
}
static void subghzTick() { if (mode == M_BOTH || mode == M_RF) subghz.loop(); }
#else
static void subghzInit() {}
static void subghzTick() {}
#endif

// ------------------------------------------------------------------ spectrum from the sub-GHz chip (optional)
//
// The same SX1276 / CC1101 that decodes sensors can read RSSI at any frequency it tunes. While the
// phone's Spectrum or Scanner screen asks ({"c":"spec","on":true,"lo":440,"hi":470}), decoding pauses
// and the chip steps across the window — up to 200 points, at least 25 kHz apart — and sends the same
// {"t":"spec"} frame the Pi bridge does (one byte per bin, 0.5 dB from -120). Carriers 15 dB over the
// sweep's median are reported as rf hits with exact_mhz, so Follow (with an SA818 fitted) can tune
// them. Reach: SX1276 433-variant 410–525 MHz; SX1276 868/915-variant 862–1020; CC1101 300–348 /
// 387–464 / 779–928. Set SPEC_LO / SPEC_HI for your module. Decoding resumes when the phone stops asking.

#ifdef HAS_SPECTRUM
#include <RadioLib.h>
#ifndef SPEC_BINS_MAX
#define SPEC_BINS_MAX 200
#endif
#ifdef RF_CC1101
static CC1101 specRadio = new Module(RF_MODULE_CS, RF_MODULE_GDO0, RADIOLIB_NC);
#else
static SX1276 specRadio = new Module(RF_MODULE_CS, RF_MODULE_DIO0, RF_MODULE_RST, RF_MODULE_DIO1);
#endif
static bool     specOn = false, specRadioUp = false;
static uint32_t specUntil = 0;
static float    specLo = SPEC_LO, specHi = SPEC_HI;
static uint16_t specN = 0, specIdx = 0;
static int8_t   specDb[SPEC_BINS_MAX];
static float    specFloor[SPEC_BINS_MAX];          // slow floor per bin, for the hit report
static bool     specFloorInit = false;
static uint32_t specLastHit[SPEC_BINS_MAX / 8 + 1];

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static void specStart(float lo, float hi) {
  if (lo < SPEC_LO) lo = SPEC_LO;
  if (hi > SPEC_HI) hi = SPEC_HI;
  if (hi - lo < 0.1f) return;
  float span = hi - lo;
  uint16_t n = (uint16_t)(span / 0.025f);        // no finer than 25 kHz
  if (n > SPEC_BINS_MAX) n = SPEC_BINS_MAX;
  if (n < 8) n = 8;
  if (lo != specLo || hi != specHi || n != specN) specFloorInit = false;
  specLo = lo; specHi = hi; specN = n; specIdx = 0;
  if (!specRadioUp) {
#ifdef HAS_SUBGHZ
    subghz.disableReceiver();                   // the chip is ours while the sweep runs
#endif
#ifdef RF_CC1101
    int st = specRadio.begin();
#else
    int st = specRadio.beginFSK();               // FSK mode: plain RSSI, no LoRa framing
#endif
    specRadioUp = st == RADIOLIB_ERR_NONE;
    if (!specRadioUp) { status("spectrum: radio init failed"); return; }
    specRadio.setBitRate(1.2);                   // narrow: RSSI of a 25 kHz slice
#ifndef RF_CC1101
    specRadio.setRxBandwidth(25.0);
#else
    specRadio.setRxBandwidth(58.0);
#endif
  }
  specOn = true;
  specUntil = millis() + 90000UL;
}

static void specStop() {
  specOn = false;
  if (specRadioUp) {
    specRadio.standby();
    specRadioUp = false;
#ifdef HAS_SUBGHZ
    subghzInit();                                // hand the chip back to the sensor decoders
#endif
  }
}

static void specEmit() {
  // one frame: lo, hi, n, bin_hz, base64 of n bytes
  static char b64[(SPEC_BINS_MAX + 2) / 3 * 4 + 1];
  size_t j = 0;
  for (uint16_t i = 0; i < specN; i += 3) {
    uint8_t a = (uint8_t)((specDb[i] + 120) * 2);
    uint8_t b = i + 1 < specN ? (uint8_t)((specDb[i + 1] + 120) * 2) : 0;
    uint8_t c = i + 2 < specN ? (uint8_t)((specDb[i + 2] + 120) * 2) : 0;
    uint32_t v = ((uint32_t)a << 16) | ((uint32_t)b << 8) | c;
    b64[j++] = B64[(v >> 18) & 63]; b64[j++] = B64[(v >> 12) & 63];
    b64[j++] = i + 1 < specN ? B64[(v >> 6) & 63] : '=';
    b64[j++] = i + 2 < specN ? B64[v & 63] : '=';
  }
  b64[j] = 0;
  char line[QL + 300];
  int binHz = (int)((specHi - specLo) * 1e6f / specN);
  snprintf(line, sizeof line,
           "{\"t\":\"spec\",\"src\":\"esp32\",\"lo\":%.3f,\"hi\":%.3f,\"n\":%u,\"bin_hz\":%d,\"db0\":-120.0,\"step_db\":0.5,\"b\":\"%s\"}\n",
           specLo, specHi, specN, binHz, b64);
  // longer than QL: send straight, chunked
  if (linked && txChar) {
    size_t len = strlen(line), chunk = mtu > 3 ? mtu - 3 : 20;
    for (size_t i = 0; i < len; i += chunk) {
      size_t n = min(chunk, len - i);
      txChar->setValue((const uint8_t*)(line + i), n);
      txChar->notify();
      delay(2);
    }
  }
  // hits: 15 dB over the floor, local peak, not reported in the last 20 s
  if (specFloorInit) {
    float sorted[SPEC_BINS_MAX]; for (uint16_t i = 0; i < specN; i++) sorted[i] = specDb[i];
    std::sort(sorted, sorted + specN);
    float med = sorted[specN / 2];
    uint32_t now = millis();
    for (uint16_t i = 1; i + 1 < specN; i++) {
      float over = specDb[i] - max(specFloor[i], med);
      if (over < 15 || specDb[i] < specDb[i - 1] || specDb[i] <= specDb[i + 1]) continue;
      if (now - specLastHit[i] < 20000UL) continue;
      specLastHit[i] = now;
      float mhz = specLo + (specHi - specLo) * (i + 0.5f) / specN;
      candAdd(mhz);
      char hit[QL];
      snprintf(hit, sizeof hit,
               "{\"t\":\"rf\",\"id\":\"esp32/%d\",\"n\":\"Carrier at %.3f MHz\",\"f\":%d,\"r\":%d,\"cat\":\"UNKNOWN\",\"conf\":\"POSSIBLE\",\"src\":\"esp32\","
               "\"x\":{\"over_floor_db\":\"%.0f\",\"bin_hz\":\"%d\",\"exact_mhz\":\"%.4f \xc2\xb1 %.4f\"}}\n",
               (int)mhz, mhz, (int)mhz, (int)specDb[i], over, binHz, mhz, binHz / 2e6f);
      enqueue(hit);
    }
  }
}

static void specTick(uint32_t now) {
  if (!specOn) return;
  if ((int32_t)(specUntil - now) < 0) { specStop(); return; }
  if (!specRadioUp) return;
  // a few points per tick so Wi-Fi/BLE sniffing keeps its share of the loop
  for (int k = 0; k < 8 && specIdx < specN; k++, specIdx++) {
    float mhz = specLo + (specHi - specLo) * (specIdx + 0.5f) / specN;
    specRadio.setFrequency(mhz);
    specRadio.startReceive();
    delayMicroseconds(600);                      // let the AGC settle
#ifdef RF_CC1101
    float db = specRadio.getRSSI();
#else
    float db = specRadio.getRSSI(false);         // current, not last-packet
#endif
    if (db < -120) db = -120; if (db > 7) db = 7;
    specDb[specIdx] = (int8_t)db;
    if (!specFloorInit) specFloor[specIdx] = db;
    else specFloor[specIdx] += (db - specFloor[specIdx]) * (db > specFloor[specIdx] ? 0.02f : 0.2f);
  }
  if (specIdx >= specN) {
    specEmit();
    specFloorInit = true;
    specIdx = 0;
  }
}
#else
static void specStart(float, float) { status("no spectrum sweep on this companion (build with HAS_SPECTRUM)"); }
static void specStop() {}
static void specTick(uint32_t) {}
#endif

// ------------------------------------------------------------------ SA818 analog scanner (optional)
//
// An SA818-U (400–480 MHz) and/or SA818-V (134–174 MHz) FM receiver module on a UART makes the ESP32 a
// conventional analog scanner for the app's Scanner tab, with the same JSON commands and the same audio
// link as the Pi bridge: {"c":"listen","mhz":462.5625,"mode":"fm","sq":30} tunes and plays, "follow"
// runs the scan loop (step a channel list, stop where the squelch opens, hang 3 s, move on; hits from
// the spectrum sweep are tuned as they come), "lockout" is honoured, "off" stops. Audio: the module's
// AF out is sampled at 8 kHz on the ADC and sent as IMA ADPCM, 164-byte frames of 320 samples, only
// while the squelch is open. Trunked systems need the Pi: "trunk" / "trunkauto" get a status saying so.
// Receive only: PTT is never driven.

#if defined(HAS_SA818_U) || defined(HAS_SA818_V)
#include <driver/adc.h>
struct Sa818 { HardwareSerial* ser; int txPin, rxPin, sqPin, adcPin; float lo, hi; bool up; };
#ifdef HAS_SA818_U
static Sa818 saU = { &Serial1, SA818U_TX, SA818U_RX, SA818U_SQ, SA818U_AUDIO, 400.0f, 480.0f, false };
#endif
#ifdef HAS_SA818_V
static Sa818 saV = { &Serial2, SA818V_TX, SA818V_RX, SA818V_SQ, SA818V_AUDIO, 134.0f, 174.0f, false };
#endif
static Sa818* saFor(float mhz) {
#ifdef HAS_SA818_U
  if (saU.up && mhz >= saU.lo && mhz <= saU.hi) return &saU;
#endif
#ifdef HAS_SA818_V
  if (saV.up && mhz >= saV.lo && mhz <= saV.hi) return &saV;
#endif
  return nullptr;
}

static NimBLECharacteristic* auChar = nullptr;
static volatile bool auOn = false;

static bool saCmd(Sa818& m, const char* cmd, char* reply, size_t cap, uint32_t waitMs = 600) {
  while (m.ser->available()) m.ser->read();
  m.ser->print(cmd); m.ser->print("\r\n");
  uint32_t t0 = millis(); size_t n = 0; reply[0] = 0;
  while (millis() - t0 < waitMs) {
    while (m.ser->available()) {
      char c = m.ser->read();
      if (c == '\n') { reply[n] = 0; return n > 0; }
      if (c != '\r' && n + 1 < cap) reply[n++] = c;
    }
    delay(1);
  }
  reply[n] = 0;
  return n > 0;
}

static bool saInitOne(Sa818& m) {
  m.ser->begin(9600, SERIAL_8N1, m.rxPin, m.txPin);
  pinMode(m.sqPin, INPUT_PULLUP);
  char r[40];
  m.up = saCmd(m, "AT+DMOCONNECT", r, sizeof r) && strstr(r, "DMOCONNECT:0");
  if (m.up) { saCmd(m, "AT+DMOSETVOLUME=6", r, sizeof r); saCmd(m, "AT+SETFILTER=0,0,0", r, sizeof r); }
  Serial.printf("SA818 %.0f-%.0f MHz: %s\n", m.lo, m.hi, m.up ? "ok" : "not responding");
  return m.up;
}

// IMA ADPCM encoder state (the DVI tables; identical to the Pi bridge and the app's decoder)
static const int16_t IMA_STEP[89] = {7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,73,80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,371,408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,7132,7845,8630,9493,10442,11487,12635,13899,15289,16818,18500,20350,22385,24623,27086,29794,32767};
static const int8_t  IMA_INDEX[8] = {-1,-1,-1,-1,2,4,6,8};
static int16_t imaPred = 0; static int8_t imaIdx = 0; static uint8_t auSeq = 0;

static void imaFrame(const int16_t* pcm, int n, uint8_t* out) {
  out[0] = auSeq++; out[1] = (uint8_t)(imaPred & 0xFF); out[2] = (uint8_t)((imaPred >> 8) & 0xFF); out[3] = (uint8_t)imaIdx;
  int pred = imaPred, idx = imaIdx; int o = 4; bool high = true; uint8_t byte = 0;
  for (int i = 0; i < n; i++) {
    int step = IMA_STEP[idx], diff = pcm[i] - pred, code = 0;
    if (diff < 0) { code = 8; diff = -diff; }
    int d = step >> 3;
    if (diff >= step) { code |= 4; diff -= step; d += step; }
    step >>= 1; if (diff >= step) { code |= 2; diff -= step; d += step; }
    step >>= 1; if (diff >= step) { code |= 1; d += step; }
    pred += (code & 8) ? -d : d;
    if (pred < -32768) pred = -32768; if (pred > 32767) pred = 32767;
    idx += IMA_INDEX[code & 7]; if (idx < 0) idx = 0; if (idx > 88) idx = 88;
    if (high) byte = code << 4; else out[o++] = byte | code;
    high = !high;
  }
  imaPred = pred; imaIdx = idx;
}

// Listen / Follow state, mirroring the Pi bridge's status fields
static bool     lsOn = false, lsFollow = false;
static float    lsMhz = 0;
static int      lsSq = 4;                        // SA818 squelch 0..8; the app's 0..1000 is scaled onto it
static Sa818*   lsMod = nullptr;
static uint32_t lsTunedAt = 0, lsLastOpen = 0, lsChanAt = 0, lsFollowUntil = 0, lsUntil = 0;
static float    lockout[64]; static uint8_t lockoutN = 0;
// scan list: defaults (NOAA weather is not here: constant carrier; GMRS/FRS, MURS, marine 16/9, 2 m / 70 cm simplex)
static float    scanList[96] = {462.5625f,462.5875f,462.6125f,462.6375f,462.6625f,462.6875f,462.7125f,462.550f,462.575f,462.600f,462.625f,462.650f,462.675f,462.700f,462.725f,
                                 467.5625f,467.5875f,467.6125f,467.6375f,467.6625f,467.6875f,467.7125f,446.000f,446.500f,
                                 151.820f,151.880f,151.940f,154.570f,154.600f,156.800f,156.450f,146.520f,151.625f,154.570f};
static uint8_t  scanN = 34, scanIdx = 0;
static uint32_t scanLastStep = 0;
// candidates from the spectrum sweep / phone hits: (mhz, when)
static float    candMhz[16]; static uint32_t candAt[16]; static uint8_t candN = 0;
static float    recent[16]; static uint32_t recentAt[16]; static uint8_t recentN = 0;   // just played: cooldown

static bool lockedOut(float mhz) { for (uint8_t i = 0; i < lockoutN; i++) if (fabsf(lockout[i] - mhz) <= 0.02f) return true; return false; }

static void lsStatus(const char* msg) {
  char line[QL];
  snprintf(line, sizeof line,
           "{\"t\":\"status\",\"msg\":\"%s\",\"band\":\"%s\",\"mode\":\"%s\",\"listen\":%s",
           msg, band, mode == M_BOTH ? "both" : mode == M_WIFI ? "wifi" : mode == M_BLE ? "ble" : "rf",
           (lsOn || lsFollow) ? "{" : "\"off\"}\n");
  if (lsOn || lsFollow) {
    size_t u = strlen(line);
    snprintf(line + u, sizeof line - u, "\"mhz\":%.4f,\"mode\":\"%s\",\"asked\":\"fm\",\"detected\":\"%s\",\"call\":\"\",\"sq\":%d,\"follow\":%s,\"lockout\":[]}}\n",
             lsOn ? lsMhz : 0.0f, lsOn ? "fm" : "", lsOn ? "FM" : "", lsSq * 125, lsFollow ? "true" : "false");
  }
  enqueue(line);
}

static bool saTune(float mhz) {
  Sa818* m = saFor(mhz);
  if (!m) return false;
  char cmd[80], r[40];
  // AT+DMOSETGROUP=bw(0=12.5k),tx,rx,ctcss_tx,sq,ctcss_rx — receive frequency is what matters; tx is never keyed
  snprintf(cmd, sizeof cmd, "AT+DMOSETGROUP=0,%.4f,%.4f,0000,%d,0000", mhz, mhz, lsSq);
  if (!saCmd(*m, cmd, r, sizeof r) || !strstr(r, "DMOSETGROUP:0")) return false;
  lsMod = m; lsMhz = mhz; lsOn = true;
  lsTunedAt = lsChanAt = millis(); lsUntil = millis() + 300000UL;
  imaPred = 0; imaIdx = 0;
  return true;
}

static void lsStop(const char* why) {
  lsOn = false; lsMod = nullptr; lsMhz = 0;
  lsStatus(why);
}

static bool sqOpen() { return lsMod && digitalRead(lsMod->sqPin) == LOW; }

// Audio: 320 samples at 8 kHz per frame, read from the ADC on a 125 µs tick. Classic ESP32: analogRead is
// ~10 µs, fine from the main loop at this rate when the loop() delay is removed while listening.
static int16_t pcm[320]; static int pcmN = 0; static uint32_t nextSampleUs = 0;

static void audioTick() {
  if (!lsOn || !auOn || !lsMod) return;
  uint32_t nowUs = micros();
  while ((int32_t)(nowUs - nextSampleUs) >= 0 && pcmN < 320) {
    int raw = analogRead(lsMod->adcPin);          // 0..4095 around the 1.65 V midpoint
    pcm[pcmN++] = (int16_t)((raw - 2048) * 16);
    nextSampleUs += 125;
    nowUs = micros();
  }
  if (pcmN >= 320) {
    pcmN = 0;
    if (sqOpen()) {
      lsLastOpen = millis();
      uint8_t frame[164];
      imaFrame(pcm, 320, frame);
      if (auChar) { auChar->setValue(frame, sizeof frame); auChar->notify(); }
    }
  }
}

static void candAdd(float mhz) {
  if (lockedOut(mhz) || !saFor(mhz)) return;
  for (uint8_t i = 0; i < candN; i++) if (fabsf(candMhz[i] - mhz) < 0.02f) { candAt[i] = millis(); return; }
  if (candN < 16) { candMhz[candN] = mhz; candAt[candN++] = millis(); }
}

static bool recentlyPlayed(float mhz, uint32_t now) {
  for (uint8_t i = 0; i < recentN; i++) if (fabsf(recent[i] - mhz) < 0.02f && now - recentAt[i] < 60000UL) return true;
  return false;
}

static void followLeave(uint32_t now) {
  if (lsOn) {
    uint8_t slot = recentN < 16 ? recentN++ : 0;
    uint32_t oldest = 0; if (recentN >= 16) for (uint8_t i = 1; i < 16; i++) if (recentAt[i] < recentAt[oldest]) oldest = i;
    if (recentN >= 16) slot = oldest;
    recent[slot] = lsMhz; recentAt[slot] = now;
  }
  lsOn = false; lsMod = nullptr; lsMhz = 0;
}

static void followTick(uint32_t now) {
  if (!lsFollow) return;
  if ((int32_t)(lsFollowUntil - now) < 0) { lsFollow = false; lsStop("follow ended (no renewal from the app)"); return; }
  if (lsOn) {
    // stay while the squelch is open (+3 s hang); 30 s max; then the next thing
    bool busy = now - lsTunedAt < 4000UL || now - lsLastOpen < 3000UL;
    if (busy && now - lsChanAt < 30000UL) return;
    char m[64]; snprintf(m, sizeof m, "follow: %.4f MHz quiet; scanning", lsMhz);
    followLeave(now);
    lsStatus(m);
  }
  // 1. a fresh candidate from the spectrum sweep or the phone, newest first
  for (int i = candN - 1; i >= 0; i--) {
    if (now - candAt[i] > 60000UL) { candN--; for (int j = i; j < candN; j++) { candMhz[j] = candMhz[j + 1]; candAt[j] = candAt[j + 1]; } continue; }
    if (recentlyPlayed(candMhz[i], now)) continue;
    if (saTune(candMhz[i])) { char m[64]; snprintf(m, sizeof m, "follow: tuned %.4f MHz (carrier)", lsMhz); lsStatus(m); return; }
  }
  // 2. the scan list: the module's S+ check answers in ~100 ms; one channel per step
  if (scanN == 0 || now - scanLastStep < 120) return;
  scanLastStep = now;
  for (uint8_t tries = 0; tries < scanN; tries++) {
    scanIdx = (scanIdx + 1) % scanN;
    float f = scanList[scanIdx];
    Sa818* m = saFor(f);
    if (!m || lockedOut(f) || recentlyPlayed(f, now)) continue;
    char cmd[32], r[24];
    snprintf(cmd, sizeof cmd, "S+%.4f", f);
    if (saCmd(*m, cmd, r, sizeof r, 300) && strstr(r, "S=0")) {        // S=0: a signal is there
      if (saTune(f)) { char ms[64]; snprintf(ms, sizeof ms, "follow: %.4f MHz active", f); lsStatus(ms); }
    }
    break;                                       // one check per tick keeps BLE and Wi-Fi responsive
  }
}

static void listenInit() {
#ifdef HAS_SA818_U
  saInitOne(saU);
#endif
#ifdef HAS_SA818_V
  saInitOne(saV);
#endif
  analogReadResolution(12);
}

static void listenTick(uint32_t now) {
  if (lsOn && !lsFollow && (int32_t)(lsUntil - now) < 0) lsStop("timed out (5 min); tap Listen again to continue");
  followTick(now);
  audioTick();
}

// JSON helpers for the listen commands (the tiny parser style used below)
static bool jsonNum(const char* line, const char* key, float* out) {
  char k[24]; snprintf(k, sizeof k, "\"%s\":", key);
  const char* p = strstr(line, k); if (!p) return false;
  *out = atof(p + strlen(k)); return true;
}

static void listenCommand(const char* line, const char* v) {
  float f;
  if (v && !strncmp(v, "\"off\"", 5)) { lsFollow = false; lsStop("listening stopped"); return; }
  if (v && !strncmp(v, "\"follow\"", 8)) {
    if (jsonNum(line, "sq", &f)) lsSq = constrain((int)(f / 125.0f), 0, 8);
    bool fresh = !lsFollow;
    lsFollow = true; lsFollowUntil = millis() + 15UL * 60000UL;
    if (fresh) lsStatus("following: scanning the channel list and tuning carriers as they come (analog FM)");
    return;
  }
  if (v && !strncmp(v, "\"lockout\"", 9)) {
    lockoutN = 0;
    const char* b = strchr(line, '['); if (b) { const char* p = b + 1; while (*p && *p != ']' && lockoutN < 64) { while (*p == ' ' || *p == ',') p++; if (*p == ']') break; lockout[lockoutN++] = atof(p); while (*p && *p != ',' && *p != ']') p++; } }
    if (lsOn && lockedOut(lsMhz)) { followLeave(millis()); lsStatus("locked out; scanning"); } else lsStatus("lockout list noted");
    return;
  }
  if (v && (!strncmp(v, "\"trunk\"", 7) || !strncmp(v, "\"trunkauto\"", 11))) {
    lsStatus("trunked systems need the Raspberry Pi companion (dsd-fme); this one is an analog scanner");
    return;
  }
  if (v && !strncmp(v, "\"scanlist\"", 10)) {
    scanN = 0;
    const char* b = strchr(line, '['); if (b) { const char* p = b + 1; while (*p && *p != ']' && scanN < 96) { while (*p == ' ' || *p == ',') p++; if (*p == ']') break; scanList[scanN++] = atof(p); while (*p && *p != ',' && *p != ']') p++; } }
    char m[48]; snprintf(m, sizeof m, "scan list: %u channels", scanN); lsStatus(m);
    return;
  }
  if (!jsonNum(line, "mhz", &f)) return;
  if (jsonNum(line, "sq", &f) ) { float s = f; lsSq = constrain((int)(s / 125.0f), 0, 8); jsonNum(line, "mhz", &f); }
  if (!saFor(f)) {
    char m[96]; snprintf(m, sizeof m, "%.4f MHz is outside this companion's receiver (%s)", f,
#if defined(HAS_SA818_U) && defined(HAS_SA818_V)
             "134-174 and 400-480 MHz");
#elif defined(HAS_SA818_U)
             "400-480 MHz");
#else
             "134-174 MHz");
#endif
    lsStatus(m); return;
  }
  lsFollow = false;
  if (saTune(f)) { char m[64]; snprintf(m, sizeof m, "listening on %.4f MHz (FM)", f); lsStatus(m); }
  else lsStatus("the receiver module did not accept that frequency");
}
#else
static NimBLECharacteristic* auChar = nullptr;
static volatile bool auOn = false;
static void listenInit() {}
static void listenTick(uint32_t) {}
static void candAdd(float) {}
static void listenCommand(const char*, const char*) {
  status("no receiver to listen with on this companion (fit an SA818 module, or use the Raspberry Pi bridge)");
}
#endif

// ------------------------------------------------------------------ commands from the phone

static char cmdBuf[256]; static size_t cmdLen = 0;

static void applyCommand(const char* line) {
  // Tiny parser: enough for {"c":"band","v":"2g"} / {"c":"ch","v":[1,6,11]} / {"c":"mode","v":"ble"} / {"c":"ping"}
  const char* c = strstr(line, "\"c\":\"");
  if (!c) return;
  c += 5;
  if (!strncmp(c, "ping", 4)) { hello(); status("ok"); return; }
  if (!strncmp(c, "blink", 5)) {
    const char* bv = strstr(line, "\"v\":");
    int secs = bv ? atoi(bv + 4) : 5; if (secs < 1 || secs > 30) secs = 5;
    blinkUntil = millis() + secs * 1000UL;
    Serial.printf("identify: blinking for %d s (code %s)\n", secs, pairCode);
    status("blinking");
    return;
  }
  const char* v = strstr(line, "\"v\":");
  if (!strncmp(c, "listen", 6)) { listenCommand(line, v ? v + 4 : nullptr); return; }
  if (!strncmp(c, "spec", 4)) {
    if (strstr(line, "\"on\":false")) { specStop(); return; }
    float lo = 0, hi = 0;
    const char* pl = strstr(line, "\"lo\":"); const char* ph = strstr(line, "\"hi\":");
    if (pl) lo = atof(pl + 5); if (ph) hi = atof(ph + 5);
    specStart(lo, hi);
    return;
  }
  if (!strncmp(c, "pages", 5) || !strncmp(c, "log", 3) || !strncmp(c, "park", 4) || !strncmp(c, "pos", 3)) return;   // Pi-bridge features: ignored quietly
  if (!v) return;
  v += 4;
  if (!strncmp(c, "mode", 4)) {
    Mode m = M_BOTH;
    if (strstr(v, "wifi")) m = M_WIFI; else if (strstr(v, "ble")) m = M_BLE; else if (strstr(v, "\"rf\"")) m = M_RF;
    mode = m;
    status("mode set");
  } else if (!strncmp(c, "band", 4)) {
    if (strstr(v, "5g")) strcpy(band, "5g"); else if (strstr(v, "2g")) strcpy(band, "2g"); else strcpy(band, "all");
    // 5 GHz is only real on an ESP32-C5; everything else keeps sniffing 2.4 GHz and says so.
    status(strcmp(band, "5g") == 0 ? "5 GHz requested; this chip is 2.4 GHz only" : "band set");
  } else if (!strncmp(c, "ch", 2)) {
    if (strstr(v, "hop")) { hopping = true; applyChannels(); status("hopping"); return; }
    const char* b = strchr(v, '[');
    if (!b) return;
    chCount = 0; hopping = false;
    const char* p = b + 1;
    while (*p && *p != ']' && chCount < sizeof chList) {
      while (*p == ' ' || *p == ',') p++;
      int n = atoi(p); if (n >= 1 && n <= 177) chList[chCount++] = (uint8_t)n;
      while (*p && *p != ',' && *p != ']') p++;
    }
    if (chCount == 0) hopping = true;
    applyChannels();
    char m[40]; snprintf(m, sizeof m, "parked on %d channel%s", chCount, chCount == 1 ? "" : "s");
    status(m);
  }
}

class RxCB : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* ch, NimBLEConnInfo&) override {
    std::string v = ch->getValue();
    for (char b : v) {
      if (b == '\n') { cmdBuf[cmdLen] = 0; applyCommand(cmdBuf); cmdLen = 0; }
      else if (cmdLen < sizeof cmdBuf - 1) cmdBuf[cmdLen++] = b;
      else cmdLen = 0;
    }
  }
};
static RxCB rxCb;

class AuCB : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic*, NimBLEConnInfo&, uint16_t subValue) override { auOn = subValue != 0; }
};
static AuCB auCb;

class ServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer*, NimBLEConnInfo&) override { linked = true; hello(); }
  void onDisconnect(NimBLEServer*, NimBLEConnInfo&, int) override {
    linked = false; mtu = 23;
    NimBLEDevice::startAdvertising();
  }
  void onMTUChange(uint16_t m, NimBLEConnInfo&) override { mtu = m; }
};
static ServerCB serverCb;

// ------------------------------------------------------------------ setup / loop

static void ledTick(uint32_t now) {
  if (LED_PIN < 0) return;
  bool on = (int32_t)(blinkUntil - now) > 0 && ((now / 150) & 1);
  digitalWrite(LED_PIN, on ? HIGH : LOW);
}

void setup() {
  Serial.begin(115200);
  if (LED_PIN >= 0) { pinMode(LED_PIN, OUTPUT); for (int i = 0; i < 2; i++) { digitalWrite(LED_PIN, HIGH); delay(120); digitalWrite(LED_PIN, LOW); delay(120); } }
  // Name and pair code from the Bluetooth MAC, so two boards in one car are told apart.
  uint8_t mac[6]; esp_read_mac(mac, ESP_MAC_BT);
  snprintf(pairCode, sizeof pairCode, "%02X%02X", mac[4], mac[5]);
  snprintf(devName, sizeof devName, "Wavelens-%s", pairCode);
  NimBLEDevice::init(devName);
  NimBLEDevice::setMTU(247);
  server = NimBLEDevice::createServer();
  server->setCallbacks(&serverCb);
  NimBLEService* svc = server->createService(SVC_UUID);
  txChar = svc->createCharacteristic(TX_UUID, NIMBLE_PROPERTY::NOTIFY);
  rxChar = svc->createCharacteristic(RX_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  rxChar->setCallbacks(&rxCb);
  auChar = svc->createCharacteristic(AU_UUID, NIMBLE_PROPERTY::NOTIFY);
  auChar->setCallbacks(&auCb);
  svc->start();
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  // 128-bit UUID + flags fill most of the 31-byte advert; the name rides in the scan response.
  NimBLEAdvertisementData sr;
  sr.setName(devName);
  adv->setScanResponseData(sr);
  adv->start();

  wifiStart();
  bleScanStart();
  rfInit();
  subghzInit();
  listenInit();
  Serial.printf("%s up; advertising as %s  (pair code %s)\n", FW_NAME, devName, pairCode);
}

void loop() {
  uint32_t now = millis();
  wifiTick(now);
  rfTick(now);
  subghzTick();
  specTick(now);
  listenTick(now);
  ledTick(now);
  drain();
#if defined(HAS_SA818_U) || defined(HAS_SA818_V)
  if (!lsOn) delay(5);                          // while listening the ADC sampler needs the loop to spin
#else
  delay(5);
#endif
}
