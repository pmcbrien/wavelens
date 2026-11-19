# Mapped plate readers (alpr.csv.gz)

The list of license-plate-reader (ALPR) camera locations that ships inside Wavelens.

- **Source:** cameras tagged `surveillance:type=ALPR`, fetched at each app build.
- **Licence:** © OpenStreetMap contributors. Available under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). This file is an adapted database and is offered under the same licence.
- **Format:** gzip CSV — `lat,lon,brand,dir`. `brand` is a short maker name derived from the `manufacturer`/`brand`/`operator` tags (empty when unknown); `dir` is the mapped facing in degrees, or −1. No source identifiers are included.
- **Caveat:** these are volunteer reports, not an official inventory.

# UK speed cameras (speedcams.csv.gz)

The list of UK fixed and average-speed camera locations that ships inside Wavelens.

- **Source:** OpenStreetMap nodes tagged `highway=speed_camera`, and the `device` members of `type=enforcement` relations with `enforcement=maxspeed` or `enforcement=average_speed`, inside the United Kingdom, fetched at each app build.
- **Licence:** © OpenStreetMap contributors. Available under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). This file is an adapted database and is offered under the same licence.
- **Format:** gzip CSV — `lat,lon,kind,limit,dir`. `kind` is `F` (fixed) or `A` (average speed); `limit` is the enforced limit in mph (0 when not mapped); `dir` is the mapped facing in degrees, or −1. No source identifiers are included.
- **Caveat:** volunteer reports, not an official list; mobile enforcement is not included.
