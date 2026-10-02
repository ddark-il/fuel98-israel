#!/usr/bin/env python3
"""price_check.py — read currently-reported 98-octane prices per Waze venue.

Runs over our own station database, using the `waze_id` (Waze venue id) we already resolved for
305/326 stations, so no name/coordinate matching is needed here. For each venue:

  1. search near our pin to obtain a fresh *search result id* for that venue (the distributor
     will not return products for a venue id on its own — measured 0/6 that way, vs ~5/6 with a
     matching result id),
  2. `Element.get_request` (2064) = `GetRequest{id, venue_id, user_info, return_all_data}`,
     retried, because Waze non-deterministically answers with the Google-backed view (no
     products) instead of the Waze venue,
  3. read `product[]` -> `{id, price, last_updated, updated_by}` and pick the 98 price with
     `gas_regular98` (98 עצמי) first, falling back to `gas_service98` (98 שרות).

Anonymous Waze session (created on the fly, or reused via --state). No API key, no login.

OUTPUT
  --jsonl   one row per station, appended and resumed on re-run (safe to interrupt)
  --out     merged JSON: metadata + every station with its prices
  index     every station and its prices written to a *directory* as data/prices.json
            (--publish DIR): { "generated": ..., "stations": { "<file>|<name>": {...} } }

CI
  Designed to be dropped into GitHub Actions (see workflows/price_check.yml.example):
  no interactive input, no secrets required, resumable, exits non-zero only when the run is
  broken (transport blocked / nothing fetched) rather than when Waze simply has no price.
  Note: Waze may treat datacenter IPs differently to residential ones - the script prints the
  first few transport errors verbatim so a CI failure is diagnosable from the log alone.

CAVEATS
  Waze prices are community reports: a missing price means nobody reported it, not that the
  station does not sell 98. Run politely (--sleep) - this is a PoC while official access is
  being negotiated.

USAGE
  python3 price_check.py --limit 10                    # sample, prints a table
  python3 price_check.py --out prices.json             # all stations with a waze_id
  python3 price_check.py --shard 0/3 --jsonl run.jsonl # shard across CI jobs
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

try:                                            # tzdata may be absent on bare runners
    from zoneinfo import ZoneInfo
    IL_TZ = ZoneInfo('Asia/Jerusalem')
except Exception:                               # noqa: BLE001
    IL_TZ = timezone.utc

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'tools'))
import importlib.util                                            # noqa: E402
_spec = importlib.util.spec_from_file_location('wp', os.path.join(_HERE, 'waze_prices.py'))
wp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wp)
P = wp.P
wl = wp.wl                                                        # waze_live module

DEFAULT_DATA_GLOB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '..', 'data', '*.json')
EL_GET_REQUEST = 2064


def period_start_ms(now: datetime | None = None) -> int:
    """Start of the current Israeli fuel-price period: the 1st of the month, 00:00 local.

    Prices in Israel are republished at the month boundary (the regulated 95 maximum changes at
    00:00 on the 1st), so a report older than that belongs to the previous price period and is
    very likely stale. 98 is not regulated, so this is a staleness heuristic, not a rule.
    """
    now = now or datetime.now(IL_TZ)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first.timestamp() * 1000)


def age_days(ms, now_ms: int | None = None) -> float | None:
    if not ms:
        return None
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    return round((now_ms - ms) / 86400000.0, 1)


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def load_stations(pattern: str, brands=None, require_venue: bool = True) -> list[dict]:
    out = []
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
            brand = s.get('brand') or brand_file
            if brands and brand not in brands:
                continue
            if require_venue and not s.get('waze_id'):
                continue
            out.append({'file': base, 'brand': brand, 'name': s.get('name'),
                        'lat': float(c['lat']), 'lon': float(c['lon']),
                        'waze_id': s.get('waze_id'),
                        'key': f"{base}|{s.get('name')}|{c['lat']},{c['lon']}"})
    return out


class PriceChecker:
    def __init__(self, state: str | None = None, sleep: float = 0.3, tries: int = 4,
                 verbose: bool = False):
        self.sleep, self.tries, self.verbose = sleep, tries, verbose
        self.state = state
        self.session: wl.Session | None = None
        self.errors: list[str] = []

    # ---------------------------------------------------------------- session
    def _sess(self) -> wl.Session:
        if self.session is None:
            s = wl.Session()
            if self.state and os.path.exists(self.state) and s.load_account(self.state):
                print(f'[{ts()}] using account from {self.state}: user_id={s.account.get("user_id")}',
                      flush=True)
            else:
                resp = s.register()
                acct = s.save_account(resp, self.state or 'waze_account.json')
                print(f'[{ts()}] registered anonymous account: {acct}', flush=True)
            self.session = s
        return self.session

    # ---------------------------------------------------------------- lookups
    def _search(self, st: dict, query: str | None, category: str | None) -> list[dict]:
        """Candidates: [{result_id, venue_id, name, lat, lon}] with a fresh result id."""
        s = self._sess()
        c = wp.PriceClient(st['lat'], st['lon'], retries=1)
        c.s = s                                    # share one session/account per run
        return c.find_stations(category=category, query=query, radius=3000, max_results=20)

    def find_result_id(self, st: dict) -> tuple[str | None, str, float | None, str | None]:
        """Fresh (result_id, venue_id, distance, name) for our venue, by id first, then nearest."""
        vids_seen = []
        for query, cat in ((f"{st['brand']} {st['name']}", None), (st['name'], None),
                           (st['brand'], 'GAS_STATION'), (None, 'GAS_STATION')):
            try:
                cands = self._search(st, query, cat)
            except Exception as e:                                  # noqa: BLE001
                self.errors.append(f'search {st["key"]}: {e!r}')
                cands = []
            if not cands:
                continue
            vids_seen += [c['venue_id'] for c in cands]
            exact = [c for c in cands if c['venue_id'] == st['waze_id']]
            pool = exact or cands
            best = min(pool, key=lambda c: haversine_m(st['lat'], st['lon'],
                                                       c['lat'] or 0, c['lon'] or 0))
            dist = haversine_m(st['lat'], st['lon'], best['lat'] or 0, best['lon'] or 0)
            # accept a lookup that found our venue, or a close one of the same kind
            if exact or dist <= 150:
                return best['result_id'], best['venue_id'], dist, best['name']
            time.sleep(self.sleep)
        return None, st['waze_id'], None, None

    # ---------------------------------------------------------------- fetching
    def fetch_products(self, result_id: str | None, venue_id: str) -> dict | None:
        """GetRequest -> products, retried; alternating id+venue_id and venue_id alone."""
        s = self._sess()
        c = wp.PriceClient(0.0, 0.0, retries=1)
        c.s = s
        c.uid = None
        c.auth_el = None
        for attempt in range(self.tries):
            if result_id and attempt % 3 != 2:
                rid, vid = result_id, venue_id
            else:
                rid, vid = None, venue_id
            req = P.pb_str(3, vid) + (P.pb_str(1, rid) if rid else b'')
            req += P.pb_bytes(2, c._user_info()) + P.pb_bool(6, True)
            try:
                resp = s.post(P.batch(c._client_info(), c._auth(),
                                      P.element(**{str(EL_GET_REQUEST): req})))
            except Exception as e:                                  # noqa: BLE001
                self.errors.append(f'get {venue_id}: {e!r}')
                time.sleep(self.sleep)
                continue
            venue = wp._first_venue(resp)
            if venue:
                prods = wp._products(venue)
                if prods:
                    return prods
            time.sleep(self.sleep)
        return None

    # ---------------------------------------------------------------- one row
    def check(self, st: dict) -> dict:
        row = {**{k: st[k] for k in ('key', 'file', 'brand', 'name', 'lat', 'lon', 'waze_id')},
               'checked_at': datetime.now(timezone.utc).isoformat(),
               'matched_venue_id': None, 'venue_name': None, 'search_distance_m': None,
               'products': {}, 'prices': {}, 'price98': None, 'price98_id': None,
               'price98_label': None, 'price98_updated': None, 'price98_by': None,
               'updated': None, 'updated_by': None}
        rid, vid, dist, vname = self.find_result_id(st)
        row['matched_venue_id'] = vid
        row['venue_name'] = vname
        row['search_distance_m'] = round(dist, 1) if dist is not None else None
        if vid != st['waze_id']:
            row['venue_mismatch'] = True            # matched a neighbour - keep, but visible
        pstart = period_start_ms()
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        row['period_start'] = datetime.fromtimestamp(pstart / 1000, tz=timezone.utc).isoformat()
        prods = self.fetch_products(rid, st['waze_id'])
        if not prods and vid and vid != st['waze_id']:
            # same station, the other representation (our id is often the Google-backed one
            # while the search returns the Waze venue id) - the distributor resolves the
            # Waze-native id far more often
            prods = self.fetch_products(rid, vid)
            if prods:
                row['price_source_venue_id'] = vid
        if prods:
            row['products'] = prods
            row['prices'] = {wp.FUEL_LABELS.get(k, k): v['price'] for k, v in prods.items()}
            pid, entry = wp.best_98(prods)
            if pid:
                upd = entry['last_updated'] or 0
                row.update({'price98': entry['price'], 'price98_id': pid,
                            'price98_label': wp.FUEL_98_LABELS[pid],
                            'price98_updated': upd,
                            'price98_by': entry['updated_by'],
                            'price98_age_days': age_days(upd, now_ms),
                            'price98_current_period': bool(upd and upd >= pstart),
                            'price98_stale': bool(upd and upd < pstart)})
            row['updated'] = max((v['last_updated'] or 0 for v in prods.values()), default=None)
            row['updated_by'] = next((v['updated_by'] for v in prods.values() if v['updated_by']),
                                     None)
        return row


def ts() -> str:
    return datetime.now(timezone.utc).strftime('%H:%M:%S')


def venue_less_rows(stations: list[dict]) -> list[dict]:
    """Rows for the stations this check cannot reach (no `waze_id`), so the file stays complete.

    Without a row here a station that another source *does* know - Mika publishes its own 98 prices
    for the pumps it runs - would have nothing to merge into, and its price would silently never
    appear anywhere. A row with `price98: null` and `not_checked` says exactly that.
    """
    return [{'key': s['key'], 'file': s['file'], 'brand': s['brand'], 'name': s['name'],
             'lat': s['lat'], 'lon': s['lon'], 'waze_id': None, 'prices': {}, 'price98': None,
             'price98_label': None, 'checked_at': None,
             'not_checked': 'no waze_id - Waze venue not resolved'} for s in stations
            if not s.get('waze_id')]


def publish(rows: list[dict], outdir: str, only_current: bool = False, shard: str | None = None) -> str:
    os.makedirs(outdir, exist_ok=True)
    by_key = {}
    for r in rows:
        rec = {k: r.get(k) for k in ('brand', 'name', 'waze_id', 'prices', 'price98',
                                     'price98_label', 'price98_updated', 'price98_by',
                                     'price98_age_days', 'price98_current_period',
                                     'checked_at')}
        if only_current and rec.get('price98') and not rec.get('price98_current_period'):
            rec['price98'] = None                      # keep the row, drop the stale figure
            rec['price98_label'] = None
            rec['price98_dropped_stale'] = True
        by_key[r['key']] = rec
    payload = {'generated': datetime.now(timezone.utc).isoformat(),
               'shard': shard,
               'source': 'Waze rt API (community-reported prices)',
               'note': 'a missing price means nobody reported it, not that 98 is unavailable',
               'stations': by_key}
    path = os.path.join(outdir, 'prices.json')
    json.dump(payload, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description='Check 98 prices by Waze venue')
    ap.add_argument('--data', default=DEFAULT_DATA_GLOB, help='station data glob')
    ap.add_argument('--brands', nargs='*', default=None)
    ap.add_argument('--out', default=None, help='merged JSON output')
    ap.add_argument('--jsonl', default='price_check.jsonl', help='resumable per-station output')
    ap.add_argument('--publish', default=None, metavar='DIR', help='write DIR/prices.json')
    ap.add_argument('--state', default=None, help='reuse/create the anonymous account here')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--shard', default=None, help='i/n')
    ap.add_argument('--tries', type=int, default=10,
                    help='fetch attempts per venue; each is ~50%% likely to return products, '
                         'so 10 attempts is what makes the run actually complete')
    ap.add_argument('--sleep', type=float, default=0.3)
    ap.add_argument('--recheck-empty', action='store_true',
                    help='re-check only stations whose existing row has no prices')
    ap.add_argument('--only-current', action='store_true',
                    help='publish only 98 prices reported in the current price period')
    ap.add_argument('--max-age-hours', type=float, default=12.0, dest='max_age_hours',
                    help='re-check stations whose last row is older than this (0 = never skip)')
    ap.add_argument('--keep-jsonl-history', action='store_true',
                    help='do not compact duplicate rows out of the jsonl at the end')
    ap.add_argument('--min-prices', type=int, default=5,
                    help='exit non-zero if fewer than this many stations yield prices (CI guard)')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args()

    stations = load_stations(a.data, a.brands)
    unreachable = venue_less_rows(load_stations(a.data, a.brands, require_venue=False))
    done, stale = {}, 0
    if os.path.exists(a.jsonl):
        cutoff = (datetime.now(timezone.utc).timestamp()
                  - a.max_age_hours * 3600) if a.max_age_hours else None
        for line in open(a.jsonl, encoding='utf-8'):
            try:
                r = json.loads(line)
            except Exception:                                       # noqa: BLE001
                continue
            if cutoff is not None:
                try:
                    when = datetime.fromisoformat(r['checked_at']).timestamp()
                except Exception:                                   # noqa: BLE001
                    when = 0
                if when < cutoff:
                    stale += 1
                    continue        # cached row is too old: re-check the station
            done[r['key']] = r
    if a.recheck_empty:
        done = {k: r for k, r in done.items() if r.get('prices')}     # forget the empty rows
    todo = [s for s in stations if s['key'] not in done]
    if a.shard:
        i, n = (int(x) for x in a.shard.split('/'))
        todo = todo[i::n]
    if a.limit:
        todo = todo[:a.limit]
    print(f'[{ts()}] {len(stations)} stations with a venue id, {len(done)} fresh rows reused'
          + (f', {stale} stale rows to re-check' if stale else '')
          + f', {len(todo)} to do', flush=True)

    checker = PriceChecker(state=a.state, sleep=a.sleep, tries=a.tries, verbose=a.verbose)
    fh = open(a.jsonl, 'a', encoding='utf-8')
    t0 = time.time()
    for i, st in enumerate(todo, 1):
        row = checker.check(st)
        done[row['key']] = row
        fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        fh.flush()
        prices = ' '.join(f'{k}={v}' for k, v in row['prices'].items())
        flag = ''
        if row.get('venue_mismatch'):
            flag = f"  [venue mismatch: matched {row['matched_venue_id']}]"
        state = (f"98 {row['price98']} ({row['price98_label']})" if row['price98']
                 else 'prices' if row['prices'] else 'no price reported')
        print(f"[{i}/{len(todo)}] {row['brand']}/{row['name']} -> {state} {prices}{flag}",
              flush=True)
        time.sleep(a.sleep)
    fh.close()

    rows = list(done.values())
    if not a.keep_jsonl_history and os.path.exists(a.jsonl):
        with open(a.jsonl + '.tmp', 'w', encoding='utf-8') as tmp:
            for r in rows:
                tmp.write(json.dumps(r, ensure_ascii=False) + '\n')
        os.replace(a.jsonl + '.tmp', a.jsonl)          # dedup by key, keep the newest
    priced = [r for r in rows if r['prices']]
    with98 = [r for r in rows if r['price98']]
    print(f"\n[{ts()}] checked {len(rows)} stations in {(time.time()-t0)/60:.1f} min")
    print(f'  with any price : {len(priced)}/{len(rows)} ({len(priced)/max(1,len(rows)):.0%})')
    print(f'  with a 98 price: {len(with98)}/{len(rows)}')
    if with98:
        vals = sorted(r['price98'] for r in with98)
        cur = [r for r in with98 if r.get('price98_current_period')]
        stale = [r for r in with98 if r.get('price98_stale')]
        ages = sorted(r['price98_age_days'] for r in with98 if r.get('price98_age_days') is not None)
        print(f'  98 range       : {vals[0]} - {vals[-1]} ₪/l')
        print(f'  98 period      : {len(cur)} reported in the current month, {len(stale)} older '
              f'(median age {ages[len(ages)//2] if ages else "-"} d)')
    if checker.errors:
        print(f'  errors         : {len(checker.errors)} (first: {checker.errors[0][:120]})')
    if a.out:
        json.dump({'generated': datetime.now(timezone.utc).isoformat(),
                   'counts': {'checked': len(rows), 'with_prices': len(priced),
                              'with_98': len(with98), 'no_venue': len(unreachable)},
                   'stations': rows + unreachable}, open(a.out, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        print('  wrote', a.out)
    if a.publish:
        # IMPORTANT: publish only this run's stations. With --shard, three jobs would otherwise
        # each rewrite the file from their own (partial) view of the shared jsonl and the last
        # writer would win - which silently published 24 stations instead of 60.
        run_rows = [done[s['key']] for s in todo if s['key'] in done]
        path = publish(run_rows + unreachable, a.publish, only_current=a.only_current)
        print(f'  wrote {path} ({len(run_rows)} stations'
              + (f", shard {a.shard}" if a.shard else '') + ')')

    if len(priced) < a.min_prices:
        print(f'FAIL: only {len(priced)} stations returned prices '
              f'(< --min-prices {a.min_prices}) - transport blocked or the API changed?')
        print('first errors:', checker.errors[:3])
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
