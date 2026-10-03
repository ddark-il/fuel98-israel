# תחנות דלק 98 — Fuel98 Israel

A simple map + list of fuel stations in Israel that sell **98-octane** petrol (בנזין 98 / Super 98).

🔗 **Live:** https://delek98.com/

Search by name or city, filter by brand, sort by distance from your location, and open any station directly in Waze.

## Prices

Where a station has a reported 98 price, the site shows it in a white ₪ bubble — in the list, on the map
card and in the mobile cards. Two sources:

| source | where it comes from |
|---|---|
| **Waze** | community reports — drivers report what they paid at that station; read per station by venue id (`price-ci/price_check.py`) |
| **Mika** | the operator's own published prices for the pumps it runs, scraped from mika.org.il (`price-ci/mika_prices.py`) |

Mika's own figure wins over a Waze report, and stations Mika publishes for are not asked of Waze at all.
The result is `data/prices.json` — one row per station that has a price, keyed by its `waze_id` (or
`pin:<lat>,<lon>` where it has none), e.g. `{"venues.22806850…": {"98": 9.81}}` — written hourly by
[`.github/workflows/prices.yml`](.github/workflows/prices.yml). A station with no price is not in the
file, and a run that could not read a station keeps its previous price instead of dropping it.

Waze serves Israeli fuel prices only from its `-il-` frontend — that data sits behind an Israeli service
mesh — and a GitHub-hosted runner never gets one (0 of 64 connections, while the same account from an
Israeli line answered 4/4 with prices in the same minute). Both Israel-only jobs therefore run on the
self-hosted runner selected by the `WAZE_RUNNER` variable; publishing and committing stay on hosted
runners.

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

`scripts/check-violations.mjs` rewrites `data/violations.json`; the `violations` job runs it **daily on
the Israeli runner** and a hosted job commits the result. It **must run from an Israeli IP** — the
Ministry API is geo-restricted, so the job refuses to run unless the machine's egress country is `IL`
(it prints the country and fails loudly otherwise; a silent 403 would look like "no violations").
Matching needs no geocoding: each Ministry entry carries the station's licence number, which is joined
to the same Ministry's station registry for a WGS84 coordinate, and that coordinate is matched to our
stations by proximity. Entries that fail to match are printed with the reason, so an empty
`violations.json` can never be the result of a lookup that quietly failed.

## Project structure

```
data/
  manifest.json     # lists the per-brand files + brand→file map
  paz.json          # one file per brand: [{ brand, name, coordinates:{lat,lon}, waze_id }, …]
  sonol.json
  doralon.json
  mika.json
  delek.json
  tapuz.json
  others.json       # the "אחר" group (יעד & small/independent brands)
  violations.json   # stations to flag ⚠️ (written by the daily job)
  prices.json       # reported 98 prices, station id → { "98": 8.59 } (written by the hourly job)
index.html          # the whole app + a generated <noscript> SEO block (between the seo-noscript markers)
scripts/
  check-violations.mjs           # cross-checks the Ministry substandard-fuel list (run by CI on the IL runner)
  build-seo.py                   # regenerates index.html's <noscript> SEO block (cities from the Ministry registry)
price-ci/           # the price pipeline that writes data/prices.json (see "98 prices" above)
  price_check.py    # Waze: asks per venue, resumable, shardable
  mika_prices.py    # Mika: scrapes the operator's published prices
  publish_prices.py # merge + guards → data/prices.json
  waze_prices.py, waze_live.py, waze_rt_poc.py, tools/pbtext.py   # the Waze client
.github/workflows/prices.yml       # the hourly/daily jobs
```

No build step — it's a static site. `index.html` fetches `data/manifest.json`, loads each brand file plus `data/violations.json` and `data/prices.json`, and renders the map (Leaflet + OpenStreetMap tiles) and list.

For crawlability, `index.html` carries a hidden `<noscript>` block listing every station grouped by city. `scripts/build-seo.py` takes each station's city from the Ministry registry's רשות_מקומית and rewrites the block on demand — offline, deterministic, and finished in well under a second. It is still a manual step, run whenever the station data changes.

The **station scrapers and coordinate-verification scripts** (Mika → its own map pins, Delek → official locator, Tapuz/Other → the data.gov.il registry, plus the pin-source planner) live **outside** this repository, in the parent project.

**Maintaining the price pipeline.** Both Israel-only jobs run on the self-hosted runner named by the
repository variable `WAZE_RUNNER` (an Israeli Mac mini, on 24/7); the publish and commit steps run on
hosted runners, because the Israeli machine's x86_64 runner cannot execute `/usr/bin/python3` or `git`
— the CommandLineTools shim is arm64-only, so the jobs discover a working interpreter themselves and
print which one they picked (installing the `osx-arm64` runner build removes the whole class of trap).
A run that reads nothing is **red on purpose**: `exit 3` means no IL frontend answered for 30 minutes,
and `continue-on-error` used to hide exactly that. `publish_prices.py` refuses to write a file with
fewer than `--min-rows` prices, or one that drops more than half against the committed file
(`--allow-shrink` overrides, after a deliberate data change). To reproduce locally, run from
`price-ci/`: `python3 price_check.py --out prices.json` (needs an Israeli line), then
`python3 mika_prices.py --merge prices.json`, then
`python3 publish_prices.py --in prices.json --out ../data/prices.json`.

## License

[WTFPL](LICENSE) — do what you want.
