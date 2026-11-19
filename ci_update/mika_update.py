#!/usr/bin/env python3
"""Refresh Mika's contribution to the stations database from mika.org.il.

For every station page (from the WordPress API):
  * brand      - verified from the brand logo on https://mika.org.il/כל-המתחמים/
                 (sonol-4.png / dor_alon-6.png / mika-21.png)
  * shabbat    - `activity-hours` term 16 ("שומרת שבת") or the page text says so
  * price98    - parsed from the page's fuel list; missing / page not loading -> none
  * price95    - the operator's self-service 95 price from the same list (בנזין 95)
  * priceDiesel- the operator's standard diesel price from the same list (סולר)
  * coords     - from the page's map link (resolved/cached)

Then matches each page to a database row by coordinates (<= 250 m) and writes
ci_update/mika_matches.json {ministry_id: {brand, price98, price95, priceDiesel, got98,
shabbat_closure, slug}}.
build_stations.py applies: brand, price, price95, got98 (page has a 98 price), shabbat_closure.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mika_prices as mp                                              # noqa: E402
from datetime import datetime, timezone
import html as _html                                 # noqa: E402

LIST_API = 'https://mika.org.il/wp-json/wp/v2/station?per_page=100&_fields=id,slug,title,modified,activity-hours'
ALL_PAGE = 'https://mika.org.il/%D7%9B%D7%9C-%D7%94%D7%9E%D7%AA%D7%97%D7%9E%D7%99%D7%9D/'
BRAND_BY_LOGO = {'sonol': 'סונול', 'dor_alon': 'דור אלון', 'dor-alon': 'דור אלון',
                 'mika': 'מיקה', 'delek': 'דלק', 'paz': 'פז', 'ten': 'טן'}


def logo_brand(html: str) -> str | None:
    m = re.search(r'uploads/([a-z_\-]+?)(?:-\d+)?\.(?:png|jpg|svg|webp)', html)
    if not m:
        return None
    key = m.group(1).lower()
    for k, b in BRAND_BY_LOGO.items():
        if k in key:
            return b
    return None


def hav(a, b, c, d):
    import math
    r = math.radians
    R = 6371000
    x = (math.sin((r(c) - r(a)) / 2) ** 2
         + math.cos(r(a)) * math.cos(r(c)) * math.sin((r(d) - r(b)) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def main() -> int:
    # The list endpoint intermittently answers 503; treating that as "0 stations" wipes every
    # Mika 98 station from the database (2026-11-19), so retry with a backoff and, if it still
    # fails, exit as a soft block without overwriting mika_matches.json.
    stations, st = [], 0
    for delay in (0, 5, 30, 90, 180):
        if delay:
            time.sleep(delay)
        st, body, _ = mp.fetch(LIST_API)
        if st == 200:
            try:
                stations = json.loads(body)
            except Exception:                                     # noqa: BLE001
                stations = []
        if stations:
            break
    if not stations:
        print(f'Mika station list unavailable (HTTP {st}) - leaving the previous matches',
              flush=True)
        return 4
    print(f'{len(stations)} Mika stations', flush=True)
    st, allpage, _ = mp.fetch(ALL_PAGE)
    # slug -> brand logo from the card that links to it
    cards = {}
    for seg in allpage.split('<a ')[1:]:
        m = re.search(r'href="[^"]*?/station/([^/"]+)/?"', seg)
        if not m:
            continue
        slug = m.group(1)
        b = logo_brand(seg[:seg.find('</a>') + 4])
        if b:
            cards[slug] = b
    print(f'{len(cards)} cards with a brand logo on the all-page', flush=True)
    sc = mp.MikaScraper(sleep=0.2)
    out = []
    for i, s in enumerate(stations, 1):
        slug = s['slug']
        link = f'https://mika.org.il/station/{slug}'
        title = _html.unescape(re.sub(r'<[^>]+>', '', (s.get('title') or {}).get('rendered', '')).strip())
        rec = {'slug': slug, 'title': title, 'brand': cards.get(slug) or mp.page_brand(title),
               'price98': None, 'price95': None, 'priceDiesel': None,
               'shabbat_closure': False, 'loaded': False,
               'http_status': None, 'lat': None, 'lon': None,
               'modified': s.get('modified'), 'fetched_at': None}
        status, page, _ = mp.fetch(link)
        rec['http_status'] = status
        rec['fetched_at'] = datetime.now(timezone.utc).isoformat()
        blocked = status == 200 and ('just a moment' in page[:2000].lower()
                                     or 'imunify' in page[:2000].lower())
        if status == 200 and not blocked:
            rec['loaded'] = True
            prices = mp.parse_prices(page)
            rec['price98'] = (prices.get('98') or {}).get('price')
            # Mika's plain "בנזין 95" is the self-service grade (the serviced one is parsed as
            # 95_service), so this is the like-for-like pair with Waze's gas_regularself.
            rec['price95'] = (prices.get('95') or {}).get('price')
            rec['priceDiesel'] = (prices.get('diesel') or {}).get('price')
            hours_ids = s.get('activity-hours') or []
            rec['shabbat_closure'] = (16 in hours_ids) or ('שומרת שבת' in page)
            mm = mp.MAP_RE.search(page)
            if mm:
                try:
                    lat, lon = sc.coordinates(mm.group(0))
                    rec['lat'], rec['lon'] = lat, lon
                except Exception:                                     # noqa: BLE001
                    pass
        else:
            # page not loading (or a bot challenge): no price, not 98; build_stations carries
            # the previous row for this licence instead of treating it as a real removal
            rec['price98'] = None
            rec['price95'] = None
            rec['priceDiesel'] = None
            rec['shabbat_closure'] = False
        out.append(rec)
        if i % 10 == 0 or i == len(stations):
            print(f'  [{i}/{len(stations)}]', flush=True)
    sc.save_cache()
    json.dump(out, open(os.path.join(HERE, 'mika_update.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    # match pages to database rows: by coordinates when the page has a map link, else by the
    # station-name tokens from the page title (brand prefix stripped) + the logo brand
    import re as _re
    db = json.load(open(os.path.join(HERE, 'stations.json'), encoding='utf-8'))
    BRAND_WORDS = {'מיקה', 'סונול', 'דור אלון', 'דורלון', 'פז', 'דלק', 'תפוז', 'יעד', 'טן'}

    def toks(s):
        s = _re.sub(r'[^0-9a-zA-Z\u0590-\u05FF ]+', ' ', s or '')
        return {t for t in s.split() if t not in BRAND_WORDS and len(t) > 1}

    try:
        _rows = {str(x['licence']): x for x in mp.load_registry()}
        reg = {k: (x.get('company') or '') for k, x in _rows.items()}
        reg_addr = {k: (x.get('address') or '') for k, x in _rows.items()}
    except Exception:                                             # noqa: BLE001
        reg, reg_addr = {}, {}

    def addr_tokens(s):
        s = _re.sub(r'[^0-9a-zA-Z\u0590-\u05FF ]+', ' ', s or '')
        # city abbreviations (פ"ת, ת"א, י"ם) are not street evidence
        return {t for t in s.split() if t not in ('פת', 'תא', 'ים', 'פ', 'ת')}


    def company_brand(c):
        c = c or ''
        if 'מיקה' in c:
            return 'מיקה'
        if 'דור אלון' in c or 'דורלון' in c:
            return 'דורלון'
        if 'סונול' in c:
            return 'סונול'
        if 'פז' in c:
            return 'פז'
        if 'דלק' in c:
            return 'דלק'
        return None

    matches = {}
    for rec in out:
        name_part = toks((rec['title'] or '').split(',')[0])
        page_addr = addr_tokens((rec.get('title') or '').split(',', 1)[1]
                                if ',' in (rec.get('title') or '') else '')
        cands = []
        for r in db:
            d = (hav(rec['lat'], rec['lon'], r['lattiude'], r['longtitude'])
                 if rec['lat'] is not None else 1e9)
            nt = toks(r['station_name'])
            overlap = len(name_part & nt) if name_part else 0
            brand_ok = bool(rec['brand']) and (rec['brand'] == r['brand']
                                              or (rec['brand'] == 'דור אלון' and r['brand'] == 'דורלון'))
            if overlap >= 1 or (d <= 250 and rec['lat'] is not None):
                cb = company_brand(reg.get(str(r['ministry_id'])))
                addr_ok = len(page_addr & addr_tokens(reg_addr.get(str(r['ministry_id'])))) if page_addr else 0
                score = (overlap * 3 + (2 if brand_ok else 0) + (2 if d <= 250 else 0)
                         + (3 if cb and cb == rec['brand'] else 0) + addr_ok * 4)
                cands.append((score, d, r))
        if not cands:
            continue
        cands.sort(key=lambda x: (-x[0], x[1]))
        r = cands[0][2]
        matches[str(r['ministry_id'])] = {
            'slug': rec['slug'], 'brand': rec['brand'],
            'price98': rec['price98'], 'got98': rec['price98'] is not None,
            'price95': rec['price95'], 'priceDiesel': rec['priceDiesel'],
            'shabbat_closure': rec['shabbat_closure'],
            'loaded': rec.get('loaded'), 'http_status': rec.get('http_status'),
            'modified': rec.get('modified'), 'fetched_at': rec.get('fetched_at'),
            'dist_m': round(cands[0][1], 1) if rec['lat'] is not None else None}
    json.dump(matches, open(os.path.join(HERE, 'mika_matches.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f'wrote mika_update.json ({len(out)} pages) and mika_matches.json ({len(matches)} rows)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
