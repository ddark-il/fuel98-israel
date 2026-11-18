#!/usr/bin/env python3
"""Read currently-reported Waze fuel prices (incl. 98) for Israeli stations.

The chain, all recovered from Waze 5.24.5.0 (see LIVE_ACCESS.md / PROTOCOL.md):

  1. transport   POST https://rt.waze.com/rtserver/distrib/command
                 body "ProtoBase64," + base64(Batch)   resp protobuf TextFormat
  2. auth        Element.authenticate (2338) in the same batch, else "UID expected"
  3. find        Element.search_request (2062): SearchRequest{intent=CATEGORY_REGULAR,
                 category="GAS_STATION", url_params="auto=Venues&max_distance_meters=…",
                 user_info{location,…}}  ->  display_group.result{ id, venue{venue_id,…} }
  4. prices      Element.get_request (2064): GetRequest{id=<result id>,
                 venue_id=<provider id>, user_info, return_all_data=true}
                 ->  venue{ product{ id, last_updated, updated_by, price } }

  Waze is non-deterministic here: the same get_request returns the Google-backed venue
  (no products) roughly a third of the time, and the Waze venue with products otherwise,
  so the fetcher retries until products appear.

Fuel-type ids seen in Israel (product.id):
    gas_regular98     -> 98 self-service      <-- the one this project cares about
    gas_regular       -> 95 full service
    gas_regularself   -> 95 self service
    gas_diesel        -> diesel
`suse:` the venue's `currency` field is wrong for IL (reports "€"); Waze prices are NIS.

Usage:
    python3 waze_prices.py --lat 32.8194 --lon 34.9556            # stations near a point
    python3 waze_prices.py --lat 32.07 --lon 34.78 --out prices.json
    python3 waze_prices.py --name "סונול שער עליה" --lat .. --lon ..
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'tools'))

import importlib.util                                            # noqa: E402
_spec = importlib.util.spec_from_file_location('wl', os.path.join(_HERE, 'waze_live.py'))
wl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wl)
P = wl.poc

FUEL_LABELS = {
    'gas_regular98': '98',
    'gas_regular': '95',
    'gas_regularself': '95_self',
    'gas_diesel': 'diesel',
    'gas_diesel_service': 'diesel_service',
    'gas_service98': '98_service',
    'gas_premium': 'premium',
    'gas_lpg': 'lpg',
}
EL_GET_REQUEST = 2064
EL_SEARCH_REQUEST = P.EL_SEARCH_REQUEST


# 98 handling: Waze IL carries two 98 grades. Prefer self-service, fall back to full service.
FUEL_98_IDS = ('gas_regular98', 'gas_service98')
FUEL_98_LABELS = {'gas_regular98': '98 עצמי', 'gas_service98': '98 שרות'}


def best_98(products: dict) -> tuple[str | None, dict | None]:
    """(id, entry) for the 98 price: gas_regular98 first, else gas_service98."""
    for pid in FUEL_98_IDS:
        if products and pid in products:
            return pid, products[pid]
    return None, None


def coord(lat: float, lon: float) -> bytes:
    return P.pb_int(101, int(lon * 1e6)) + P.pb_int(102, int(lat * 1e6))


class PriceClient:
    def __init__(self, lat: float, lon: float, country: str = 'IL', retries: int = 14):
        self.lat, self.lon = lat, lon
        self.retries = retries
        self.s = wl.Session(country=country)
        self.s.load_account()
        self.uid = None
        self.auth_el = None

    def _auth(self) -> bytes:
        if self.auth_el is None:
            info = self.s.authenticate()
            self.uid = info['uid']
            u = self.s.account['username']
            pw = self.s.account['password']
            self.auth_el = P.element(**{str(2338): P.pb_int(1, 4) + P.pb_str(2, u)
                                        + P.pb_str(3, pw)})
        return self.auth_el

    def _client_info(self) -> bytes:
        return P.element(**{str(P.EL_CLIENT_INFO): self.s.client_info()
                            + P.pb_bytes(4, coord(self.lat, self.lon))})

    def _user_info(self) -> bytes:
        if self.uid is None:
            self._auth()
        return (P.pb_int(1, self.uid) + P.pb_str(3, self.s.account['username'])
                + P.pb_bytes(4, coord(self.lat, self.lon))
                + P.pb_bytes(5, coord(self.lat, self.lon))
                + P.pb_str(10, 'IL'))

    def find_stations(self, category: str = 'GAS_STATION', query: str | None = None,
                      radius: int = 50000, max_results: int = 50) -> list[dict]:
        """SearchRequest -> [{result_id, venue_id, name, lat, lon, brand}].

        Two passes, because no single one is complete:
          * category search with the `Venues` provider -> Waze-native venues
            (`venue_id: venues.*`), but sparse (empty in some cities);
          * plain text query (`תחנת דלק`, brand names) -> Google-backed venues
            (`venue_id: googlePlaces.*`), plenty of them. A get_request on either
            pair still resolves to the Waze venue that carries the prices.
        """
        queries = [query] if query else []
        out: list[dict] = []
        seen: set[str] = set()
        passes = [(category, queries[0] if queries else None)] if category else []
        passes += [(None, q) for q in queries]
        for q, cat in passes:
            for st in self._search(cat, q, radius, max_results):
                if st['venue_id'] in seen:
                    continue
                seen.add(st['venue_id'])
                out.append(st)
        return out

    def _search(self, category: str | None, query: str | None,
                radius: int, max_results: int) -> list[dict]:
        req = P.pb_int(11, 4)                                     # intent CATEGORY_REGULAR
        req += P.pb_str(2, 'Venues')        # provider: makes the Waze venue view reliable
        if query:
            req += P.pb_str(5, query)
        if category:
            req += P.pb_str(6, category)
        # NOTE: with a text query, adding max_distance_meters to url_params makes the
        # search return nothing; plain `auto=Venues` is what works (verified).
        if category:
            req += P.pb_str(10, f'auto=Venues&max_distance_meters={radius}')
            req += P.pb_int(12, radius)
        else:
            req += P.pb_str(10, 'auto=Venues')
        req += P.pb_int(4, max_results) + P.pb_bytes(3, self._user_info())
        resp = self.s.post(P.batch(self._client_info(), self._auth(),
                                   P.element(**{str(EL_SEARCH_REQUEST): req})))
        out = []
        for el in resp.get('element', []):
            for sr in (el.get('search_response') or []):
                for group in (sr.get('display_group') or []):
                    for result in (group.get('result') or []):
                        rid = _str(result.get('id'))
                        venue = (result.get('venue') or [{}])[0]
                        vid = _str(venue.get('venue_id'))
                        if not rid or not vid:
                            continue
                        loc = (venue.get('location') or [{}])[0]
                        cats = venue.get('categories') or []
                        cats = [str(c) for c in (cats if isinstance(cats, list) else [cats])]
                        out.append({
                            'result_id': rid, 'venue_id': vid,
                            'name': _str(venue.get('name')), 'brand': _str(venue.get('brand')),
                            'lat': _num(loc.get('y')), 'lon': _num(loc.get('x')),
                            'gas_station': any('GAS_STATION' in c.upper() or c.lower() == 'gas_station'
                                               for c in cats),
                        })
        return out

    def get_prices(self, result_id: str, venue_id: str) -> dict | None:
        """GetRequest{id, venue_id, return_all_data} -> {fuel_id: {...}} (retried)."""
        req = (P.pb_str(1, result_id) + P.pb_str(3, venue_id)
               + P.pb_bytes(2, self._user_info()) + P.pb_bool(6, True))
        # the google-backed / Waze-native view is chosen per request, so retry the pair,
        # then fall back to venue_id alone (sometimes resolves to the Waze venue)
        for attempt in range(self.retries):
            payload = req if attempt % 3 else (P.pb_str(3, venue_id)
                                              + P.pb_bytes(2, self._user_info())
                                              + P.pb_bool(6, True))
            resp = self.s.post(P.batch(self._client_info(), self._auth(),
                                       P.element(**{str(EL_GET_REQUEST): payload})))
            venue = _first_venue(resp)
            if venue is not None:
                prods = _products(venue)
                if prods:
                    return prods
            time.sleep(0.5)
        return None

    def station_prices(self, station: dict, requery: str | None = None,
                       cycles: int = 4) -> dict:
        """Fetch products, re-running the search between cycles.

        A get_request can keep resolving to the Google-backed view on one backend while
        the same pair returns the Waze venue (with prices) on another, so after a failed
        cycle we search again for a fresh result id and try once more.
        """
        prods = self.get_prices(station['result_id'], station['venue_id'])
        for _ in range(cycles - 1 if prods is None else 0):
            fresh = self.find_stations(query=requery or station.get('name'))
            match = next((s for s in fresh if s['venue_id'] == station['venue_id']),
                         next((s for s in fresh if s.get('name') == station.get('name')), None))
            if not match:
                break
            station = {**station, 'result_id': match['result_id']}
            prods = self.get_prices(match['result_id'], match['venue_id'])
            if prods:
                break
        out = dict(station)
        out['products'] = prods or {}
        out['prices'] = {FUEL_LABELS.get(k, k): v['price'] for k, v in (prods or {}).items()}
        out['updated'] = _iso(max((v['last_updated'] for v in (prods or {}).values()), default=None))
        out['updated_by'] = (sorted({v['updated_by'] for v in (prods or {}).values() if v['updated_by']})
                             or [None])[0]
        return out


# ------------------------------------------------------------------ helpers
def _str(v):
    if isinstance(v, list):
        v = v[0] if v else None
    return v


def _num(v):
    v = _str(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first_venue(resp: dict) -> dict | None:
    for el in resp.get('element', []):
        for sr in (el.get('search_response') or []):
            for dg in (sr.get('display_group') or []):
                for result in (dg.get('result') or []):
                    v = (result.get('venue') or [None])[0]
                    if v:
                        return v
    return None


def _products(venue: dict) -> dict:
    """venue{product{id,last_updated,updated_by,price}} -> {id: {...}}"""
    out = {}
    for prod in (venue.get('product') or []):
        pid = _str(prod.get('id'))
        if not pid:
            continue
        out[pid] = {'price': _num(prod.get('price')),
                    'last_updated': _num(prod.get('last_updated')),
                    'updated_by': _str(prod.get('updated_by'))}
    return out


def _iso(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description='Waze fuel prices (incl. 98) for IL stations')
    ap.add_argument('--lat', type=float, required=True)
    ap.add_argument('--lon', type=float, required=True)
    ap.add_argument('--name', default=None, help='optional free-text query to narrow the search')
    ap.add_argument('--radius', type=int, default=50000)
    ap.add_argument('--max-stations', type=int, default=8)
    ap.add_argument('--only-98', action='store_true', help='keep only stations with a 98 price')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()

    c = PriceClient(a.lat, a.lon)
    stations = c.find_stations(query=a.name, radius=a.radius)
    gas = [s for s in stations if s.get('gas_station')] or stations
    print(f'{len(gas)} gas stations found near {a.lat},{a.lon}'
          + (f' matching {a.name!r}' if a.name else ''))
    rows, with98 = [], 0
    for st in gas[:a.max_stations]:
        row = c.station_prices(st, requery=a.name)
        has98 = 'gas_regular98' in row['products']
        with98 += has98
        rows.append(row)
        flags = ' | '.join(f"{FUEL_LABELS.get(k, k)}={v['price']}" for k, v in row['products'].items())
        print(f"  {row['name'] or '?':<28} {row['brand'] or '':<16} {flags or '(no prices returned)'}"
              + ('  [98 ✓]' if has98 else ''))
    print(f'\n{with98}/{len(rows)} stations returned a 98 price')
    payload = {'fetched_at': datetime.now(timezone.utc).isoformat(),
               'center': {'lat': a.lat, 'lon': a.lon}, 'stations': rows}
    if a.only_98:
        payload['stations'] = [r for r in rows if 'gas_regular98' in r['products']]
    if a.out:
        json.dump(payload, open(a.out, 'w'), ensure_ascii=False, indent=1)
        print('wrote', a.out, f"({len(payload['stations'])} stations)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
