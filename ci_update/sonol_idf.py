#!/usr/bin/env python3
"""Refresh the Sonol stations that accept the IDF fuel card (דלקן צה״לי).

Source: https://www.sonol.co.il/gas-stations/sonol_idf/ — a server-rendered table with
columns עיר / כתובת / שם תחנה / מס' תחנה. Sonol's station number is stable and matches the
numbering used by the IDF workbook, so the join is by number (idf_sonol_numbers.json), not
by station name.

The script is deliberately fail-soft: if the page is unreachable or no longer a table,
the previous idf_sonol.json stays in place and the run continues. A new station number
that is not in the mapping is reported as unmatched and does not silently become an IDF
station.
"""
from __future__ import annotations

import datetime as dt
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
URL = 'https://www.sonol.co.il/gas-stations/sonol_idf/'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


def fetch() -> str | None:
    for delay in (0, 5, 20):
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(URL, headers={'User-Agent': UA,
                                                       'Accept': 'text/html,application/xhtml+xml'})
            with urllib.request.urlopen(req, timeout=45) as r:
                if r.status == 200:
                    return r.read().decode('utf-8', 'replace')
                print(f'Sonol IDF: HTTP {r.status}', flush=True)
        except Exception as exc:                                   # noqa: BLE001
            print(f'Sonol IDF: fetch failed ({exc})', flush=True)
    return None


def parse(body: str) -> list[dict]:
    rows = []
    for tr in re.findall(r'<tr\b.*?</tr>', body, re.I | re.S):
        cells = []
        for td in re.findall(r'<td\b.*?</td>', tr, re.I | re.S):
            text = re.sub(r'<[^>]+>', ' ', td)
            text = html_mod.unescape(text)
            text = re.sub(r'\s+', ' ', text).strip()
            cells.append(text)
        if len(cells) >= 4 and cells[3].strip().isdigit():
            rows.append({'city': cells[0], 'address': cells[1],
                         'name': cells[2], 'no': cells[3].strip()})
    return rows


def main() -> int:
    body = fetch()
    if not body:
        print('Sonol IDF: keeping the previous file', flush=True)
        return 0
    rows = parse(body)
    if len(rows) < 100:
        print(f'Sonol IDF: only {len(rows)} rows parsed - keeping the previous file', flush=True)
        return 0
    num_path = os.path.join(HERE, 'idf_sonol_numbers.json')
    numbers = json.load(open(num_path, encoding='utf-8')).get('numbers', {})
    licences, unmatched = set(), []
    for row in rows:
        lic = numbers.get(row['no'])
        if lic:
            licences.add(str(lic))
        else:
            unmatched.append({'no': row['no'], 'name': row['name'], 'city': row['city'],
                              'address': row['address']})
    out = {
        'source': URL,
        'updated': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'rows': len(rows),
        'matched': len(licences),
        'licences': sorted(licences, key=int),
        'unmatched': unmatched,
    }
    path = os.path.join(HERE, 'idf_sonol.json')
    tmp = path + '.tmp'
    json.dump(out, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    print(f'Sonol IDF: {len(rows)} rows, {len(licences)} matched, '
          f'{len(unmatched)} unmatched', flush=True)
    if unmatched:
        print('  unmatched: ' + ', '.join(f"{u['no']} {u['name']}" for u in unmatched), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
