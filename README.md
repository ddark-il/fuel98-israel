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
| **מיקה** Mika | Official website | Mika's own map pins &sup1; |
| **דלק** Delek | Official Delek representative &sup2; | Official locator |
| **תפוז** Tapuz | Official Tapuz representative | Operator pins + registry fallback &sup3; |
| **אחר** Other (יעד Yaad & small brands) | User reports | Official registry &sup4; |

&sup1; Mika's pages embed the operator's own Google Maps links ("לחץ לצפיה במפה"); the place marker in that link is the operator's coordinate and is used directly. Stations whose page has no such link fall back to the nearest **Ministry registry** pin.
&sup2; The Delek list — station names **and station numbers** — is the definitive list supplied by Delek's official representative (98-octane availability per that list, superseding the earlier user reports). Coordinates come from Delek's official station locator (delek.co.il), matched by station name; the one station not in the public locator (הסדנא, ירושלים) takes the registry pin.
&sup3; The Tapuz list — station names **and addresses** — is the definitive list supplied by Tapuz's official representative. Coordinates are the operator's own map pins where published, otherwise the nearest **Ministry registry** pin (matched by proximity, with the brand+address as confirmation).
&sup4; User-reported; the station names and coordinates are taken from the **Ministry of Energy's public-stations registry** (data.gov.il open data).

> **Coordinates are sourced from operator data, with the Ministry of Energy's [open-data station registry](https://data.gov.il/dataset/gas-station) (~1,255 public stations) as the fallback and cross-check.** Nothing is geocoded by a third-party service any more — no Google, no Apple, no Nominatim — so there is no API key, no rate limit and no licence restriction on the result. Each station records where its pin came from in `pin_source` (`brand-nav` · `manual` · `registry` · `unverified-current`).

> Note: *Ten / 10 / טן* stations are intentionally excluded — they do not offer 98-octane.

## Fuel-quality flags ⚠️

Stations are cross-checked against the Israeli Ministry of Energy's [substandard-fuel list](https://migdal-webpages.energy-apps.org/fuelGasStation) (stations where off-spec fuel was found in the last 6 months). Any of our stations that appear on it are flagged with a ⚠️ in the app (hover/tap for the fuel type and sampling date). A flag is raised for **any** fuel type — petrol, diesel, or LPG — because 98-octane engines are especially sensitive to bad fuel, so a quality failure anywhere at the station is worth surfacing.

To refresh, run `node scripts/check-violations.mjs` (it rewrites `data/violations.json`) and commit. **It must run from an Israeli IP** — the Ministry API is geo-restricted and rejects requests from outside Israel (so GitHub-hosted Actions can't reach it; this is a manual/local step). Matching needs no geocoding: each Ministry entry carries the station's licence number, which is joined to the same Ministry's station registry for a WGS84 coordinate, and that coordinate is matched to our stations by proximity. Entries that fail to match are printed with the reason, so an empty `violations.json` can never be the result of a lookup that quietly failed.

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
  build-seo.py                   # regenerates index.html's <noscript> SEO block (cities from the Ministry registry)
```

No build step — it's a static site. `index.html` fetches `data/manifest.json`, loads each brand file plus `data/violations.json`, and renders the map (Leaflet + OpenStreetMap tiles) and list. 

For crawlability, `index.html` carries a hidden `<noscript>` block listing every station grouped by city. `scripts/build-seo.py` takes each station's city from the Ministry registry's רשות_מקומית and rewrites the block on demand — offline, deterministic, and finished in well under a second.

The **station scrapers and coordinate-verification scripts** (Mika → its own map pins, Delek → official locator, Tapuz/Other → the data.gov.il registry, plus the pin-source planner) live **outside** this repository, in the parent project. The two in-repo helpers are run manually: `check-violations.mjs` from an Israeli IP (the Ministry API is geo-restricted, so GitHub Actions can't reach it), and `build-seo.py` whenever the station data changes. Neither needs an API key or network access beyond the two government endpoints.

## License

[WTFPL](LICENSE) — do what you want.
