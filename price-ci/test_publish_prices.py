"""The published `reported_at` must follow the source: Waze's report time, Mika's scrape time."""
import unittest
from publish_prices import row_of

ST = {'brand': 'פז', 'name': 'בדיקה', 'lat': 32.0, 'lon': 34.0}


class ReportedAt(unittest.TestCase):
    def test_waze_uses_report_time(self):
        row = row_of({'price98': 8.5, 'price98_updated': 1790000000000}, ST)
        self.assertEqual(row['reported_at'], '2026-09-21T14:13:20Z')

    def test_mika_uses_scrape_time_not_page_modified(self):
        row = row_of({'price98': 7.4, 'price98_source': 'mika',
                      'price98_updated': 1780000000000,
                      'mika_scraped_at': '2026-10-28T05:00:00+00:00'}, ST)
        self.assertEqual(row['reported_at'], '2026-10-28T05:00:00Z')


if __name__ == '__main__':
    unittest.main()
