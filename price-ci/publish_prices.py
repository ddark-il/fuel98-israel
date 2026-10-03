#!/usr/bin/env python3
"""Build ../data/prices.json from a price check run (Waze + Mika merged).

The site is a static no-build page, so the price layer is one small JSON file that CI rewrites:
keyed by the station's `waze_id`, with a coordinate key for the stations that have no venue id, so
every row the frontend already renders can find its price.

    python3 price-ci/publish_prices.py --in merged.json          # writes data/prices.json
    python3 price-ci/publish_prices.py --in merged.json --check   # validate, don't write

Two guards, because a failed CI run must never blank the site's prices:
  * --min-rows N     refuse to write a file with fewer priced stations than N
  * never shrink by more than --max-drop-pct (default 50%) against the file already committed,
    unless --allow-shrink is passed. A run that found fewer prices is a run worth reading, not one
    worth publishing.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'data')
OUT_DEFAULT = os.path.join(DATA, 'prices.json')

# Only these two sources are allowed to speak for a price; anything else is a bug in the merge.
SOURCES = {'waze', 'mika'}


def iter_stations(doc: dict):
    """Yield (station key, record) from any of our price files.

    `price_check.py --out` writes a LIST of rows, `--publish` and the merged file write a DICT
    keyed by station key - the CI hands us one of each depending on the step, and a price pipeline
    should not die on that difference.
    """
    s = doc.get('stations', {})
    if isinstance(s, dict):
        yield from s.items()
    else:
        for rec in s:
            k = rec.get('key')
            if k:
                yield k, rec


def station_index(pattern: str) -> dict:
    """our station key -> the fields a consumer needs to match a price row back to a station."""
    out = {}
    for f in sorted(glob.glob(pattern)):
        base = os.path.basename(f)
        if base in ('manifest.json', 'violations.json', 'prices.json'):
            continue
        d = json.load(open(f, encoding='utf-8'))
        brand_file = d.get('brand') if isinstance(d, dict) else None
        for s in (d if isinstance(d, list) else (d.get('stations') if isinstance(d.get('stations'), list) else [])):
            c = s.get('coordinates') or {}
            if c.get('lat') is None:
                continue
            key = f"{base}|{s.get('name')}|{c['lat']},{c['lon']}"
            out[key] = {'waze_id': s.get('waze_id'), 'brand': s.get('brand') or brand_file,
                        'name': s.get('name'), 'lat': float(c['lat']), 'lon': float(c['lon'])}
    return out


def row_of(rec: dict, st: dict) -> dict | None:
    """One price row: what we know about this station's 98 price, or None if we know nothing."""
    price = rec.get('price98')
    src = rec.get('price98_source') or ('waze' if price is not None else None)
    if price is None and not rec.get('prices'):
        return None                                    # nothing reported at all: no row
    if src and src not in SOURCES:
        src = 'waze'
    out = {'brand': st['brand'], 'name': st['name'],
           '98': price if price is not None else None,
           'source': src if price is not None else None}
    if rec.get('prices'):
        out['fuels'] = rec['prices']
    if rec.get('price98_updated'):
        out['updated'] = datetime.fromtimestamp(
            rec['price98_updated'] / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    for k_from, k_to in (('price98_age_days', 'age_days'),
                         ('price98_current_period', 'current_period'),
                         ('price98_label', 'label')):
        if rec.get(k_from) is not None:
            out[k_to] = rec[k_from]
    # A join we could not confirm on the ground: the site should soften or hide the figure.
    if rec.get('price98_review'):
        out['review'] = rec['price98_review']
    # ...and the opposite case: a doubt a person settled, with the proof, travels with the price.
    if rec.get('price98_verified'):
        out['verified'] = rec['price98_verified']
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='src', required=True,
                    help='price_check output with the Mika prices merged in')
    ap.add_argument('--out', default=OUT_DEFAULT)
    ap.add_argument('--data', default=os.path.join(DATA, '*.json'))
    ap.add_argument('--min-rows', type=int, default=10, dest='min_rows')
    ap.add_argument('--max-drop-pct', type=float, default=50.0, dest='max_drop_pct')
    ap.add_argument('--allow-shrink', action='store_true', dest='allow_shrink')
    ap.add_argument('--check', action='store_true', help='validate and report, do not write')
    ap.add_argument('--with-meta', action='store_true', dest='with_meta',
                    help='also publish source/updated/review per station (debugging; the site reads '
                         'only the price)')
    a = ap.parse_args()

    merged = json.load(open(a.src, encoding='utf-8'))
    ours = station_index(a.data)
    by_id: dict[str, dict] = {}
    collisions: list[str] = []
    no_price = no_station = 0
    for key, rec in iter_stations(merged):
        st = ours.get(key)
        if not st:
            no_station += 1                       # the price file knows a station we removed
            continue
        row = row_of(rec, st)
        if not row:
            no_price += 1
            continue
        # waze_id first (it is the stable handle), coordinate second (the fallback the site draws).
        # The pin key is rounded to 6 decimals (~0.1 m) so the string is canonical: the site builds
        # the same key from its own copy of the coordinates, and float repr cannot make them differ
        # ("32.02517" vs "32.025170" is enough to detach a price from its station).
        ident = st['waze_id'] or f"pin:{float(st['lat']):.6f},{float(st['lon']):.6f}"
        row['key'] = ident
        row['venue'] = bool(st['waze_id'])
        prev = by_id.get(ident)
        if prev is not None:
            # Two of our records share one venue id - Mika runs pumps inside stations whose brand
            # record we also carry (סונול גיסין / מיקה גיסין, and three more), and both point at the
            # same forecourt. The site keys prices by venue id, so exactly one entry may be published:
            # keep the one that has a price, and Mika's when both have one (the operator's own
            # published figure wins). Without this the later record silently overwrote the earlier
            # one, so which price survived depended on dict order - גיסין showed a 9-day-old Waze
            # report at 01:30 and Mika's own price at 16:46, from identical inputs.
            collisions.append(f"{row['brand']}/{st['name']} shares a venue with "
                              f"{prev['brand']}/{prev['name']}")
            takes = (prev['98'] is None and row['98'] is not None) or \
                    (prev['98'] is not None and row['98'] is not None
                     and row.get('source') == 'mika' and prev.get('source') != 'mika')
            if not takes:
                continue
        by_id[ident] = row

    prev_rows = {}
    if os.path.exists(a.out):
        try:
            raw_prev = json.load(open(a.out, encoding='utf-8')).get('stations', {})
            # accept both shapes: {"id": {"98": 8.4}} and a hand-written {"id": 8.4}
            prev_rows = {}
            for k, v in raw_prev.items():
                if k.startswith('pin:'):      # accept the older, unrounded pin keys
                    lat, lon = k[4:].split(',')
                    k = f'pin:{float(lat):.6f},{float(lon):.6f}'
                prev_rows[k] = (v if isinstance(v, dict) else {'98': v})
        except Exception:                                          # noqa: BLE001
            print(f'note: existing {a.out} is unreadable, writing a fresh file')

    # Carry over what this run could not read. A cold Waze window (or any source that was down) means
    # the merge simply has no row for a station - deleting the price the site already shows would be
    # the wrong answer to "we could not ask this time". The row keeps its own timestamp and reporter,
    # so an old price stays recognisable as old. Stations dropped from data/*.json are not resurrected.
    live = {st['waze_id'] or f"pin:{float(st['lat']):.6f},{float(st['lon']):.6f}"
            for st in ours.values()}
    carried = 0
    for ident, prev in prev_rows.items():
        if ident in by_id or ident not in live:
            continue
        by_id[ident] = prev
        carried += 1

    priced = [r for r in by_id.values() if r['98'] is not None]
    current = [r for r in priced if r.get('current_period')]
    from_mika = [r for r in priced if r.get('source') == 'mika']

    # The published file is deliberately minimal: station identity -> price, and nothing else.
    # Everything a reader needs to *show* a price (brand, name, coordinates, the station page) is
    # already in data/*.json, keyed by the same `waze_id`; shipping it twice meant every consumer had
    # to know which copy was authoritative, and it made the file 40 KB of mostly repeated strings.
    # A station with no price is not listed at all - that IS the signal (see README).
    stations_out: dict[str, dict] = {}
    for ident, row in by_id.items():
        if row.get('98') is None:
            continue
        entry = {'98': row['98']}
        if a.with_meta:                       # for debugging: where it came from and how old it is
            for k in ('source', 'updated', 'age_days', 'current_period', 'label', 'review',
                      'verified', 'brand', 'name'):
                if row.get(k) is not None:
                    entry[k] = row[k]
        stations_out[ident] = entry

    payload = {
        'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'fuel': '98',
        'counts': {'stations': len(stations_out), 'with_98': len(stations_out),
                   'from_mika': len(from_mika), 'carried_over': carried},
        'stations': stations_out,
    }

    print(f'{len(priced)} stations with a 98 price ({len(current)} in the current price period, '
          f'{len(from_mika)} from Mika); {no_price} stations had nothing reported and are not in '
          f'the file')
    if carried:
        print(f'  carried over {carried} price(s) this run could not read (kept from the previous '
              f'file as the last known price - pass --with-meta to publish how old each one is)')
    if no_station:
        print(f'  note: {no_station} priced station(s) are not in data/*.json any more (dropped)')
    for c in collisions:
        print(f'  note: {c} (one entry published; see the comment in the code)')

    problems = []
    if len(priced) < a.min_rows:
        problems.append(f'only {len(priced)} priced stations (< --min-rows {a.min_rows})')
    if prev_rows and not a.allow_shrink:
        was = sum(1 for r in prev_rows.values() if r.get('98') is not None)
        if was and len(priced) < was * (1 - a.max_drop_pct / 100):
            problems.append(f'{len(priced)} priced stations vs {was} in the committed file '
                            f'(> {a.max_drop_pct:.0f}% drop) - pass --allow-shrink if intended')
    for p in problems:
        print(f'FAIL: {p}')
    if problems or a.check:
        return 1 if problems else 0

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(payload, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False,
              indent=1, sort_keys=False)
    print(f'wrote {a.out} ({os.path.getsize(a.out)/1024:.0f} KB)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
