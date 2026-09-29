#!/usr/bin/env node
/*
 * Fuel-quality flagging — no geocoding service required.
 *
 * Fetches the Israeli Ministry of Energy "substandard fuel" list (stations where
 * off-spec fuel was found in the last 6 months) and matches it against our
 * stations through the Ministry's OWN station registry:
 *
 *   violations entry → StationNumber → registry row (מס_מינהל_הדלק) → WGS84 coords
 *                    → nearest of our stations within THRESHOLD_M
 *
 * Both lists come from the same ministry, so the join is exact and key-free: no
 * Google Places, no Nominatim, no API key, no rate limit, no geocoding that can
 * silently resolve to the wrong place.
 *
 * Every violation entry is accounted for in the output — matched, no registry row,
 * or no station of ours nearby — so an empty violations.json can never be the
 * result of a lookup that quietly failed.
 *
 * ⚠ Must run from an Israeli IP: the Ministry API is geo-restricted.
 * Run:  node scripts/check-violations.mjs
 */
import { readFileSync, writeFileSync, existsSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const DATA = join(dirname(fileURLToPath(import.meta.url)), "..", "data");
const VIOLATIONS_API = "https://migdal-api.energy-apps.org/api/GetFuelGasStationDataForWeb";
const REGISTRY_RID = "5537a0ef-3eeb-449c-90c8-51e27564f0cb"; // data.gov.il "gas-station"
const REGISTRY_CACHE = "/tmp/ministry-registry.json";
const CACHE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const THRESHOLD_M = 150; // a violation within this distance of our station = same station

const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

function haversine(a, b, c, d) {
  const R = 6371000, r = Math.PI / 180;
  const x = Math.sin((c - a) * r / 2) ** 2 + Math.cos(a * r) * Math.cos(c * r) * Math.sin((d - b) * r / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
}

async function fetchViolations() {
  // The API sits behind CloudFront and 403s without browser-like Sec-Fetch headers.
  const res = await fetch(VIOLATIONS_API, {
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

// The Ministry's own station registry, keyed by fuel-administration licence number.
// Falls back to a cached copy (with a warning) if data.gov.il is unreachable.
async function fetchRegistry() {
  let stale = null;
  if (existsSync(REGISTRY_CACHE)) {
    try {
      const cached = JSON.parse(readFileSync(REGISTRY_CACHE, "utf-8"));
      // the cache is shared with build-seo.py — it is only usable if rows carry the
      // licence number (the join key) and are not empty
      if (Array.isArray(cached) && cached.length && cached[0].licence) {
        stale = cached;
        const age = Date.now() - statSync(REGISTRY_CACHE).mtimeMs;
        if (age < CACHE_MAX_AGE_MS) return stale;
      }
    } catch {}
  }
  try {
    const res = await fetch(
      "https://data.gov.il/api/3/action/datastore_search?" +
        new URLSearchParams({ resource_id: REGISTRY_RID, limit: "2000" }),
      { headers: { "User-Agent": UA } }
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const recs = (await res.json()).result.records
      .map((r) => ({
        licence: String(r["מס_מינהל_הדלק"] ?? "").trim(),
        company: (r["חברה"] || "").trim(), name: (r["שם_תחנה"] || "").trim(),
        address: (r["כתובת"] || "").trim(), authority: (r["רשות_מקומית"] || "").trim(),
        lat: parseFloat(r["נ.צ. רוחב"]), lon: parseFloat(r["נ.צ. אורך"]),
      }))
      .filter((r) => r.licence && r.lat > 29 && r.lat < 34 && r.lon > 34 && r.lon < 36);
    writeFileSync(REGISTRY_CACHE, JSON.stringify(recs), "utf-8");
    return recs;
  } catch (e) {
    if (stale) {
      console.error(`⚠ registry fetch failed (${e.message}) — using cached copy from ${REGISTRY_CACHE}`);
      return stale;
    }
    throw new Error(`cannot load the Ministry registry and no cache at ${REGISTRY_CACHE}: ${e.message}`);
  }
}

function loadStations() {
  const manifest = JSON.parse(readFileSync(join(DATA, "manifest.json"), "utf-8"));
  return manifest.files.flatMap((f) => JSON.parse(readFileSync(join(DATA, f), "utf-8")));
}

const violations = await fetchViolations();
const registry = await fetchRegistry();
const byLicence = new Map(registry.map((r) => [r.licence, r]));
const stations = loadStations();

const flagged = [];
const seen = new Set();
const noRegistryRow = [];
const noStationNearby = [];

for (const v of violations) {
  const licence = String(v.StationNumber ?? "").trim();
  const reg = byLicence.get(licence);
  if (!reg) { noRegistryRow.push(v); continue; }

  let best = null, bestD = Infinity;
  for (const s of stations) {
    const d = haversine(reg.lat, reg.lon, s.coordinates.lat, s.coordinates.lon);
    if (d < bestD) { bestD = d; best = s; }
  }
  const entry = {
    brand: best?.brand, name: best?.name, distM: Math.round(bestD),
    fuelType: (v.FuelType || "").trim(),
    samplingDate: (v.SamplingDate || "").trim(),
    publishedDate: (v.PublishedDate || "").trim(),
    ministryName: (v.CompanyName || "").trim(),
    licence,
  };
  if (best && bestD <= THRESHOLD_M) {
    const key = `${best.brand}|${best.name}`;
    if (!seen.has(key)) { seen.add(key); flagged.push(entry); }
  } else {
    noStationNearby.push(entry);
  }
}

flagged.sort((a, b) => (a.brand + a.name).localeCompare(b.brand + b.name, "he"));
writeFileSync(
  join(DATA, "violations.json"),
  JSON.stringify(flagged.map(({ distM, ...f }) => f), null, 2) + "\n",
  "utf-8"
);

console.log(`Ministry list: ${violations.length} entries · registry rows: ${registry.length}`);
console.log(`matched to our stations: ${flagged.length} · not in our data: ${noStationNearby.length} · rejected (no registry row): ${noRegistryRow.length}`);
for (const f of flagged) console.log(`  ⚠️ ${f.brand}/${f.name} — ${f.fuelType}, sampled ${f.samplingDate} (${f.distM}m, licence ${f.licence})`);
for (const f of noStationNearby) console.log(`  ·  no station of ours within ${THRESHOLD_M}m: ${f.ministryName} (licence ${f.licence}, ${f.distM}m)`);
if (noRegistryRow.length) {
  console.log("  ‼  violations with no registry row — check manually:");
  for (const v of noRegistryRow) console.log(`       ${v.CompanyName} · ${v.Address} · ${v.City} (station #${v.StationNumber})`);
}
console.log(`wrote data/violations.json (${flagged.length} flags)`);
