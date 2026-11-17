#!/usr/bin/env python3
"""Fetch Ten's (טן) full station list from its website API into auto_poc/official/ten.json.

The site (10ten.co.il) is a React app; its bundle calls
POST https://10ten.co.il/website_api/website/1.0/generalDeclaration, which returns
`data.stationsArr` with title, full_address, gps{lat,lng} and opening_hours per weekday.
"""
from __future__ import annotations

import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'official', 'ten.json')
URL = 'https://10ten.co.il/website_api/website/1.0/generalDeclaration'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'


def main() -> int:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    req = urllib.request.Request(URL, data=b'x=1', method='POST',
                                 headers={'User-Agent': UA,
                                          'Content-Type': 'multipart/form-data; boundary=----x'})
    data = json.load(urllib.request.urlopen(req, timeout=30))
    stations = (data.get('data') or {}).get('stationsArr') or []
    rows = []
    for s in stations:
        gps = s.get('gps') or {}
        hours = s.get('opening_hours') or {}
        sat = hours.get('7') or hours.get('6') or {}
        sat_hours = sat.get('hoursArr') or []
        sat_text = next((str(x.get('hours_text')) for x in (s.get('display_opening_hoursArr') or [])
                         if str(x.get('order_num')) == '7'), None)
        rows.append({
            'name': s.get('title') or '',
            'address': s.get('full_address') or '',
            'lat': float(gps['lat']) if gps.get('lat') else None,
            'lon': float(gps['lng']) if gps.get('lng') else None,
            'shabbat_closed': bool(sat_text and 'סגור' in sat_text),
            'saturday_text': sat_text,
            'opening_hours': hours,
            'services': s.get('propertiesArr') or [],
        })
    json.dump(rows, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'wrote {OUT}: {len(rows)} stations; shabbat_closed={sum(r["shabbat_closed"] for r in rows)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
