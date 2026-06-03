# תחנות דלק 98 — Fuel98 Israel

A simple map + list of fuel stations in Israel that sell **98-octane** petrol (בנזין 98 / Super 98).

🔗 **Live:** https://delek98.com/

Search by name or city, filter by brand, sort by distance from your location, and open any station directly in Waze.

## Data sources

The 98-octane station list for each brand comes from the source below. Station **locations** (coordinates) sometimes come from a different source than the **98-octane availability** — the table separates them where they differ.

| Brand | Stations & 98-octane data | Coordinates |
|-------|---------------------------|-------------|
| **פז** Paz | Official website | Official website |
| **סונול** Sonol | Official website | Official website |
| **דור אלון** Dor Alon | Official website | Official website |
| **מיקה** Mika | Official website | Google Places + Mika's own map pins &sup1; |
| **דלק** Delek | Locations: official locator · **98 availability: user reports** &sup2; | Official locator (verified) |
| **תפוז** Tapuz | Official Tapuz representative | Geocoded from the rep's addresses &sup3; |
| **אחר** Other (יעד Yaad & small brands) | User reports | Official registry &sup4; |

&sup1; Mika's site exposes no map coordinates, so each one is the station's real street address geocoded via **Google Places** — or, for the stations whose Mika page embeds a Google Maps share-link, that pin's exact location (the operator's own coordinate).
&sup2; Delek's locator doesn't flag which stations carry 98-octane, so that detail comes from user reports; the coordinates are taken from — and were verified against — Delek's official station locator.
&sup3; The Tapuz list — station names **and addresses** — is the definitive list supplied by Tapuz's official representative. Coordinates are geocoded from those addresses via **Google Places** and cross-checked against the Ministry registry; a couple keep a client-confirmed pin or a street-address pin where Google has no tagged station yet (שדרות, רהט מרכז).
&sup4; User-reported; the station names and coordinates are taken from the **Ministry of Energy's public-stations registry** (data.gov.il open data), cross-checked on Google Places.

> **Coordinates were cross-checked against the Ministry of Energy's [open-data station registry](https://data.gov.il/dataset/gas-station)** (~1,255 public stations with coordinates).

> Note: *Ten / 10 / טן* stations are intentionally excluded — they do not offer 98-octane.

## Fuel-quality flags ⚠️

Stations are cross-checked against the Israeli Ministry of Energy's [substandard-fuel list](https://migdal-webpages.energy-apps.org/fuelGasStation) (stations where off-spec fuel was found in the last 6 months). Any of our stations that appear on it are flagged with a ⚠️ in the app (hover/tap for the fuel type and sampling date). A flag is raised for **any** fuel type — petrol, diesel, or LPG — because 98-octane engines are especially sensitive to bad fuel, so a quality failure anywhere at the station is worth surfacing.

To refresh, run `node scripts/check-violations.mjs` (it rewrites `data/violations.json`) and commit. **It must run from an Israeli IP** — the Ministry API is geo-restricted and rejects requests from outside Israel (so GitHub-hosted Actions can't reach it; this is a manual/local step). Each Ministry entry is matched to our stations by coordinate, geocoded via Google Places (`MAPS_API_KEY`).

## Project structure

```
data/
  manifest.json     # lists the per-brand files + brand→file map
  paz.json          # one file per brand: [{ brand, name, coordinates:{lat,lon} }, …]
  sonol.json
  doralon.json
  mika.json
  delek.json
  tapuz.json
  others.json       # the "אחר" group (יעד & small/independent brands)
  violations.json   # stations to flag ⚠️ (auto-generated, see below)
index.html          # the whole app + a generated <noscript> SEO block (between the seo-noscript markers)
scripts/
  check-violations.mjs           # cross-checks the Ministry substandard-fuel list (run manually, no deps)
  build-seo.py                   # regenerates index.html's <noscript> SEO block (reverse-geocodes cities, in memory)
```

No build step — it's a static site. `index.html` fetches `data/manifest.json`, loads each brand file plus `data/violations.json`, and renders the map (Leaflet + OpenStreetMap tiles) and list. 

For crawlability, `index.html` carries a hidden `<noscript>` block listing every station grouped by city. `scripts/build-seo.py` reverse-geocodes them in memory and rewrites the block on demand.

The **station scrapers and coordinate-verification scripts** (Mika → Google Places + map pins, Delek → official locator, Tapuz/Other → the data.gov.il registry) live **outside** this repository, in the parent project. The two in-repo helpers are run manually: `check-violations.mjs` from an Israeli IP (the Ministry API is geo-restricted, so GitHub Actions can't reach it), and `build-seo.py` is slow (it reverse-geocodes a city label for every station at ~1 req/s). This repo holds the published data + frontend, plus those helpers.

## License

[WTFPL](LICENSE) — do what you want.
