#!/usr/bin/env node
/*
 * Fuel-quality flagging.
 *
 * Fetches the Israeli Ministry of Energy "substandard fuel" list (stations where
 * off-spec fuel was found in the last 6 months), matches each entry against our
 * stations by geocoded coordinate, and writes data/violations.json — the list of
 * OUR stations to flag in the app.
 *
 * Any fuel type counts (petrol / diesel / LPG): a station that fails a quality
 * check is flagged regardless, since 98-octane engines are sensitive to bad fuel.
 *
 * Geocoding: Google Places (New) if MAPS_API_KEY is set (robust), else Nominatim
 * (keyless, resolves clean street addresses only). No npm dependencies.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const DATA = join(dirname(fileURLToPath(import.meta.url)), "..", "data");
const API = "https://migdal-api.energy-apps.org/api/GetFuelGasStationDataForWeb";
const KEY = process.env.MAPS_API_KEY || "";
const THRESHOLD_M = 150; // a violation within this distance of one of our stations = same station

const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

function haversine(a, b, c, d) {
  const R = 6371000, r = Math.PI / 180;
  const x = Math.sin((c - a) * r / 2) ** 2 + Math.cos(a * r) * Math.cos(c * r) * Math.sin((d - b) * r / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
}

async function fetchViolations() {
  // The API sits behind CloudFront and 403s without browser-like Sec-Fetch headers.
  const res = await fetch(API, {
    headers: {
      "User-Agent": UA,
      "Accept": "application/json, text/plain, */*",
      "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
      "Origin": "https://migdal-webpages.energy-apps.org",
      "Referer": "https://migdal-webpages.energy-apps.org/",
      "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
    },
  });
  if (!res.ok) throw new Error(`Ministry API returned ${res.status}`);
  return res.json();
}

async function geocodeGoogle(q) {
  const res = await fetch("https://places.googleapis.com/v1/places:searchText", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Goog-Api-Key": KEY, "X-Goog-FieldMask": "places.location" },
    body: JSON.stringify({ textQuery: q, languageCode: "iw", regionCode: "IL" }),
  });
  if (!res.ok) return null;
  const p = ((await res.json()).places || [])[0];
  return p?.location ? [p.location.latitude, p.location.longitude] : null;
}

async function geocodeNominatim(q) {
  const url = "https://nominatim.openstreetmap.org/search?" +
    new URLSearchParams({ q, format: "json", limit: "1", countrycodes: "il" });
  const res = await fetch(url, { headers: { "User-Agent": "fuel98-israel-violation-check/1.0" } });
  if (!res.ok) return null;
  const d = await res.json();
  return d[0] ? [parseFloat(d[0].lat), parseFloat(d[0].lon)] : null;
}

async function geocode(v) {
  if (KEY) {
    const g = await geocodeGoogle(`${v.CompanyName} ${v.Address} ${v.City}`.replace(/\s+/g, " ").trim());
    if (g) return g;
  }
  const n = await geocodeNominatim(`${v.Address}, ${v.City}`.trim());
  if (!KEY) await new Promise((r) => setTimeout(r, 1200)); // Nominatim: max ~1 req/s
  return n;
}

function loadStations() {
  const manifest = JSON.parse(readFileSync(join(DATA, "manifest.json"), "utf-8"));
  return manifest.files.flatMap((f) => JSON.parse(readFileSync(join(DATA, f), "utf-8")));
}

const violations = await fetchViolations();
const stations = loadStations();
const flagged = [];
const seen = new Set();

for (const v of violations) {
  let coord = null;
  try { coord = await geocode(v); } catch (e) { console.error(`geocode failed: ${v.CompanyName}: ${e.message}`); }
  if (!coord) continue;
  let best = null, bestD = Infinity;
  for (const s of stations) {
    const d = haversine(coord[0], coord[1], s.coordinates.lat, s.coordinates.lon);
    if (d < bestD) { bestD = d; best = s; }
  }
  if (best && bestD <= THRESHOLD_M) {
    const key = `${best.brand}|${best.name}`;
    if (seen.has(key)) continue;
    seen.add(key);
    flagged.push({
      brand: best.brand,
      name: best.name,
      fuelType: (v.FuelType || "").trim(),
      samplingDate: (v.SamplingDate || "").trim(),
      publishedDate: (v.PublishedDate || "").trim(),
      ministryName: (v.CompanyName || "").trim(),
    });
  }
}

flagged.sort((a, b) => (a.brand + a.name).localeCompare(b.brand + b.name, "he"));
writeFileSync(join(DATA, "violations.json"), JSON.stringify(flagged, null, 2) + "\n", "utf-8");
console.log(`Ministry list: ${violations.length} stations · matched in our data: ${flagged.length}`);
for (const f of flagged) console.log(`  ⚠️ ${f.brand}/${f.name}  (${f.fuelType}, sampled ${f.samplingDate})`);
