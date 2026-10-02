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
import http.client
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

try:                                            # tzdata may be absent on bare runners
    from zoneinfo import ZoneInfo
    IL_TZ = ZoneInfo('Asia/Jerusalem')
except Exception:                               # noqa: BLE001
    IL_TZ = timezone.utc

sys.path.insert(0, 'tools')
import importlib.util                                            # noqa: E402
_spec = importlib.util.spec_from_file_location('wp', 'waze_prices.py')
wp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wp)
P = wp.P
wl = wp.wl                                                        # waze_live module

def _default_data_glob() -> str:
    """Where the station data lives, found by looking rather than by assuming the layout.

    price-ci/ ships inside the repo next to `data/`, while the dev tree keeps this script a level
    above it - so `../data` and `../fuel98-israel/data` are both right somewhere, and one of them is
    silently empty everywhere else. An empty data dir is the worst possible failure here: every
    join comes back "nothing matched", which reads exactly like a data gap. Resolved against this
    file, not the CWD, so a workflow's `working-directory:` cannot change the answer.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in (os.path.join('..', 'data', '*.json'),
                os.path.join('..', 'fuel98-israel', 'data', '*.json'),
                os.path.join('data', '*.json')):
        p = os.path.normpath(os.path.join(here, rel))
        if glob.glob(p):
            return p
    return os.path.normpath(os.path.join(here, '..', 'data', '*.json'))

DEFAULT_DATA_GLOB = _default_data_glob()
EL_GET_REQUEST = 2064

# Stations Waze is *known* to price, checked against the app by hand (see VERIFY_IN_APP.md). Used
# as a canary: if these come back without fuel data, the feed is not serving us, and "no price"
# for the other 300 stations means nothing.
CANARY = ('אלוף שדה', 'בת שלמה', 'קוממיות')

# The rt distributor answers from one of two Waze clusters, and it is chosen **per request**, not
# per session and not by us: `realtime-frontend-prod-il-v248-*` carries the Israeli fuel prices,
# `realtime-frontend-prod-row-v189-*` (rest of world) answers with the very same venue and never a
# single `product`. Send the identical request twice and one reply can be `il` and the next `row`.
# A `row` reply is therefore not an answer about a station at all - it is the wrong backend - and
# the only correct thing to do with it is ask again.
CLUSTER_IL = '-il-'
CLUSTER_ROW = '-row-'


def reply_cluster(resp: dict) -> str:
    """Which distributor cluster answered: 'il', 'row' or '' when it did not say."""
    for el in (resp or {}).get('element', []):
        for rt in (el.get('response_timestamp') or []):
            host = str(rt.get('server_hostname') or '')
            if CLUSTER_IL in host:
                return 'il'
            if CLUSTER_ROW in host:
                return 'row'
    return ''


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


class Pin:
    """A kept-alive connection that answered from the IL cluster, with its own lock.

    http.client is not thread-safe, and only one request may be in flight per connection.
    """

    __slots__ = ('s', 'conn', 'lock')

    def __init__(self, s, conn):
        import threading
        self.s, self.conn, self.lock = s, conn, threading.Lock()


class PriceChecker:
    def __init__(self, state: str | None = None, sleep: float = 0.3, tries: int = 4,
                 verbose: bool = False, rounds: int = 3, parallel: int = 1):
        self.sleep, self.tries, self.verbose = sleep, tries, verbose
        self.rounds = rounds            # attempts per station (see products_for)
        self.parallel = max(1, parallel)   # concurrent connections per attempt (see _burst)
        self.last_reply = None        # shape of the last reply that had no products
        self.last_cluster = ''        # cluster that served it ('il' carries the fuel prices)
        self.state = state
        self.session: wl.Session | None = None
        self.errors: list[str] = []
        self._uid: int | None = None      # cached login (see _auth_pair)
        self._auth_el: bytes | None = None

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

    def _search(self, st: dict, query: str | None, category: str | None) -> list[dict]:
        """Candidates: [{result_id, venue_id, name, lat, lon}] with a fresh result id."""
        s = self._sess()
        c = wp.PriceClient(st['lat'], st['lon'], retries=1)
        c.s = s                                    # share one session/account per run
        try:
            c.uid, c.auth_el = self._auth_pair()   # one login for the whole run, not one per call
        except Exception:                                          # noqa: BLE001
            return []
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

    def _auth_pair(self) -> tuple[int, bytes]:
        """(uid, Authenticate element) - obtained **once** and reused by every request.

        Each draw opens its own connection, but none of them needs its own login: the Authenticate
        element is built from the account's username/password, and the uid comes from a single call.
        Authenticating per request meant eight logins per station - the server starts refusing them
        (`KeyError: 'uid'` inside `_auth`, which crashed a sweep) and it doubled the traffic for no
        gain, because the login carries no information the server uses to answer a GetRequest.
        """
        if self._uid is None:
            s = self._sess()
            try:
                info = s.authenticate()
                self._uid = int(info['uid'])
            except Exception as e:                                  # noqa: BLE001
                self.errors.append(f'authenticate: {e!r}')
                self._uid = None
                raise
            acct = s.account or {}
            self._auth_el = P.element(**{str(2338): P.pb_int(1, 4) + P.pb_str(2, acct.get('username', ''))
                                         + P.pb_str(3, acct.get('password', ''))})
        return self._uid, self._auth_el

    def _conn_session(self) -> wl.Session:
        """A session for one request: the same account, its own sessionid/cookie/connection.

        One session per request on purpose - a shared one would serialise the parallel draws, and
        the session carries no state the server uses to pick a cluster (measured).
        """
        s = wl.Session()
        if self.state and os.path.exists(self.state) and s.load_account(self.state):
            return s
        return self._sess()

    def _get_over(self, s: wl.Session, conn, st: dict, rid: str | None) -> tuple[dict, str]:
        """One GetRequest over `conn` (or a fresh connection when conn is None)."""
        c = wp.PriceClient(st['lat'], st['lon'], retries=1)
        c.s = s
        try:
            c.uid, c.auth_el = self._auth_pair()
        except Exception:                                           # noqa: BLE001
            return {}, ''
        req = P.pb_str(3, st['waze_id']) + (P.pb_str(1, rid) if rid else b'')
        req += P.pb_bytes(2, c._user_info()) + P.pb_bool(6, True)
        batch = P.batch(c._client_info(), c._auth(), P.element(**{str(EL_GET_REQUEST): req}))
        resp = s.post_keepalive(conn, batch) if conn is not None else s.post(batch)
        venue = wp._first_venue(resp)
        return (wp._products(venue) if venue else {}), reply_cluster(resp)

    def _one_request(self, st: dict, rid: str | None) -> tuple[dict, str]:
        """One GetRequest on its own connection, from a fresh session on the shared account.

        A fresh connection per request, because the edge routes **each request** independently: ten
        rapid requests over one kept-alive connection came back nine times from `row` and once from
        `il`, and two runs of ten *different* connections gave the same spread. There is no
        connection affinity to exploit and nothing to pin - the only thing that helps is making more
        draws, which is why they are made in parallel (`--parallel`) rather than one after another.
        """
        s = self._conn_session()
        conn = http.client.HTTPSConnection('rt.waze.com', timeout=45)
        try:
            return self._get_over(s, conn, st, rid)
        finally:
            conn.close()

    def _burst(self, st: dict, rid: str | None, n: int) -> list[tuple[dict, str]]:
        """`n` independent draws for one station, fired at once."""
        if n <= 1:
            return [self._one_request(st, rid)]
        with ThreadPoolExecutor(max_workers=n) as ex:
            return list(ex.map(lambda i: self._one_request(st, rid if i % 4 != 3 else None),
                               range(n)))

    def products_for(self, st: dict, rid: str | None, vid: str | None,
                     rounds: int) -> tuple[dict, str | None, int, str]:
        """One station's prices, from the first reply the IL cluster sends.

        The fuel list is not intermittent data, it is a different backend. The distributor's edge
        routes **each request** to one of two Waze clusters: `realtime-frontend-prod-il-*` answers a
        GetRequest with the venue and its `product` list, `realtime-frontend-prod-row-*` (rest of
        world) answers with the same venue and no products at all. Ten requests over a single
        kept-alive connection split 9 `row` / 1 `il`, and so did ten separate connections - the
        choice is per request and not ours to influence: no URL parameter changes it (`env=il`,
        `ilil`, `ilrow`, `row` all behave the same), a real-device client identity does not change
        it, and an Israeli address only shifts the odds (some 3-10% of requests when quiet, versus
        0 in 64 from a GitHub runner).

        So the lever is simply to draw again: `--parallel` requests at once, and a `row` reply is
        discarded rather than read as "no price". A reply from the `il` cluster settles the station
        either way - with products that is the price, without products nobody has reported one.

        Returns (products, venue_id the price came from, attempts used, cluster of the verdict).
        """
        used, cluster = 0, ''
        for _ in range(max(1, math.ceil(max(1, rounds) / self.parallel))):
            n = min(self.parallel, max(1, rounds) - used)
            replies = self._burst(st, rid, n)
            used += n
            for prods, cl in replies:
                if prods:
                    return prods, st['waze_id'], used, cl
                cluster = cl or cluster
                if cl == 'il':
                    # the cluster that carries prices answered and has nothing for this station
                    return {}, None, used, 'il'
            if vid and vid != st['waze_id'] and used < max(1, rounds):
                # the other representation of the same station (our id is often the Google-backed
                # one, the search returns the Waze-native id)
                prods, cl = self._one_request(st, vid)
                used += 1
                if prods:
                    return prods, vid, used, cl
                if cl == 'il':
                    return {}, None, used, cl
            time.sleep(self.sleep)
        return {}, None, used, cluster

    def wait_for_il(self, stations: list[dict], probes: int = 6, gap: float = 25.0) -> bool:
        """Wait until the IL cluster answers, before spending a sweep on a cold window.

        The cluster is handed out as a lottery: measured on one Pelephone line, 0 in 256 draws while
        a sweep was hammering and 3-12% when idle, and there are minutes when nearly every draw
        lands on `il`. Requests made in a cold window are wasted - a station gets its 24 draws, all
        from `row`, and the row is a non-answer - so a sweep should start when the lottery is warm.
        This asks a canary station and waits, a few times, until an IL reply comes back.
        """
        st = next((s for s in stations if CANARY[0] in s['name']), None) or (stations or [None])[0]
        if st is None:
            return False
        for i in range(max(1, probes)):
            rid, vid, _d, _n = self.find_result_id(st)
            prods, _src, used, cluster = self.products_for(st, rid, vid, self.parallel * 2)
            if prods or cluster == 'il':
                print(f'[{ts()}] IL cluster is answering (after {i + 1} probe(s), {used} draws) - '
                      f'starting the sweep', flush=True)
                return True
            if i + 1 < probes:
                print(f'[{ts()}] no IL reply yet ({i + 1}/{probes}); waiting {gap:.0f}s', flush=True)
                time.sleep(gap)
        print(f'[{ts()}] the IL cluster did not answer {probes} probe(s) in a row. Continuing, but '
              f'every "no price" in this run is a non-answer - consider re-running later.', flush=True)
        return False

    def canary(self, stations: list[dict], names: tuple = CANARY) -> list[dict]:
        """Probe the stations Waze is known to price, so a silent feed is not read as an empty one.

        Absence of a 98 price is normal here (it is a community report nobody may have filed), so
        `0 prices` means nothing on its own - until you have asked a station that definitely has a
        price today. These names carry one, verified against the app by hand (VERIFY_IN_APP.md); if
        they come back stripped, then the run - not the data - is the problem.
        """
        out = []
        for nm in names:
            m = [s for s in stations if nm in s['name']]
            if not m:
                continue
            st = m[0]
            rid, vid, _d, _n = self.find_result_id(st)
            prods, _src, attempts, cluster = self.products_for(st, rid, vid, self.rounds)
            pid, entry = wp.best_98(prods)
            out.append({'name': f"{st['brand']}/{st['name']}", 'products': len(prods),
                        'price98': entry['price'] if pid else None, 'attempts': attempts,
                        'cluster': cluster or None})
        return out

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
        prods, src, attempts, cluster = self.products_for(st, rid, vid, self.rounds)
        row['price_attempts'] = attempts
        # Which backend answered for this station. 'row' at the end of the budget means we never
        # reached the cluster that carries prices, so this row is a non-answer, not a "no 98".
        row['reply_cluster'] = cluster or None
        if src and src != st['waze_id']:
            row['price_source_venue_id'] = src
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
        else:
            # What Waze actually sent when it had no price (see describe_response). "venue with no
            # fuel data" and "no reply for the GetRequest at all" are different failures with
            # different fixes, and a run from abroad cannot tell them apart any other way.
            row['no_price_reply'] = self.last_reply
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
    ap.add_argument('--wait-for-il', type=float, default=25.0, dest='wait_for_il',
                    help='seconds between canary probes while waiting for the IL cluster to answer '
                         '(0 disables the wait)')
    ap.add_argument('--require-il', action=argparse.BooleanOptionalAction, default=True,
                    dest='require_il',
                    help='stop instead of sweeping when the IL cluster never answers (a sweep in a '
                         'cold window produces only non-answers, at full cost)')
    ap.add_argument('--wait-probes', type=int, default=6, dest='wait_probes',
                    help='how many times to probe while waiting for the IL cluster')
    ap.add_argument('--base', default=None,
                    help='distributor endpoint; default is wl.BASE, the Israeli regional proxy '
                         '(rtproxy-il.waze.com) - the world endpoint rt.waze.com only reaches the '
                         'price-carrying IL cluster by luck')
    ap.add_argument('--parallel', type=int, default=8,
                    help='requests fired at once per station: the edge picks the cluster per request '
                         '(il carries fuel prices, row never does), so the pool has to be sampled')
    ap.add_argument('--rounds', type=int, default=24,
                    help='GetRequest draws per station, until the IL cluster answers; a reply from '
                         'the row cluster is the wrong backend and is retried, never read as a '
                         '"no price"')
    ap.add_argument('--tries', type=int, default=4,
                    help='identical GetRequest attempts inside one round. The priced view is not '
                         'chosen per request but per few minutes (measured: 12/12 attempts worked, '
                         'then 0/5 minutes later), so hammering one request is mostly transport '
                         'resilience - --rounds, which re-searches, is what actually recovers prices')
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
    if not stations:
        # A wrong --data is indistinguishable from "Waze has nothing": both end with zero rows, so
        # the pipeline would publish an empty prices.json and the site would lose every price.
        print(f"ERROR: no stations loaded from {a.data!r} - nothing to check, and publishing that "
              f"would look like a data gap. Fix the path.", flush=True)
        return 3
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

    if a.base:
        wl.BASE = a.base
    print(f'[{ts()}] distributor: {wl.BASE}', flush=True)
    checker = PriceChecker(state=a.state, sleep=a.sleep, tries=a.tries, verbose=a.verbose,
                           rounds=a.rounds, parallel=a.parallel)
    if a.wait_for_il and a.wait_probes and stations:
        if not checker.wait_for_il(stations, probes=a.wait_probes, gap=a.wait_for_il) and a.require_il:
            # Nothing below this point would be a price, and a full sweep of non-answers costs the
            # same as a real one. Say so once and let the caller decide when to try again.
            print(f'[{ts()}] stopping: no IL cluster from this network right now '
                  f'({"no Waze layer this run" if not a.out else "no output written"})', flush=True)
            return 3

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

    can = checker.canary(stations)
    if can:
        got = sum(1 for c in can if c['price98'])
        print(f"[{ts()}] canary (stations Waze is known to price): "
              + ', '.join(f"{c['name']} 98={c['price98'] if c['price98'] else '-'}"
                          f" [{c['products']} products, {c['attempts']} rounds]" for c in can),
              flush=True)
        if not got:
            print('  every canary came back stripped: Waze answered with venue cards but no fuel '
                  'data. Every "no price reported" below is then the feed\'s answer, not the '
                  'station\'s - do not read this run as "these stations have no 98".', flush=True)

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
    il_rows = [r for r in rows if r.get('reply_cluster') == 'il']
    row_only = [r for r in rows if r.get('reply_cluster') == 'row']
    no_cluster = [r for r in rows if r.get('reply_cluster') == 'none']
    note = ''
    if row_only:
        note += f', {len(row_only)} got only the row cluster (their "no price" is a non-answer)'
    if no_cluster:
        note += (f', {len(no_cluster)} could not be checked at all - no IL connection was available '
                 f'from this network')
    print(f'  backend        : {len(il_rows)}/{len(rows)} answered by the IL cluster' + note)
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
                   'canary': can,
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
              f'(< --min-prices {a.min_prices}) - transport blocked, or the feed is answering '
              f'without fuel data (see the canary above)?')
        print('first errors:', checker.errors[:3])
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
