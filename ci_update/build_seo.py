#!/usr/bin/env python3
"""Regenerate the invisible <noscript> SEO blocks in index.html and 95/index.html.

Both pages are the same app over ci_update/stations.json, so both blocks come from one
deterministic pass: the 98 page keeps the 98 sellers (`got98`); the 95 page keeps every
station whose venue is identified (`waze_id` or `google_id`) — Ten included — but the
"אחר" bucket only with a reported 95 price (independents are often diesel-only truck stops).

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

Run:  python3 ci_update/build_seo.py      (seconds, not minutes)
"""
import json, re, html, math, urllib.request, urllib.parse
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # fuel98-israel/
DATA = Path(__file__).resolve().parent                 # ci_update/
INDEX = ROOT / "index.html"
DISP = {"דורלון": "דור אלון", "שיא אנרגיה": "אחר"}      # display-friendly brand names
# The seven networks the site names; everything else is "אחר" (independents, Yaad, Sadash).
NETWORK_BRANDS = {"פז", "דורלון", "דלק", "סונול", "מיקה", "תפוז", "טן"}
SKIP = {"manifest.json", "violations.json", "prices.json", "stations.json"}

REGISTRY_RID = "5537a0ef-3eeb-449c-90c8-51e27564f0cb"
REGISTRY_CACHE = DATA / "registry_cache.json"

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
    """The Ministry registry, from the pipeline cache when present."""
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
    """Every station row with the flags each page filters on.

    One pass over the single station database; the 98 page keeps the 98 sellers, the 95 page
    keeps every station with an identified venue. The legacy per-brand files (no database yet)
    are still the 98 list, so they all count as 98 and carry no 95 price.
    """
    db = DATA / "stations.json"
    if db.exists():
        rows = json.loads(db.read_text(encoding="utf-8"))
        return [{"brand": r["brand"], "name": r["station_name"],
                 "coordinates": {"lat": r["lattiude"], "lon": r["longtitude"]},
                 "got98": r.get("got98") is True, "price95": r.get("price95"),
                 "venue": bool(r.get("waze_id") or r.get("google_id")),
                 "listed": r.get("listed") is not False}
                for r in rows if r.get("lattiude") is not None and r.get("longtitude") is not None]
    out = []
    for f in sorted(DATA.glob("*.json")):
        if f.name in SKIP:
            continue
        for r in json.loads(f.read_text(encoding="utf-8")):
            r.update({"got98": True, "price95": None, "venue": True, "listed": True})
            out.append(r)
    return out

# The two pages are the same app over the same rows, differing in which grade they show. Keep the
# block wording per page in sync with each page's own <h1>/<title>.
PAGES = (
    {"num": "98", "path": INDEX,
     "intro": "רשימת תחנות הדלק המספקות בנזין 98 (אוקטן 98) בכל הארץ, מסודרות לפי עיר וחברה, עם מחיר 98 מעודכן בתחנות מדווחות.",
     "pick": lambda r: bool(r["got98"]) and r["listed"]},
    {"num": "95", "path": ROOT / "95" / "index.html",
     "intro": "רשימת תחנות הדלק בישראל, מסודרות לפי עיר וחברה, עם מחיר בנזין 95 (אוקטן 95) בכל תחנה שדווח בה מחיר.",
     "pick": lambda r: (r["venue"] and r["listed"]
                        and (r["brand"] in NETWORK_BRANDS or r["price95"] is not None))},
)

def build_block(by_city, no_city, num, intro):
    p = ["<noscript>",
         f"<h2>תחנות דלק {num} (בנזין {num}) בישראל — לפי עיר</h2>",
         f"<p>{intro}</p>"]
    for city in sorted(by_city):
        p.append(f"<h3>תחנות דלק {num} ב{html.escape(city)}</h3>")
        p.append("<ul>" + "".join(f"<li>{e}</li>" for e in sorted(by_city[city])) + "</ul>")
    if no_city:
        p.append(f"<h3>תחנות דלק {num} נוספות</h3>")
        p.append("<ul>" + "".join(f"<li>{e}</li>" for e in sorted(no_city)) + "</ul>")
    p.append("</noscript>")
    return "<!-- seo-noscript:start -->\n" + "\n".join(p) + "\n<!-- seo-noscript:end -->"

def write_page(path, block):
    h = path.read_text(encoding="utf-8")
    if "<!-- seo-noscript:start -->" in h:
        h = re.sub(r"<!-- seo-noscript:start -->.*?<!-- seo-noscript:end -->", lambda _: block, h, flags=re.S)
    else:  # first run: insert right after the visually-hidden <h1>
        m = re.search(r'<h1 class="visually-hidden">.*?</h1>\n', h, flags=re.S)
        if not m:
            raise SystemExit("No <h1 class=\"visually-hidden\"> anchor and no existing markers found.")
        h = h[:m.end()] + block + "\n" + h[m.end():]
    path.write_text(h, encoding="utf-8")

def main():
    stations = load_stations()
    registry = load_registry()
    print(f"Ministry registry: {len(registry)} stations with coordinates")
    for page in PAGES:
        if not page["path"].exists():
            print(f"\nskip {page['path']}: not found")
            continue
        picked = [s for s in stations if page["pick"](s)]
        by_city, no_city = defaultdict(list), []
        for i, s in enumerate(picked, 1):
            city = city_for(s, registry)
            entry = html.escape(f"{DISP.get(s['brand'], s['brand'])} — {s['name']}")
            (by_city[city] if city else no_city).append(entry)
            print(f"[{page['num']} {i}/{len(picked)}] {s['name']} -> {city or '(no city)'}",
                  flush=True)
        write_page(page["path"], build_block(by_city, no_city, page["num"], page["intro"]))
        print(f"\n{page['path'].relative_to(ROOT)}: {len(picked)} stations, "
              f"{len(by_city)} cities ({len(no_city)} without city)")
    print("\nDONE")

if __name__ == "__main__":
    main()
