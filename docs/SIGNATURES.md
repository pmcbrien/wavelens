# Signature packs

Wavelens classifies every emitter it hears against a JSON signature pack. The pack shipped
in the APK is `app/src/main/assets/signatures.json`. Nothing is ever downloaded: an updated
pack is a file you move to the phone yourself and import from **More → Signature packs**,
which shows exactly what would change before anything is applied.

This document describes the file as `Classifier.kt` actually reads it. When the two
disagree, the code wins and this file has a bug.

## Where packs live on the phone

| File | What it is |
|---|---|
| `assets/signatures.json` | Bundled pack. Always present, read-only. |
| `filesDir/signatures.json` | Imported pack. If present and parseable it replaces the bundled pack entirely (no merging). Deleted by **Revert to bundled**, or automatically if it fails to parse at startup. |
| `filesDir/user_rules.json` | Your overlay: single rules you imported. Loaded after the pack; a user rule with the same `id` as a pack rule replaces it. Survives pack imports and reverts. |

## Pack layout

```json
{
  "pack_name": "Wavelens bundled signatures",
  "pack_version": 2,
  "pack_date": "2026-10-20",
  "version": 9,
  "updated": "2026-10-18",
  "_readme": ["free text, ignored by the app"],
  "rules": [ { ...rule... }, { ...rule... } ]
}
```

| Key | Type | Required | Meaning |
|---|---|---|---|
| `pack_version` | integer | no | Version used by the importer. Importing a pack with a lower `pack_version` than the active one is a downgrade and needs an explicit tick in the dialog. Start new packs at 2. |
| `pack_date` | string `YYYY-MM-DD` | no | Shown next to the version. |
| `pack_name` | string | no | Shown as the pack title. |
| `version`, `updated` | integer, string | no | Older spelling. Still used for the status-line string on the Setup screen and in recordings. If `pack_version` / `pack_date` are absent these are read as the fallback. |
| `rules` | array of rule objects | **yes** | The pack is rejected if this is missing or yields zero rules. |

Any other top-level key is ignored. The bundled pack keeps both spellings so that older
builds still show a version.

## Rule object

```json
{
  "id": "flock-oui",
  "category": "ALPR",
  "confidence": "CONFIRMED",
  "weight": 100,
  "verified": true,
  "note": "Flock Safety's own IEEE OUI assignment (B4:1E:52)",
  "source": "https://standards-oui.ieee.org/",
  "checked": "ieee-2026-10-18",
  "match": {
    "ouis": ["B4:1E:52"],
    "publicAddr": true
  }
}
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | `rule<index>` | Unique within the pack. Shown on the device card and used for diffs and overrides, so keep it stable once published. The single-rule importer requires 2–64 characters from `A-Z a-z 0-9 . _ -`. |
| `category` | string | `UNKNOWN` | One of the names below, case-insensitive. Unknown names fall through to `UNKNOWN`. |
| `confidence` | string | `POSSIBLE` | `POSSIBLE`, `LIKELY` or `CONFIRMED`, case-insensitive. Anything else is `POSSIBLE`. |
| `weight` | integer | `10` | Rules are evaluated in descending weight; the heaviest matching rule decides the category. Use 100 for registry-grade identifiers, lower for heuristics. |
| `verified` | boolean | `false` | `true` only when the identifier comes from a registry (IEEE MA-L OUI, Bluetooth SIG company ID, assigned GATT service). `false` rules are rendered as "(heuristic)" on the card. |
| `note` | string | none | One line shown on the device card as the reason. Say what the identifier is and how it can be wrong. |
| `source` | string | none | Informational: URL where the identifier was taken from. Not read by the app. |
| `checked` | string | none | Informational: which registry snapshot the identifier was checked against, e.g. `ieee-2026-10-18`. Not read by the app. |
| `match` | object | `{}` | The predicates, below. A rule with an empty `match` matches everything the `sources` filter lets through, so always supply at least one predicate. |

### Categories

Exactly the `Category` enum in `Models.kt`, listed with the label the app shows and the
threat rank that drives sorting and alerting (higher sorts first):

| Name | Label | Rank |
|---|---|---|
| `ALPR` | ALPR / Fixed camera | 100 |
| `ACOUSTIC` | Acoustic sensor | 95 |
| `DRONE` | Drone / UAS | 93 |
| `LE_GEAR` | Body cam / LE gear | 90 |
| `TOOL` | Pentest / RF tool | 87 |
| `TRACKER` | Item tracker | 85 |
| `TRAFFIC_SENSOR` | Traffic / MAC sensor | 80 |
| `CAMERA` | Camera / doorbell | 70 |
| `VEHICLE` | Vehicle / TPMS | 50 |
| `PHONE` | Phone / tablet | 40 |
| `WEARABLE` | Wearable / audio | 35 |
| `IOT` | IoT sensor | 30 |
| `BEACON` | Beacon / advertiser | 25 |
| `NETWORK` | AP / network gear | 20 |
| `CELL_SITE` | Cell site | 15 |
| `UNKNOWN` | Unclassified | 5 |

## Match predicates

Every predicate present in `match` must pass (AND). A predicate that is absent, or an
empty array, is skipped. Evaluation order and exact semantics, from `Rule.matches`:

| Predicate | Type | Passes when |
|---|---|---|
| `sources` | array of `"BLE"`, `"BR_EDR"`, `"WIFI"`, `"CELL"` | The observation's radio is in the list. Unknown names are dropped silently; if that leaves the list empty the predicate is skipped. |
| `publicAddr` | boolean | The observation's address type equals the value. `true` = public (vendor-assigned) MAC, `false` = randomized. Always pair `ouis` with `"publicAddr": true`; a randomized address never carries a vendor prefix. |
| `nameRegex` | string, Java regex | The advertised name is present and the regex **finds a match anywhere in it** (`containsMatchIn`, not a full match). Anchor with `^`/`$` yourself; use `(?i)` for case-insensitive. A regex that does not compile is dropped, which makes the predicate skip. |
| `ssidRegex` | string, Java regex | Same as `nameRegex` but against the Wi-Fi SSID (`extra["ssid"]`), falling back to the name when no SSID is recorded. |
| `ouis` | array of strings | The first 6 hex digits of the observation's MAC (colons and dashes stripped, upper-cased) are in the list. Entries may be written `B4:1E:52` or `B41E52`. Only use 24-bit MA-L assignments; MA-M/MA-S blocks share a prefix across companies. |
| `services` | array of strings | At least one advertised service UUID (lower-cased) matches at least one entry, where an entry: ending in `*` is a prefix match on the UUID; otherwise equals the UUID exactly, **or**, if the entry is longer than 4 characters, is a substring of it. So `"fd44"` matches only the 16-bit `fd44`; `"0000fd44-0000"` matches any 128-bit UUID containing it. Entries are lower-cased on load. |
| `manufacturers` | array of integers | At least one of the observation's manufacturer-data company IDs (decimal Bluetooth SIG assigned number) is in the list. Negative or non-numeric entries are dropped. |
| `mfgPrefix` | array of `{ "id": int, "hex": string }` | For at least one entry, the manufacturer-data payload for company `id` starts with `hex` (case-insensitive). The payload is the bytes after the 2-byte company ID. |
| `sdPrefix` | array of `{ "uuid": string, "hex": string }` | For at least one entry, the service-data payload recorded for `uuid` (lower-cased; looked up as `extra["sd_<uuid>"]`) starts with `hex` (case-insensitive). |

What a predicate *cannot* do: no OR between different predicates, no negation, no RSSI
or interval conditions. Write two rules instead of an OR.

## How a verdict is formed

From `Classifier.classify`:

1. All rules are tested; rules are held sorted by `weight`, descending. User-overlay rules
   replace same-id pack rules before sorting.
2. No hits: Wi-Fi observations become `NETWORK`, everything else `UNKNOWN`, both `POSSIBLE`.
3. Otherwise the first (heaviest) hit sets the category and starting confidence.
4. If two or more hits share that category, confidence is raised one step
   (`POSSIBLE → LIKELY`, `LIKELY → CONFIRMED`). A single rule can never be raised; a
   `CONFIRMED` rule is never raised further.
5. Every hit's `id` is recorded on the track, so the card can show each rule's `note`
   and whether it is verified.

Corollary: a weight-100 heuristic beats a weight-50 registry match. Give the registry
identifiers the higher weight.

## Single-rule files

**Share a rule** renders one rule as the object above. **Import a rule…** accepts either a
bare rule object or `{"rules": [ one rule ]}` and validates it: id format, known category,
confidence/weight/verified types, a `match` with at least one known predicate and no
unknown ones, regexes that compile, OUIs that are 6 hex digits, decimal `manufacturers`,
`sources` from the list above, and well-formed `mfgPrefix` / `sdPrefix` entries. The
preview shows whether the id overrides a pack rule. Accepted rules are written to
`user_rules.json`:

```json
{
  "_readme": "User rule overlay for Wavelens. Rules here override pack rules with the same id.",
  "rules": [ { ...rule... } ]
}
```

Pack imports are more lenient than single-rule imports: they only require valid JSON, a
`rules` array, unique non-empty ids and at least one rule, because a pack that reached
you has normally been reviewed already.

## Examples

A name heuristic limited to one radio:

```json
{
  "id": "example-name",
  "category": "CAMERA",
  "confidence": "LIKELY",
  "weight": 60,
  "verified": false,
  "note": "Example doorbell advertising its model name; collides with any device named the same",
  "match": { "nameRegex": "(?i)^examplecam-", "sources": ["BLE"] }
}
```

A registry identifier (company ID plus a payload prefix):

```json
{
  "id": "example-mfg-prefix",
  "category": "TRACKER",
  "confidence": "CONFIRMED",
  "weight": 100,
  "verified": true,
  "note": "Example vendor company ID with the tag's advertisement type byte",
  "source": "https://www.bluetooth.com/specifications/assigned-numbers/",
  "checked": "sig-2026-10-18",
  "match": { "manufacturers": [65535], "mfgPrefix": [{ "id": 65535, "hex": "01" }] }
}
```

A Wi-Fi SSID with a vendor OUI, both required:

```json
{
  "id": "example-ssid-oui",
  "category": "ALPR",
  "confidence": "CONFIRMED",
  "weight": 100,
  "verified": true,
  "note": "Example camera in setup mode: vendor OUI and its provisioning SSID together",
  "match": { "sources": ["WIFI"], "ssidRegex": "^EXAMPLE-[0-9A-F]{4}$", "ouis": ["00:00:5E"], "publicAddr": true }
}
```

## How to contribute signatures

Signatures are the part of Wavelens that most benefits from other people's captures.

1. Open a pull request against the repository that edits
   `app/src/main/assets/signatures.json`. **One rule per PR** so each identifier can be
   reviewed and reverted on its own.
2. Cite the source in the rule's `source` field: the registry page for an OUI or company
   ID, or the write-up / capture that documents a name, SSID or payload prefix. Rules
   without a source are not merged.
3. Set `verified` honestly. Registry identifiers are `true`; anything inferred from a
   name, SSID or third-party research is `false`.
4. Bump `pack_version` by one and set `pack_date` to the day of the change.
5. Include, in the PR description, the CSV export (Setup screen) or a Sessions recording
   excerpt that shows the device matching — with coordinates and anything personal
   removed. Replay (Setup → Sessions) is the quickest way to test a rule against a
   previous drive before opening the PR.

Rules you only want on your own phone do not need a PR: import them from **More →
Signature packs → Import a rule…** and they persist across pack updates.
