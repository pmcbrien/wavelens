# Mapped plate readers (alpr.csv.gz)

The list of license-plate-reader (ALPR) camera locations that ships inside Wavelens.

- **Source:** every OpenStreetMap node tagged `surveillance:type=ALPR`, fetched from the Overpass API at each app build.
- **Licence:** © OpenStreetMap contributors. Available under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). This file is an adapted database and is offered under the same licence.
- **Format:** gzip CSV — `lat,lon,brand,dir,osm_id`. `brand` is a short maker name derived from the `manufacturer`/`brand`/`operator` tags (empty when unknown); `dir` is the mapped facing in degrees, or −1.
- **Caveat:** these are volunteer reports, not an official inventory. Missing a camera you've seen? Add it on [openstreetmap.org](https://www.openstreetmap.org) and it reaches Wavelens in the next build. Community projects such as DeFlock map these cameras.
