# Mapped plate readers (alpr.csv.gz)

The list of license-plate-reader (ALPR) camera locations that ships inside Wavelens.

- **Source:** cameras tagged `surveillance:type=ALPR`, fetched at each app build.
- **Licence:** © OpenStreetMap contributors. Available under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). This file is an adapted database and is offered under the same licence.
- **Format:** gzip CSV — `lat,lon,brand,dir`. `brand` is a short maker name derived from the `manufacturer`/`brand`/`operator` tags (empty when unknown); `dir` is the mapped facing in degrees, or −1. No source identifiers are included.
- **Caveat:** these are volunteer reports, not an official inventory.
