# Wavelens packs

Community-made packs for [Wavelens](https://wavelensapp.us). A pack is one JSON file with the settings for where you drive: units (km/h or mph), extra device rules, rules to switch off locally, and the band names a companion radio (Raspberry Pi bridge) uses.

The app ships one built-in pack (US settings) and uses it by default. Anyone can make a pack for their country and share the file; people choose which one they use.

## Using a pack

- **Phone:** put the `.json` file on the phone, then in Wavelens open **More → Signature packs → Import pack** and pick it. The app shows what the pack contains before anything changes. Switch back to *Built-in* on the same screen at any time. Wavelens never downloads anything itself.
- **Companion Pi:** copy the same file to `/etc/wavelens/packs/` and restart the bridge (`sudo systemctl restart wavelens-bridge`). The phone tells the bridge which pack is in use when it connects.

**Follow the laws where you drive.** Detecting some devices is an offence in some places (for example, radar and speed-camera detectors are banned in many countries and states). A pack must switch off rules for anything that is unlawful to detect where it applies, and say so in its `notes`. Packs that try to detect speed-measuring devices where that is prohibited are not accepted here.

## Making a pack

1. Copy `TEMPLATE.json` to `<cc>.json`, where `<cc>` is the two-letter ISO country code in lower case (`nz.json`).
2. Fill it in (format below). Start with `"status": "testing"`.
3. Check it: `python3 packs/validate.py nz.json --signatures path/to/signatures.json` (the `--signatures` part is optional; it checks that `disable_rules` names real rules). The app runs the same checks on import.
4. Open a pull request with the file and, in the description, where each band and identifier comes from.

### Format 1

```json
{
  "kind": "wavelens-country",
  "format": 1,
  "country": "NZ",
  "name": "New Zealand",
  "pack_version": 1,
  "pack_date": "2026-11-02",
  "status": "testing",
  "maintainers": ["your name or handle"],
  "summary": "One or two sentences shown before someone installs it.",
  "units": "metric",
  "scanner_idle": null,
  "rules": [],
  "disable_rules": [],
  "notes": ["Shown under the pack on the phone: what is different, what you checked about local law."],
  "companion": {
    "drop_us_bands": true,
    "rtl433_args": "-f 433.92M -f 868M -H 30 -g 40",
    "bands": [
      {"lo": 476.4, "hi": 477.42, "name": "UHF CB radio nearby", "cat": "VEHICLE", "kind": ""}
    ]
  }
}
```

| Field | Meaning |
|---|---|
| `units` | `metric` (km/h, road mode from 25 km/h) or `imperial` |
| `scanner_idle` | `null`, or `[lo, hi]` MHz shown on the Scanner tab when nothing is tuned |
| `rules` | Device rules in the same format as the app's signature pack. Add `"regions": ["NZ"]`. Every rule needs a `source`. OUI rules need `"publicAddr": true` and only 6-hex IEEE MA-L assignments; check the registrant really is that company |
| `disable_rules` | Ids of built-in rules to switch off in this pack (unlawful or meaningless there) |
| `companion.drop_us_bands` | Forget the band names that come from the US allocation table, so an unnamed frequency shows as a plain carrier, not a wrong US label |
| `companion.bands` | `lo`/`hi` in MHz, `name` shown on the phone, `cat` a device category, `kind` `""` (report), `persist`, `bg` (background, once a minute) or `video`; optional `steady` `{name, cat}` for a carrier that stays on. First match wins, before the built-in table |
| `companion.rtl433_args` | rtl_433 frequency flags for tyre sensors and remotes in that country |

Categories: ALPR, ACOUSTIC, DRONE, TOOL, LE_GEAR, TRACKER, TRAFFIC_SENSOR, CAMERA, FLEET, VEHICLE, PHONE, WEARABLE, IOT, BEACON, NETWORK, CELL_SITE, UNKNOWN.

### Good sources

- Bands: the country's national spectrum plan (the regulator's frequency allocation table), and mobile operators' published band plans.
- Identifiers: the IEEE OUI registry and Bluetooth SIG assigned numbers. Name or SSID patterns from field observation are fine with `"verified": false` and a note saying where they were seen.
- Everything is a hint. A vendor prefix means *this maker's hardware is nearby*, never a confirmed device.
