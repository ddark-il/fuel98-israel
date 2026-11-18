#!/usr/bin/env node
/*
 * The Ministry's substandard-fuel list, by fuel-administration licence number.
 * Writes auto_poc/violation_licences.json: the StationNumber of every current entry, so
 * stations.json can mark a station by its ministry_id even when it is not one of our live
 * 326 (the live-data join in check-violations.mjs only matches those).
 *
 * Must run from an Israeli IP (the API is geo-restricted).
 */
const fs = require('fs');
const path = require('path');
const OUT = path.join(__dirname, 'violation_licences.json');
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
fetch('https://migdal-api.energy-apps.org/api/GetFuelGasStationDataForWeb', {
  headers: {
    'User-Agent': UA, 'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'he-IL,he;q=0.9,en;q=0.8',
    'Origin': 'https://migdal-webpages.energy-apps.org',
    'Referer': 'https://migdal-webpages.energy-apps.org/',
    'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty',
  },
}).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
  .then(list => {
    const rows = list.map(v => ({ licence: String(v.StationNumber), company: v.CompanyName,
      address: v.Address, city: v.City, sampling: v.SamplingDate, fuel: v.FuelType }));
    fs.writeFileSync(OUT, JSON.stringify(rows, null, 1));
    console.log(`wrote ${rows.length} violation licences -> ${OUT}`);
  }).catch(e => { console.error(e); process.exit(1); });
