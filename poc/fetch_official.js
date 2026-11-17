#!/usr/bin/env node
/*
 * Fetch the FULL station lists from the brand websites (all stations, not only 98) into
 * auto_poc/official/*.json, because the crosscheck needs every official pin/address.
 *
 *   node auto_poc/fetch_official.js            # paz sonol doralon delek
 *   node auto_poc/fetch_official.js sonol      # one brand
 *
 * Adapted from ../update_stations.js, which filters to 98-octane stations; here every station is
 * kept and the 98 flag is recorded where the site exposes it. Ten is fetched by Python
 * (fetch_ten.py) from its JSON API; Mika from our own data + its WordPress API.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const OUT_DIR = path.join(__dirname, "official");
fs.mkdirSync(OUT_DIR, { recursive: true });

function save(brand, rows) {
  if (!rows || !rows.length) { console.log(`  ✗ ${brand}: no data`); return; }
  fs.writeFileSync(path.join(OUT_DIR, `${brand}.json`), JSON.stringify(rows, null, 1));
  console.log(`  ✓ ${brand}.json: ${rows.length} stations`);
}

async function fetchSonol(ctx) {
  const page = await ctx.newPage();
  await page.goto("https://www.sonol.co.il/gas-stations/", { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(6000);
  const stations = await page.evaluate(() => {
    const getCell = (a, label) => a.querySelector(`[data-label="${label}"]`)?.textContent?.trim() || "";
    return Array.from(document.querySelectorAll("article.shop-box")).map((a) => {
      const waze = a.querySelector('a[href*="waze"]')?.href || "";
      const m = waze.match(/[?&]lat=([\d.]+).*?[?&]lon=([\d.]+)/);
      return {
        name: getCell(a, "שם התחנה"),
        city: getCell(a, "עיר"),
        area: getCell(a, "איזור"),
        address: a.innerText.replace(/\s+/g, " ").slice(0, 200),
        has98: !!a.querySelector(".branch-ficon-petrol98"),
        services: Array.from(a.querySelectorAll(".branch-params span[aria-label]"))
          .map((x) => x.getAttribute("aria-label")).filter(Boolean),
        waze_link: waze,
        lat: m ? parseFloat(m[1]) : null,
        lon: m ? parseFloat(m[2]) : null,
      };
    });
  });
  await page.close();
  return stations.filter((s) => s.name);
}

async function fetchDoralon(ctx) {
  const page = await ctx.newPage();
  await page.goto("https://www.doralon.co.il/station/", { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(10000);
  const stations = await page.evaluate(() => {
    return Array.from(document.querySelectorAll(".sl__item")).map((item) => {
      const services = Array.from(item.querySelectorAll(".sl__item-services li"))
        .map((li) => li.textContent.trim());
      const hoursEl = item.querySelector(".sl__item-hours");
      const hours = {};
      if (hoursEl) {
        hoursEl.querySelectorAll("span").forEach((span) => {
          const text = span.textContent.trim();
          const i = text.indexOf(":");
          if (i > 0) hours[text.substring(0, i).trim()] = text.substring(i + 1).trim();
        });
      }
      return {
        name: item.querySelector(".sl__item-title")?.textContent?.trim() || "",
        address: item.querySelector(".sl__items-address")?.textContent?.trim() || "",
        phone: item.querySelector(".sl__item-phone")?.textContent?.trim() || null,
        lat: item.dataset.lat ? parseFloat(item.dataset.lat) : null,
        lon: item.dataset.lng ? parseFloat(item.dataset.lng) : null,
        has98: services.some((s) => s.includes("98")),
        services,
        opening_hours: hours,
      };
    });
  });
  await page.close();
  return stations.filter((s) => s.name);
}

async function fetchPaz(ctx) {
  const page = await ctx.newPage();
  await page.goto("https://www.paz.co.il/service-locator?metadata=%5B16%5D", { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForFunction(() => {
    const c = self.__next_f || [];
    return c.some((x) => Array.isArray(x) && typeof x[1] === "string" && x[1].includes("product98"));
  }, { timeout: 20000 });
  const stations = await page.evaluate(() => {
    const chunks = self.__next_f || [];
    let raw = "";
    for (const chunk of chunks) {
      if (Array.isArray(chunk) && chunk.length >= 2 && typeof chunk[1] === "string"
          && chunk[1].includes("stations") && chunk[1].includes("product98")) { raw = chunk[1]; break; }
    }
    if (!raw) return [];
    const start = raw.indexOf("[", raw.indexOf('"stations"'));
    let depth = 0, end = start, inStr = false, esc = false;
    for (let i = start; i < raw.length; i++) {
      const ch = raw[i];
      if (esc) { esc = false; continue; }
      if (ch === "\\") { esc = true; continue; }
      if (ch === '"') { inStr = !inStr; continue; }
      if (!inStr) { if (ch === "[") depth++; else if (ch === "]" && --depth === 0) { end = i + 1; break; } }
    }
    try { return JSON.parse(raw.substring(start, end)); } catch (_) { return []; }
  });
  await page.close();
  return stations.map((s) => ({
    name: (s.name || "").replace(/\s+/g, " ").trim(),
    city: s.city, address: (s.address || "").replace(/\s+/g, " ").trim(),
    lat: s.geoLocation?.latitude ?? null, lon: s.geoLocation?.longitude ?? null,
    has98: s.product98 === true,
    services: Array.isArray(s.metaData) ? s.metaData.map((m) => m.name) : [],
  })).filter((s) => s.name);
}

async function fetchDelek(ctx) {
  const page = await ctx.newPage();
  let nonce = null;
  page.on("request", (req) => {
    if (nonce || !req.url().includes("admin-ajax")) return;
    const m = (req.postData() || "").match(/name="nonce"\r?\n\r?\n([^\r\n]*)/);
    if (m) nonce = m[1];
  });
  await page.goto("https://delek.co.il/%D7%90%D7%99%D7%AA%D7%95%D7%A8-%D7%AA%D7%97%D7%A0%D7%94/", { waitUntil: "domcontentloaded", timeout: 45000 });
  for (let i = 0; i < 30 && !nonce; i++) await page.waitForTimeout(500);
  if (!nonce) throw new Error("no admin-ajax nonce");
  const stations = await page.evaluate(async (nonce) => {
    const out = [], seen = new Set();
    let p = 1;
    for (let i = 0; i < 60; i++) {
      const fd = new FormData();
      fd.append("search", ""); fd.append("action", "sq_get_stations"); fd.append("page", String(p));
      fd.append("nonce", nonce);
      const res = await fetch("/wp-admin/admin-ajax.php", { method: "POST", body: fd });
      const json = await res.json().catch(() => null);
      const batch = json?.data?.stations || [];
      if (!batch.length) break;
      for (const s of batch) if (!seen.has(s.id)) { seen.add(s.id); out.push(s); }
      const next = json?.data?.next;
      if (!next || next === p) break;
      p = next;
    }
    return out;
  }, nonce);
  await page.close();
  return stations.map((s) => ({
    name: (s.name || "").trim(), city: (s.city || "").trim(), address: (s.address || "").trim(),
    lat: s.lat ? parseFloat(s.lat) : null, lon: s.lang ? parseFloat(s.lang) : null,
    has98: null,   // Delek's locator exposes no fuel grades
    hours: (s.hours || "").replace(/<br\s*\/?>/gi, " ").replace(/\s+/g, " ").trim(),
  }));
}

(async () => {
  const only = process.argv.slice(2).map((s) => s.toLowerCase());
  const want = (b) => !only.length || only.includes(b);
  const browser = await chromium.launch({ headless: true, args: ["--disable-blink-features=AutomationControlled"] });
  const ctx = await browser.newContext({
    userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    locale: "he-IL", viewport: { width: 1366, height: 900 },
  });
  await ctx.addInitScript(() => { Object.defineProperty(navigator, "webdriver", { get: () => undefined }); });
  try {
    if (want("sonol")) save("sonol", await fetchSonol(ctx));
    if (want("doralon")) save("doralon", await fetchDoralon(ctx));
    if (want("paz")) save("paz", await fetchPaz(ctx));
    if (want("delek")) save("delek", await fetchDelek(ctx));
  } finally {
    await browser.close();
  }
  console.log("done");
})().catch((e) => { console.error(e); process.exit(1); });
