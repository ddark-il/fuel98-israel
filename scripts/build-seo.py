#!/usr/bin/env python3
"""Regenerate the invisible <noscript> SEO block in index.html.

City of each station comes from the **Ministry of Energy's own station registry**
(data.gov.il "gas-station"): every public station carries WGS84 coordinates and its
רשות_מקומית (local authority). For each of our stations we take the nearest official
pin — preferring a row of the same brand — and use that authority as the city,
rendering "מ.א X" as "מועצה אזורית X".

No geocoding service is involved: no Nominatim, no Google, no API key, no rate
limit — and the result is deterministic, so re-running produces identical output
(unlike reverse geocoding, which shuffled stations between cities on every run).

City data is never written to data/*.json and never shown in the UI — it lives
only inside this generated block. Stations with no official pin nearby are listed
under "נוספות".

Run:  python3 scripts/build-seo.py      (seconds, not minutes)
"""
import json, re, html, math, urllib.request, urllib.parse
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # fuel98-israel/
DATA = ROOT / "data"
INDEX = ROOT / "index.html"
DISP = {"דורלון": "דור אלון", "שיא אנרגיה": "אחר"}      # display-friendly brand names
SKIP = {"manifest.json", "violations.json"}

REGISTRY_RID = "5537a0ef-3eeb-449c-90c8-51e27564f0cb"
REGISTRY_CACHE = Path("/tmp/ministry-registry.json")

# Registry company wording per brand, used to prefer the right pin when several
# stations sit close together. Brands rename stations constantly, so this only ever
# breaks a tie — proximity does the matching.
COMPANY_HINT = {
    "פז": ("פז",), "סונול": ("סונול",), "דורלון": ("דור-אלון", "דור אלון", "דורלון"),
    "דלק": ("דלק",), "מיקה": ("מיקה",), "תפוז": ("תפוז",),
    "אחר": ("עצמאי", "יעד", "בל", "טן", "גליל", "קרן"),
}

def norm(c):
    return c.replace("־", " ").replace("–", "-").strip()  # maqaf->space, en-dash->hyphen

def load_registry():
    """The Ministry registry, from cache when present (see check-violations.mjs)."""
    if REGISTRY_CACHE.exists():
        try:
            recs = json.loads(REGISTRY_CACHE.read_text(encoding="utf-8"))
            # the cache is shared with check-violations.mjs — require its fields
            if recs and all(k in recs[0] for k in ("company", "authority", "lat", "lon")):
                return recs
        except Exception:
            pass
    url = "https://data.gov.il/api/3/action/datastore_search?" + urllib.parse.urlencode(
        {"resource_id": REGISTRY_RID, "limit": "2000"})
    req = urllib.request.Request(url, headers={"User-Agent": "fuel98-israel-seo-build/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = json.load(r)["result"]["records"]
    out = []
    for x in raw:
        try:
            lat, lon = float(x["נ.צ. רוחב"]), float(x["נ.צ. אורך"])
        except (TypeError, ValueError):
            continue
        if not (29 < lat < 34 and 34 < lon < 36):
            continue
        out.append({"licence": str(x["מס_מינהל_הדלק"] or "").strip(),
                    "company": (x["חברה"] or "").strip(),
                    "name": (x["שם_תחנה"] or "").strip(),
                    "address": (x["כתובת"] or "").strip(),
                    "authority": (x["רשות_מקומית"] or "").strip(),
                    "lat": lat, "lon": lon})
    REGISTRY_CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out

def haversine(a, b, c, d):
    R, r = 6371000, math.pi / 180
    x = (math.sin((c - a) * r / 2) ** 2
         + math.cos(a * r) * math.cos(c * r) * math.sin((d - b) * r / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))

def display_authority(name):
    t = (name or "").strip()
    if re.match(r"^מ\.?\s?א\b", t):
        return "מועצה אזורית " + re.sub(r"^מ\.?\s?א\s*", "", t).strip()
    return t

def city_for(station, registry):
    """Nearest official pin's local authority; brand hint breaks dense clusters."""
    lat, lon = station["coordinates"]["lat"], station["coordinates"]["lon"]
    hints = COMPANY_HINT.get(station["brand"], ())
    best = best_same = None
    best_d = best_same_d = float("inf")
    for g in registry:
        d = haversine(lat, lon, g["lat"], g["lon"])
        if d < best_d:
            best, best_d = g, d
        if hints and any(h in g["company"] for h in hints) and d < best_same_d:
            best_same, best_same_d = g, d
    # A same-brand pin within 300m is certainly the station. Otherwise the nearest
    # official pin anywhere within 1.5km — fuel stations are sparse, so this assigns
    # the right municipality even where our pin and the official one disagree by a
    # few hundred metres (rural and highway sites). Beyond that we leave it blank
    # rather than guess at a neighbouring authority.
    pick = best_same if best_same and best_same_d <= 300 else (best if best_d <= 1500 else None)
    return display_authority(pick["authority"]) if pick else ""

def load_stations():
    out = []
    for f in sorted(DATA.glob("*.json")):
        if f.name in SKIP:
            continue
        out += json.loads(f.read_text(encoding="utf-8"))
    return out

def build_block(by_city, no_city):
    p = ["<noscript>",
         "<h2>תחנות דלק 98 (בנזין 98) בישראל — לפי עיר</h2>",
         "<p>רשימת תחנות הדלק המספקות בנזין 98 (אוקטן 98) בכל הארץ, מסודרות לפי עיר וחברה.</p>"]
    for city in sorted(by_city):
        p.append(f"<h3>תחנות דלק 98 ב{html.escape(city)}</h3>")
        p.append("<ul>" + "".join(f"<li>{e}</li>" for e in sorted(by_city[city])) + "</ul>")
    if no_city:
        p.append("<h3>תחנות דלק 98 נוספות</h3>")
        p.append("<ul>" + "".join(f"<li>{e}</li>" for e in sorted(no_city)) + "</ul>")
    p.append("</noscript>")
    return "<!-- seo-noscript:start -->\n" + "\n".join(p) + "\n<!-- seo-noscript:end -->"

def main():
    stations = load_stations()
    registry = load_registry()
    print(f"Ministry registry: {len(registry)} stations with coordinates")
    by_city, no_city = defaultdict(list), []
    for i, s in enumerate(stations, 1):
        city = city_for(s, registry)
        entry = html.escape(f"{DISP.get(s['brand'], s['brand'])} — {s['name']}")
        (by_city[city] if city else no_city).append(entry)
        print(f"[{i}/{len(stations)}] {s['name']} -> {city or '(no city)'}", flush=True)

    block = build_block(by_city, no_city)
    h = INDEX.read_text(encoding="utf-8")
    if "<!-- seo-noscript:start -->" in h:
        h = re.sub(r"<!-- seo-noscript:start -->.*?<!-- seo-noscript:end -->", lambda _: block, h, flags=re.S)
    else:  # first run: insert right after the visually-hidden <h1>
        m = re.search(r'<h1 class="visually-hidden">.*?</h1>\n', h, flags=re.S)
        if not m:
            raise SystemExit("No <h1 class=\"visually-hidden\"> anchor and no existing markers found.")
        h = h[:m.end()] + block + "\n" + h[m.end():]
    INDEX.write_text(h, encoding="utf-8")
    print(f"\nDONE — {len(by_city)} cities, {len(stations)} stations ({len(no_city)} without city)")

if __name__ == "__main__":
    main()
