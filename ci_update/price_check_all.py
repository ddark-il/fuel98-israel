#!/usr/bin/env python3
"""Price-check every venue we collected (waze_id and google_id) through Waze's fuel layer.

For each station with a venue id, ask Waze for the venue's product list and read the 98 price
(gas_regular98 first, gas_service98 fallback). Writes ci_update/price_check.jsonl - resumable.

Rule applied by build_stations.py afterwards: for every brand except תפוז and דלק (whose station
lists come from representatives), got98 = (a 98 price was reported) and price = that price;
Tapuz/Delek stay untouched.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PRICE_CI = HERE
os.chdir(PRICE_CI)
sys.path.insert(0, PRICE_CI)
import price_check as pc                                              # noqa: E402


def main() -> int:
    st_path = os.path.join(HERE, 'stations.json')
    st_rows = json.load(open(st_path, encoding='utf-8'))
    todo = [r for r in st_rows if r.get('waze_id') or r.get('google_id')]
    out_path = os.path.join(HERE, 'price_check.jsonl')
    done = set()
    if os.path.exists(out_path):
        for line in open(out_path, encoding='utf-8'):
            try:
                done.add(json.loads(line)['ministry_id'])
            except Exception:                                         # noqa: BLE001
                pass
    todo = [r for r in todo if r['ministry_id'] not in done]
    print(f'{len(st_rows)} stations, {len(done)} checked, {len(todo)} to do', flush=True)
    checker = pc.PriceChecker(state=os.path.join(PRICE_CI, 'waze_account.json'),
                              sleep=0.1, tries=2, rounds=3, parallel=3)
    out = open(out_path, 'a', encoding='utf-8')
    for i, r in enumerate(todo, 1):
        vid = r.get('waze_id') or r.get('google_id')
        st = {'key': r['ministry_id'], 'file': 'stations.json', 'brand': r['brand'],
              'name': r['station_name'], 'lat': r['lattiude'], 'lon': r['longtitude'],
              'waze_id': vid}
        try:
            row = checker.check(st)
            rec = {'ministry_id': r['ministry_id'], 'venue_id': vid,
                   'venue_type': 'waze' if vid.startswith('venues.') else 'google',
                   'price98': row.get('price98'), 'price98_updated': row.get('price98_updated'),
                   'attempts': row.get('attempts'), 'cluster': row.get('cluster')}
        except Exception as e:                                        # noqa: BLE001
            rec = {'ministry_id': r['ministry_id'], 'venue_id': vid, 'price98': None,
                   'error': repr(e)[:120]}
        out.write(json.dumps(rec, ensure_ascii=False) + '\n')
        out.flush()
        if i % 50 == 0 or i == len(todo):
            print(f'  [{i}/{len(todo)}]', flush=True)
    out.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
