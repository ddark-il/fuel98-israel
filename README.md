# תחנות דלק 98 — Fuel98 Israel

A simple map + list of fuel stations in Israel that sell **98-octane** petrol (בנזין 98 / Super 98).

🔗 **Live:** https://delek98.com/

Search by name or city, filter by brand, sort by distance from your location, and open any station directly in Waze.

## Prices & the station database

The site loads one file: **`ci_update/stations.json`** — the station database, rebuilt hourly by
[`.github/workflows/prices.yml`](.github/workflows/prices.yml). Each row carries the station's name,
brand, coordinates, venue ids, `got98`, `price` / `price_reported`, `shabbat_closure` and the
fuel-quality `violation` flag. Prices come from:

| source | what it provides |
|---|---|
| the brand sites | the 98 availability (Paz, Sonol, Dor Alon, Mika, Ten) and the Shabbat hours |
| **Mika** | the operator's own prices for the pumps it runs — they win over a Waze report |
| **Waze** | community-reported 98 prices; also the only `got98` source for small/independent stations |

`price` is `null` when the station does not sell 98. A station with no fresh report this run keeps
its previous price; a Mika station that no longer lists a price is marked `got98: false`. The
`price_reported` timestamp is the Waze report time, or the Mika scrape time.

The Venue/Waze data lives behind Israel-only frontends, so the Waze price check and the violations
check run on the self-hosted runner selected by the `WAZE_RUNNER` variable; the brand-site fetches
and the commit run on GitHub-hosted runners.

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

`ci_update/check-violations.mjs` rewrites `ci_update/violations.json`; the `violations` job runs it **daily on
the Israeli runner** and a hosted job commits the result. It **must run from an Israeli IP** — the
Ministry API is geo-restricted, so the job refuses to run unless the machine's egress country is `IL`
(it prints the country and fails loudly otherwise; a silent 403 would look like "no violations").
Matching needs no geocoding: each Ministry entry carries the station's licence number, which is joined
to the same Ministry's station registry for a WGS84 coordinate, and that coordinate is matched to our
stations by proximity. Entries that fail to match are printed with the reason, so an empty
`violations.json` can never be the result of a lookup that quietly failed.

## Project structure

```
ci_update/                 # everything the hourly pipeline needs, in one folder
  build_stations.py        # Ministry registry + brand lists + matches -> stations.json
  build_seo.py             # regenerates index.html's <noscript> city block
  price_check_all.py       # Waze's reported 98 price for every collected venue
  mika_update.py           # Mika pages: prices, 98 flag, brand logo, Shabbat hours
  paz_update.py … ten_update.py   # one updater per brand site
  fetch_official.js / fetch_ten.py # the brand-locator fetch (Playwright/Python)
  check-violations.mjs     # Ministry substandard-fuel list (IL runner)
  violation_licences.js    # the licence list the database marks violations by
  mika_prices.py / price_check.py  # shared clients (Mika pages, Waze products)
  tapuz.json               # source of truth for the Tapuz station list
  mika.json, sonol.json, paz.json, doralon.json, delek.json, others.json  # the brand station lists
  official/                # last fetched brand locator lists (fallback)
  official_pins.json, ministry_venues.jsonl, remap.jsonl, …             # base station mapping
  stations.json            # THE SITE DATABASE (rebuilt hourly by CI, committed here)
  violations.json          # fuel-quality flags (rebuilt daily on the IL runner)
index.html                 # the whole app + a generated <noscript> SEO block
.github/workflows/prices.yml   # the hourly pipeline (nine jobs -> commit)
```

No build step — it's a static site. `index.html` fetches `ci_update/stations.json` and renders the
map (Leaflet + OpenStreetMap tiles) and list. Every row carries the brand, coordinates, `got98`,
`price`/`price_reported`, `shabbat_closure` and the violation flag, so no other file is read.

For crawlability, `index.html` carries a hidden `<noscript>` block listing every station grouped by
city. `ci_update/build_seo.py` takes each station's city from the Ministry registry's רשות_מקומית and
rewrites the block on demand — offline, deterministic, and finished in well under a second.

**The pipeline.** Seven gatherers run in parallel (`mika`, `violations`, `paz`, `sonol`,
`doralon`, `delek`, `ten`), then the chain is serialized so prices cover the stations this run
discovered:

1. **build** — `ci_update/build_stations.py` folds the gatherers' artifacts into the database;
2. **pricecheck** — on the Israeli runner, every station in that fresh database is asked of Waze
   (`ci_update/price_check_all.py`);
3. **publish** — the database is rebuilt with those prices, the SEO block is regenerated
   (`ci_update/build_seo.py`), and `ci_update/stations.json` + `ci_update/violations.json` +
   `index.html` are committed.
The Waze price check and the violation check need an Israeli egress and run on the runner named by
the `WAZE_RUNNER` repository variable; the brand fetches and the commit run on GitHub-hosted
runners. A failed gatherer costs that source's refresh, not the run: the commit job runs
`if: always()`, and a station with no fresh price keeps its previous one.

Run it locally from the repository root:

```sh
python3 ci_update/build_stations.py      # writes ci_update/stations.json
python3 ci_update/build_seo.py           # refreshes the noscript block in index.html
```

## License

[WTFPL](LICENSE) — do what you want.
