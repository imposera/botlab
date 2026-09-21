from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from tb_queue_summary import summary, panel
import tb_wall as wall


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base/'state'
        self.state.mkdir()
        self.now = datetime(2026, 9, 9, 1, tzinfo=timezone.utc)
        self.rows = [dict(market_id=str(i), track='Example', market_name=f'R{i}', country_code='AU',
                         market_start_time=(self.now+timedelta(minutes=i+1)).isoformat(),
                         eligible=True, arming_eligible=True) for i in range(9)]
        self.queue = dict(updated_at=self.now.isoformat(), races=self.rows)
        self.save()

    def save(self):
        (self.state/'tb_race_queue.json').write_text(json.dumps(self.queue))

    def test_order_limit_armed_and_excluded_are_separate(self):
        self.rows[0].update(human_action='remove', arming_eligible=False)
        self.rows[1].update(arming_eligible=False, clash_skip={'reason': 'Prefer other race', 'alternative_market_id': '3'})
        (self.state/'betfair_t15_target.json').write_text(json.dumps(dict(
            status='armed', updated_at=self.now.isoformat(), market=self.rows[2])))
        self.save()
        data = summary(self.state, now=self.now)
        self.assertEqual(data['armed']['market_id'], '2')
        self.assertEqual(data['next_eligible']['market_id'], '3')
        self.assertEqual([r['market_id'] for r in data['eligible']], ['3','4','5','6','7'])
        self.assertEqual(data['counts']['eligible'], 6)
        self.assertEqual([r['status'] for r in data['excluded']], ['removed','clash_skip'])
        self.assertEqual(data['excluded'][1]['alternative_market_id'], '3')

    def test_stale_and_unknown_do_not_advertise_next_selection(self):
        data = summary(self.state, now=self.now+timedelta(seconds=121))
        self.assertFalse(data['ok'])
        self.assertEqual(data['freshness'], 'stale')
        self.assertIsNone(data['next_eligible'])
        self.assertGreater(len(data['eligible']), 0)
        self.assertIn('Cached eligible', panel(data))
        for value in [None, 'bad', (self.now+timedelta(hours=1)).isoformat()]:
            self.queue['updated_at'] = value
            self.save()
            data = summary(self.state, now=self.now)
            self.assertEqual(data['freshness'], 'unknown')
            self.assertIsNone(data['next_eligible'])

    def test_elapsed_and_expired_target_are_not_upcoming(self):
        self.rows[0]['market_start_time'] = (self.now-timedelta(minutes=1)).isoformat()
        self.save()
        (self.state/'betfair_t15_target.json').write_text(json.dumps({'status':'expired', 'market':self.rows[1]}))
        data = summary(self.state, now=self.now)
        self.assertIsNone(data['armed'])
        self.assertEqual(data['next_eligible']['market_id'], '1')

    def test_missing_corrupt_invalid_state_and_html_escape(self):
        self.rows[0]['track'] = '<script>alert(1)</script>'
        self.save()
        markup = panel(summary(self.state, now=self.now))
        self.assertNotIn('<script>', markup)
        self.assertIn('&lt;script&gt;', markup)
        self.assertIn('http://core7070:8792', markup)
        path = self.state/'tb_race_queue.json'
        for content in ['{bad', '[]', '{"races":[null]}', '{"races":[{"market_id":"x"}]}']:
            path.write_text(content)
            self.assertEqual(summary(self.state, now=self.now)['freshness'], 'unavailable')
        path.unlink()
        self.assertEqual(summary(self.state, now=self.now)['freshness'], 'unavailable')

    def test_endpoint_and_live_panel_are_read_only(self):
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        handler = object.__new__(wall.WallHandler)
        handler.base_dir, handler.path, handler.send_text = self.base, '/api/queue-summary', Mock()
        handler.do_GET()
        self.assertEqual(handler.send_text.call_args.kwargs['status'], 200)
        data = json.loads(handler.send_text.call_args.args[0])
        self.assertEqual(data['schema'], 'tb_queue_summary/v1')
        page = wall.html_page(self.base)
        self.assertEqual(page.count('data-poll="queue"'), 1)
        self.assertNotIn('data-poll="queue"', wall.html_page(self.base, 'history-id'))
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.state.iterdir()})
        (self.state/'tb_race_queue.json').unlink()
        handler.do_GET()
        self.assertEqual(handler.send_text.call_args.kwargs['status'], 503)


if __name__ == '__main__':
    unittest.main()
