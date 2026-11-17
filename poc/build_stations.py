#!/usr/bin/env python3
"""Build auto_poc/stations.json: one record per Ministry of Energy station licence.

Fields (exactly as requested):
    ministry_id, brand, longtitude, lattiude, got98, waze_id, price, price_reported,
    updated_at, shabbat_closure, violation

Sources (all local):
  * fuel98-israel/price-ci/registry_cache.json   - the 1,253 Ministry rows
  * fuel98-israel/data/*.json                    - our 326 stations (the 98 list, pins, waze ids)
  * fuel98-israel/data/prices.json               - the published 98 price layer
  * price_poc/ministry_venues.jsonl              - Ministry pin -> Waze venue mapping
  * fuel98-israel/data/violations.json           - Ministry fuel-quality flags (by licence)
  * sonol.txt / doralon.txt / paz.txt            - brand 98 lists: services / hours ("שבת")

Every non-default value has positive evidence: got98 = the station is one of ours, a Waze 98
price exists for its venue, or its name is on a brand's 98-only list; shabbat_closure = that
brand list says the station is closed on Shabbat. Unknown stays false/null - not guessed.

Run:  python3 auto_poc/build_stations.py
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import unicodedata
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                                  # .../Delek98
REPO = (os.path.join(ROOT, 'fuel98-israel') if os.path.isdir(os.path.join(ROOT, 'fuel98-israel'))
        else ROOT)
REGISTRY = os.path.join(REPO, 'price-ci', 'registry_cache.json')
PRICES = os.path.join(REPO, 'data', 'prices.json')
VIOLATIONS = os.path.join(REPO, 'data', 'violations.json')
MAP_JSONL = (os.path.join(ROOT, 'price_poc', 'ministry_venues.jsonl')
             if os.path.exists(os.path.join(ROOT, 'price_poc', 'ministry_venues.jsonl'))
             else os.path.join(HERE, 'ministry_venues.jsonl'))
OUT = os.path.join(HERE, 'stations.json')

BRAND_WORDS = {'פז', 'סונול', 'דור אלון', 'דורלון', 'מיקה', 'דלק', 'תפוז', 'יעד', 'טן',
               'סד"ש', 'רום', 'תחנת'}
COMPANY_BRAND = [('פז', 'פז'), ('דלק', 'דלק'), ('סונול', 'סונול'),
                 ('דור אלון', 'דורלון'), ('דור-אלון', 'דורלון'), ('מיקה', 'מיקה'),
                 ('תפוז', 'תפוז'), ('יעד', 'יעד'), ('טן', 'טן'), ('סד"ש', 'סד"ש'),
                 ('רון', 'דורלון'), ('אחר', 'אחר')]


def norm(s: str) -> str:
    s = unicodedata.normalize('NFKC', s or '')
    for q in '״"\'‘’“”':
        s = s.replace(q, '')
    return re.sub(r'[^0-9a-zA-Z\u0590-\u05FF]+', ' ', s).strip()


def tokens(s: str) -> set[str]:
    return {t for t in norm(s).split() if t not in BRAND_WORDS}


def brand_of(company: str, name: str = '') -> str:
    c = (company or '').strip()
    for k, v in COMPANY_BRAND:
        if c.startswith(k):
            return v
    n = norm(name)
    for w in ('פז', 'סונול', 'דור אלון', 'דורלון', 'מיקה', 'דלק', 'תפוז', 'יעד'):
        if w in n:
            return 'דורלון' if w in ('דור אלון', 'דורלון') else w
    return 'אחר'


def hav(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def load_ours():
    out = []
    for f in glob.glob(os.path.join(REPO, 'data', '*.json')):
        base = os.path.basename(f)
        if base in ('manifest.json', 'violations.json', 'prices.json', 'stations.json'):
            continue
        for s in json.load(open(f, encoding='utf-8')):
            out.append({'brand': s['brand'], 'name': s['name'],
                        'lat': s['coordinates']['lat'], 'lon': s['coordinates']['lon'],
                        'waze_id': s.get('waze_id')})
    return out


def load_brand_lists():
    """brand -> [(station_name, closed_on_shabbat)] from the brand 98-only lists (Oct 19)."""
    lists = {}
    def lp(name):
        p = os.path.join(ROOT, name)
        return p if os.path.exists(p) else os.path.join(HERE, name)
    sonol = json.load(open(lp('sonol.txt'), encoding='utf-8'))
    lists['סונול'] = [(r.get('שם התחנה', ''),
                       any('שבת' in str(x) for x in (r.get('שירותים') or []))
                       or 'שבת' in str(r.get('שם התחנה', '')))
                      for r in sonol]
    doralon = json.load(open(lp('doralon.txt'), encoding='utf-8'))
    lists['דורלון'] = [(r.get('station_name', ''),
                        'סגור' in str((r.get('opening_hours') or {}).get('שבת', '')))
                       for r in doralon]
    paz = json.load(open(lp('paz.txt'), encoding='utf-8'))
    lists['פז'] = [(r.get('name', ''),
                    any('שבת' in str(x) for x in (r.get('services') or [])))
                   for r in paz]
    return lists


def brand_list_match(name, brand, lists):
    """(found, closed) - the brand's 98 list has a row whose name is this station (tokens)."""
    nt = tokens(name)
    if not nt:
        return False, False
    best = None
    for other, closed in lists.get(brand) or []:
        ot = tokens(other)
        if not ot:
            continue
        if ot == nt or ot <= nt or nt <= ot:
            overlap = len(ot & nt)
            if best is None or overlap > best[0]:
                best = (overlap, closed)
    return (best is not None), bool(best and best[1])



def apply_tapuz(rows):
    """Brand list is the source of truth for Tapuz: one row per data/tapuz.json station.

    The Ministry list carries several licences on a Tapuz forecourt (and stale rows), which
    produced duplicate titles. Each brand-file station takes the nearest Tapuz database row as
    its licence (preferring a venue-id match); every other Tapuz row is marked listed=False so
    the site and the SEO block show the brand list only. A station with no ministry row gets a
    synthetic id (tapuz:<name>) so it still gets a price.
    """
    taps = []
    tp = os.path.join(REPO, 'data', 'tapuz.json')
    if os.path.exists(tp):
        taps = json.load(open(tp, encoding='utf-8'))
    if not taps:
        return rows
    used, added = set(), []

    def hav(a, b, c, d):
        r = math.radians
        R = 6371000
        x = (math.sin((r(c) - r(a)) / 2) ** 2
             + math.cos(r(a)) * math.cos(r(c)) * math.sin((r(d) - r(b)) / 2) ** 2)
        return 2 * R * math.asin(math.sqrt(x))

    for st in taps:
        name = st['name']
        shabbat = 'שמרת שבת' in name or 'שומרת שבת' in name
        name = re.sub(r'\s*[-–,|]?\s*שומרת שבת\s*', ' ', name).strip()
        lat, lon = st['coordinates']['lat'], st['coordinates']['lon']
        cand, best = None, None
        for r in rows:
            if r['brand'] != 'תפוז' or r['ministry_id'] in used:
                continue
            d = hav(lat, lon, r['lattiude'], r['longtitude'])
            if d > 300:
                continue
            score = (0 if (r.get('waze_id') and r.get('waze_id') == st.get('waze_id')) else 1,
                     d, int(r['ministry_id']) if str(r['ministry_id']).isdigit() else 10**9)
            if best is None or score < best:
                best, cand = score, r
        if cand is not None:
            cand['station_name'] = name
            cand['lattiude'], cand['longtitude'] = lat, lon
            if st.get('waze_id'):
                cand['waze_id'] = st['waze_id']
            cand['listed'] = True
            if shabbat:
                cand['shabbat_closure'] = True
            used.add(cand['ministry_id'])
        else:
            rec = {k: None for k in ('price', 'price_reported', 'violation_fuel',
                                     'violation_sampling')}
            rec.update({
                'ministry_id': 'tapuz:' + re.sub(r'\s+', '-', name),
                'station_name': name, 'brand': 'תפוז', 'lattiude': lat, 'longtitude': lon,
                'waze_id': st.get('waze_id') or None, 'google_id': None,
                'got98': True, 'price': None, 'price_reported': None,
                'shabbat_closure': shabbat, 'violation': False,
                'violation_fuel': None, 'violation_sampling': None, 'listed': True})
            added.append(rec)
    for r in rows:
        if r['brand'] == 'תפוז' and r['ministry_id'] not in used:
            r['listed'] = False
    print(f'tapuz: {len(taps)} brand-list stations, {len(added)} without a ministry row')
    return rows + added

def main() -> int:
    rows = json.load(open(REGISTRY, encoding='utf-8'))
    if isinstance(rows, dict):
        rows = list(rows.values())
    ours = load_ours()
    by_venue = {s['waze_id']: s for s in ours if s.get('waze_id')}
    our_ids = {}
    for f in glob.glob(os.path.join(REPO, 'data', '*.json')):
        if os.path.basename(f) in ('manifest.json', 'violations.json', 'prices.json', 'stations.json'):
            continue
        for st0 in json.load(open(f, encoding='utf-8')):
            w = st0.get('waze_id')
            if w:
                our_ids[w] = {'brand': st0['brand'], 'name': st0['name'],
                              'lat': st0['coordinates']['lat'], 'lon': st0['coordinates']['lon'],
                              'waze_id': w}
    prices = json.load(open(PRICES, encoding='utf-8')).get('stations', {})
    name_map = {}
    nm_path = os.path.join(HERE, 'name_map_filtered.json')
    if os.path.exists(nm_path):
        for r in json.load(open(nm_path, encoding='utf-8')):
            if r.get('venue_id'):
                name_map[str(r['ministry_id'])] = r
    official_pins = {}
    op_path = os.path.join(HERE, 'official_pins.json')
    if os.path.exists(op_path):
        official_pins = json.load(open(op_path, encoding='utf-8'))
    unapplied = set()
    ua_path = os.path.join(HERE, 'unapplied.json')
    if os.path.exists(ua_path):
        unapplied = set(json.load(open(ua_path, encoding='utf-8')))
    gmaps = {}
    gm_path = os.path.join(HERE, 'gmaps_ids.json')
    if os.path.exists(gm_path):
        gmaps = json.load(open(gm_path, encoding='utf-8'))
    prev_hist = {}
    prev_doc = os.path.join(HERE, 'stations.json')
    if os.path.exists(prev_doc):
        try:
            for x in json.load(open(prev_doc, encoding='utf-8')):
                if x.get('price_history'):
                    prev_hist[str(x['ministry_id'])] = x['price_history']
        except Exception:                                         # noqa: BLE001
            prev_hist = {}
    site_prices = {}
    if os.path.exists(PRICES):
        for k, v in json.load(open(PRICES, encoding='utf-8')).get('stations', {}).items():
            if v.get('98') is not None:
                site_prices[k] = v
    prev_prices = {}
    prev_doc = os.path.join(HERE, 'stations.json')
    if os.path.exists(prev_doc):
        try:
            for x in json.load(open(prev_doc, encoding='utf-8')):
                if x.get('price') is not None:
                    prev_prices[str(x['ministry_id'])] = (x['price'], x.get('price_reported'))
        except Exception:                                         # noqa: BLE001
            prev_prices = {}
    site_prices = {}
    if os.path.exists(PRICES):
        for k, v in json.load(open(PRICES, encoding='utf-8')).get('stations', {}).items():
            if v.get('98') is not None:
                site_prices[k] = v
    price_map = {}
    pc_path = os.path.join(HERE, 'price_check.jsonl')
    if os.path.exists(pc_path):
        for line in open(pc_path, encoding='utf-8'):
            try:
                x = json.loads(line)
                price_map[str(x['ministry_id'])] = x
            except Exception:                                         # noqa: BLE001
                pass
    ten_m = {}
    tn_path = os.path.join(HERE, 'ten_matches.json')
    if os.path.exists(tn_path):
        ten_m = json.load(open(tn_path, encoding='utf-8'))
    ten_venues = {}
    tv_path = os.path.join(HERE, 'ten_venues.json')
    if os.path.exists(tv_path):
        ten_venues = json.load(open(tv_path, encoding='utf-8'))
    delek = {}
    dl_path = os.path.join(HERE, 'delek_matches.json')
    if os.path.exists(dl_path):
        delek = json.load(open(dl_path, encoding='utf-8'))
    doralon = {}
    dn_path = os.path.join(HERE, 'doralon_matches.json')
    if os.path.exists(dn_path):
        doralon = json.load(open(dn_path, encoding='utf-8'))
    sonol = {}
    sn_path = os.path.join(HERE, 'sonol_matches.json')
    if os.path.exists(sn_path):
        sonol = json.load(open(sn_path, encoding='utf-8'))
    paz = {}
    pz_path = os.path.join(HERE, 'paz_matches.json')
    if os.path.exists(pz_path):
        paz = json.load(open(pz_path, encoding='utf-8'))
    mika = {}
    mk_path = os.path.join(HERE, 'mika_matches.json')
    if os.path.exists(mk_path):
        mika = json.load(open(mk_path, encoding='utf-8'))
    google_resolve = {}
    gr_path = os.path.join(HERE, 'google_resolve.json')
    if os.path.exists(gr_path):
        google_resolve = json.load(open(gr_path, encoding='utf-8'))
    brandm = {}
    bm_path = os.path.join(HERE, 'brand_matches.json')
    if os.path.exists(bm_path):
        for r in json.load(open(bm_path, encoding='utf-8')):
            if r.get('status') == 'confirmed' and r.get('venue_id'):
                brandm[str(r['ministry_id'])] = r
    remap = {}
    remap_path = os.path.join(HERE, 'remap.jsonl')
    if os.path.exists(remap_path):
        for line in open(remap_path, encoding='utf-8'):
            r = json.loads(line)
            if r.get('venue_id') and r.get('status') == 'mapped':
                remap[str(r['ministry_id'])] = r
    mapping = {}
    if os.path.exists(MAP_JSONL):
        for line in open(MAP_JSONL, encoding='utf-8'):
            r = json.loads(line)
            mapping[str(r['licence'])] = r
    violations = json.load(open(VIOLATIONS, encoding='utf-8'))
    viol_list_path = os.path.join(HERE, 'violation_licences.json')
    viol_extra = set()
    viol_meta = {}
    if os.path.exists(viol_list_path):
        for v in json.load(open(viol_list_path, encoding='utf-8')):
            lic_v = str(v.get('licence'))
            viol_extra.add(lic_v)
            viol_meta[lic_v] = {'fuel': v.get('fuel'), 'sampling': v.get('sampling')}
    viol_lic = set()
    if isinstance(violations, list):
        for v in violations:
            for key in ('licence', 'StationNumber', 'stationNumber', 'ministry_id'):
                if v.get(key) is not None:
                    viol_lic.add(str(v[key]))
                    break
    lists = load_brand_lists()
    now = datetime.now(timezone.utc).isoformat()

    out = []
    stats = {'linked': 0, 'got98': 0, 'priced': 0, 'shabbat': 0, 'viol': 0, 'waze': 0}
    for r in rows:
        lic = str(r.get('licence'))
        lat, lon = r.get('lat'), r.get('lon')
        name = r.get('name') or ''
        company = r.get('company') or ''
        brand = brand_of(company, name)
        maprow = mapping.get(lic) or {}
        linked = None
        if maprow.get('matched') and maprow.get('venue_id'):
            linked = by_venue.get(maprow['venue_id'])
        if linked is None:
            # proximity + name/brand evidence, like the Mika join
            best, best_score = None, 0
            nt = tokens(name)
            for s in ours:
                d = hav(lat, lon, s['lat'], s['lon'])
                if d > 150:
                    continue
                overlap = len(nt & tokens(s['name']))
                score = overlap + (2 if brand == s['brand'] else 0)
                if score > best_score or (score == best_score and best is not None and
                                          d < hav(lat, lon, best['lat'], best['lon'])):
                    best, best_score = s, score
            if best is not None and best_score > 0:
                linked = best
        if linked:
            brand = linked['brand']
            # the mapping may know a venue even when our own station has no waze_id
            venue_id = linked.get('waze_id') or (maprow.get('venue_id') if maprow.get('matched') else None)
            price_rec = prices.get(venue_id) if venue_id else None
            if price_rec is None:
                price_rec = prices.get(f"pin:{linked['lat']:.6f},{linked['lon']:.6f}")
        else:
            venue_id = maprow.get('venue_id') if maprow.get('matched') else None
            price_rec = prices.get(venue_id) if venue_id else None
        # official-site crosscheck: a venue found around the brand's own pin
        rm = remap.get(lic) or {}
        if not venue_id and rm.get('venue_id'):
            venue_id = rm['venue_id']
            price_rec = prices.get(venue_id)
        if not venue_id and (name_map.get(lic) or {}).get('venue_id'):
            venue_id = name_map[lic]['venue_id']
            price_rec = prices.get(venue_id)
        if not venue_id and (brandm.get(lic) or {}).get('venue_id'):
            venue_id = brandm[lic]['venue_id']
            price_rec = prices.get(venue_id)
        if not venue_id and ten_venues.get(lic):
            venue_id = ten_venues[lic]
            price_rec = prices.get(venue_id)
        if lic in unapplied:
            venue_id = None
            price_rec = None
        # two id spaces: waze_id is the Waze-native venue, google_id the Google-backed record
        google_id = None
        gr = google_resolve.get(lic) or {}
        if venue_id and str(venue_id).startswith('googlePlaces'):
            google_id = venue_id
            venue_id = gr.get('waze_id') if gr.get('waze_id') else None
        elif venue_id:
            google_id = gr.get('google_id')
        if lic in unapplied:
            google_id = None
        if not google_id and lic not in unapplied:
            google_id = gmaps.get(lic)
        if venue_id and str(venue_id).startswith('googlePlaces'):
            google_id = google_id or venue_id
            venue_id = None
        hit = our_ids.get(venue_id) if venue_id else None
        if hit is None and google_id:
            hit = our_ids.get(google_id)
        if hit is not None:
            linked = hit
            brand = hit['brand']
        if price_rec is None and (venue_id or google_id):
            price_rec = prices.get(venue_id) if venue_id else None
            if price_rec is None and google_id:
                price_rec = prices.get(google_id)
        price_out = price_rec.get('98') if price_rec else None
        price_rep_out = price_rec.get('reported_at') if price_rec else None
        mk = mika.get(lic) or {}
        if mk:
            brand = {'דור אלון': 'דורלון'}.get(mk.get('brand') or '', mk.get('brand') or brand)
            price_out = mk.get('price98')
            price_rep_out = None
            closed_mk = bool(mk.get('shabbat_closure'))
            got98_mk = mk.get('got98') is True and price_out is not None
        else:
            closed_mk = None
            got98_mk = None
        on_98_list, closed = brand_list_match(linked['name'] if linked else name, brand, lists)
        got98 = bool(got98_mk) if got98_mk is not None else bool(
            linked or on_98_list or (rm.get('has98') is True)
            or (price_rec and price_rec.get('98') is not None))
        shabbat_set = closed_mk
        pz = paz.get(lic) or {}
        if pz and brand == 'פז':
            # Paz publishes no prices: only the 98 flag and the Shabbat closure come from its site
            got98 = pz.get('got98') is True
            shabbat_set = pz.get('shabbat_closure') is True
        sn = sonol.get(lic) or {}
        if sn and brand == 'סונול':
            got98 = sn.get('got98') is True
            shabbat_set = sn.get('shabbat_closure') is True
        dn = doralon.get(lic) or {}
        if dn and brand == 'דורלון':
            got98 = dn.get('got98') is True
            shabbat_set = dn.get('shabbat_closure') is True
        dl = delek.get(lic) or {}
        if dl and brand == 'דלק':
            shabbat_set = dl.get('shabbat_closure') is True
        tn = ten_m.get(lic) or {}
        if tn and brand == 'טן':
            got98 = False
            if tn.get('shabbat_closure') is not None:
                shabbat_set = tn.get('shabbat_closure') is True
        # got98 by source of truth: brand sites for Paz/Sonol/Dor Alon/Mika, never for Ten,
        # representative lists for Tapuz/Delek, Waze reports only for the rest.
        waze = price_map.get(lic) or {}
        waze98 = waze.get('price98')
        if brand == 'טן':
            got98 = False
        elif brand == 'פז' and pz:
            got98 = pz.get('got98') is True
        elif brand == 'סונול' and sn:
            got98 = sn.get('got98') is True
        elif brand == 'דורלון' and dn:
            got98 = dn.get('got98') is True
        elif brand == 'מיקה' and mk:
            got98 = mk.get('got98') is True
        elif brand in ('תפוז', 'דלק'):
            got98 = bool(linked or on_98_list)
        else:
            got98 = waze98 is not None
        # price: Mika's site first, Waze as the fallback; when this run scraped nothing for the
        # station, carry the previous scraped price over (previous database, then the site layer).
        # Exception: a Mika station with no Mika price means got98=false - no carry-over.
        mika_unpriced = brand == 'מיקה'          # Mika stations never carry a previous price
        if brand == 'מיקה' and mk:
            got98 = mk.get('got98') is True
            price_out = mk.get('price98')
            price_rep_out = None
        elif mk.get('price98') is not None:
            price_out = mk.get('price98')
            price_rep_out = None
        elif waze98 is not None:
            price_out = waze98
            price_rep_out = waze.get('price98_updated')
        else:
            price_out = None
            price_rep_out = None
        if price_out is None and not mika_unpriced:
            prev = prev_prices.get(lic)
            if prev:
                price_out, price_rep_out = prev
        if price_out is None and not mika_unpriced:
            srec = site_prices.get(venue_id or '') or site_prices.get(google_id or '')
            if srec is not None:
                price_out = srec.get('98')
                price_rep_out = srec.get('reported_at')
        # other (non-network) stations: a carried price is enough - got98 follows the price
        is_other = brand not in ('פז', 'סונול', 'דורלון', 'דלק', 'מיקה', 'תפוז', 'טן')
        if is_other:
            got98 = price_out is not None
        # a station that does not sell 98 carries no price at all
        if not got98:
            price_out = None
            price_rep_out = None
        if 'שומרת שבת' in (name or ''):
            name = re.sub(r'\s*[-–,|]?\s*שומרת שבת\s*', ' ', name).strip()
        station_name = linked['name'] if linked else name
        if 'שומרת שבת' in (station_name or ''):
            station_name = re.sub(r'\s*[-–,|]?\s*שומרת שבת\s*', ' ', station_name).strip()
        row = {
            'ministry_id': lic,
            'station_name': station_name,
            'brand': brand,
            'longtitude': (official_pins.get(lic) or {}).get('lon')
                          if (official_pins.get(lic) or {}).get('lon') is not None
                          else (linked['lon'] if linked else lon),
            'lattiude': (official_pins.get(lic) or {}).get('lat')
                        if (official_pins.get(lic) or {}).get('lat') is not None
                        else (linked['lat'] if linked else lat),
            'got98': got98,
            'waze_id': venue_id,
            'google_id': google_id,
            'price': price_out,
            'price_reported': price_rep_out,
            'updated_at': now,
            'shabbat_closure': (shabbat_set if shabbat_set is not None
                                else (closed if (linked or on_98_list) else False)),
            'violation': lic in viol_lic or lic in viol_extra,
            'violation_fuel': (viol_meta.get(lic) or {}).get('fuel'),
            'violation_sampling': (viol_meta.get(lic) or {}).get('sampling'),
        }
        out.append(row)
        stats['linked'] += bool(linked)
        stats['got98'] += got98
        stats['priced'] += row['price'] is not None
        stats['shabbat'] += row['shabbat_closure']
        stats['viol'] += row['violation']
        stats['waze'] += bool(venue_id)

    out = apply_tapuz(out)
    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'wrote {OUT}: {len(out)} stations')
    print('  linked to our data:', stats['linked'], '| with a waze_id:', stats['waze'])
    print('  got98:', stats['got98'], '| with a price:', stats['priced'])
    print('  shabbat_closure:', stats['shabbat'], '| violation:', stats['viol'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
