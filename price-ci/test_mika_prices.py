#!/usr/bin/env python3
"""Tests for the Immunify360 bot-block handling in mika_prices.py.

The site intermittently answers our CI runner with its anti-bot wall (observed 2026-11-06 and
2026-11-07: `station list HTTP 403: Access denied by Imunify360 bot-protection`). A block must
be retried with a backoff and then reported as a soft-fail (exit 4), while a site change must
still fail hard (exit 1). No network here: fetch and sleep are patched.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mika_prices as mp

DENY = json.dumps({'message': 'Access denied by Imunify360 bot-protection. '
                              'IPs used for automation should be whitelisted'})
CHALLENGE = '<!DOCTYPE html><html><head><title>Just a moment</title></head><body>…</body></html>'
LIST_JSON = json.dumps([{'id': 1, 'slug': 's', 'link': 'https://mika.org.il/station/s',
                         'title': {'rendered': 'סונול תל חנן'}, 'modified': '2026-11-01'}])


class LooksBlocked(unittest.TestCase):
    def test_the_real_403_deny(self):
        self.assertTrue(mp.looks_blocked(403, DENY))

    def test_415_greylist_answer(self):
        # WebShield answers a greylisted IP's Accept: application/json request with 415
        # (docs.imunify360.com/features/, WebShield > Greylist and Anti-Bot Challenge).
        self.assertTrue(mp.looks_blocked(415, ''))

    def test_html_challenge_on_a_json_endpoint(self):
        self.assertTrue(mp.looks_blocked(200, CHALLENGE, want_json=True))
        self.assertFalse(mp.looks_blocked(200, LIST_JSON, want_json=True))

    def test_a_site_change_is_not_a_block(self):
        self.assertFalse(mp.looks_blocked(404, json.dumps({'code': 'rest_no_route'})))
        self.assertFalse(mp.looks_blocked(500, 'Internal Server Error'))


class StationListRetry(unittest.TestCase):
    def setUp(self):
        self._sleep, mp.time.sleep = mp.time.sleep, lambda s: None
        self._fetch = mp.fetch

    def tearDown(self):
        mp.time.sleep, mp.fetch = self._sleep, self._fetch

    def _feed(self, answers):
        it = iter(answers)

        def fake_fetch(url):
            return next(it)
        mp.fetch = fake_fetch

    def test_block_after_all_retries_is_flagged(self):
        self._feed([(403, DENY, '')] * (len(mp.MikaScraper.BLOCK_WAITS) + 1))
        sc = mp.MikaScraper(cache='/nonexistent-nav-cache.json')
        self.assertEqual(sc.stations(), [])
        self.assertTrue(sc.blocked)

    def test_block_that_decays_mid_retry_is_not_a_failure(self):
        self._feed([(403, DENY, '')] * 2 + [(200, LIST_JSON, '')])
        sc = mp.MikaScraper(cache='/nonexistent-nav-cache.json')
        rows = sc.stations()
        self.assertEqual([r['slug'] for r in rows], ['s'])
        self.assertFalse(sc.blocked)

    def test_a_404_is_not_flagged_as_a_block(self):
        self._feed([(404, json.dumps({'code': 'rest_no_route'}), '')]
                   * (len(mp.MikaScraper.BLOCK_WAITS) + 1))
        sc = mp.MikaScraper(cache='/nonexistent-nav-cache.json')
        self.assertEqual(sc.stations(), [])
        self.assertFalse(sc.blocked)


class PageTitleFallback(unittest.TestCase):
    def test_junk_seo_title_falls_back_to_the_post_title(self):
        # Seen live 2026-11-11: the Rishon page's <title> is "מיקה ראשון-תחנת דלק בראשון לציון"
        # (no spaces around the dash), so the "page name" parsed to the whole sentence and the
        # page matched nothing - then a price-less page took its station in the last-resort pass
        # and its 98 price stopped merging.
        ours = [{'name': 'ראשון', 'brand': 'מיקה', 'key': 'mika.json|ראשון|31.986506,34.772124',
                 'file': 'mika.json'}]
        row = {'mika_name': 'מיקה ראשון, משה לוי 7 ראשל"צ',
               'page_name': 'מיקה ראשון-תחנת דלק בראשון לציון'}
        joined, method, _ = mp.join_page_by_name(row, ours)
        self.assertEqual(joined.get('name'), 'ראשון')
        self.assertEqual(method, 'name-brand')


if __name__ == '__main__':
    unittest.main(verbosity=2)
