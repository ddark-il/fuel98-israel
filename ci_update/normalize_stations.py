#!/usr/bin/env python3
"""Re-apply the station-database invariants after a merge.

Two CI runs can publish `stations.json` at the same time, and the publish job's pull/merge is
line-based. A row can therefore end up with `got98` from one version and `price` from the other —
observed on 2026-11-19: בל - זנוח בע"מ had `got98: true` with `price: null`, so the 98 page
listed it under "אחר" with a dash.

The build enforces two rules that the merge can break:
  * an independent ("אחר": anything outside the seven networks) is a 98 station only when it
    has a 98 price;
  * a station that does not sell 98 carries no 98 price.

Run after any pull/merge, before the data commit is pushed. It only touches the two fields and
is a no-op on a consistent database.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATIONS = os.path.join(HERE, 'stations.json')
NETWORKS = {'פז', 'סונול', 'דורלון', 'דלק', 'מיקה', 'תפוז', 'טן'}


def main() -> int:
    rows = json.load(open(STATIONS, encoding='utf-8'))
    fixed_flag = fixed_price = 0
    for r in rows:
        if r.get('brand') not in NETWORKS and r.get('got98') and r.get('price') is None:
            r['got98'] = False
            fixed_flag += 1
        if not r.get('got98') and r.get('price') is not None:
            r['price'] = None
            r['price_reported'] = None
            fixed_price += 1
    if fixed_flag or fixed_price:
        json.dump(rows, open(STATIONS, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f'normalised {fixed_flag} unpriced "אחר" flags and {fixed_price} prices on '
              f'non-98 rows')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
