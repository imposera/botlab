import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import tb_wall as wall


class WallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        wall.history_index.cache_clear()

    def race(self, market_id, hour):
        race = self.base / 'history' / '2026-09-06' / market_id
        race.mkdir(parents=True)
        (race / 'result.json').write_text('{"complete": true}')
        (race / 'market_book_t30.json').write_text(json.dumps({
            'market': {'market_start_time': f'2026-09-06T{hour:02d}:00:00Z'},
            'runners': [],
        }))
        return race

    def test_matched_amounts(self):
        cases = [
            ({"total_matched": 0}, 0),
            ({"totalMatched": 125.5}, 125.5),
            ({"total_matched": None, "totalMatched": 20}, 20),
            ({"total_matched": 0, "traded_levels": [{"size": 10}, {"size": 15}]}, 25),
            ({"ex": {"tradedVolume": [{"size": 42}]}}, 42),
            ({"total_matched": 100, "traded_levels": [{"size": 10}]}, 100),
            ({"traded_levels": [{"size": 10}, {"size": "bad"}]}, None),
            ({"total_matched": float("nan")}, None),
            ({"total_matched": -1}, None),
            ({}, None),
        ]
        for runner, expected in cases:
            with self.subTest(runner=runner):
                self.assertEqual(wall.matched_from_runner(runner), expected)
        rows = wall.build_rows({"runners": [{"selection_id": 1, "total_matched": 0}]}, {}, None)
        self.assertEqual(wall.fmt_money(rows[0]["matched"]), "0")

    def test_navigation_completed_current(self):
        self.race('1.1', 1)
        self.race('1.2', 2)
        nav = wall.history_navigation(self.base, None, '1.2')
        self.assertEqual(nav['previous']['market_id'], '1.1')
        nav = wall.history_navigation(self.base, '1.1', '1.2')
        self.assertEqual(nav['next'], {'market_id': '1.2', 'live': True})
        self.assertIsNone(wall.history_navigation(self.base, '1.2', '1.2')['next'])

    def test_empty_single_and_unknown(self):
        self.assertIsNone(wall.history_navigation(self.base, None, '')['previous'])
        self.race('1.1', 1)
        wall.history_index.cache_clear()
        self.assertIsNone(wall.history_navigation(self.base, None, '1.1')['previous'])
        self.assertIsNone(wall.history_navigation(self.base, 'missing', '1.1')['next'])
        self.assertEqual(wall.history_navigation(self.base, '1.1', '1.2')['next'],
                         {'market_id': '1.2', 'live': True})

    def test_cache_reuse_refresh_and_concurrency(self):
        first = self.race('1.1', 1)
        with patch.object(wall, 'scan_completed_history', wraps=wall.scan_completed_history) as scan:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: wall.completed_history(self.base), range(16)))
            self.assertEqual(scan.call_count, 1)
            self.assertTrue(all(len(result) == 1 for result in results))
            self.race('1.2', 2)
            (first / 'result.json').unlink()
            self.assertEqual(wall.completed_history(self.base)[0]['market_id'], '1.1')
            index = wall.history_index(self.base.resolve())
            with patch.object(wall.time, 'monotonic', return_value=index.expires_at + 1):
                self.assertEqual([e['market_id'] for e in wall.completed_history(self.base)], ['1.2'])
            self.assertEqual(scan.call_count, 2)

    def test_previous_route_and_page_version(self):
        self.race('1.1', 1)
        self.race('1.2', 2)
        state = self.base / 'state'
        state.mkdir()
        (state / 'betfair_t15_target.json').write_text('{"market":{"market_id":"1.2"}}')
        handler = object.__new__(wall.WallHandler)
        handler.base_dir = self.base
        handler.path = '/prev'
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.do_GET()
        handler.send_header.assert_called_with('Location', '/race/1.1')
        self.assertIn(f'v{wall.VERSION}', wall.html_page(self.base))
        self.assertEqual(wall.json_status(self.base)['version'], wall.VERSION)
        self.assertNotIn('http-equiv="refresh"', wall.html_page(self.base, '1.1'))

    def test_shape_badges_classify_paths_and_explain_available_stages(self):
        cases = [
            ([10, 8, 5], 'steady_firm', 'Steady firm'),
            ([5, 8, 10], 'steady_drift', 'Steady drift'),
            ([10, 12, 8], 'late_firm', 'Late firm'),
            ([10, 5, 10], 'v_shape', 'V-shape'),
            ([10, 5, 15, 5, 10], 'whipsaw', 'Whipsaw'),
            ([10, 10.1, 10], 'flat_hold', 'Flat hold'),
            ([10], 'insufficient', 'Insufficient'),
            ([], 'insufficient', 'Insufficient'),
        ]
        for values, category, label in cases:
            with self.subTest(category=category, values=values):
                prices = dict(zip(wall.STAGES, values))
                row = wall.build_rows({'runners': [{'selection_id': 1}]}, {'sid:1': prices}, None)[0]
                self.assertEqual(row['shape_class'], category)
                markup = wall.shape_badge(row, live=True)
                self.assertIn(f'shape-{category}', markup)
                self.assertIn(label + ' · so far', markup)
                self.assertIn('Available stages:', markup)
                self.assertIn('First → last:', markup)
                self.assertIn('aria-label=', markup)
                self.assertNotIn('so far', wall.shape_badge(row, live=False))
                if len(values) < 2:
                    self.assertIsNone(row['move'])
                    self.assertIn('Net movement: —', markup)

    def test_shape_rendering_preserves_winner_and_live_history_context(self):
        race = self.race('1.1', 1)
        market = {'market_id': '1.1', 'market_start_time': '2026-09-06T01:00:00Z'}
        runner = {'selection_id': 1, 'runner_name': 'Example', 'last_price_traded': 10}
        first = {'market': market, 'runners': [runner]}
        last = {'market': market, 'runners': [{**runner, 'last_price_traded': 5}]}
        (race / 'market_book_t15.json').write_text(json.dumps(first))
        (race / 'market_book_t30.json').write_text(json.dumps(last))
        (race / 'result.json').write_text(json.dumps({'winner': runner}))
        state = self.base / 'state'
        state.mkdir()
        (state / 'active_market_book.json').write_text(json.dumps(last))
        live = wall.html_page(self.base)
        historical = wall.html_page(self.base, '1.1')
        for page in (live, historical):
            self.assertIn('<tr class="winner">', page)
            self.assertIn('▼ · Steady firm', page)
            self.assertIn('Shape color legend', page)
            self.assertIn('T−15m, T−30s', page)
            self.assertIn('Net movement: -50.0%', page)
            self.assertIn('shape-badge:focus-visible', page)
        self.assertIn('▼ · Steady firm · so far</span>', live)
        self.assertIn('▼ · Steady firm</span>', historical)

    def test_shape_uses_review_classifier(self):
        from tb_review import shape_class
        self.assertIs(wall.shape_class, shape_class)


if __name__ == '__main__':
    unittest.main()
