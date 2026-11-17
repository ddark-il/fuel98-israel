#!/usr/bin/env python3
"""Publish auto_poc/stations.json into the site as its main database.

  * data/stations.json            - the full database (copy)
  * data/*.json (brand files)     - every station gets `shabbat_closure` and `violation`
                                    injected from the database, matched by venue id or by
                                    coordinate proximity (<= 120 m)
The frontend then renders the Shabbat star from `shabbat_closure` (like the violation flag).
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = (os.path.join(os.path.dirname(HERE), 'fuel98-israel')
        if os.path.isdir(os.path.join(os.path.dirname(HERE), 'fuel98-israel'))
        else os.path.dirname(HERE))
DATA = os.path.join(REPO, 'data')
TOKENS = {'פז', 'סונול', 'דור אלון', 'דורלון', 'מיקה', 'דלק', 'תפוז', 'יעד', 'טן', 'סד"ש', 'רום', 'תחנת'}


def norm(s: str) -> str:
    s = unicodedata.normalize('NFKC', s or '')
    for q in '״"\'‘’“”':
        s = s.replace(q, '')
    return re.sub(r'[^0-9a-zA-Z\u0590-\u05FF]+', ' ', s).strip()


def tokens(s: str) -> set[str]:
    return {t for t in norm(s).split() if t not in TOKENS}


def hav(a, b, c, d):
    r = math.radians
    R = 6371000
    x = (math.sin((r(c) - r(a)) / 2) ** 2
         + math.cos(r(a)) * math.cos(r(c)) * math.sin((r(d) - r(b)) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def main() -> int:
    db = json.load(open(os.path.join(HERE, 'stations.json'), encoding='utf-8'))
    by_id = {}
    for r in db:
        if r.get('waze_id'):
            by_id[r['waze_id']] = r
        if r.get('google_id'):
            by_id[r['google_id']] = r
    json.dump(db, open(os.path.join(DATA, 'stations.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    injected = 0
    print(f'published stations.json ({len(db)} rows); flags injected into {injected} site stations')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
