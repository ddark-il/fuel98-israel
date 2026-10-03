#!/usr/bin/env python3
"""mika_prices.py — Mika's own published fuel prices (mika.org.il), joined to our stations.

Mika is a pump *operator*, not a brand: it runs pumps inside other operators' stations
(סונול, דור אלון, …), so its prices are attached to whichever of our stations is on the same
forecourt. Two facts make this cheap and browser-free:

  * the station list is a WordPress post type — `GET /wp-json/wp/v2/station`
  * each station page carries its price list in static HTML:
        <li><span class="sub-title">בנזין 98</span><span class="value">&#8362;8.53</span></li>
    and a short map link (`goo.gl/maps/…` / `maps.app.goo.gl/…`) that 302s to a Google Maps URL
    containing the `!3d<lat>!4d<lon>` place marker — the coordinate we join on. (Per this
    project's rules: never match stations by name - `mika-ashkelon` vs `mika-hadari-ashkelon`
    overlap, and the JSON-LD address is unreliable.)

Prices are the operator's published consumer prices, not community reports; the page's
`modified` timestamp is used as the freshness signal.

    python3 mika_prices.py                                  # scrape + print
    python3 mika_prices.py --out mika_prices.json           # + JSON
    python3 mika_prices.py --publish out                    # + out/prices_mika.json
    python3 mika_prices.py --merge out/prices.json          # inject into a price_check output

Exit status is 1 when the station list cannot be fetched at all (site changed / blocked), so a
CI job fails loudly instead of publishing an empty file.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = 'https://mika.org.il'
STATION_API = BASE + '/wp-json/wp/v2/station'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
def _sibling(*parts: str) -> str:
    """A file that lives with the site's data/, found by looking instead of by assuming a layout.

    This script runs from two checkouts: `price-ci/` inside the repo (the site is then
    `../index.html`) and the dev tree one level above it (`../fuel98-israel/index.html`). Hardcoding
    either path silently disables half the join logic in the other one - the run still finishes, it
    just reports `city index: 0 stations in 0 cities` and quietly loses its weakest join pass.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in (os.path.join('..', *parts), os.path.join('..', 'fuel98-israel', *parts),
                os.path.join(*parts)):
        p = os.path.normpath(os.path.join(here, rel))
        if os.path.exists(p) or glob.glob(p):
            return p
    return os.path.normpath(os.path.join(here, '..', *parts))


_HERE = os.path.dirname(os.path.abspath(__file__))   # files that ship next to this script
DATA_GLOB = _sibling('data', '*.json')
NAV_CACHE = 'mika_navpoints_cache.json'

TITLE_RE = re.compile(r'<title>([^<]{2,200})</title>', re.S)
# The station page states its own address under כתובת: - the only place Mika gives a street
# (`מיקה פריימן, פריימן 18 ראשל"צ` in the post title, `יעקב פריימן 18` in the page body).
ADDR_RE = re.compile(r'<div class="info address">.*?<span class="content">\s*(.*?)</span>', re.S)


def parse_address(html: str) -> str | None:
    m = ADDR_RE.search(html)
    if not m:
        return None
    p = re.search(r'<p[^>]*>(.*?)</p>', m.group(1), re.S)
    txt = clean_text(p.group(1)) if p else clean_text(m.group(1))
    return txt.split('\n')[0].strip() or None
ITEM_RE = re.compile(
    r'<li>\s*<span class="sub-title">(?P<label>.*?)</span>\s*'
    r'<span class="value">(?P<value>.*?)</span>\s*</li>', re.S)
MAP_RE = re.compile(r'https://(?:goo\.gl/maps/|maps\.app\.goo\.gl/)[A-Za-z0-9_\-]+')
PRICE_RE = re.compile(r'([0-9]+(?:[.,][0-9]+)?)')
MARKER_RE = re.compile(r'!3d(-?[0-9.]+)!4d(-?[0-9.]+)')
AT_RE = re.compile(r'@(-?[0-9.]+),(-?[0-9.]+)')
# the inline `var lat/lng` on the pages is a template default - not a station coordinate
INLINE_DEFAULT = (32.087280, 34.804090)


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def fetch(url: str, timeout: int = 30) -> tuple[int, str, str]:
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'he-IL,he;q=0.9'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace'), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace'), url
    except Exception as e:                                             # noqa: BLE001
        return 0, repr(e), url


def fuel_key(label: str) -> str:
    """Mika's Hebrew labels -> our price keys."""
    s = re.sub(r'\s+', ' ', label).strip()
    low = s.replace('"', '').replace('"', '')
    if '98' in low and ('שרות' in low or 'שירות' in low):
        return '98_service'
    if '98' in low:
        return '98'
    if '95' in low and ('שרות' in low or 'שירות' in low or 'מלא' in low):
        return '95_service'
    if '95' in low:
        return '95'
    if 'סולר' in low:
        return 'diesel_service' if ('שרות' in low or 'שירות' in low) else 'diesel'
    if 'אוריאה' in low:
        return 'urea'
    if low.startswith('גפ') or 'lpg' in low.lower():
        return 'lpg'
    return re.sub(r'\W+', '_', low)[:24] or 'unknown'


def clean_text(txt: str) -> str:
    """Decode HTML entities and strip tags: `&#8362;8.53` -> `₪8.53`.

    Getting this wrong is subtle: without decoding, the shekel entity reads as the number 8362.
    """
    txt = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), txt)
    txt = (txt.replace('&nbsp;', ' ').replace('&amp;', '&')
              .replace('&quot;', '"').replace('&#39;', "'"))
    return re.sub(r'<[^>]+>', '', txt).strip()


def parse_prices(html: str) -> dict:
    out = {}
    for m in ITEM_RE.finditer(html):
        label = clean_text(m.group('label'))
        value = clean_text(m.group('value'))
        num = PRICE_RE.search(value)
        if not label or not num:
            continue
        try:
            price = float(num.group(1).replace(',', '.'))
        except ValueError:
            continue
        if not (0 < price < 100):              # sanity: ₪/litre, not a stray number
            continue
        key = fuel_key(label)
        out[key] = {'label': label, 'price': price}
    return out


def period_start_ms(now: datetime | None = None) -> int:
    """1st of the current month, 00:00 Israel time - the fuel price period boundary."""
    now = now or datetime.now(timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first.timestamp() * 1000)


class MikaScraper:
    def __init__(self, sleep: float = 0.5, cache: str = NAV_CACHE):
        self.sleep = sleep
        self.cache_path = cache
        self.cache = {}
        if os.path.exists(cache):
            try:
                self.cache = json.load(open(cache, encoding='utf-8'))
            except Exception:                                          # noqa: BLE001
                self.cache = {}
        self.errors: list[str] = []

    def save_cache(self):
        json.dump(self.cache, open(self.cache_path, 'w', encoding='utf-8'), indent=1)

    # ------------------------------------------------------------------ list
    def stations(self) -> list[dict]:
        out, page = [], 1
        while True:
            url = (f'{STATION_API}?per_page=100&page={page}'
                   '&_fields=id,slug,link,title,modified,station-area')
            status, body, _ = fetch(url)
            if status != 200 or not body.strip().startswith('['):
                if page == 1:
                    self.errors.append(f'station list HTTP {status}: {body[:120]}')
                    return []
                break
            batch = json.loads(body)
            if not batch:
                break
            out += batch
            if len(batch) < 100:
                break
            page += 1
        return out

    # ---------------------------------------------------------------- detail
    def coordinates(self, map_url: str) -> tuple[float | None, float | None]:
        if map_url in self.cache:
            c = self.cache[map_url]
            return c.get('lat'), c.get('lon')
        status, body, final = fetch(map_url)
        lat = lon = None
        m = MARKER_RE.search(final) or MARKER_RE.search(body)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
        else:
            m = AT_RE.search(final)
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
        if lat is not None:
            self.cache[map_url] = {'lat': lat, 'lon': lon}
        return lat, lon

    def scrape(self, st: dict) -> dict:
        status, html, _ = fetch(st['link'])
        row = {'id': st['id'], 'slug': st['slug'], 'link': st['link'],
               'mika_name': re.sub(r'<[^>]+>', '', st.get('title', {}).get('rendered', '')).strip(),
               'modified': st.get('modified'), 'fetched_at': datetime.now(timezone.utc).isoformat(),
               'http_status': status, 'prices': {}, 'lat': None, 'lon': None, 'map_url': None}
        if status != 200:
            self.errors.append(f"{st['slug']}: HTTP {status}")
            return row
        row['prices'] = parse_prices(html)
        # The WordPress post title (h1) can be stale after an operator renames a station: on
        # `mika-קריית-ביאליק` the CPT still says "מיקה ביאליק" while the page's own SEO title says
        # "דור אלון קריית ביאליק". The page is authoritative, so keep both and prefer the page.
        mt = TITLE_RE.search(html)
        if mt:
            row['page_title'] = clean_text(mt.group(1)).split('|')[0].strip()
            row['page_name'] = re.split(r'\s+[-–]\s+', row['page_title'])[0].strip()
            row['page_brand'] = page_brand(row['page_title'])
        row['mika_address'] = parse_address(html)
        # NOTE: the page also declares `var lat/lng` - verified identical on every station page
        # (32.087280,34.804090), i.e. a template default, so it is deliberately ignored. Only the
        # goo.gl map link carries the real place marker.
        mm = MAP_RE.search(html)
        if mm:
            row['map_url'] = mm.group(0)
            lat, lon = self.coordinates(mm.group(0))
            row['lat'], row['lon'] = lat, lon
            row['coord_source'] = 'map-link'
        return row


BRAND_PREFIXES = ('מיקה', 'סונול', 'דור אלון', 'דורלון', 'פז', 'דלק', 'תפוז', 'יעד')


def norm_name(s: str) -> str:
    s = re.sub(r'[\"\'״׳\u201c\u201d]', '', s or '')
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def core_name(title: str) -> str:
    """'מיקה הפלד, הפלד 1 חולון' -> 'הפלד' (station name, without the operator prefix/address)."""
    head = norm_name(title.split(',')[0])
    for p in BRAND_PREFIXES:
        if head.startswith(p):
            head = head[len(p):].strip()
            break
    return head


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r'[\s\-–]+', norm_name(s)) if t]


PAGE_BRAND = {'מיקה': 'מיקה', 'סונול': 'סונול', 'דור אלון': 'דורלון', 'דורלון': 'דורלון',
              'פז': 'פז', 'דלק': 'דלק', 'תפוז': 'תפוז'}


def page_brand(title: str) -> str | None:
    head = norm_name(title.split(',')[0])
    for pref, brand in PAGE_BRAND.items():
        if head.startswith(pref):
            return brand
    return None


def join_by_name(mika_name: str, ours: list[dict]) -> tuple[dict | None, str, list[str]]:
    """Match a Mika page to our station by name, without coordinates.

    Mika only publishes a coordinate (a `goo.gl` map link) on some pages; the rest carry a
    template default, which is why our own mika.json rows for those stations are registry-pinned.
    Matching is on *tokens*, never substrings (`עמי` must not match `עמית`), brand prefix first
    (that separates our duplicate names like `אשקלון` vs `הדרי אשקלון`), and it refuses when two
    candidates tie.
    """
    core = core_name(mika_name)
    pt = set(tokens(core))
    if not pt:
        return None, 'no name on the page', []
    brand = page_brand(mika_name)

    scored = []
    for o in ours:
        ot = set(tokens(o['name']))
        if not ot:
            continue
        if ot == pt:
            score = 4                                   # identical token sets
        elif ot <= pt and len(ot) >= 2:
            score = 3                                   # our name inside the page's name
        elif pt <= ot and len(pt) >= 2:
            score = 2                                   # page name shorter (ביאליק ⊂ קרית ביאליק)
        else:
            continue
        # a single-token page name ("אשדוד") must not be absorbed by a longer station
        # ("הקידמה אשדוד") - that produced a false positive on a different site
        if brand and o.get('brand') == brand:
            score += 5                                  # brand prefix from the page wins
        scored.append((score, o))
    if not scored:
        return None, f'no station named {core!r}', []
    scored.sort(key=lambda s: (-s[0], -len(tokens(s[1]['name']))))
    best = scored[0][0]
    top = [o for s, o in scored if s == best]
    names = [f"{o['brand']}/{o['name']}" for o in top]
    if len(top) == 1:
        how = 'name-brand' if best >= 5 else ('name-exact' if best == 4 else 'name-contains')
        return top[0], how, names
    # equal score: the most specific name wins (הדרי אשקלון over אשקלון), else refuse
    top.sort(key=lambda o: -len(tokens(o['name'])))
    if len(tokens(top[0]['name'])) > len(tokens(top[1]['name'])):
        return top[0], 'name-specific', names
    return None, f'ambiguous ({", ".join(names[:4])})', names


CITY_ALIASES = {'ראשל"צ': 'ראשון לציון', 'ראשלצ': 'ראשון לציון', 'פ"ת': 'פתח תקווה',
                'ת"א': 'תל אביב-יפו', 'י-ם': 'ירושלים', 'באר שבע': 'באר שבע'}


REGISTRY_RID = '5537a0ef-3eeb-449c-90c8-51e27564f0cb'      # Ministry of Energy public stations
REGISTRY_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'registry_cache.json')
SHARED_REGISTRY = '/tmp/ministry-registry.json'            # written by fuel98-israel/scripts/build-seo.py
STREET_PREFIXES = ('רח\'', 'רחוב', 'שד\'', 'שדרות', 'שד', 'דרך')
# Words that are not a street: a page can state "כביש 232" (a road) or "מושב בית שקמה" (a place)
# where the registry states a street name, and that is not a disagreement - it is a different
# kind of address. Only real street names are allowed to raise a conflict.
STREET_STOPWORDS = {'כביש', 'צומת', 'כניסה', 'יציאה', 'שער', 'אזור', 'תעשייה', 'ת', 'א',
                    'אז', 'פארק', 'סנטר', 'קניון', 'מרכז', 'מסחרי', 'מושב', 'קיבוץ', 'מועצה',
                    'אזורית', 'צפוני', 'דרומי', 'מזרחי', 'מערבי', 'עיר', 'בכניסה', 'למושב',
                    'דרום', 'צפון', 'מזרח', 'מערב', 'אזה', 'הצפוני', 'הדרומי'}
HOUSE_RE = re.compile(r'(?<!\d)(\d{1,4}[א-ת]?)(?!\d)')

# Registry `חברה` wording per our brand - only ever used to prefer the right row when several
# official pins sit within a few dozen metres of ours.
COMPANY_HINT = {'פז': ('פז',), 'סונול': ('סונול',), 'דורלון': ('דור-אלון', 'דור אלון', 'דורלון'),
                'דלק': ('דלק',), 'מיקה': ('מיקה',), 'תפוז': ('תפוז',),
                'אחר': ('עצמאי', 'יעד', 'בל', 'טן', 'גליל', 'קרן')}


def load_registry() -> list[dict]:
    """Official station registry rows (licence, company, address, authority, lat/lon).

    Same source and same field discipline as the rest of the project: WGS84 straight from
    `נ.צ. רוחב`/`נ.צ. אורך`, never the ITM grid columns. Cached, because a Mika run should not
    depend on data.gov.il being up.
    """
    for path in (SHARED_REGISTRY, REGISTRY_CACHE):
        if path and os.path.exists(path):
            try:
                recs = json.load(open(path, encoding='utf-8'))
                if recs and all(k in recs[0] for k in ('address', 'authority', 'lat', 'lon')):
                    return recs
            except Exception:                                          # noqa: BLE001
                pass
    url = 'https://data.gov.il/api/3/action/datastore_search?' + urllib.parse.urlencode(
        {'resource_id': REGISTRY_RID, 'limit': '2000'})
    req = urllib.request.Request(url, headers={'User-Agent': 'fuel98-israel-price-poc/1.0'})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = json.load(r)['result']['records']
    out = []
    for x in raw:
        try:
            lat, lon = float(x['נ.צ. רוחב']), float(x['נ.צ. אורך'])
        except (TypeError, ValueError):
            continue
        if not (29 < lat < 34 and 34 < lon < 36):
            continue
        out.append({'licence': str(x.get('מס_מינהל_הדלק') or '').strip(),
                    'company': (x.get('חברה') or '').strip(),
                    'name': (x.get('שם_תחנה') or '').strip(),
                    'address': (x.get('כתובת') or '').strip(),
                    'authority': (x.get('רשות_מקומית') or '').strip(),
                    'lat': lat, 'lon': lon})
    try:
        json.dump(out, open(REGISTRY_CACHE, 'w', encoding='utf-8'), ensure_ascii=False)
    except Exception:                                                  # noqa: BLE001
        pass
    return out


def authority_label(name: str) -> str:
    t = (name or '').strip()
    if re.match(r'^מ\.?\s?א\b', t):
        return 'מועצה אזורית ' + re.sub(r'^מ\.?\s?א\s*', '', t).strip()
    return t


def street_parts(addr: str) -> tuple[list[str], str | None]:
    """'יעקב פריימן 18, ראשון לציון' -> (['יעקב', 'פריימן'], '18').

    Street prefixes are dropped so `שד' לישנסקי 20` and `לישנסקי 20` compare equal; a house
    number is kept separately because it is the strongest part of an Israeli address.
    """
    head = norm_name((addr or '').split(',')[0])
    nums = HOUSE_RE.findall(head)
    house = nums[0] if nums else None
    words = [w for w in tokens(HOUSE_RE.sub(' ', head)) if w not in STREET_STOPWORDS]
    while words and words[0] in STREET_PREFIXES:
        words.pop(0)
    return [w for w in words if w not in STREET_STOPWORDS], house


def same_street(a: str, b: str) -> bool:
    """Fuzzy street-name equality: the same street is spelled inconsistently on both sides
    ('הקידמה' vs 'הקדמה', 'הרצל' vs 'הרצליה', 'בן גוריון' vs 'בן־גוריון')."""
    a, b = a.replace('-', ''), b.replace('-', '')
    if a == b:
        return True
    if abs(len(a) - len(b)) > 2:
        return False
    if a[:3] == b[:3]:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    prev = list(range(len(b) + 1))                      # Levenshtein, small strings
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= 1


def our_address_book(ours: list[dict], registry: list[dict],
                     max_m: float = 300.0) -> dict[str, dict]:
    """Our station key -> the official address at its own pin (nearest row, brand-preferred).

    Our pins and the registry's are the same forecourt 76-83 m apart by design, so the nearest
    row within 300 m is the station; the brand hint only breaks ties in dense clusters.
    """
    book: dict[str, dict] = {}
    for o in ours:
        best = best_same = None
        bd = bsd = float('inf')
        hints = COMPANY_HINT.get(o['brand'], ())
        for g in registry:
            d = haversine_m(o['lat'], o['lon'], g['lat'], g['lon'])
            if d < bd:
                best, bd = g, d
            if hints and any(h in g['company'] for h in hints) and d < bsd:
                best_same, bsd = g, d
        pick = best_same if best_same and bsd <= max_m else (best if bd <= max_m else None)
        if not pick:
            continue
        words, house = street_parts(pick['address'])
        book[o['key']] = {'key': o['key'], 'name': o['name'], 'brand': o['brand'], 'file': o['file'],
                          'address': pick['address'], 'city': authority_label(pick['authority']),
                          'street': words, 'house': house,
                          'distance_m': round(min(bd, bsd) if best_same else bd, 1)}
    return book


def join_by_address(row: dict, book: dict, detect: str | None,
                    taken: set[str]) -> tuple[dict | None, str, list[str]]:
    """Join a Mika page to our station through the street both sides state.

    Mika names pumps after their own brand/street (`מיקה פריימן`), our names are the site names
    (`פריימן`) and several can share a city - so the street+house number is the discriminator.
    """
    page_words, page_house = street_parts(row.get('mika_address') or '')
    if not page_words:
        return None, 'no street on the page', []
    want_city = norm_name(detect) if detect else ''
    scored = []
    for b in book.values():
        if b['key'] in taken or not b['street']:
            continue
        if want_city and b['city'] and norm_name(b['city']) != want_city:
            continue
        overlap = sum(1 for x in page_words for y in b['street'] if same_street(x, y))
        if not overlap:
            continue
        if page_house and b['house'] and page_house != b['house']:
            continue                                    # a different number on the same street
        scored.append((overlap + (2 if page_house and b['house'] else 0), b))
    if not scored:
        street = ' '.join(page_words) + (f" {page_house}" if page_house else '')
        return None, f"no station of ours on '{street}' in {detect or 'the detected city'}", []
    scored.sort(key=lambda s: -s[0])
    names = [f"{b['brand']}/{b['name']} ({b['address']})" for _, b in scored]
    top = [b for s, b in scored if s == scored[0][0]]
    if len(top) > 1:
        return None, f"address ambiguous: {', '.join(names[:3])}", names
    b = top[0]
    return b, f"address ({b['address']})", names


def address_check(row: dict, cand: dict, book: dict) -> str:
    """Does the street the page states agree with the official address at that station?

    This is the check that keeps a co-branded pump from being attached to the wrong forecourt:
    Mika names a pump by the site it sits in, so a name/brand/city match can still be the wrong
    station. Returns 'ok' | 'conflict: ...' | 'page has no street' | 'no official address'.
    """
    page_words, page_house = street_parts(row.get('mika_address') or '')
    b = book.get(cand['key'])
    if not page_words:
        return 'page has no street'
    if not b or not b['street']:
        return 'no official address'
    if not any(same_street(x, y) for x in page_words for y in b['street']):
        return f"conflict: page '{' '.join(page_words)}' vs official '{b['address']}'"
    if page_house and b['house'] and page_house != b['house']:
        # Neighbouring numbers are normal: our pins are the operator's own navigation points and
        # the registry pins are the licensed parcel, so entrance numbering differs by design.
        return f"ok (house {page_house} vs official {b['house']} on the same street)"
    return 'ok'


def load_city_index(index_html: str) -> dict:
    """{normalised city -> [(brand, station name)]} from the site's generated SEO block.

    The `<noscript>` block groups our own stations by the registry city, which is exactly the
    link we need for Mika pumps that sit inside another operator's station (they carry the host's
    name, so no name join can succeed).
    """
    if not index_html or not os.path.exists(index_html):
        return {}
    html = open(index_html, encoding='utf-8').read()
    out: dict[str, list] = {}
    for m in re.finditer(r'<h3[^>]*>תחנות דלק 98 ב([^<]+)</h3>\s*<ul>(.*?)</ul>', html, re.S):
        city = norm_name(m.group(1))
        for li in re.findall(r'<li>(.*?)</li>', m.group(2), re.S):
            txt = clean_text(li)
            if '—' in txt:
                brand, name = [p.strip() for p in txt.split('—', 1)]
                out.setdefault(city, []).append((brand, name))
    return out


def load_overrides(path: str | None) -> list[dict]:
    """Owner rulings about mika.org.il content: entries we must not report.

    `mika_overrides.json` exists because Mika fixes a station's identity in the *page* while the
    WordPress post behind it keeps publishing under the old, wrong title. We still list what the
    site serves - unless the owner has confirmed it is a mistake on Mika's side.
    """
    if not path or not os.path.exists(path):
        return {'exclude': [], 'verified': []}
    try:
        data = json.load(open(path, encoding='utf-8'))
    except Exception as exc:                                          # noqa: BLE001
        print(f'WARN: ignoring unreadable overrides {path}: {exc}', flush=True)
        return {'exclude': [], 'verified': []}
    out = {'exclude': [e for e in data.get('exclude', []) if e.get('match')],
           'verified': [e for e in data.get('verified', []) if e.get('match')]}
    print(f'overrides: {len(out["exclude"])} exclusion(s), {len(out["verified"])} verified '
          f'join(s) from {os.path.basename(path)}', flush=True)
    return out


def _blob(row: dict) -> str:
    return norm_name(' '.join(str(row.get(k) or '')
                              for k in ('mika_name', 'page_title', 'page_name', 'slug',
                                        'station_name', 'station_file', 'station_brand')))


def override_hit(row: dict, overrides: dict) -> dict | None:
    """First exclusion matching a scraped row (matched on the site titles/slug, not coordinates)."""
    blob = _blob(row)
    for o in overrides.get('exclude', []):
        if norm_name(str(o['match'])) in blob:
            return o
    return None


def verified_hit(row: dict, overrides: dict) -> dict | None:
    """A street conflict a human has settled, with the evidence written down.

    `verified` entries exist so a resolved question stops being re-litigated on every run: the
    join stays flagged in the data (`address_check` keeps the proof) but stops being reported as
    an open review item. Add an entry only with a checkable source - an operator page, a venue's
    own address, a satellite view - never from the join itself.
    """
    blob = _blob(row)
    for v in overrides.get('verified', []):
        if norm_name(str(v['match'])) in blob:
            return v
    return None


def detect_city(title: str, city_index: dict) -> str | None:
    """City mentioned in a Mika page title ('מיקה אשדוד, שדרות בני ברית' -> 'אשדוד')."""
    blob = norm_name(clean_text(title))
    for alias, full in CITY_ALIASES.items():
        if alias in blob:
            return full
    best = None
    for city in city_index:
        if not city:
            continue
        core = city.split('(')[0].strip()
        token = core.split()[-1] if core.split() else core      # 'קרית ביאליק' -> 'ביאליק'
        for needle in (core, token):
            if needle and len(needle) >= 4 and needle in blob:
                if best is None or len(needle) > len(best[1]):
                    best = (city, needle)
    return best[0] if best else None


def join_by_city_brand(mika_name: str, ours: list[dict], city_index: dict,
                       taken: set[str], brand_hint: str | None = None
                       ) -> tuple[dict | None, str, list[str]]:
    """Last resort for co-branded stations: same city + same brand, and only one candidate."""
    city = detect_city(mika_name, city_index)
    if not city:
        return None, 'city not recognised', []
    brand = brand_hint or page_brand(mika_name)
    entries = city_index.get(city, [])
    names = {n for _b, n in entries}
    cands = [o for o in ours if o['name'] in names and (brand is None or o['brand'] == brand)]
    label = [f"{o['brand']}/{o['name']}" for o in cands]
    if not cands:
        return None, f'no {brand or "matching"} station of ours in {city}', label
    free = [o for o in cands if o['key'] not in taken]
    if len(free) == 1:
        return free[0], f'city+brand ({city})', label
    if not free:
        return None, f'all {len(cands)} candidates in {city} already matched elsewhere', label
    return None, f'ambiguous in {city}: {", ".join(label[:4])}', label


def load_ours(pattern: str) -> list[dict]:
    out = []
    for f in sorted(glob.glob(pattern)):
        base = os.path.basename(f)
        if base in ('manifest.json', 'violations.json', 'prices.json'):
            continue
        d = json.load(open(f, encoding='utf-8'))
        brand_file = d.get('brand') if isinstance(d, dict) else None
        for s in (d if isinstance(d, list) else (d.get('stations') if isinstance(d.get('stations'), list) else [])):
            c = s.get('coordinates') or {}
            if c.get('lat') is None:
                continue
            out.append({'file': base, 'brand': s.get('brand') or brand_file, 'name': s.get('name'),
                        'lat': float(c['lat']), 'lon': float(c['lon']),
                        'key': f"{base}|{s.get('name')}|{c['lat']},{c['lon']}"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description='Mika fuel prices -> our stations')
    ap.add_argument('--data', default=DATA_GLOB)
    ap.add_argument('--index-html', default=_sibling('index.html'),
        help='site index.html, used for the station->city index')
    ap.add_argument('--out', default=None)
    ap.add_argument('--publish', default=None, metavar='DIR')
    ap.add_argument('--from-json', default=None, metavar='MIKA_JSON', dest='from_json',
                    help='reuse an existing mika_prices.json instead of scraping again')
    ap.add_argument('--covered-out', default='mika_covered.json', dest='covered_out',
                    help='write the stations this run priced, for the Waze sweep to skip next time')
    ap.add_argument('--merge', default=None, metavar='PRICES_JSON',
                    help='inject Mika prices into a price_check output (written in place)')
    ap.add_argument('--overrides', default=os.path.join(_HERE, 'mika_overrides.json'),
        help='owner rulings: site content we must not report (Mika mistakes already fixed by them)')
    ap.add_argument('--max-match-m', type=float, default=250.0, dest='max_match_m')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--sleep', type=float, default=0.5)
    a = ap.parse_args()

    city_index = load_city_index(a.index_html)
    print(f'city index: {sum(len(v) for v in city_index.values())} stations in '
          f'{len(city_index)} cities', flush=True)
    if a.from_json:
        payload = json.load(open(a.from_json, encoding='utf-8'))
        rows, overridden = payload['stations'], payload.get('excluded', [])
        with98 = sum(1 for r in rows if r.get('price98') is not None)
        matched = sum(1 for r in rows if r.get('station_key'))
        print(f'{len(rows)} rows reused from {a.from_json} '
              f'({with98} with a 98 price, {matched} matched, {len(overridden)} excluded)',
              flush=True)
        return write_outputs(a, rows, overridden, with98, matched, [])
    sc = MikaScraper(sleep=a.sleep)
    stations = sc.stations()
    if not stations:
        print('FAIL: could not list Mika stations:', sc.errors[:2])
        return 1
    if a.limit:
        stations = stations[:a.limit]
    print(f'{len(stations)} Mika stations on the site', flush=True)

    ours = load_ours(a.data)
    try:
        registry = load_registry()
        book = our_address_book(ours, registry)
        print(f'registry: {len(registry)} official pins -> addresses for {len(book)}/{len(ours)} '
              'of our stations', flush=True)
    except Exception as exc:                                          # noqa: BLE001
        print(f'WARN: registry/addresses unavailable ({exc}); address pass disabled', flush=True)
        book = {}
    if not ours:
        # Usually a wrong --ours path: joining against nothing matches nothing, which looks exactly
        # like Mika having no stations on our map. Say it before the report is believed.
        print(f"ERROR: --ours {a.ours!r} yielded no stations - nothing to join to, and a run that "
              f"matches nothing is indistinguishable from a data gap. Fix the path.", flush=True)
        return 3
    overrides = load_overrides(a.overrides)   # {'exclude': [...], 'verified': [...]}
    rows, overridden, with98 = [], [], 0
    for i, st in enumerate(stations, 1):
        row = sc.scrape(st)
        row['join_method'] = 'not attempted'      # overwritten below; never missing from a report
        hit = override_hit(row, overrides)
        if hit:                              # a confirmed mistake on Mika's site - do not report it
            row['excluded_reason'] = hit.get('reason', '')
            overridden.append(row)
            print(f"[{i}/{len(stations)}] {row['mika_name'][:38]:<38} EXCLUDED by override: "
                  f"{hit.get('match')}", flush=True)
            continue
        rows.append(row)
        prices = ' '.join(f'{k}={v["price"]}' for k, v in row['prices'].items())
        print(f"[{i}/{len(stations)}] {row['mika_name'][:38]:<38} {prices}", flush=True)
        time.sleep(a.sleep)

    # ---- join to our stations, strongest evidence first, and never twice to the same station
    taken: dict[str, str] = {}
    for row in rows:                                   # pass 1: a real map marker
        if row['lat'] is None or not ours:
            continue
        cand = min(ours, key=lambda o: haversine_m(row['lat'], row['lon'], o['lat'], o['lon']))
        d = haversine_m(row['lat'], row['lon'], cand['lat'], cand['lon'])
        if d <= a.max_match_m and cand['key'] not in taken:
            row.update({'station_key': cand['key'], 'station_file': cand['file'],
                        'station_name': cand['name'], 'station_brand': cand['brand'],
                        'match_distance_m': round(d, 1), 'join_method': f'coord ({d:.0f} m)'})
            taken[cand['key']] = row['slug']
            row['address_check'] = ('ok (same forecourt)' if d <= 60
                                    else address_check(row, cand, book))
    for row in rows:                                   # pass 2: station name on the page
        if row.get('station_key'):
            continue
        joined, method, cands = join_by_name(row.get('page_name') or row['mika_name'], ours)
        row['name_candidates'] = cands
        if joined and joined['key'] not in taken:
            row.update({'station_key': joined['key'], 'station_file': joined['file'],
                        'station_name': joined['name'], 'station_brand': joined['brand'],
                        'join_method': method})
            taken[joined['key']] = row['slug']
            row['address_check'] = address_check(row, joined, book)
        elif joined:
            row['join_method'] = f"conflict: {joined['name']} already matched"
    for row in rows:                                   # pass 3: the street both sides publish
        if row.get('station_key') or not book:
            continue
        detect = detect_city(f"{row.get('mika_address') or ''} {row['mika_name']}", city_index)
        row['detected_city'] = detect
        joined, method, cands = join_by_address(row, book, detect, set(taken))
        row['address_candidates'] = cands
        row['join_method'] = method
        if joined:
            row.update({'station_key': joined['key'], 'station_file': joined['file'],
                        'station_name': joined['name'], 'station_brand': joined['brand']})
            taken[joined['key']] = row['slug']
            row['address_check'] = address_check(row, joined, book)
    for row in rows:                                   # pass 4: co-branded pumps (city + brand)
        if row.get('station_key') or not city_index:
            continue
        joined, method, cands = join_by_city_brand(row['mika_name'], ours, city_index,
                                                   set(taken), brand_hint=row.get('page_brand'))
        row['city_candidates'] = cands
        row['join_method'] = method
        if joined:
            check = address_check(row, joined, book)
            row['address_check'] = check
            if check.startswith('conflict'):
                # Name+brand+city matched and this was the *only* candidate of that brand left in
                # the city (the pass refuses to pick when several remain) - yet the page's street
                # and the official address disagree. Mika lists a pump at a gate/street corner, so
                # this is usually the same forecourt; it is joined but marked for review, because a
                # wrong attach would show a wrong price.
                row['join_method'] = f'{method} (address unverified)'
                row['needs_attention'] = True
                row['review'] = check
            row.update({'station_key': joined['key'], 'station_file': joined['file'],
                        'station_name': joined['name'], 'station_brand': joined['brand']})
            taken[joined['key']] = row['slug']

    for row in rows:
        p98 = row['prices'].get('98')
        with98 += bool(p98)
        row['price98'] = p98['price'] if p98 else None
    matched = sum(1 for r in rows if r.get('station_key'))
    # An unmatched page is only harmless if it sells no 98. If it does, we are missing a station
    # that publishes a 98 price - that is a data gap, not a join detail, so say so out loud.
    review = []
    for row in rows:
        if not row.get('station_key'):
            row['reason'] = row.get('join_method') or 'not matched'
            if row['price98'] is not None:
                row['needs_attention'] = True
                print(f"  !! {row['mika_name']} publishes a 98 price but matches none of our "
                      f"stations: {row['reason']}", flush=True)
            continue
        if (row.get('address_check') or '').startswith('conflict'):
            v = verified_hit(row, overrides)
            if v:
                row['address_check_raw'] = row['address_check']
                row['address_check'] = f"ok, verified: {v['reason']}"
                row['review'] = None
                continue
            # Joined on the name alone while the page and the registry name different streets:
            # probably the same forecourt (pumps sit at gate/street corners), but a wrong attach
            # would show a wrong price, so it is marked for review instead of being trusted.
            row['needs_attention'] = row.get('needs_attention') or True
            row['review'] = row['address_check']
            review.append(f"{row['mika_name']} -> {row['station_brand']}/{row['station_name']} "
                          f"({row['join_method']}; {row['address_check']})")
    if review:
        print(f'\n  {len(review)} join(s) need a human look (name matched, street differs):')
        for line in review:
            print('   -', line)
    sc.save_cache()
    return write_outputs(a, rows, overridden, with98, matched, sc.errors)


def write_outputs(a, rows, overridden, with98, matched, errors) -> int:
    """Print the join report, write --out/--publish, and merge into a price_check file."""
    print('\njoined pages:')
    for row in rows:
        tag = (f"{row['station_brand']}/{row['station_name']} [{row.get('join_method')}]"
               if row.get('station_key') else f"UNMATCHED ({row.get('join_method')})")
        print(f"   {row['mika_name'][:40]:<42} -> {tag}")

    print(f"\n{len(rows)} Mika stations scraped, {with98} with a 98 price, "
          f"{matched} matched to our stations")
    if with98:
        vals = sorted(r['price98'] for r in rows if r['price98'])
        print(f'98 range: {vals[0]} - {vals[-1]} ₪/l')
    if errors:
        print('errors:', errors[:3])

    payload = {'generated': datetime.now(timezone.utc).isoformat(),
               'source': 'mika.org.il (operator-published prices)',
               'counts': {'stations': len(rows), 'with_98': with98, 'matched': matched,
                          'excluded': len(overridden)},
               'stations': rows}
    if overridden:                       # kept in the output so an empty scrape can never hide a rule
        payload['excluded'] = [{'mika_name': r['mika_name'], 'url': r.get('url'),
                                'reason': r.get('excluded_reason', ''),
                                'page_title': r.get('page_title')} for r in overridden]
    if a.out:
        json.dump(payload, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('wrote', a.out)
    if a.publish:
        os.makedirs(a.publish, exist_ok=True)
        path = os.path.join(a.publish, 'prices_mika.json')
        compact = {r.get('station_key') or f"mika|{r['slug']}": {
            'brand': r.get('station_brand') or 'מיקה', 'name': r.get('station_name') or r['mika_name'],
            'mika_slug': r['slug'], 'mika_name': r['mika_name'], 'prices': r['prices'],
            'price98': r['price98'], 'price98_source': 'mika', 'modified': r['modified'],
            'review': r.get('review'), 'checked_at': r['fetched_at']} for r in rows}
        json.dump({'generated': payload['generated'], 'source': payload['source'],
                   'stations': compact}, open(path, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        print('wrote', path)

    # What Waze should not be asked again: the stations this run priced from the operator's own site.
    # The next Waze sweep reads this file and skips them, which keeps the sweep (and its windows) for
    # the stations nobody else publishes.
    if a.covered_out:
        covered = {r['station_key']: (r.get('mika_name') or '')
                   for r in rows if r.get('station_key') and r.get('price98') is not None}
        json.dump({'generated': datetime.now(timezone.utc).isoformat(), 'stations': covered},
                  open(a.covered_out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f'wrote {a.covered_out} ({len(covered)} station(s) whose price comes from Mika)')

    if a.merge:
        try:
            merged = json.load(open(a.merge, encoding='utf-8'))
        except FileNotFoundError:
            # No Waze layer this run (its shards never arrived, or the Waze job died). Mika stands on
            # its own, so merge into an empty set and let the merge add rows rather than update them.
            print(f'note: {a.merge} not found - merging Mika into an empty set (Waze contributed '
                  f'nothing this run)', flush=True)
            merged = {'stations': {}}
        by_key = {r.get('station_key'): r for r in rows if r.get('station_key')}
        added = updated = 0
        # A matched station can still be absent from the file we merge into (a partial Waze run):
        # its Mika price would otherwise never appear, so report it as a gap instead.
        missing = {k for k, r in by_key.items()
                   if k not in merged.get('stations', {}) and r.get('price98') is not None}
        for key, rec in merged.get('stations', {}).items():
            m = by_key.get(key)
            if not m:
                continue
            src = rec.setdefault('sources', {})
            if rec.get('prices') and 'waze' not in src:
                src['waze'] = rec['prices']       # what the Waze check saw for this station
            src['mika'] = m['prices']
            if m['price98'] is not None:            # operator's published price wins
                if rec.get('price98') is not None:
                    updated += 1
                else:
                    added += 1
                rec['price98'] = m['price98']
                rec['price98_source'] = 'mika'
                rec['price98_mika'] = m['price98']
                rec['price98_label'] = '98'
                rec['mika_modified'] = m['modified']
                # Mika's page timestamp is the only date it gives us - use it for freshness
                ms = None
                try:
                    ms = int(datetime.fromisoformat(m['modified']).replace(
                        tzinfo=timezone.utc).timestamp() * 1000)
                except Exception:                                  # noqa: BLE001
                    pass
                if ms:
                    rec['price98_updated'] = ms
                    rec['price98_age_days'] = round(
                        (time.time() * 1000 - ms) / 86400000.0, 1)
                    rec['price98_current_period'] = ms >= period_start_ms()
                    rec['mika_price_date'] = m['modified']
            rec['mika_name'] = m['mika_name']
            # Set AND clear: a join settled later (an override with evidence) must not keep a
            # review tag left behind by an earlier run, or the site softens a price we confirmed.
            if m.get('needs_attention'):
                rec['price98_review'] = m.get('review') or m.get('reason') or 'joined with doubt'
            else:
                rec.pop('price98_review', None)
            if str(m.get('address_check') or '').startswith('ok, verified'):
                rec['price98_verified'] = m['address_check'][len('ok, verified: '):]
            else:
                rec.pop('price98_verified', None)
        merged['mika_merged_at'] = datetime.now(timezone.utc).isoformat()
        json.dump(merged, open(a.merge, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f'merged into {a.merge}: {added} stations gained a 98 price from Mika, '
              f'{updated} overrode a Waze one')
        if missing:
            print(f'  !! {len(missing)} matched Mika station(s) are absent from {a.merge} - '
                  f'no price written for them: {", ".join(sorted(missing)[:3])}'
                  + (' ...' if len(missing) > 3 else ''))
        return 0


        return 0


if __name__ == '__main__':
    sys.exit(main())
