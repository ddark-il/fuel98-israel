# price-ci — 98 prices from Waze and Mika, updated by GitHub Actions

`data/prices.json` is written here, by
[`.github/workflows/prices.yml`](../.github/workflows/prices.yml), three times a day. Nothing else
in the site writes it; nothing here writes any other data file.

| file | what it does |
|---|---|
| `price_check.py` | asks Waze for the 98 price of every station that has a `waze_id` (venue-driven, resumable `--jsonl`, `--shard i/n` for parallel jobs) |
| `mika_prices.py` | scrapes Mika's own published prices and merges them into the same file (the operator's figure wins; the Waze view is kept under `sources.waze`) |
| `mika_overrides.json` | owner rulings on mika.org.il content — `exclude` (a page Mika already fixed) and `verified` (a station match a person settled, with the evidence) |
| `publish_prices.py` | turns the merged result into `data/prices.json`, with the two guards below |
| `waze_prices.py`, `waze_live.py`, `waze_rt_poc.py`, `tools/pbtext.py` | the Waze client: anonymous registration, the protobuf request/response codec |

Run any of them from this directory:

```bash
python3 price_check.py --limit 20 --out out.json --state waze_account.json
python3 mika_prices.py --out mika.json --publish out --merge out.json
python3 publish_prices.py --in merged.json --out ../data/prices.json
```

## What `data/prices.json` looks like

```json
{ "generated": "2026-10-02T09:40:00Z", "fuel": "98",
  "counts": { "stations": 320, "with_98": 61, "current_period": 34, "from_mika": 30, "by_venue": 265 },
  "stations": {
    "googlePlaces.ChIJ…": { "brand": "מיקה", "name": "הפלד", "98": 8.59, "source": "mika",
                            "updated": "2026-10-01T00:01:24Z", "age_days": 1.4,
                            "current_period": true, "venue": true },
    "pin:31.986506,34.772124": { "brand": "מיקה", "name": "ראשון", "98": 8.68, "source": "mika",
                                 "review": "conflict: page 'משה לוי' vs official 'רוזנסקי 9…'" } } }
```

* **Key:** the station's `waze_id` when it has one, else `pin:<lat>,<lon>` — the same numbers the
  site already has, so the join is a lookup, never a distance calculation.
* **A missing row means nobody reported a price**, not that 98 is unavailable at that station.
* `source`: `waze` (community report) or `mika` (the operator's own published price — Mika runs
  pumps inside other brands' stations, so it publishes prices Sונול and פז do not).
* `review`: our match for that station is unconfirmed → soften or hide the figure.
  `verified`: the opposite, a settled doubt, with the proof.
* `current_period`: reported since the 1st of this month. Prices are revised twice a month, so a
  figure outside the period is *last* period's, and the UI should say "as of".

## Why it can be trusted unattended

* The job fails on a **broken run** (`--min-prices`: too few stations returned any price), and
  stays green when Waze simply has nothing to report — which is most stations.
* `publish_prices.py` refuses to write when there are fewer than `--min-rows` priced stations, or
  when the count drops by more than `--max-drop-pct` (default 50%) against the committed file. A
  bad run therefore cannot blank the site's prices; it needs `--allow-shrink` after a deliberate
  data change.
* The merged file covers **every** station we track, including the 55 without a `waze_id`, which
  ship with `price98: null` + `not_checked` — so a source that does know them (Mika) has a row to
  fill. Stations without a venue id are exactly the ones left as coordinate pins on the map.

## Known unknown, checked by the first runs

Waze's distributor may treat a datacenter IP differently from a residential one. If that turns out
to be the case, the shards fail with transport errors in the log (`--min-prices` catches it) and the
fix is a self-hosted runner in Israel, not a code change.
