import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import tb_race_lifecycle as life
import tb_race_observer as observer
import tb_race_observation_wall as display
import tb_wall as wall
import tb_review as review
import tb_blackbook as blackbook
from test_betfair_t15_emit import load_emitter


class RaceObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.start = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
        self.market = {'market_id': '1.2', 'market_start_time': self.start.isoformat(), 'track': 'Test',
                       'runners': [{'selection_id': 1, 'runner_name': 'Horse', 'cloth_number': '1'},
                                   {'selection_id': 2, 'runner_name': 'Other'}]}
        self.settings = dict(life.DEFAULTS)

    def raw(self, status='OPEN', inplay=False, removed=False):
        return {'marketId': '1.2', 'status': status, 'inplay': inplay, 'totalMatched': 100,
                'isMarketDataDelayed': True, 'runners': [
                    {'selectionId': 1, 'lastPriceTraded': 10, 'status': 'ACTIVE'},
                    {'selectionId': 2, 'lastPriceTraded': 20, 'status': 'REMOVED' if removed else 'ACTIVE'}]}

    def transition(self, raw, now, old=None, rolling=None):
        return observer.observe(self.market, raw, now, old or {}, rolling or {}, self.settings)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_delayed_suspended_reopened_then_started(self):
        now = self.start + timedelta(minutes=5)
        state, rolling, derived = self.transition(self.raw(), now)
        self.assertEqual(state['state'], 'delayed')
        self.assertTrue(life.evaluate(self.market, state, now)['hold'])
        state, rolling, _ = self.transition(self.raw('SUSPENDED'), now+timedelta(seconds=10), state, rolling)
        self.assertEqual(state['state'], 'suspended')
        self.assertEqual(len(rolling['captures']), 1)
        self.assertNotIn('observed_start_at', state)
        state, rolling, _ = self.transition(self.raw(), now+timedelta(seconds=20), state, rolling)
        self.assertEqual(state['state'], 'delayed')
        state, rolling, derived = self.transition(self.raw(inplay=True), now+timedelta(seconds=30), state, rolling)
        self.assertEqual(state['state'], 'observed_start')
        self.assertFalse(life.evaluate(self.market, state, now+timedelta(seconds=30))['hold'])
        self.assertEqual(derived['timing_basis'], 'observed_start')
        self.assertEqual(derived['captures']['t30']['captured_at'], now.isoformat())
        self.assertTrue(derived['delayed_feed'])
        self.assertEqual(len(rolling['captures']), 2)

    def test_terminal_state_is_sticky_and_closed_is_not_start(self):
        now = self.start
        state, rolling, _ = self.transition(self.raw(inplay=True), now)
        again, _, derived = self.transition(self.raw(), now+timedelta(seconds=10), state, rolling)
        self.assertEqual(again['observed_start_at'], now.isoformat())
        self.assertIsNone(derived)
        closed, _, derived = self.transition(self.raw('CLOSED', True), now)
        self.assertEqual(closed['state'], 'closed')
        self.assertIsNone(derived)
        self.assertNotIn('observed_start_at', closed)

    def test_freshness_and_bounded_timeouts(self):
        now = self.start+timedelta(minutes=4)
        state, rolling, _ = self.transition(self.raw(), now)
        decision = life.evaluate(self.market, state, now+timedelta(seconds=60))
        self.assertTrue(decision['hold']); self.assertEqual(decision['state'], 'unknown')
        decision = life.evaluate(self.market, state, now+timedelta(seconds=181))
        self.assertFalse(decision['hold'])
        self.assertEqual(decision['reason'], 'stale_feed_timeout')
        state['last_received_at']=(self.start+timedelta(minutes=30)).isoformat()
        decision=life.evaluate(self.market,state,self.start+timedelta(minutes=30))
        self.assertFalse(decision['hold']); self.assertEqual(decision['reason'],'maximum_delay_reached')
        self.assertTrue(life.evaluate(self.market,{},self.start+timedelta(seconds=60))['hold'])
        self.assertFalse(life.evaluate(self.market,{},self.start+timedelta(seconds=181))['hold'])

    def test_feed_error_does_not_refresh_last_received(self):
        state, rolling, _ = self.transition(self.raw(), self.start)
        failed, _, _ = self.transition(None, self.start+timedelta(seconds=10), state, rolling)
        self.assertEqual(failed['last_received_at'], self.start.isoformat())
        self.assertTrue(failed['feed_error'])
        self.assertEqual(failed['state'],'unknown')
        recovered, _, _ = self.transition(self.raw(),self.start+timedelta(seconds=20),failed,rolling)
        self.assertEqual(recovered['state'],'delayed')
        self.assertFalse(recovered['feed_error'])

    def test_wrong_market_and_future_timestamps_cannot_hold(self):
        wrong={**self.raw(),'marketId':'1.99'}
        state, _, _=self.transition(wrong,self.start)
        self.assertNotIn('last_received_at',state)
        future={'market_id':'1.2','state':'preplay','last_received_at':(self.start+timedelta(days=1)).isoformat()}
        self.assertFalse(life.evaluate(self.market,future,self.start+timedelta(minutes=5))['hold'])
        other={**future,'market_id':'1.99','state':'observed_start'}
        self.assertTrue(life.evaluate(self.market,other,self.start-timedelta(minutes=1))['hold'])

    def test_rolling_buffer_retention_and_missing_offsets(self):
        state, rolling={},{}
        for minute in range(-15, 10):
            state,rolling,_=self.transition(self.raw(),self.start+timedelta(minutes=minute),state,rolling)
        self.assertEqual(len(rolling['captures']),21)
        self.assertEqual(rolling['captures'][0]['captured_at'],(self.start-timedelta(minutes=11)).isoformat())
        selected=life.select_offsets(rolling['captures'],self.start+timedelta(minutes=9),tolerance=20)
        self.assertIn('t15',selected)
        self.assertNotIn('t30',selected)

    def test_scratching_event_once_and_normalized_identity(self):
        state,rolling,_=self.transition(self.raw(),self.start)
        state,rolling,_=self.transition(self.raw(removed=True),self.start+timedelta(seconds=10),state,rolling)
        state,rolling,_=self.transition(self.raw(removed=True),self.start+timedelta(seconds=20),state,rolling)
        self.assertEqual(len(state['events']),1)
        self.assertTrue(life.scratching_break(rolling['captures']))
        self.assertFalse(life.scratching_break([rolling['captures'][-1]]))
        self.assertEqual(state['latest_book']['runners'][0]['runner_name'],'Horse')

    def test_tick_persistence_restart_and_no_raw_history_changes(self):
        self.write(self.base/'state'/'betfair_t15_target.json',{'status':'armed','market':self.market})
        sentinel=self.base/'history'/'2026-09-07'/'1.2'/'market_book_t30.json'
        self.write(sentinel,{'sentinel':True})
        before=sentinel.read_bytes()
        fetch=Mock(return_value=self.raw())
        self.assertEqual(observer.tick(self.base,fetch,self.start),'delayed')
        fetch.return_value=self.raw(inplay=True)
        self.assertEqual(observer.tick(self.base,fetch,self.start+timedelta(seconds=30)),'observed_start')
        directory=life.observation_dir(self.base,self.market)
        self.assertTrue((directory/'observed_start.json').exists())
        fetch.reset_mock()
        self.assertEqual(observer.tick(self.base,fetch,self.start+timedelta(seconds=40)),'observed_start')
        fetch.assert_not_called()
        self.assertEqual(sentinel.read_bytes(),before)

    def test_gateway_errors_are_not_persisted(self):
        self.write(self.base/'state'/'betfair_t15_target.json',{'status':'armed','market':self.market})
        self.assertEqual(observer.tick(self.base,Mock(side_effect=RuntimeError('secret-token')),self.start),'unknown')
        state=life.load_lifecycle(self.base,self.market)
        self.assertNotIn('secret-token',json.dumps(state))

    def test_emitter_holds_delayed_and_releases_observed_start(self):
        emitter=load_emitter()
        now=self.start+timedelta(minutes=5)
        state,rolling,_=self.transition(self.raw(),now)
        directory=life.observation_dir(self.base,self.market)
        self.write(directory/'lifecycle.json',state)
        target={'status':'armed','market':self.market}
        with patch.object(emitter,'utc_now',return_value=now):
            self.assertTrue(emitter.existing_target_is_active(target,self.base/'state'))
            state['state']='observed_start'; self.write(directory/'lifecycle.json',state)
            self.assertFalse(emitter.existing_target_is_active(target,self.base/'state'))

    def test_wall_labels_and_rolling_separation(self):
        now=self.start+timedelta(minutes=4)
        state,rolling,_=self.transition(self.raw(),now)
        state,rolling,_=self.transition(self.raw(removed=True),now+timedelta(seconds=30),state,rolling)
        directory=life.observation_dir(self.base,self.market)
        self.write(directory/'lifecycle.json',state);self.write(directory/'rolling.json',rolling)
        self.assertIn('Delayed +4m30s',display.timing_label(self.base,self.market,now+timedelta(seconds=30)))
        page=display.panel(self.base,self.market,now=now+timedelta(seconds=30))
        self.assertIn('elapsed lookbacks, not time to jump',page)
        self.assertIn('Scratching break',page)
        self.assertIn('Runner 2 removed',page)
        self.assertIn('stale',display.panel(self.base,self.market,now=now+timedelta(minutes=2)))

    def test_review_suppresses_movement_across_scratching(self):
        folder=self.base/'history'/'2026-09-07'/'1.2'
        first=observer.normalize(self.raw(),self.market,self.start-timedelta(minutes=15))
        last=observer.normalize(self.raw(removed=True),self.market,self.start-timedelta(seconds=30))
        last['runners'][0]['last_price_traded']=5
        self.write(folder/'market_book_t15.json',first);self.write(folder/'market_book_t30.json',last)
        self.write(folder/'result.json',{'winners':[{'selection_id':1,'runner_name':'Horse'}]})
        result=review.build_review(folder)
        self.assertTrue(result['race']['price_series_break'])
        self.assertEqual(result['winner']['shape_class'],'scratching_break')
        self.assertIsNone(result['winner']['move_pct'])
        self.write(folder/'closed.json',{'complete':True,'has_result':True})
        entry=blackbook.build_blackbook(self.base)['entries'][0]
        self.assertIsNone(entry['wins'][0]['move_pct'])
        self.assertTrue(entry['wins'][0]['price_series_break'])

    def test_tick_maximum_hold_stops_fetching(self):
        self.write(self.base/'state'/'betfair_t15_target.json',{'status':'armed','market':self.market})
        fetch=Mock(return_value=self.raw())
        self.assertEqual(observer.tick(self.base,fetch,self.start+timedelta(minutes=31)),'timed_out')
        fetch.assert_not_called()
        self.assertEqual(life.load_lifecycle(self.base,self.market)['reason'],'maximum_delay_reached')

    def test_recover_committed_start_after_partial_write(self):
        self.write(self.base/'state'/'betfair_t15_target.json',{'status':'armed','market':self.market})
        directory=life.observation_dir(self.base,self.market)
        self.write(directory/'observed_start.json',{'market_id':'1.2','observed_start_at':self.start.isoformat(),
                                                   'source':'first_received_inplay_true'})
        fetch=Mock(return_value=self.raw(inplay=True))
        self.assertEqual(observer.tick(self.base,fetch,self.start+timedelta(minutes=1)),'observed_start')
        fetch.assert_not_called()
        self.assertEqual(life.load_lifecycle(self.base,self.market)['observed_start_at'],self.start.isoformat())

    def test_full_wall_uses_observer_and_preserves_scheduled_board(self):
        now=self.start+timedelta(minutes=4)
        state,rolling,_=self.transition(self.raw(),now)
        directory=life.observation_dir(self.base,self.market)
        self.write(directory/'lifecycle.json',state);self.write(directory/'rolling.json',rolling)
        self.write(self.base/'state'/'betfair_t15_target.json',{'status':'armed','market':self.market})
        with patch.object(wall,'utc_now',return_value=now), patch.object(display,'datetime') as clock:
            clock.now.return_value=now
            page=wall.html_page(self.base)
        self.assertIn('Delayed +4m00s',page)
        self.assertIn('Rolling prices · last 15 minutes',page)
        self.assertIn('<th>T15</th>',page)
        self.assertIn('>Horse</td>',page)

    def test_policy_limits_and_path_validation(self):
        self.write(self.base/'config'/'race_observation.json',{'max_delay_seconds':3600,'poll_seconds':0,'fresh_seconds':False})
        config=life.policy(self.base)
        self.assertEqual(config['max_delay_seconds'],3600)
        self.assertEqual(config['poll_seconds'],5)
        self.assertEqual(config['fresh_seconds'],45)
        self.assertIsNone(life.observation_dir(self.base,{**self.market,'market_id':'../../elsewhere'}))


if __name__ == '__main__':
    unittest.main()
