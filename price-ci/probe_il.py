#!/usr/bin/env python3
"""Can *this* machine reach the distributor cluster that carries fuel prices?

`rt.waze.com` answers a GetRequest from one of two Waze clusters, chosen by their edge, and the two
are not equivalent: `realtime-frontend-prod-il-*` returns the venue with its `product[]` list,
`realtime-frontend-prod-row-*` (rest of world) returns the same venue with no products at all. From
an Israeli line the IL cluster answers some of the time - 1 in 8 connections in a quiet moment, 0 in
256 while a sweep was hammering - and the sweep therefore pins a connection that lands on it and
reads everything over that.

This probe answers the question for a *given* machine, which is what decides whether the Waze layer
can be part of a scheduled run at all: open N connections, count how many answer from `il`, and (if
any) read the canary stations over one of them. It is diagnostic only - run it on a new runner, a
VPS or a laptop and compare the numbers before trusting that machine with the sweep.

    python3 probe_il.py                 # 64 connections
    python3 probe_il.py -n 256          # more samples when the rate is low
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('pc', os.path.join(_HERE, 'price_check.py'))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


def _sample(pc, st, a):
    """Open `a.connections` connections and return the ones answered by the IL cluster."""
    checker = pc.PriceChecker(state=a.state, sleep=0.2, tries=1, rounds=1, parallel=a.batch)
    found, opened = [], 0
    while opened < a.connections:
        n = min(a.batch, a.connections - opened)
        found += checker._pin_batch(st, n)
        opened += n
        print(f'  {opened:>4} connection(s): {len(found)} from the IL cluster', flush=True)
    return found, opened


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('-n', '--connections', type=int, default=64, help='connections to sample')
    ap.add_argument('--batch', type=int, default=16, help='connections opened at once')
    ap.add_argument('--compare', action='store_true',
                    help='sample without the cookie first, then with it')
    ap.add_argument('--state', default=os.path.join(_HERE, 'waze_account.json'))
    a = ap.parse_args()

    stations = pc.load_stations(pc.DEFAULT_DATA_GLOB)
    if not stations:
        print('ERROR: no stations loaded - wrong data path', flush=True)
        return 3
    st = [s for s in stations if pc.CANARY[0] in s['name']][0]

    cookie = os.environ.get('WAZE_SESSION_COOKIE', '')
    print(f'  WAZE_SESSION_COOKIE: {"set (" + str(len(cookie)) + " chars)" if cookie else "not set"}',
          flush=True)
    if cookie and a.compare:
        # Same machine, same moment, same account: with the affinity cookie and without it. This is
        # the only way to tell whether the cookie (rather than the network) is what selects the IL
        # frontend.
        os.environ.pop('WAZE_SESSION_COOKIE', None)
        print('  control run without it:', flush=True)
        _sample(pc, st, a)
        os.environ['WAZE_SESSION_COOKIE'] = cookie
        print('  run with the cookie:', flush=True)
    found, opened = _sample(pc, st, a)
    rate = len(found) / max(1, opened)
    print(f'\n{len(found)}/{opened} connections answered from the IL cluster ({rate:.1%})')
    if not found:
        print('This machine is not being given the cluster that carries fuel prices. A sweep here '
              'would produce rows with no prices and, worse, they would look like "no 98 reported".'
              ' Do not schedule the Waze layer on this machine.', flush=True)
        return 0

    for pin in found[:2]:
        for name in pc.CANARY:
            m = [s for s in stations if name in s['name']]
            if not m:
                continue
            s = m[0]
            prods, cluster = checker._get_over(pin.s, pin.conn, s, rid=None)
            pid, entry = pc.wp.best_98(prods)
            print(f'  canary over a pinned connection: {s["brand"]}/{s["name"]:<16} '
                  f'cluster={cluster or "?":<4} products={len(prods):<2} '
                  f'98={entry["price"] if pid else "-"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
