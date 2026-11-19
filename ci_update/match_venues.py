#!/usr/bin/env python3
"""Re-match stations to Waze venues using the brands' own pins and addresses.

The ministry-pin picker (price_poc/ministry_venues.py) accepts any venue within 250 m whose
name contains the station's name, so a station named after its town can inherit a neighbour's
venue: Ten מגדל העמק was pointed at "דלק-ג'ו מגדל העמק", 201 m away. The brands publish their
own navigation pin *and* address, so this matcher searches around the brand's pin and requires
the venue's brand to agree (and, where the venue carries a street/house number, the address).

Sources (all local):
  * ci_update/official_pins.json        licence -> the brand's own pin (lat/lon/name/brand)
  * ci_update/official/<brand>.json     the brand's list, with addresses
  * ci_update/stations.json             the current database row (brand, name, waze_id)
  * ci_update/ministry_venues.jsonl     the current mapping, updated with --apply

The Waze search is the same Venues search the price pipeline uses; it returns the venue's
street/house_number/city, which is what makes address matching possible.

Usage:
  python3 match_venues.py --brand ten            # proposals only
  python3 match_venues.py --brand ten --apply    # write mapping + stations.json
  python3 match_venues.py --brand paz --suspect-only
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import waze_prices as wp                                              # noqa: E402

P = wp.P
PINS = os.path.join(HERE, 'official_pins.json')
STATIONS = os.path.join(HERE, 'stations.json')
MAPPING = os.path.join(HERE, 'ministry_venues.jsonl')

BRANDS = {
    'ten': {'file': 'ten.json', 'aliases': ('טן', 'ten')},
    'paz': {'file': 'paz.json', 'aliases': ('פז', 'paz')},
    'sonol': {'file': 'sonol.json', 'aliases': ('סונול', 'sonol')},
    'doralon': {'file': 'doralon.json', 'aliases': ('דור אלון', 'דורלון', 'dor alon')},
    'delek': {'file': 'delek.json', 'aliases': ('דלק', 'delek')},
}
ALL_ALIASES = [a for b in BRANDS.values() for a in b['aliases']]
MAX_DIST = 200.0
RADIUS = 1500


# ─────────────────────────────────────────────────────────────── geo / text helpers ──
def hav(lat1, lon1, lat2, lon2):
    R, r = 6371000.0, math.pi / 180
    x = (math.sin((lat2 - lat1) * r / 2) ** 2
         + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lon2 - lon1) * r / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def norm(s):
    s = unicodedata.normalize('NFKC', s or '')
    s = re.sub(r'[^0-9a-zA-Z\u0590-\u05FF]+', ' ', s).strip().lower()
    return re.sub(r'\s+', ' ', s)


def has_alias(name, aliases):
    n = norm(name)
    return any(norm(a) and norm(a) in n for a in aliases)


def street_tokens(address):
    """Street-ish tokens + house number from an address, ignoring filler words."""
    n = norm(address)
    n = re.sub(r'\b(רחוב|רח|street|st|כביש|אזור|תעשיה|תעשייה|מתחם|כניסה|א ת|א״ת)\b', ' ', n)
    toks = {t for t in n.split() if len(t) > 1 and not t.isdigit()}
    nums = {t for t in n.split() if t.isdigit()}
    return toks, nums


def addr_score(official_address, venue):
    """How well the venue's street/house/city matches the brand's published address."""
    v_street = ' '.join(str(venue.get(k) or '') for k in ('street', 'address', 'city'))
    o_toks, o_nums = street_tokens(official_address)
    v_toks, v_nums = street_tokens(v_street)
    street = len(o_toks & v_toks)
    house = len(o_nums & v_nums)
    return street + (2 if house else 0), sorted(o_toks & v_toks), sorted(o_nums & v_nums)


def one(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def num(v):
    try:
        return float(one(v))
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────── waze search ──
class Searcher:
    def __init__(self, lat, lon):
        self.c = wp.PriceClient(lat, lon)

    def near(self, lat, lon, radius=RADIUS):
        c = self.c
        c.lat, c.lon = lat, lon
        req = P.pb_int(11, 4) + P.pb_str(2, 'Venues') + P.pb_str(6, 'GAS_STATION')
        req += P.pb_str(10, f'auto=Venues&max_distance_meters={radius}') + P.pb_int(12, radius)
        req += P.pb_int(4, 50) + P.pb_bytes(3, c._user_info())
        resp = c.s.post(P.batch(c._client_info(), c._auth(),
                                P.element(**{str(P.EL_SEARCH_REQUEST): req})))
        out = []
        for el in resp.get('element', []):
            for sr in (el.get('search_response') or []):
                for group in (sr.get('display_group') or []):
                    for res in (group.get('result') or []):
                        v = (res.get('venue') or [{}])[0]
                        loc = (v.get('location') or [{}])[0]
                        y, x = num(loc.get('y')), num(loc.get('x'))
                        if y is None or x is None:
                            continue
                        out.append({
                            'venue_id': one(v.get('venue_id')), 'name': one(v.get('name')),
                            'brand': one(v.get('brand')), 'street': one(v.get('street')),
                            'house': one(v.get('house_number')), 'city': one(v.get('city')),
                            'address': one(v.get('address')),
                            'lat': y, 'lon': x, 'dist': hav(lat, lon, y, x),
                        })
        return out


# ───────────────────────────────────────────────────────────────────────────── matching ──
def official_address(official, pin):
    """The brand record at the pin (nearest by coordinates)."""
    best, best_d = None, float('inf')
    for rec in official:
        if not rec.get('lat') or not rec.get('lon'):
            continue
        d = hav(pin['lat'], pin['lon'], rec['lat'], rec['lon'])
        if d < best_d:
            best, best_d = rec, d
    return best


def choose(cands, brand, official_rec, station_name):
    aliases = BRANDS[brand]['aliases']
    others = [a for a in ALL_ALIASES if a not in aliases]
    scored = []
    for v in cands:
        if not v.get('venue_id'):
            continue
        if re.search(r'charge|charging|טעינה|\bev\b|עמדת',
                     f"{v.get('name')} {v.get('brand')}", re.I):
            continue                       # an EV charger is not the fuel venue
        vname = str(v.get('name') or '')
        if any(has_alias(vname, [o]) for o in others) and not has_alias(vname, aliases):
            continue                       # the venue's name is another network's station
        if not (has_alias(v.get('brand'), aliases) or has_alias(vname, aliases)):
            continue                       # the venue must name this brand
        if v['dist'] > MAX_DIST:
            continue
        a_score, a_tokens, a_nums = addr_score((official_rec or {}).get('address', ''), v)
        n_tokens = len(set(street_tokens(station_name)[0]) & street_tokens(v.get('name'))[0])
        scored.append((v, a_score, a_tokens, a_nums, n_tokens))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[1], -t[4], t[0]['dist']))
    return scored[0]


def current_ok(mapping_row, brand):
    """The current venue is already this brand, not an EV charger, and close: leave it alone.

    A pass that re-picks every station would also "improve" a good venue (Paz אשקelon דוד,
    32 m) into a generic same-brand venue 118 m away. Only a missing/other-brand/EV/remote
    venue is worth re-matching.
    """
    m = mapping_row or {}
    name = str(m.get('venue_name') or '')
    if not m.get('matched') or not name:
        return False
    if re.search(r'charge|charging|טעינה|\bev\b|עמדת', name, re.I):
        return False
    return has_alias(name, BRANDS[brand]['aliases'])


def suspect(mapping_row, station):
    """A row worth rechecking: no venue, or the venue names a different brand."""
    if not mapping_row.get('matched'):
        return True
    vn = str(mapping_row.get('venue_name') or '')
    if re.search(r'charge|charging|טעינה|\bev\b', vn, re.I):
        return True
    for b, cfg in BRANDS.items():
        if b == station['brand']:
            continue
        if has_alias(vn, cfg['aliases']):
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description='Match stations to Waze venues via brand pins/addresses')
    ap.add_argument('--brand', required=True, choices=sorted(BRANDS))
    ap.add_argument('--apply', action='store_true', help='write the mapping and stations.json')
    ap.add_argument('--suspect-only', action='store_true', help='only rows with a missing/other-brand venue')
    ap.add_argument('--recheck-ok', action='store_true', help='also re-pick venues that already look right')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--licences', default='', help='comma-separated licence numbers')
    ap.add_argument('--sleep', type=float, default=0.15)
    a = ap.parse_args()

    pins = json.load(open(PINS, encoding='utf-8'))
    stations = json.load(open(STATIONS, encoding='utf-8'))
    rows = {str(r['ministry_id']): r for r in stations}
    official = json.load(open(os.path.join(HERE, 'official', BRANDS[a.brand]['file']),
                              encoding='utf-8'))
    mapping = {}
    for line in open(MAPPING, encoding='utf-8'):
        r = json.loads(line)
        mapping[str(r['licence'])] = r

    todo = []
    for lic, pin in pins.items():
        if pin.get('brand') != a.brand or lic not in rows:
            continue
        st = rows[lic]
        if a.suspect_only and not suspect(mapping.get(lic) or {}, st):
            continue
        if not a.recheck_ok and current_ok(mapping.get(lic) or {}, a.brand):
            continue
        todo.append((lic, pin, st))
    if a.licences:
        wanted = {x.strip() for x in a.licences.split(',') if x.strip()}
        todo = [t for t in todo if t[0] in wanted]
    if a.limit:
        todo = todo[:a.limit]
    print(f'{a.brand}: {len(todo)} stations to check', flush=True)

    searcher = Searcher(todo[0][1]['lat'], todo[0][1]['lon']) if todo else None
    proposals = []
    for i, (lic, pin, st) in enumerate(todo, 1):
        try:
            cands = searcher.near(pin['lat'], pin['lon'])
        except Exception as e:                                        # noqa: BLE001
            print(f'  {lic} search failed: {e!r}', flush=True)
            continue
        rec = official_address(official, pin)
        best = choose(cands, a.brand, rec, st.get('station_name') or st.get('name') or '')
        cur = st.get('waze_id')
        if not best:
            print(f'  {lic} {st["station_name"][:22]:<22} no matching venue within '
                  f'{MAX_DIST:.0f} m (cur {cur})', flush=True)
            continue
        v, a_score, a_tokens, a_nums, n_tokens = best
        if v['venue_id'] == cur:
            continue
        proposals.append({'lic': lic, 'station': st, 'venue': v, 'addr_score': a_score,
                          'addr_tokens': a_tokens, 'addr_nums': a_nums, 'pin': pin,
                          'official': rec})
        print(f'  FIX {lic} {st["station_name"][:20]:<20} {v["dist"]:4.0f} m  '
              f'{str(v["name"])[:22]:<22} street={v["street"]!r} {v["house"]!r} '
              f'addr_match={a_tokens}{a_nums}', flush=True)
        time.sleep(a.sleep)

    print(f'\n{len(proposals)} proposals for {a.brand}')
    if not a.apply:
        print('(dry run - pass --apply to write)')
        return
    for p in proposals:
        lic, st, v = p['lic'], p['station'], p['venue']
        cur = st.get('waze_id')
        st['waze_id'] = v['venue_id']
        m = mapping.get(lic) or {}
        m.update({
            'matched': True, 'venue_id': v['venue_id'], 'venue_name': v['name'],
            'venue_lat': v['lat'], 'venue_lon': v['lon'], 'dist_m': round(v['dist'], 1),
            'name_match': has_alias(v['name'], [p['station'].get('station_name') or '']),
            'brand_match': has_alias(v['name'], BRANDS[a.brand]['aliases'])
                           or has_alias(v['brand'], BRANDS[a.brand]['aliases']),
            'status': 'official-pin', 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'match_note': (f'brand pin + address; old venue {cur}; '
                           f'address tokens {p["addr_tokens"]} {p["addr_nums"]}'),
        })
        mapping[lic] = m
    with open(MAPPING, 'w', encoding='utf-8') as f:
        for lic, m in mapping.items():
            f.write(json.dumps(m, ensure_ascii=False) + '\n')
    json.dump(stations, open(STATIONS, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'wrote {MAPPING} and {STATIONS}')


if __name__ == '__main__':
    raise SystemExit(main())
