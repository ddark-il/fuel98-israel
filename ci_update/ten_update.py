#!/usr/bin/env python3
"""Ten updater - Ten sells no 98 octane, so every matched station is marked got98 = false.

Source: ci_update/official/ten.json (76 stations from Ten's website API, with GPS + opening
hours). No prices are published per station in a usable form.

Matches each Ten station to a database row by coordinate proximity (<= 250 m) and writes
ci_update/ten_matches.json {ministry_id: {got98: false, dist_m}}; the builder applies it to טן rows.
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
    off = json.load(open(os.path.join(HERE, 'official', 'ten.json'), encoding='utf-8'))
    db = json.load(open(os.path.join(HERE, 'stations.json'), encoding='utf-8'))
    matches, unmatched = {}, []
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
                              'nearest_db': (None if best is None else
                                             {'ministry_id': best['ministry_id'],
                                              'name': best['station_name'],
                                              'brand': best['brand'],
                                              'dist_m': round(best_d, 1)})})
        else:
            matches[str(best['ministry_id'])] = {
                'got98': False, 'shabbat_closure': s.get('shabbat_closed') is True,
                'dist_m': round(best_d, 1)}
    json.dump(matches, open(os.path.join(HERE, 'ten_matches.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    json.dump(unmatched, open(os.path.join(HERE, 'ten_unmatched.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    nshab = sum(1 for m in matches.values() if m.get('shabbat_closure'))
    print(f'ten stations: {len(off)}, matched: {len(matches)}, unmatched: {len(unmatched)} '
          f'(all got98=false, shabbat-closed: {nshab})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
