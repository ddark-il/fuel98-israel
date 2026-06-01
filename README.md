# תחנות דלק 98 — Fuel98 Israel

A simple map + list of fuel stations in Israel that sell **98-octane** petrol (בנזין 98 / Super 98).

🔗 **Live:** https://ddark-il.github.io/fuel98-israel/

Search by name or city, filter by brand, sort by distance from your location, and open any station directly in Waze.

## Data sources

The 98-octane station list for each brand comes from the source below. Station **locations** (coordinates) sometimes come from a different source than the **98-octane availability** — the table separates them where they differ.

| Brand | Stations & 98-octane data | Coordinates |
|-------|---------------------------|-------------|
| **פז** Paz | Official website | Official website |
| **סונול** Sonol | Official website | Official website |
| **דור אלון** Dor Alon | Official website | Official website |
| **מיקה** Mika | Official website | Geocoded from address &sup1; |
| **דלק** Delek | Locations: official website · **98 availability: user reports** &sup2; | Official website |
| **תפוז** Tapuz | User reports | Verified via Google Maps |
| **אחר** Other (יעד Yaad & small brands) | User reports | Verified via Google Maps |

&sup1; Mika's website does not expose map coordinates, so they are geocoded from each station's address.
&sup2; Delek's station locator does not indicate which stations carry 98-octane, so that detail is sourced from user reports.

> Note: *Ten / 10 / טן* stations are intentionally excluded — they do not offer 98-octane.

## Project structure

```
data/
  manifest.json     # lists the per-brand files + brand→file map
  paz.json          # one file per brand: [{ brand, name, coordinates:{lat,lon} }, …]
  sonol.json
  doralon.json
  mika.json
  delek.json
  tapuz.json
  others.json       # the "אחר" group
index.html          # the whole app: Leaflet map + list, brand filters, geolocation, Waze links
```

No build step — it's a static site. `index.html` fetches `data/manifest.json`, loads each brand file, and renders the map (Leaflet + OpenStreetMap tiles) and list. Because it uses `fetch()`, it must be served over HTTP (a `file://` open will load no stations); GitHub Pages serves it correctly.

The scrapers that generate this data live **outside** this repository (in the parent project) — this repo holds only the published data and the frontend.

## License

[WTFPL](LICENSE) — do what you want.
