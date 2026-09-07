import contextlib
import importlib.util
import io
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch


def load_emitter():
    gateway = types.ModuleType('betfair_gateway')
    gateway.HORSE_RACING_EVENT_TYPE_ID = '7'
    gateway.utc_now = lambda: datetime.now(timezone.utc)
    gateway.iso_z = lambda dt: dt.isoformat()
    for name in ('betfair_login', 'betting_api', 'load_secrets', 'market_summary'):
        setattr(gateway, name, Mock())
    manual = types.ModuleType('manual_arm_request')
    for name in ('consume_request', 'pending_request', 'reject_request'):
        setattr(manual, name, Mock(return_value=None))
    with patch.dict(sys.modules, betfair_gateway=gateway, manual_arm_request=manual):
        spec = importlib.util.spec_from_file_location('emitter_test_module', __file__.replace('test_betfair_t15_emit.py', 'betfair_t15_emit.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class EmitterTests(unittest.TestCase):
    def setUp(self):
        self.e = load_emitter()
        self.market = {'market_id': '1.2',
                       'market_start_time': (self.e.utc_now() + timedelta(minutes=15)).isoformat(),
                       'runners': [{'selection_id': 1, 'runner_name': 'Horse', 'cloth_number': '3'},
                                   {'selection_id': 2}, {'selection_id': 3}]}
        self.e.betting_api.return_value = [{'marketId': '1.2', 'totalMatched': 150,
            'isMarketDataDelayed': True, 'runners': [
                {'selectionId': 2, 'totalMatched': 0},
                {'selectionId': 1, 'totalMatched': 125, 'ex': {'tradedVolume': [{'price': 2, 'size': 125}]}},
                {'selectionId': 4, 'ex': {'tradedVolume': [{'price': 3, 'size': 25}]}}]}]

    def test_volume_join_and_missing_values(self):
        result = self.e.with_runner_matched(self.market)
        rows = {r['selection_id']: r for r in result['runners']}
        self.assertEqual(rows[1]['runner_name'], 'Horse')
        self.assertEqual(rows[1]['cloth_number'], '3')
        self.assertEqual([rows[i]['total_matched'] for i in (1, 2, 3, 4)], [125, 0, None, 25])
        self.assertEqual(result['total_matched'], 150)
        self.assertTrue(result['is_market_data_delayed'])
        self.assertNotIn('total_matched', self.market['runners'][0])
        self.assertEqual(self.e.betting_api.call_args.args[1]['priceProjection']['priceData'], ['EX_TRADED'])

    def test_failure_does_not_reuse_stale_volume(self):
        self.market['runners'][0]['total_matched'] = 999
        for response in ([], [{'marketId': 'other'}]):
            self.e.betting_api.return_value = response
            result = self.e.with_runner_matched(self.market)
            self.assertEqual(result['matched_data_status'], 'unavailable')
            self.assertIsNone(result['runners'][0]['total_matched'])
        self.e.betting_api.side_effect = RuntimeError('private gateway detail')
        target = self.e.build_armed_target(self.market, 15, 3, None)
        self.assertEqual(target['status'], 'armed')
        self.assertNotIn('private gateway detail', str(target))

    def test_target_contract_and_manual_lookup(self):
        previous = {'market': self.market, 'emitted_at': 'original'}
        target = self.e.build_armed_target(self.market, 15, 3, previous)
        self.assertEqual(target['arm_id'], 'betfair:1.2')
        self.assertEqual(target['emitted_at'], 'original')
        self.assertEqual(target['market']['runners'][0]['total_matched'], 125)
        self.assertGreater(self.e.find_market_by_id([self.market], '1.2')['seconds_to_jump'], 0)
        self.assertIsNone(self.e.find_market_by_id([self.market], 'missing'))

    def run_main(self, previous):
        with patch.object(sys, 'argv', ['emit', '--dry-run']), \
             patch.object(self.e, 'read_json', return_value=previous), \
             patch.object(self.e, 'load_priority_config', return_value=self.e.default_priority_config()), \
             patch.object(self.e, 'load_track_liquidity', return_value={}), \
             patch.object(self.e, 'discover_markets', return_value=[]), \
             patch.object(self.e, 'active_target_is_hard_locked', return_value=(True, 'snapshot')), \
             patch.object(self.e, 'atomic_write_json') as write, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.e.main(), 0)
            write.assert_not_called()

    def test_idle_and_tracking_dry_run(self):
        self.run_main(None)
        self.e.pending_request.return_value = {'market_id': 'other'}
        self.run_main({'status': 'armed', 'market': self.market})
        self.e.reject_request.assert_not_called()
        self.e.consume_request.assert_not_called()
        self.e.betting_api.assert_called_once()


if __name__ == '__main__':
    unittest.main()
