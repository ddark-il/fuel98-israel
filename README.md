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
| **מיקה** Mika | Official website | Geocoded from address &sup1; |
| **דלק** Delek | Locations: official website · **98 availability: user reports** &sup2; | Official website |
| **תפוז** Tapuz | User reports | Verified via Google Maps |
| **אחר** Other (יעד Yaad & small brands) | User reports | Verified via Google Maps |

&sup1; Mika's website does not expose map coordinates, so they are geocoded from each station's address.
&sup2; Delek's station locator does not indicate which stations carry 98-octane, so that detail is sourced from user reports.

> Note: *Ten / 10 / טן* stations are intentionally excluded — they do not offer 98-octane.

## Fuel-quality flags ⚠️

Stations are cross-checked against the Israeli Ministry of Energy's [substandard-fuel list](https://migdal-webpages.energy-apps.org/fuelGasStation) (stations where off-spec fuel was found in the last 6 months). Any of our stations that appear on it are flagged with a ⚠️ in the app (hover/tap for the fuel type and sampling date).

A flag is raised for **any** fuel type — petrol, diesel, or LPG — because 98-octane engines are especially sensitive to bad fuel, so a quality failure anywhere at the station is worth surfacing.

To refresh, run `node scripts/check-violations.mjs` (it rewrites `data/violations.json`) and commit. **It must run from an Israeli IP** — the Ministry API is geo-restricted and rejects requests from outside Israel (so GitHub-hosted Actions can't reach it; this is a manual/local step). Matching is by geocoded coordinate, using Google Places if `MAPS_API_KEY` is set in the environment, otherwise Nominatim (clean street addresses only).

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
  others.json       # the "אחר" group
  violations.json   # stations to flag ⚠️ (auto-generated, see below)
index.html          # the whole app: Leaflet map + list, brand filters, geolocation, Waze links
scripts/
  check-violations.mjs           # cross-checks the Ministry substandard-fuel list (run manually, no deps)
```

No build step — it's a static site. `index.html` fetches `data/manifest.json`, loads each brand file plus `data/violations.json`, and renders the map (Leaflet + OpenStreetMap tiles) and list. Because it uses `fetch()`, it must be served over HTTP (a `file://` open will load no stations); GitHub Pages serves it correctly.

The **station scrapers** that generate the per-brand data live **outside** this repository (in the parent project). The fuel-quality check (`scripts/check-violations.mjs`) is run manually from an Israeli IP — it can't run in CI because the Ministry API is geo-restricted. This repo holds the published data + frontend, plus that one helper script.

## License

[WTFPL](LICENSE) — do what you want.
