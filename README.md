# תחנות דלק 98 — Fuel98 Israel

A simple map + list of fuel stations in Israel that sell **98-octane** petrol (בנזין 98 / Super 98).

🔗 **Live:** https://delek98.com/

Search by name or city, filter by brand, sort by distance from your location, and open any station directly in Waze.

## 98 prices

Where a station has a reported 98 price, the site shows it in a white ₪ bubble — in the list column, on
the map card and in the mobile cards. Two sources feed it:

| source | what it is | produced by |
|---|---|---|
| **Waze** | community reports — a driver told Waze what they paid | `price-ci/price_check.py` (per venue, via `waze_id`) |
| **Mika** | the operator's own published price for the pumps it runs | `price-ci/mika_prices.py` (scrapes mika.org.il) |

The operator's own figure always wins over a community report, and stations Mika publishes for are not
asked of Waze at all (`price-ci/mika_covered.json`, written by the Mika step and committed).

`data/prices.json` is written by [`.github/workflows/prices.yml`](.github/workflows/prices.yml):

```json
{ "generated": "2026-10-03T01:30:05Z", "fuel": "98",
  "counts": { "stations": 78, "with_98": 78, "from_mika": 32, "carried_over": 23 },
  "stations": {
    "googlePlaces.ChIJ…":             { "98": 8.59 },
    "venues.22806850.228134032.1804": { "98": 9.81 },
    "pin:31.986506,34.772124":        { "98": 8.68 } } }
```

* **Key**: the station's `waze_id`, else `pin:<lat>,<lon>` (6 decimals) for the 21 stations without a
  venue id — the same numbers the site already has, so the join is a lookup, never a distance
  calculation.
* **A station with no price is not in the file.** Absence means nobody reported one, not that 98 is
  unavailable at that station.
* `counts.carried_over`: a run that could not read a station keeps the previous file's price for it as
  the last known one, so a cold source cannot blank the site. `--with-meta` adds `source`, `updated`,
  `review` and `verified` per row for debugging the feed.
* Nothing else in the site writes this file, and it writes no other data file.

### Where the pipeline runs, and why it runs in Israel

Waze serves Israeli fuel prices only from the `-il-` frontend, which is attached to an Israeli service
mesh holding that data (`xds:///venue.prod.il.mesh-waze:12401`, named in a bridge error once we got an
`il` reply; the `-row-` frontend answers with the venue and no products at all). Which frontend a
request lands on is decided per request by Waze's edge, and a **GitHub-hosted runner never gets an
`il` one** — measured: 0 of 64 connections, and 0 in 135 canary attempts, while the same account from
an Israeli line answered 4/4 with prices in the same minute. Nothing in the request changes it: not the
URL parameters, the session, the device identity, HTTP/1.1 vs HTTP/2 vs HTTP/3, or a pinned
`rtserver-id`. The app's own server list and certificate SANs gave the Israeli endpoint directly
(`rtproxy-il.waze.com`, plus the legacy `rt-il.waze.com`), which `price-ci/waze_live.py` uses by
default (`WAZE_BASE` overrides it).

So both Israel-only jobs run on a **self-hosted runner** — the Mac mini that is on 24/7 on an Israeli
line — selected by the repository variable `WAZE_RUNNER` (currently `["self-hosted","macOS","X64"]`).
Move them by changing that variable, not the workflow.

| job | where | when |
|---|---|---|
| `waze` — 98 prices | `WAZE_RUNNER` (Israel) | hourly at :15 |
| `violations` — Ministry fuel-quality list | `WAZE_RUNNER` (Israel) | daily 02:45 UTC |
| `publish` — Mika + merge + commit | hosted | after `waze` |
| `violations-commit` | hosted | after `violations` |

What that machine needs: `python3` ≥ 3.10 and `node` ≥ 18 (the scripts are stdlib-only — no pip, no
npm), `git`, `curl`, and an Israeli egress. The jobs discover the interpreters themselves and print
which one they picked. Two traps worth knowing, both handled by the workflow:

* **an x86_64 runner on an Apple Silicon Mac cannot execute `/usr/bin/python3`** (or `git`): the
  CommandLineTools shim is arm64-only while the runner process is translated, so it dies with
  `unable to load libxcrun`. Install the `osx-arm64` runner build, or let the discovery step pick a
  Homebrew python — which is why committing is a separate job on a hosted runner.
* **`continue-on-error` on the shard step hid a dead shard**, so it reported as a green job. The step
  now maps exit codes: `3` (no IL cluster from this network) fails with that reason, anything else
  fails as a real error. A run that reads nothing is red, not quietly green.

Two guards keep a bad run from emptying the site: `publish_prices.py` refuses to write when fewer than
`--min-rows` stations are priced, or when the count drops by more than half against the committed file
(`--allow-shrink` overrides, after a deliberate data change).

### Reproducing locally

Run these from `price-ci/` (the scripts import their siblings relative to the working directory):

```bash
python3 price_check.py --out prices.json            # asks Waze; needs an Israeli line
python3 mika_prices.py --merge prices.json          # operator prices on top
python3 publish_prices.py --in prices.json --out ../data/prices.json
```

`price_check.py --shard i/n` splits the list for parallel jobs, `--jsonl` resumes an interrupted run,
and `--wait-probes`/`--wait-for-il` control how long it waits for the Israeli frontend before giving up.

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

## License

[WTFPL](LICENSE) — do what you want.
