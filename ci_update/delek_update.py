#!/usr/bin/env python3
"""Delek updater - matching only.

Delek's locator publishes NO 98-octane flag, NO prices, and NO Shabbat information (its `hours`
values are 24/7, 24/5, day/time ranges; none says "closed Saturday"). So unlike Paz/Sonol/Dor
Alon, this updater contributes no flags: it matches the locator's 241 stations to database rows
(<= 250 m) and reports them, leaving got98 / price / shabbat_closure to other sources.

Writes ci_update/delek_unmatched.json for the discrepancy backlog.
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
    off = json.load(open(os.path.join(HERE, 'official', 'delek.json'), encoding='utf-8'))
    db = json.load(open(os.path.join(HERE, 'stations.json'), encoding='utf-8'))
    matched, unmatched, matches = 0, [], {}
    for s in off:
        if s.get('lat') is None:
            continue
        best, best_d = None, None
        for r in db:
            d = hav(s['lat'], s['lon'], r['lattiude'], r['longtitude'])
            if best_d is None or d < best_d:
                best, best_d = r, d
        if best is not None and best_d <= 250:
            hours = (s.get('hours') or '').strip()
            # Delek's only Shabbat signal: 24/5 or 24/6 (24 hours for 5/6 days -> closed Saturday)
            shabbat = hours.startswith('24/5') or hours.startswith('24/6')
            matches[str(best['ministry_id'])] = {'shabbat_closure': shabbat,
                                                 'dist_m': round(best_d, 1)}
        if best is None or best_d > 250:
            unmatched.append({'name': s.get('name'), 'city': s.get('city'),
                              'address': s.get('address'), 'lat': s.get('lat'), 'lon': s.get('lon'),
                              'nearest_db': (None if best is None else
                                             {'ministry_id': best['ministry_id'],
                                              'name': best['station_name'],
                                              'brand': best['brand'],
                                              'dist_m': round(best_d, 1)})})
        else:
            matched += 1
    json.dump(matches, open(os.path.join(HERE, 'delek_matches.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    json.dump(unmatched, open(os.path.join(HERE, 'delek_unmatched.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    nshab = sum(1 for m in matches.values() if m['shabbat_closure'])
    print(f'delek stations: {len(off)}, matched: {matched}, unmatched: {len(unmatched)}, '
          f'shabbat-closed (24/5, 24/6): {nshab}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
