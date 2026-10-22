/*
 * Wavelens companion — ESP32 firmware
 *
 * A second radio for the Wavelens Android app. The ESP32 sniffs Wi-Fi management frames
 * (beacons, probe responses) and BLE advertisements, and pushes every hit to the phone as
 * one JSON line over a BLE GATT notify characteristic. The phone can tell it which Wi-Fi
 * channels to park on. Optionally an RX5808 5.8 GHz receiver sweeps for analog video
 * carriers (FPV / older camera links). Nothing here ever touches the internet.
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
#include "esp_wifi.h"

// ------------------------------------------------------------------ config

#define FW_NAME     "wavelens-esp32 0.1"
#define DEV_NAME    "Wavelens ESP32"
#define SVC_UUID    "57617665-6c65-6e73-0100-000000000001"
#define TX_UUID     "57617665-6c65-6e73-0100-000000000002"
#define RX_UUID     "57617665-6c65-6e73-0100-000000000003"

// #define HAS_RX5808           // uncomment if an RX5808 5.8 GHz module is wired (see below)
// #define HAS_SUBGHZ           // uncomment for a CC1101 / SX1276 sub-GHz receiver via the rtl_433_ESP library (see below)
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
           "{\"t\":\"hello\",\"fw\":\"%s\",\"name\":\"%s\",\"caps\":[\"wifi\",\"ble\"%s%s]}\n",
           FW_NAME, DEV_NAME,
#ifdef HAS_RX5808
           ",\"rf\"",
#else
           "",
#endif
#ifdef HAS_SUBGHZ
           ",\"subghz\""
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

// ------------------------------------------------------------------ commands from the phone

static char cmdBuf[256]; static size_t cmdLen = 0;

static void applyCommand(const char* line) {
  // Tiny parser: enough for {"c":"band","v":"2g"} / {"c":"ch","v":[1,6,11]} / {"c":"mode","v":"ble"} / {"c":"ping"}
  const char* c = strstr(line, "\"c\":\"");
  if (!c) return;
  c += 5;
  if (!strncmp(c, "ping", 4)) { hello(); status("ok"); return; }
  const char* v = strstr(line, "\"v\":");
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

void setup() {
  Serial.begin(115200);
  NimBLEDevice::init(DEV_NAME);
  NimBLEDevice::setMTU(247);
  server = NimBLEDevice::createServer();
  server->setCallbacks(&serverCb);
  NimBLEService* svc = server->createService(SVC_UUID);
  txChar = svc->createCharacteristic(TX_UUID, NIMBLE_PROPERTY::NOTIFY);
  rxChar = svc->createCharacteristic(RX_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  rxChar->setCallbacks(&rxCb);
  svc->start();
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  adv->setName(DEV_NAME);
  adv->start();

  wifiStart();
  bleScanStart();
  rfInit();
  subghzInit();
  Serial.printf("%s up; advertising as %s\n", FW_NAME, DEV_NAME);
}

void loop() {
  uint32_t now = millis();
  wifiTick(now);
  rfTick(now);
  subghzTick();
  drain();
  delay(5);
}
