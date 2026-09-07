import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from argparse import Namespace
import tb_blackbook as bb

class BlackbookReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    def market(self, mid='1.1', sid=10, name='Example', date='2026-09-01', start='2026-09-01T10:00:00Z'):
        path = self.base / 'history' / date / mid
        self.write(path / 'closed.json', {'complete': True, 'has_result': True})
        self.write(path / 'result.json', {'market_id': mid, 'market': {'track': 'Test Track', 'market_start_time': start}, 'winners': [{'selection_id': sid, 'runner_name': name, 'cloth_number': 1}]})
        for stage, price in [('t15', 10), ('t10', 8), ('t5', 8), ('t2', 9), ('t30', 7)]:
            self.write(path / bb.STAGE_FILES[stage], {'runners': [{'selection_id': sid, 'runner_name': name, 'cloth_number': 1, 'last_price_traded': price}]})
        return path
    def scan(self, market_id=None, dry_run=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return bb.cmd_scan(Namespace(base_dir=self.base, market_id=market_id, dry_run=dry_run))
    def test_price_path_and_aggregation(self):
        self.market()
        self.market('1.2', date='2026-09-02')
        entry = bb.build_blackbook(self.base)['entries'][0]
        self.assertEqual(entry['wins_seen'], 2)
        self.assertEqual(entry['last_seen'], '2026-09-02')
        self.assertEqual(entry['last_win']['shape'], '▼▬▲▼')
        self.assertAlmostEqual(entry['last_win']['move_pct'], -30)
    def test_metadata_and_repeat_scan(self):
        self.market()
        self.scan()
        state = self.base / 'state' / bb.STATE_FILE_NAME
        payload = json.loads(state.read_text())
        payload['entries'][0].update(tags=['watch'], notes='Manual note', status='active')
        self.write(state, payload)
        self.scan()
        entry = bb.load_blackbook(self.base)['entries'][0]
        self.assertEqual((entry['tags'], entry['notes'], entry['status'], entry['wins_seen']), (['watch'], 'Manual note', 'active', 1))
    def test_dry_run_no_write(self):
        self.market()
        self.scan(dry_run=True)
        self.assertFalse((self.base / 'state').exists())
    def test_incomplete_market_skipped(self):
        path = self.market()
        self.write(path / 'closed.json', {'complete': False, 'has_result': True})
        payload = bb.build_blackbook(self.base)
        self.assertEqual((payload['entry_count'], payload['skipped']), (0, 1))
    def test_cli_list_and_show(self):
        self.market()
        self.scan()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bb.cmd_list(Namespace(base_dir=self.base, min_wins=1, limit=40)), 0)
            self.assertEqual(bb.cmd_show(Namespace(base_dir=self.base, query='example')), 0)
        self.assertIn('Example', output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bb.cmd_show(Namespace(base_dir=self.base, query='missing')), 1)
    def test_nonfinite_prices_rejected(self):
        for value in ['NaN', 'Infinity', -1, 0, 'invalid']:
            self.assertIsNone(bb.price_from_runner({'last_price_traded': value}))
    def test_filtered_scan_preserves_other_entries(self):
        self.market()
        self.market('1.2', sid=20, name='Other')
        self.scan()
        self.scan(market_id='1.1')
        self.assertEqual(bb.load_blackbook(self.base)['entry_count'], 2)
    def test_exact_selection_id_beats_cloth_fallback(self):
        path = self.market()
        self.write(path / bb.STAGE_FILES['t15'], {'runners': [
            {'selection_id': 99, 'runner_name': 'Different', 'cloth_number': 1, 'last_price_traded': 50},
            {'selection_id': 10, 'runner_name': 'Example', 'cloth_number': 1, 'last_price_traded': 10}]})
        self.assertEqual(bb.build_blackbook(self.base)['entries'][0]['wins'][0]['prices']['t15'], 10)
    def test_last_win_uses_start_time(self):
        self.market('1.9', start='2026-09-01T09:00:00Z')
        self.market('1.10', start='2026-09-01T10:00:00Z')
        self.assertEqual(bb.build_blackbook(self.base)['entries'][0]['last_win']['market_id'], '1.10')

    def test_filtered_refresh_preserves_same_runner_history_without_duplicates(self):
        self.market()
        self.market('1.2', date='2026-09-02')
        self.scan()
        self.scan(market_id='1.1')
        self.scan(market_id='1.1')
        entry = bb.load_blackbook(self.base)['entries'][0]
        self.assertEqual(entry['wins_seen'], 2)
        self.assertEqual(entry['last_win']['market_id'], '1.2')

    def test_filtered_refresh_updates_changed_winner(self):
        self.market()
        self.market('1.2', date='2026-09-02')
        self.scan()
        self.market(sid=20, name='Corrected Winner')
        self.scan(market_id='1.1')
        entries = {e['key']: e for e in bb.load_blackbook(self.base)['entries']}
        self.assertEqual(entries['sid:10']['wins_seen'], 1)
        self.assertEqual(entries['sid:10']['last_win']['market_id'], '1.2')
        self.assertEqual(entries['sid:20']['wins_seen'], 1)

    def test_unknown_market_preserves_register_and_metadata(self):
        self.market()
        self.scan()
        state = self.base / 'state' / bb.STATE_FILE_NAME
        payload = bb.load_blackbook(self.base)
        payload['entries'][0].update(tags=['watch'], notes='Keep me', status='')
        self.write(state, payload)
        self.scan(market_id='missing')
        self.assertEqual(bb.load_blackbook(self.base)['entries'], payload['entries'])

    def test_matching_rejects_conflicting_id_and_allows_missing_id(self):
        path = self.market()
        snapshot = path / bb.STAGE_FILES['t15']
        self.write(snapshot, {'runners': [{'selection_id': 99, 'runner_name': 'Example', 'cloth_number': 1, 'last_price_traded': 50}]})
        self.assertNotIn('t15', bb.build_blackbook(self.base)['entries'][0]['wins'][0]['prices'])
        self.write(snapshot, {'runners': [{'runner_name': 'Example', 'last_price_traded': 12}]})
        self.assertEqual(bb.build_blackbook(self.base)['entries'][0]['wins'][0]['prices']['t15'], 12)

    def test_race_order_normalises_timezones_and_handles_missing_time(self):
        early = {'market_start_time': '2026-09-01T12:00:00+10:00'}
        late = {'market_start_time': '2026-09-01T03:00:00Z'}
        self.assertLess(bb.race_sort_key(early), bb.race_sort_key(late))
        self.assertLess(bb.race_sort_key({'date': '2026-08-31'}), bb.race_sort_key(late))

    def test_version_in_output(self):
        self.assertEqual(bb.build_blackbook(self.base)['version'], bb.VERSION)

if __name__ == '__main__':
    unittest.main(verbosity=2)
