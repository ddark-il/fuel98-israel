#!/usr/bin/env node
// Static guard for the per-station GoatCounter click events: the three Waze links (card, table,
// map popup) must carry the event attributes, and re-renders must re-bind the handlers.
import fs from 'node:fs';

const html = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const need = [
  'function wazeEventAttrs(s)',
  'data-goatcounter-click="waze-',
  'data-goatcounter-title="',
  'data-goatcounter-no-session="1"',
  'bindCounter();',
  "marker.on('popupopen', bindCounter)",
];
for (const n of need) {
  if (!html.includes(n)) {
    console.error(`missing: ${n}`);
    process.exit(1);
  }
}
const uses = (html.match(/\$\{wazeEventAttrs\(s\)\}/g) || []).length;
if (uses !== 3) {
  console.error(`expected 3 wazeEventAttrs() uses, found ${uses}`);
  process.exit(1);
}
console.log('ok: 3 Waze links carry per-station GoatCounter events');
