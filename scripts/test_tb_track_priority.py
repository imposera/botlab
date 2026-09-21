from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tb_track_priority import validate, order_meetings, clash_preferences
from test_betfair_t15_emit import load_emitter
from tb_card_forecast import forecast
from tb_race_queue import apply_track_preferences


class TrackPriorityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 11, tzinfo=timezone.utc)
        self.priority = json.loads(Path(__file__).with_name('australian_card_priority.example.json').read_text())

    def race(self, track, minutes, country='AU'):
        return dict(market_id=track, track=track, country_code=country,
                    market_name='R1 1000m Hcap', arming_eligible=True,
                    market_start_time=(self.now+timedelta(minutes=minutes)).isoformat(), runners=[{}]*8)

    def choose(self, races, decisions=None):
        return clash_preferences(races, decisions or {}, self.priority, self.now)

    def test_order_labels_and_unlisted_stability(self):
        rows = order_meetings([{'track': t} for t in ['Bordertown', 'Goulburn', 'Tuncurry', 'Geelong', 'Sunshine Coast']], self.priority)
        self.assertEqual([r['track_label'] for r in rows], ['Geelong VIC', 'Tuncurry NSW', 'Goulburn NSW', 'Bordertown SA', 'Sunshine Coast QLD'])
        self.priority['tracks'].append({'track':' GEELONG ', 'state':'VIC'})
        with self.assertRaises(ValueError):
            validate(self.priority)

    def test_future_preferred_race_and_country_boundary(self):
        races = [self.race('Goulburn', 15), self.race('Geelong', 25), self.race('NZ track', 15, 'NZ')]
        allowed, skipped = self.choose(races)
        self.assertEqual([r['track'] for r in allowed], ['Geelong', 'NZ track'])
        self.assertEqual(skipped[0]['alternative_market_id'], 'Geelong')

    def test_keep_remove_and_non_clash(self):
        races = [self.race('Goulburn', 15), self.race('Geelong', 25)]
        self.assertFalse(self.choose(races, {'Goulburn':{'action':'keep'}})[1])
        self.assertFalse(self.choose(races, {'Geelong':{'action':'remove'}})[1])
        self.assertFalse(self.choose([races[0], self.race('Geelong', 33)])[1])
        self.assertFalse(clash_preferences(races, {}, self.priority, self.now,
                         alternative_allowed=lambda r:r['track']!='Geelong')[1])

    def test_missed_arming_window_cannot_suppress_future_race(self):
        self.assertFalse(self.choose([self.race('Geelong', 8), self.race('Goulburn', 15)])[1])

    def test_skipped_rival_cannot_suppress_another_race(self):
        allowed, skipped = self.choose([self.race('Geelong', 20), self.race('Tuncurry', 35), self.race('Goulburn', 50)])
        self.assertEqual([r['track'] for r in allowed], ['Geelong', 'Goulburn'])
        self.assertEqual(len(skipped), 1)

    def test_emitter_queue_and_forecast_agree(self):
        emitter = load_emitter()
        config = emitter.default_priority_config()
        config.update(enabled=True, australian_card_priority=self.priority)
        races = [self.race('Goulburn', 20), self.race('Geelong', 30)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); state = root/'state'; state.mkdir()
            cfg = root/'config'; cfg.mkdir()
            (cfg/'australian_card_priority.json').write_text(json.dumps(self.priority))
            with patch.object(emitter, 'utc_now', return_value=self.now):
                allowed, _ = emitter.automatic_queue_markets(races, state, config)
            self.assertEqual([r['track'] for r in allowed], ['Geelong'])
            annotated = apply_track_preferences([dict(r) for r in races], {}, cfg, self.now)
            self.assertFalse(annotated[0]['arming_eligible'])
            self.assertTrue(annotated[1]['arming_eligible'])
        result = forecast({'races':races}, {'decisions':{}, 'races':[], 'updated_at':self.now.isoformat()},
                          {}, emitter, config, {}, {}, self.now)
        self.assertEqual(result['races']['Goulburn']['outcome'], 'track_priority_skip')
        self.assertEqual(result['sequence'][0]['market_id'], 'Geelong')


if __name__ == '__main__':
    unittest.main()
