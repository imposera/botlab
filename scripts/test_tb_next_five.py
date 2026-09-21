from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import http.client
import importlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import tb_next_five as wall
from tb_au_snapshot_analysis import SLOTS, analyse_race
from tb_today_card import merge_card


class NextFiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base/'state').mkdir()
        self.now = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)
        self.day = '2026-09-21'
        self.mid = '1.99'
        self.start = self.now+timedelta(seconds=20)
        self.market = {'market_id': self.mid, 'market_start_time': self.start.isoformat(),
                       'country_code': 'AU', 'market_name': 'R1', 'track': '<Grafton>',
                       'runners': [{'selection_id': i, 'runner_name': f'Horse {i}'} for i in range(1, 6)]}
        self.book = {'marketId': self.mid, 'status': 'OPEN', 'inplay': False,
                     'isMarketDataDelayed': False, 'numberOfActiveRunners': 5,
                     'runners': [{'selectionId': i, 'status': 'ACTIVE'} for i in range(1, 6)]}
        self.card = merge_card({}, [self.market], [self.book], self.now, discovered=True)
        self.save('state/tb_today_card.json', self.card)
        self.save('state/tb_au_observations.json', {'updated_at': self.now.isoformat(), 'races': {
            self.mid: {'date': self.day, 'market': self.market, 'slots': {}}}})
        self.paths = {}
        # One clear firm, drift, recovery, rebound, and stable runner.
        sequences = ([8, 7, 6, 5, 4], [4, 5, 6, 7, 8], [5, 6, 7, 6, 5], [7, 6, 5, 6, 7], [10]*5)
        for j, (slot, offset) in enumerate(zip(SLOTS, (900, 600, 300, 120, 30))):
            stamp = (self.start-timedelta(seconds=offset)).isoformat()
            book = {**self.book, 'runners': [{**r, 'lastPriceTraded': sequences[i][j]} for i, r in enumerate(self.book['runners'])]}
            capture = {'schema': 'tb_au_snapshot/v1', 'market_id': self.mid, 'slot': slot,
                       'captured_at': stamp, 'due_at': stamp, 'late_seconds': 0, 'book': book}
            self.paths[slot] = f'observations/au/{self.day}/{self.mid}/{slot}.json'
            self.save(self.paths[slot], capture)
        self.forms = {str(i): {'baseline': 55, 'pre_race': True,
                             'starts': [{'date': '2026-08-01', 'rating': 55, 'venue': 'Example'}],
                             'imported_at': (self.now-timedelta(hours=1)).isoformat()} for i in range(1, 6)}
        self.save_forms()

    def save(self, name, value):
        path = self.base/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def save_forms(self):
        self.save('state/tb_ra_form.json', {'days': {self.day: {self.mid: self.forms}}})

    def mutate(self, slot, function):
        value = json.loads((self.base/self.paths[slot]).read_text())
        function(value)
        self.save(self.paths[slot], value)

    def analysis(self, **kw):
        return analyse_race(self.base, self.day, self.mid, self.market, now=kw.pop('now', self.now), **kw)

    def test_complete_rank_and_columns(self):
        data = wall.summary(self.base, now=self.now)
        race = data['races'][0]
        self.assertEqual(data['freshness'], 'fresh')
        self.assertTrue(data['observer_fresh'])
        self.assertEqual([r['selection_id'] for r in race['runners']], ['2', '1', '3'])
        self.assertEqual(race['qualifier_count'], 4)
        r = race['runners'][0]
        self.assertEqual((r['base'], r['class'], r['latest_slot'], r['latest_price']), (55, 'Steady drift', 'T30', 8))
        self.assertAlmostEqual(r['move'], 100)
        self.assertAlmostEqual(r['chance']*r['fair_odds'], 100)
        self.assertFalse(r['partial'])
        page = wall.render(data)
        self.assertIn('&lt;Grafton&gt;', page)
        self.assertNotIn('<Grafton>', page)
        for heading in ('Base', 'Chance', 'Fair odds', 'Move', 'Class', 'Latest'):
            self.assertIn('<th>'+heading+'</th>', page)

    def test_chronological_five_including_removed_queue_race(self):
        self.card['races'] = [{**self.card['races'][0], 'market_id': f'1.{i}',
                              'market_start_time': (self.now+timedelta(minutes=i)).isoformat()}
                             for i in (8, 2, 7, 3, 6, 4, 5, 1)]
        self.card['races'][0]['country_code'] = 'NZ'
        self.card['races'][-1]['inplay'] = True
        self.save('state/tb_today_card.json', self.card)
        self.save('state/tb_race_queue.json', {'races': [], 'decisions': {'1.2': {'action': 'remove'}}})
        races = wall.summary(self.base, now=self.now)['races']
        self.assertEqual([r['market_id'] for r in races], ['1.2','1.3','1.4','1.5','1.6'])
        self.assertEqual(races[0]['selection_status'], 'removed')

    def test_started_and_closed_excluded(self):
        for key, value in [('market_start_time', self.now.isoformat()), ('market_status', 'CLOSED')]:
            saved = dict(self.card['races'][0])
            self.card['races'][0][key] = value
            self.save('state/tb_today_card.json', self.card)
            self.assertEqual(wall.summary(self.base, now=self.now)['races'], [])
            self.card['races'][0] = saved

    def test_partial_missing_fields_and_common_latest(self):
        (self.base/self.paths['T10']).unlink()
        self.mutate('T30', lambda c: c['book']['runners'][1].pop('lastPriceTraded'))
        result = self.analysis()
        self.assertTrue(all(r['partial'] for r in result['runners']))
        self.assertNotIn('2', [r['selection_id'] for r in result['runners']])
        self.assertTrue(all('T10' in r['missing'] for r in result['runners']))

    def test_no_mixed_market_probability(self):
        for slot in SLOTS:
            self.mutate(slot, lambda c: c['book']['runners'][4].pop('lastPriceTraded'))
        result = self.analysis()
        self.assertIsNone(result['reference_slot'])
        self.assertTrue(result['runners'])
        self.assertTrue(all(r['chance'] is None and r['fair_odds'] is None for r in result['runners']))

    def test_scratching_break_suppresses_field(self):
        self.mutate('T5', lambda c: c['book']['runners'][4].update(status='REMOVED'))
        result = self.analysis()
        self.assertTrue(result['scratching_break'])
        self.assertEqual(result['runners'], [])
        self.assertIsNone(result['reference_slot'])

    def test_delayed_actual_timestamp_and_lateness(self):
        self.mutate('T30', lambda c: (c['book'].update(isMarketDataDelayed=True), c.update(late_seconds=4)))
        result = self.analysis()
        self.assertTrue(all(r['delayed_feed'] for r in result['runners']))
        self.assertEqual(result['timing'][-1]['late_seconds'], 4)
        page = wall.render(wall.summary(self.base, now=self.now))
        self.assertIn('Delayed feed', page)
        self.assertIn('2026-09-21T02:59:50+00:00', page)

    def test_stale_card_and_observer_are_visible(self):
        self.card['discovery_updated_at'] = (self.now-timedelta(hours=1)).isoformat()
        self.save('state/tb_today_card.json', self.card)
        self.save('state/tb_au_observations.json', {'updated_at': '2020-01-01T00:00:00Z'})
        data = wall.summary(self.base, now=self.now)
        self.assertEqual(data['freshness'], 'stale')
        self.assertFalse(data['observer_fresh'])
        self.assertTrue(data['races'])
        self.assertIn('stale / unavailable', wall.render(data))

    def test_corrupt_or_missing_card(self):
        for contents in ('{', '[]', '{"races":[null]}'):
            (self.base/'state/tb_today_card.json').write_text(contents)
            data = wall.summary(self.base, now=self.now)
            self.assertEqual(data['freshness'], 'unavailable')
            self.assertEqual(data['races'], [])
        (self.base/'state/tb_today_card.json').unlink()
        self.assertEqual(wall.summary(self.base, now=self.now)['freshness'], 'unavailable')

    def test_corrupt_snapshot_shapes_and_wrong_identity(self):
        for value in ('bad json', [], {'book': []}, {'schema': 'tb_au_snapshot/v1', 'market_id': self.mid, 'slot': 'T30', 'book': {'runners': [None]}}):
            self.save(self.paths['T30'], value)
            result = self.analysis()
            self.assertEqual(result['latest_slot'], 'T2')
        self.mutate('T2', lambda c: c.update(market_id='1.123'))
        self.assertEqual(self.analysis()['latest_slot'], 'T5')

    def test_future_capture_and_post_start_capture_excluded(self):
        early = self.start-timedelta(minutes=4)
        result = self.analysis(now=early)
        self.assertEqual(result['latest_slot'], 'T5')
        self.mutate('T30', lambda c: c.update(captured_at=(self.start+timedelta(seconds=1)).isoformat()))
        self.assertEqual(self.analysis(now=self.start+timedelta(days=1))['latest_slot'], 'T2')

    def test_future_form_import_and_retrospective_form_missing(self):
        for form in self.forms.values():
            form['imported_at'] = (self.now+timedelta(seconds=1)).isoformat()
        self.save_forms()
        self.assertTrue(all(r['base'] is None for r in self.analysis()['runners']))
        for form in self.forms.values():
            form['imported_at'] = (self.now-timedelta(minutes=1)).isoformat()
            form['pre_race'] = False
        self.save_forms()
        self.assertTrue(all(r['base'] is None for r in self.analysis()['runners']))

    def test_same_day_or_future_form_start_is_not_history(self):
        for form in self.forms.values():
            form['starts'][0]['date'] = self.day
        self.save_forms()
        self.assertTrue(all(r['base'] is None for r in self.analysis()['runners']))

    def test_wrong_day_card_and_missing_runner_metadata(self):
        self.card['date'] = '2026-09-20'
        self.save('state/tb_today_card.json', self.card)
        self.assertEqual(wall.summary(self.base, now=self.now)['freshness'], 'unavailable')
        self.market['runners'] = None
        self.assertTrue(self.analysis()['runners'])

    def test_stable_and_mixed_are_not_qualifiers(self):
        for j, slot in enumerate(SLOTS):
            self.mutate(slot, lambda c, j=j: [r.update(lastPriceTraded=[8, 6, 9, 5, 8][j] if r['selectionId']==1 else 5) for r in c['book']['runners']])
        self.assertEqual(self.analysis()['runners'], [])

    def test_duplicate_and_reversed_times_ignored(self):
        self.mutate('T30', lambda c: c.update(captured_at=(self.start-timedelta(minutes=20)).isoformat()))
        self.assertEqual(self.analysis()['latest_slot'], 'T2')
        self.mutate('T2', lambda c: c['book']['runners'].append(c['book']['runners'][0]))
        self.assertEqual(self.analysis()['latest_slot'], 'T5')

    def test_restart_idempotent_and_read_only(self):
        before = {p: p.read_bytes() for p in self.base.rglob('*.json')}
        first = wall.summary(self.base, now=self.now)
        importlib.reload(wall)
        self.assertEqual(first, wall.summary(self.base, now=self.now))
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*.json')})

    def test_capture_wall_removes_profiles_highlights_same_three(self):
        from tb_au_capture_wall import render
        page = render(self.base, {'date': [self.day], 'market': [self.mid]})
        self.assertNotIn('Top winner profiles', page)
        self.assertEqual(page.count('class=" shape-qualifier"'), 3)
        self.assertIn('/next-five', page)

    def test_local_routes(self):
        import tb_wall
        handler = object.__new__(tb_wall.WallHandler)
        handler.base_dir = self.base
        for path in ('/api/next-five', '/next-five'):
            handler.path = path
            handler.send_text = Mock()
            handler.do_GET()
            body = handler.send_text.call_args.args[0]
            self.assertIn('tb_next_five/v1' if path.startswith('/api') else 'Next five Australian races', body)
        (self.base/'state/tb_today_card.json').unlink()
        handler.path = '/api/next-five'
        handler.do_GET()
        self.assertEqual(handler.send_text.call_args.kwargs['status'], 503)

    def test_authoritative_routes(self):
        from tb_race_queue_wall import make_handler
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.base/'state'))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection(*server.server_address)
        self.addCleanup(connection.close)
        for path in ('/next-five', '/api/next-five'):
            connection.request('GET', path)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn('no-store', response.getheader('Cache-Control'))
            self.assertIn(b'tb_next_five/v1' if path.startswith('/api') else b'Next five Australian races', response.read())


if __name__ == '__main__':
    unittest.main()
