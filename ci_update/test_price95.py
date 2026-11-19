#!/usr/bin/env python3
"""Tests for the self-service 95 price: which Waze product it is, and how Mika labels it.

`stations.json` carries `price95` next to the 98 `price`. Both figures must describe the same
thing - the self-service grade - so the two sources' labels are pinned here.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mika_prices as mp                                              # noqa: E402
import waze_prices as wp                                              # noqa: E402


class Waze95(unittest.TestCase):
    def test_picks_the_self_service_grade(self):
        prods = {'gas_regular': {'price': 7.55, 'last_updated': 1, 'updated_by': 'a'},
                 'gas_regularself': {'price': 7.31, 'last_updated': 2, 'updated_by': 'b'}}
        pid, entry = wp.best_95(prods)
        self.assertEqual(pid, 'gas_regularself')
        self.assertEqual(entry['price'], 7.31)
        self.assertEqual(entry['last_updated'], 2)

    def test_a_full_service_only_venue_has_no_95(self):
        # the 2026-10 sweep saw no such venue (46 reported both, 29 self only, 0 service only);
        # if one appears, storing the serviced figure as `price95` would mislabel it
        self.assertEqual(wp.best_95({'gas_regular': {'price': 7.55}}), (None, None))

    def test_no_products(self):
        self.assertEqual(wp.best_95({}), (None, None))


class Mika95(unittest.TestCase):
    def test_plain_95_is_self_service(self):
        html = ('<li><span class="sub-title">בנזין 95</span>'
                '<span class="value">7.45 ₪</span></li>')
        self.assertEqual(mp.parse_prices(html)['95']['price'], 7.45)

    def test_serviced_95_is_not_price95(self):
        html = ('<li><span class="sub-title">בנזין 95 שרות</span>'
                '<span class="value">7.75 ₪</span></li>')
        prices = mp.parse_prices(html)
        self.assertNotIn('95', prices)
        self.assertEqual(prices['95_service']['price'], 7.75)


if __name__ == '__main__':
    unittest.main()
