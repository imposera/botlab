import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tb_queue_policy import annotate, learn, key, filter_automatic
from test_betfair_t15_emit import load_emitter


class ClashPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.race = dict(market_id='1.1', track='Mountaineer Park', country_code='US',
                         market_name='R5 6f Claim', market_start_time=(self.now + timedelta(minutes=15)).isoformat())
        self.other = dict(self.race, market_id='1.2', track='Other', market_start_time=(self.now + timedelta(minutes=19)).isoformat())
        self.events = [dict(market_id=f'old{i}', action='remove',
                            recorded_at=(self.now-timedelta(days=i+1)).isoformat(),
                            race_snapshot=dict(self.race, market_start_time=(self.now-timedelta(days=i+1)).isoformat()))
                       for i in range(3)]
        self.save()

    def save(self):
        (self.state/'tb_race_queue_decisions.jsonl').write_text('\n'.join(json.dumps(e) for e in self.events)+'\n')

    def rows(self, markets=None, decisions=None):
        return annotate(markets or [self.race, self.other], decisions or {}, learn(self.state, self.now), self.now)

    def test_skip_only_with_nearby_alternative_and_keep_override(self):
        self.assertFalse(self.rows()[0]['arming_eligible'])
        self.assertEqual(self.rows()[0]['clash_skip']['alternative_market_id'], '1.2')
        self.assertTrue(self.rows([self.race])[0]['arming_eligible'])
        self.assertTrue(self.rows(decisions={'1.1': {'action': 'keep'}})[0]['arming_eligible'])
        self.assertTrue(self.rows(decisions={'1.2': {'action': 'remove'}})[0]['arming_eligible'])
        far = dict(self.other, market_start_time=(self.now+timedelta(minutes=20, seconds=1)).isoformat())
        self.assertTrue(self.rows([self.race, far])[0]['arming_eligible'])

    def test_two_disfavoured_races_do_not_remove_each_other(self):
        self.assertTrue(all(r['arming_eligible'] for r in self.rows([self.race, dict(self.other, track=self.race['track'])])))

    def test_duplicates_and_one_meeting_do_not_manufacture_evidence(self):
        self.events = [self.events[0]] * 5
        self.save()
        self.assertEqual(learn(self.state, self.now), {})
        self.events = [dict(self.events[0], market_id=str(i)) for i in range(5)]
        self.save()
        self.assertEqual(learn(self.state, self.now), {})

    def test_latest_restore_and_keep_cancel_training_signal(self):
        for action in ['restore', 'keep']:
            self.events = self.events[:3]
            self.events.append(dict(self.events[0], action=action, recorded_at=self.now.isoformat()))
            self.save()
            self.assertEqual(learn(self.state, self.now), {})

    def test_old_evidence_expires_and_class_is_scoped(self):
        self.assertTrue(self.rows([dict(self.race, market_name='R5 Listed Stakes'), self.other])[0]['arming_eligible'])
        self.assertTrue(self.rows([dict(self.race, country_code='AU'), self.other])[0]['arming_eligible'])
        self.assertEqual(learn(self.state, self.now+timedelta(days=31)), {})

    def test_emitter_waits_for_clashing_alternative_and_respects_human_remove(self):
        e = load_emitter()
        with patch.object(e, 'utc_now', return_value=self.now):
            allowed, skipped = e.automatic_queue_markets([self.race, self.other], self.state, {})
            self.assertEqual([m['market_id'] for m in allowed], ['1.2'])
            # Preferred race at T-19 is outside the arming window: wait, do not
            # fall back to the skipped T-15 race.
            self.assertIsNone(e.find_t15_candidate(allowed, 15, 3, {})[0])
            self.assertEqual(skipped[0]['alternative_market_id'], '1.2')
            (self.state/'tb_race_queue.json').write_text(json.dumps({'decisions': {'1.2': {'action': 'remove'}}}))
            allowed, _ = e.automatic_queue_markets([self.race, self.other], self.state, {})
            self.assertEqual(e.find_t15_candidate(allowed, 15, 3, {})[0]['market_id'], '1.1')

    def test_invalid_audit_fails_instead_of_arming_from_partial_history(self):
        (self.state/'tb_race_queue_decisions.jsonl').write_text('{broken')
        with self.assertRaises(ValueError):
            filter_automatic([self.race], self.state, self.now)

    def test_unselectable_alternative_does_not_trigger_skip(self):
        e = load_emitter()
        with patch.object(e, 'utc_now', return_value=self.now), patch.object(
                e, 'score_market', side_effect=lambda m, c: (-9999 if m['market_id'] == '1.2' else 10, [])):
            allowed, skipped = e.automatic_queue_markets([self.race, self.other], self.state, {'enabled': True})
        self.assertEqual(len(allowed), 2)
        self.assertEqual(skipped, [])

    def test_queue_refresh_and_immediate_keep_use_same_policy(self):
        import tb_race_queue as q
        with patch.object(q, 'utc_now', return_value=self.now), patch.object(q, 'load_secrets'), patch.object(
                q, 'discover_markets', return_value=[self.race, self.other]):
            q.atomic_write_json(self.state/q.QUEUE_FILE_NAME, {'decisions': {}, 'races': []})
            result = q.refresh_queue(self.state, self.state, self.state/'unused', 10, 4)
            self.assertTrue(result['races'][0]['eligible'])
            self.assertFalse(result['races'][0]['arming_eligible'])
            self.assertEqual(q.next_eligible(result)['market_id'], '1.2')
            kept = q.update_decision(self.state, '1.1', 'keep', 'override')
            self.assertEqual(q.next_eligible(kept)['market_id'], '1.1')

    def test_clash_learning_is_limited_to_next_ten(self):
        earlier = [dict(self.other, market_id=f'early{i}', market_start_time=(self.now+timedelta(minutes=i+1)).isoformat()) for i in range(10)]
        allowed, skipped = filter_automatic(earlier+[self.race, self.other], self.state, self.now)
        self.assertEqual(len(allowed), 12)
        self.assertEqual(skipped, [])


if __name__ == '__main__':
    unittest.main()
