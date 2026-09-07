from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tb_market_volume as volume


class MarketVolumeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.start = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)

    def book(self, start=None, seconds=120, value=100, **market):
        start = start or self.start
        return {'captured_at': (start - timedelta(seconds=seconds)).isoformat(),
                'market': {'track': 'Example', 'market_id': '1.2', 'market_start_time': start.isoformat(),
                           'inplay': False, 'currency': 'AUD', 'market_type': 'WIN',
                           'total_matched': value, **market}}

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def register(self, n=25):
        records = [{'market_id': f'old-{i}', 'start': (self.start - timedelta(days=i+1)).isoformat(),
                    'cohort': ['example', 'WIN', 'AUD'], 'slots': {'t2': 100, 't30': 200}} for i in range(n)]
        self.write(self.base/'state'/volume.STATE_FILE, {'generated_at': self.start.isoformat(), 'records': records})
        return records

    def test_quantiles_and_colors(self):
        stats = volume.stats(list(range(1, 101)))
        self.assertEqual(stats['median'], 50.5)
        self.assertEqual(stats['q25'], 25.75)
        self.assertEqual(volume.comparison(10, stats)[1], 'amber')
        self.assertEqual(volume.comparison(50, stats)[1], 'blue')
        self.assertEqual(volume.comparison(90, stats)[1], 'green')
        self.assertEqual(volume.comparison(10, volume.stats([5]))[1], 'grey')
        self.assertIsNone(volume.comparison(10, volume.stats([0]))[0])
        self.assertEqual(volume.stats([None, float('nan'), -1])['n'], 0)

    def test_capture_timing_and_inplay(self):
        self.assertIsNotNone(volume.capture(self.book(), 't2'))
        self.assertIsNone(volume.capture(self.book(seconds=600), 't2'))
        self.assertIsNone(volume.capture(self.book(inplay=True), 't2'))
        self.assertIsNone(volume.capture(self.book(inplay=None), 't2'))
        self.assertIsNone(volume.capture(self.book(seconds=-1), 't30'))
        self.assertIsNone(volume.capture(self.book(value=-1), 't2'))
        self.assertIsNotNone(volume.capture(self.book(value=0), 't2'))

    def test_register_only_completed_valid_snapshots(self):
        folder = self.base/'history'/'2026-09-06'/'1.1'
        self.write(folder/'closed.json', {'complete': True, 'has_result': True})
        self.write(folder/'market_book_t2.json', self.book(self.start-timedelta(days=1)))
        self.write(folder/'market_book_t30.json', self.book(self.start-timedelta(days=1), seconds=-10))
        result = volume.build_register(self.base, self.start)
        self.assertEqual(result['record_count'], 1)
        self.assertEqual(result['records'][0]['slots'], {'t2': 100})
        self.assertEqual(result['groups'][0]['stages']['t2']['median'], 100)
        self.write(folder/'closed.json', {'complete': False, 'has_result': True})
        self.assertEqual(volume.build_register(self.base, self.start)['record_count'], 0)

    def test_baseline_excludes_future_old_current_and_other_cohorts(self):
        records = self.register()
        for mid, days, identity in [('future', -1, ['example','WIN','AUD']), ('old', 61, ['example','WIN','AUD']), ('1.2', 1, ['example','WIN','AUD']), ('other', 1, ['example','WIN','GBP']), ('place', 1, ['example','PLACE','AUD'])]:
            records.append({'market_id': mid, 'start': (self.start-timedelta(days=days)).isoformat(), 'cohort': identity, 'slots': {'t2': 99999}})
        self.write(self.base/'state'/volume.STATE_FILE, {'records': records})
        _, groups = volume.volume_cache(self.base).get()
        result = volume.baseline(groups, ('example','WIN','AUD'), self.start, '1.2', 't2')
        self.assertEqual(result['n'], 25)
        self.assertEqual(result['median'], 100)

    def test_cache_reload_and_missing_data(self):
        self.register()
        cache = volume.volume_cache(self.base)
        with patch.object(volume, 'read_json', wraps=volume.read_json) as read:
            cache.get(); cache.get()
            self.assertEqual(read.call_count, 1)
            self.register(2)
            _, groups = cache.get()
            self.assertEqual(len(groups[('example','WIN','AUD')][0]), 2)
        (self.base/'state'/volume.STATE_FILE).write_text('corrupt')
        self.assertEqual(cache.get(), ({}, {}))
        (self.base/'state'/volume.STATE_FILE).unlink()
        self.assertEqual(cache.get(), ({}, {}))

    def test_panel_bars_trend_and_over_100(self):
        self.register()
        page = volume.panel(self.base, self.book(value=300), None, now=self.start-timedelta(seconds=100))
        self.assertIn('300%', page)
        self.assertIn('150%', page)
        self.assertIn('Above typical', page)
        self.assertIn('<svg', page)
        self.assertIn('25 races', page)
        self.assertIn('role="img"', page)

    def test_inplay_and_after_start_disable(self):
        self.register()
        for book, now in [(self.book(inplay=True), self.start-timedelta(seconds=100)), (self.book(), self.start+timedelta(seconds=10))]:
            page = volume.panel(self.base, book, None, now=now)
            self.assertIn('comparison disabled', page)
            self.assertNotIn('volume-track', page)

    def test_provisional_stale_and_unknown_states(self):
        self.register(2)
        page = volume.panel(self.base, self.book(), None, now=self.start-timedelta(seconds=100))
        self.assertIn('Provisional', page)
        self.assertIn('volume-fill grey', page)
        page = volume.panel(self.base, self.book(), None, live=False, now=self.start+timedelta(days=2))
        self.assertIn('stale', page)
        page = volume.panel(self.base, self.book(market_start_time=None), None)
        self.assertIn('start unavailable', page)

    def test_between_stages_uses_latest_saved_capture(self):
        self.register()
        folder = self.base/'history'/'2026-09-07'/'1.2'
        self.write(folder/'market_book_t2.json', self.book(value=100))
        page = volume.panel(self.base, self.book(seconds=60, value=150), folder, now=self.start-timedelta(seconds=50))
        self.assertIn('100 / 100 median', page)
        self.assertIn('150 / 200 median', page)
        self.assertIn('T−2 minutes', page)


if __name__ == '__main__':
    unittest.main()
