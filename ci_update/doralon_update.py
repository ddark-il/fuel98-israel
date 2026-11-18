#!/usr/bin/env python3
"""Dor Alon updater - same shape as Paz/Sonol.

From ci_update/official/doralon.json (the locator: 270 stations, each with a 98 flag, services and
opening hours) provide only:
  * got98           - the locator's 98 flag
  * shabbat_closure - opening_hours["שבת"] is "סגור"
No price is written (the locator publishes none).

Matches each Dor Alon station to a database row by coordinate proximity (<= 250 m) and writes
ci_update/doralon_matches.json; unmatched stations go to doralon_unmatched.json.
"""
from __future__ import annotations

import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def hav(a, b, c, d):
    r = math.radians
    R = 6371000
    x = (math.sin((r(c) - r(a)) / 2) ** 2
         + math.cos(r(a)) * math.cos(r(c)) * math.sin((r(d) - r(b)) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def main() -> int:
    off = json.load(open(os.path.join(HERE, 'official', 'doralon.json'), encoding='utf-8'))
    db = json.load(open(os.path.join(HERE, 'stations.json'), encoding='utf-8'))
    matches, unmatched = {}, []
    n98 = nshab = 0
    for s in off:
        if s.get('lat') is None:
            continue
        best, best_d = None, None
        for r in db:
            d = hav(s['lat'], s['lon'], r['lattiude'], r['longtitude'])
            if best_d is None or d < best_d:
                best, best_d = r, d
        if best is None or best_d > 250:
            unmatched.append({'name': s.get('name'), 'address': s.get('address'),
                              'lat': s.get('lat'), 'lon': s.get('lon'),
                              'has98': s.get('has98'),
                              'nearest_db': (None if best is None else
                                             {'ministry_id': best['ministry_id'],
                                              'name': best['station_name'],
                                              'brand': best['brand'],
                                              'dist_m': round(best_d, 1)})})
            continue
        hours = s.get('opening_hours') or {}
        shabbat = 'סגור' in str(hours.get('שבת') or '')
        rec = {'got98': s.get('has98') is True, 'shabbat_closure': shabbat,
               'dist_m': round(best_d, 1)}
        n98 += rec['got98']
        nshab += shabbat
        matches[str(best['ministry_id'])] = rec
    json.dump(matches, open(os.path.join(HERE, 'doralon_matches.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    json.dump(unmatched, open(os.path.join(HERE, 'doralon_unmatched.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f'dor alon stations: {len(off)}, matched: {len(matches)}, unmatched: {len(unmatched)}, '
          f'with 98: {n98}, shabbat-closed: {nshab}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
