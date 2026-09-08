import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import tb_blackbook as bb
import tb_blackbook_wall as integration
import tb_wall as wall


class BlackbookWallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.market = {'market_id': '1.3', 'track': 'Test Track', 'market_name': 'R3 1200m Hcap', 'market_start_time': '2026-09-03T10:00:00Z'}
        self.wins = [dict(self.market, date='2026-09-01', market_id='1.1', market_start_time='2026-09-01T10:00:00Z', prices={'t15': 10, 't30': 8}),
                     dict(self.market, date='2026-09-03'),
                     dict(self.market, date='2026-09-04', market_id='1.4', market_start_time='2026-09-04T10:00:00Z')]
        self.entry = {'key': 'sid:10', 'selection_ids': [10], 'runner_name': 'Example', 'wins': self.wins, 'tags': ['watch'], 'notes': '<script>alert(1)</script>'}
        self.write()

    def write(self, entries=None):
        path = self.base / 'state' / bb.STATE_FILE_NAME
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'entries': [self.entry] if entries is None else entries}))

    def rows(self, sid='10', name='Example', market=None):
        rows = [{'selection_id': sid, 'name': name}]
        integration.enrich_rows(rows, market or self.market, self.base)
        return rows[0]['blackbook']

    def test_distance_formats(self):
        for label, expected in [('R1 1200m Mdn', 1200), ('1m2f Hcap', 2011.68), ('10f Hcap', 2011.68), ('2m 1f 110y Hcap', 3520.44), ('6f Hcap', 1207.008), ('Unknown', None), ('R3 Hcap', None)]:
            with self.subTest(label=label):
                self.assertEqual(bb.distance_metres(label), expected)
        self.assertNotEqual(bb.distance_metres('6f'), bb.distance_metres('1200m'))

    def test_counts_exclude_current_and_future_races(self):
        record = self.rows()
        self.assertEqual([record[k] for k in ('w', 't', 'd', 'td')], [1, 1, 1, 1])
        self.assertEqual(record['wins'][0]['market_id'], '1.1')
        self.assertEqual(self.rows(market={**self.market, 'market_name': 'Unknown'})['d'], None)

    def test_missing_timestamps_are_conservative(self):
        self.wins[0].pop('market_start_time')
        self.wins[1].pop('market_start_time')
        self.write()
        self.assertEqual(self.rows()['w'], 1)
        self.assertFalse(self.rows(market={**self.market, 'market_start_time': None})['available'])
        self.assertEqual(self.rows(market={**self.market, 'market_start_time': None})['w'], 0)

    def test_identity_fallback_and_ambiguity(self):
        self.assertEqual(self.rows(sid='99')['w'], 0)
        self.assertEqual(self.rows(sid='')['w'], 1)
        self.write([self.entry, {**self.entry, 'key': 'sid:20', 'selection_ids': [20]}])
        self.assertEqual(self.rows(sid='')['w'], 0)
        self.assertEqual(self.rows()['w'], 1)

    def test_cache_reload_missing_and_corrupt(self):
        with patch.object(integration, 'read_json', wraps=integration.read_json) as read:
            self.rows()
            self.rows()
            self.assertEqual(read.call_count, 1)
            self.entry['tags'].append('new tag')
            self.write()
            self.assertEqual(self.rows()['tags'], ['watch', 'new tag'])
            self.assertEqual(read.call_count, 2)
        path = self.base / 'state' / bb.STATE_FILE_NAME
        path.write_text('invalid json')
        self.assertFalse(self.rows()['available'])
        path.unlink()
        self.assertFalse(self.rows()['available'])

    def test_history_links_and_escaping(self):
        markup = integration.cell(self.rows())
        self.assertIn('/race/1.1', markup)
        self.assertNotIn('/race/1.4', markup)
        self.assertNotIn('<script>', markup)
        self.assertIn('&lt;script&gt;', markup)
        self.assertIn('T15: 10 → T30: 8', markup)
        self.assertIn('★', markup)

    def test_render_filter_sort_and_route(self):
        state = self.base / 'state'
        (state / 'active_market_book.json').write_text(json.dumps({'market': self.market, 'runners': [
            {'selection_id': 99, 'runner_name': 'Favourite'}, {'selection_id': 10, 'runner_name': 'Example'}]}))
        race = self.base / 'history' / '2026-09-03' / '1.3'
        race.mkdir(parents=True)
        for filename in ('market_book_t15.json', 'market_book_t30.json'):
            (race / filename).write_text(json.dumps({'market': self.market, 'runners': [
                {'selection_id': 99, 'last_price_traded': 2},
                {'selection_id': 10, 'last_price_traded': 5}]}))
        page = wall.html_page(self.base, sort_by='td')
        self.assertLess(page.index('>Example</td>'), page.index('>Favourite</td>'))
        filtered = wall.html_page(self.base, blackbook_only=True)
        self.assertNotIn('>Favourite</td>', filtered)
        self.assertIn('W 1 · T 1 · D 1 · T+D 1', filtered)
        handler = object.__new__(wall.WallHandler)
        handler.base_dir = self.base
        handler.path = '/?blackbook=1&sort=td'
        handler.send_text = Mock()
        handler.do_GET()
        self.assertNotIn('>Favourite</td>', handler.send_text.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
