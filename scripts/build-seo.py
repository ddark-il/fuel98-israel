#!/usr/bin/env python3
"""Regenerate the invisible <noscript> SEO block in index.html.

Reverse-geocodes every station's coordinates -> city (Nominatim, in memory),
groups the stations by city, and rewrites ONLY the content between the
  <!-- seo-noscript:start -->  /  <!-- seo-noscript:end -->
markers in index.html.

City data is never written to data/*.json and never shown in the UI — it lives
only inside this generated block (and transiently in memory during the build).

Run:  python3 scripts/build-seo.py
Note: ~6 min — Nominatim's usage policy allows ~1 request/second.
"""
import json, re, html, time, urllib.request, urllib.parse
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # fuel98-israel/
DATA = ROOT / "data"
INDEX = ROOT / "index.html"
DISP = {"דורלון": "דור אלון", "שיא אנרגיה": "אחר"}      # display-friendly brand names
SKIP = {"manifest.json", "violations.json"}

def norm(c):
    return c.replace("־", " ").replace("–", "-").strip()  # maqaf->space, en-dash->hyphen

def reverse_geocode(lat, lon):
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "accept-language": "he", "addressdetails": "1"})
    req = urllib.request.Request(url, headers={"User-Agent": "fuel98-israel-seo-build/1.0"})
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                a = json.load(r).get("address", {})
                return norm(a.get("city") or a.get("town") or a.get("village")
                            or a.get("municipality") or a.get("suburb") or a.get("hamlet") or "")
        except Exception:
            time.sleep(2)
    return ""

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
    by_city, no_city = defaultdict(list), []
    for i, s in enumerate(stations, 1):
        city = reverse_geocode(s["coordinates"]["lat"], s["coordinates"]["lon"])
        entry = html.escape(f"{DISP.get(s['brand'], s['brand'])} — {s['name']}")
        (by_city[city] if city else no_city).append(entry)
        print(f"[{i}/{len(stations)}] {s['name']} -> {city or '(no city)'}", flush=True)
        time.sleep(1.1)

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
