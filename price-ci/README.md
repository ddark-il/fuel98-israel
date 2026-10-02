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
{ "generated": "2026-10-02T15:12:04Z",
  "fuel": "98",
  "counts": { "stations": 73, "with_98": 73, "from_mika": 30, "carried_over": 50 },
  "stations": {
    "googlePlaces.ChIJ…":          { "98": 8.59 },
    "venues.22806850.228134032.1804": { "98": 9.81 },
    "pin:31.986506,34.772124":     { "98": 8.68 }
  } }
```

Deliberately minimal: **station identity → price, nothing else** (~1-2 KB, not 40). Everything a page
needs to *show* a price — brand, name, coordinates, the station's own page — is already in
`data/*.json`, keyed by the same `waze_id`; publishing it twice meant every consumer had to know which
copy was authoritative.

* **Key**: the station's `waze_id` (`venues.*` or `googlePlaces.*`), else `pin:<lat>,<lon>` for the 21
  stations that have no venue id — the same numbers the site already has, so the join is a lookup.
* **A station with no price is not in the file.** Absence means nobody reported one, not that 98 is
  unavailable; a row exists only when there is a figure to show.
* `counts.carried_over`: a run that cannot read a station (a cold Waze window) keeps the previous
  file's price for it as the last known one, instead of dropping the row. With `--with-meta` each row
  also carries `source` (`waze` / `mika`), `updated`, `review` and `verified` — useful when debugging
  the feed, unnecessary for the site.

Reproduce locally:

```bash
python3 price_check.py --out prices.json          # asks Waze (needs an Israeli line)
python3 mika_prices.py --merge prices.json        # operator prices on top
python3 publish_prices.py --in prices.json --out ../data/prices.json
```

## Why it can be trusted unattended

* The Waze layer only runs when the **Israeli cluster** is answering. `rt.waze.com` routes every
  request to one of two frontends, and only `realtime-frontend-prod-il-*` is attached to the Israeli
  service mesh that holds the fuel data (`venue.prod.il.mesh-waze`, seen in a bridge error); a
  `-row-` reply carries the venue with no products at whatever price. The run therefore probes a
  canary first (`--wait-probes`/`--wait-for-il`) and, with `--require-il`, **stops** instead of
  spending a sweep on non-answers. From GitHub-hosted runners the IL frontend did not answer once
  in 64 connections, so the Waze layer belongs on an Israeli line — the same "run me from Israel"
  bucket as `check-violations.mjs` — and Mika carries this job by itself.
* The job fails on a **broken run** (`--min-prices`: too few stations returned any price), and
  stays green when Waze simply has nothing to report — which is most stations.
* `publish_prices.py` refuses to write when there are fewer than `--min-rows` priced stations, or
  when the count drops by more than `--max-drop-pct` (default 50%) against the committed file. A
  bad run therefore cannot blank the site's prices; it needs `--allow-shrink` after a deliberate
  data change.
* The merged file covers **every** station we track, including the 55 without a `waze_id`, which
  ship with `price98: null` + `not_checked` — so a source that does know them (Mika) has a row to
  fill. Stations without a venue id are exactly the ones left as coordinate pins on the map.

## The Israeli runner

Both Israel-only jobs (Waze prices and the fuel-quality check) run on the self-hosted runner, selected
by the repository variable `WAZE_RUNNER` - currently `["self-hosted","macOS","X64"]`, the Mac mini on
the office network that is on 24/7. Move them again by changing that variable, not the workflow.

What that machine needs:

| need | why | check |
|---|---|---|
| `python3` >= 3.10 | the price scripts (stdlib only, no pip) | the job prints the interpreter it picked |
| `node` >= 18 | `scripts/check-violations.mjs` (uses `fetch`) | same, printed by the job |
| `git`, `curl` | checkout, and the egress-country guard on the violations job | - |
| an Israeli egress | the Ministry API is geo-restricted and Waze's fuel data sits behind the IL mesh | the violations job fails loudly with `egress country: …` |

Two traps seen in practice, both now handled by the workflow but worth knowing:

* **an x86_64 runner on an Apple Silicon Mac** cannot execute `/usr/bin/python3`: the file exists, and
  `--version` works from a native shell, but the CommandLineTools shim is arm64-only and the runner
  process is translated, so it dies with `unable to load libxcrun`. Install the `osx-arm64` runner
  build (or point the jobs at a Homebrew python, which is what the discovery step does).
* **`continue-on-error` on the shard step hid a dead shard**: it reported as a green job. The step now
  maps price_check's exit codes instead - `3` (no IL cluster from this network) is a warning,
  anything else fails.

## Where the prices are, and why this job cannot fetch them

`rt.waze.com` load-balances every request between the `-il-` and `-row-` frontends, and only the
Israeli one is attached to the service mesh that holds the fuel data - a bridge error named it:
`xds:///venue.prod.il.mesh-waze:12401`. The app's own server list and its certificate SANs give the
Israeli endpoint directly (`rtproxy-il.waze.com`, plus the legacy `rt-il.waze.com`), now the default
in `waze_live.py` (`WAZE_BASE` overrides it), and two other things were measured with it:

* the **account matters**: from an Israeli line the established account was answered by the IL
  cluster 12/12 with prices, while an account registered seconds earlier got 0/12 - so the frontend
  is not simply "any client from Israel";
* the **source network matters more**: at 13:35 the same account, endpoint and minute gave 4/4 IL
  replies with prices from an Israeli line, and **no IL reply at all in three GitHub-hosted shards**
  (0 in 64 probe connections as well). Eligibility is therefore the network, and this job cannot
  carry the Waze layer.

Set the repository variable `WAZE_RUNNER` (JSON labels, e.g. `["self-hosted","israel"]`) to run the
Waze job on an Israeli machine. Until then the shards stop after one probe ("no IL cluster from this
network right now"), cost ~30 s, and Mika - which is unrestricted and carries the operators' own
published prices - is the layer that actually updates.

## Known unknown — answered by the first runs

The guess was that Waze's distributor treats a datacenter IP differently. It is not the datacenter:
the fuel data sits behind an Israeli service mesh, and a GitHub-hosted runner never gets an IL
frontend (0 in 64 connections, 0 in 135 canary attempts). The shards now say so in one line each
(`stopping: no IL cluster from this network right now`) instead of grinding for an hour, and the fix
is a runner on an Israeli line, not a code change.
