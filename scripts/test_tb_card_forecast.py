from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from test_betfair_t15_emit import load_emitter
from tb_card_forecast import forecast
from tb_queue_policy import key
import tb_today_card as card

class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,9,11,0,tzinfo=timezone.utc)
        self.e=load_emitter();self.config=self.e.default_priority_config()
        self.queue={'decisions':{},'races':[],'updated_at':self.now.isoformat()}
    def race(self,mid,minutes,track=None):
        return {'market_id':mid,'market_name':'R1 1000m Hcap','track':track or mid,
                'country_code':'AU','market_start_time':(self.now+timedelta(minutes=minutes)).isoformat(),
                'market_status':'OPEN','inplay':False,'runners':[{}]*8}
    def run_forecast(self,races,**kwargs):
        return forecast({'races':races},self.queue,kwargs.pop('target',{}),self.e,self.config,
                        kwargs.pop('profiles',{}),{},self.now,**kwargs)
    def test_ten_minute_gap_blocks_later_race_but_spaced_race_arms(self):
        f=self.run_forecast([self.race('a',20),self.race('b',30),self.race('c',50)])
        self.assertEqual([r['market_id'] for r in f['sequence']],['a','c'])
        self.assertEqual(f['races']['b']['outcome'],'timing_blocked')
        self.assertEqual(f['races']['b']['blockers'],['a'])
        self.assertEqual(f['races']['a']['projected_arm_at'],(self.now+timedelta(minutes=2)).isoformat().replace('+00:00','Z'))
        self.assertEqual(f['counts']['blocked'],1)
    def test_three_minute_delay_sensitivity_and_tight_turnaround(self):
        f=self.run_forecast([self.race('a',20),self.race('b',34)])
        self.assertEqual(f['races']['b']['outcome'],'projected_arm')
        self.assertTrue(f['races']['b']['delay_risk'])
        self.assertEqual(f['races']['b']['timing_clashes'][0]['kind'],'tight_turnaround')
    def test_remove_changes_projection_without_auto_decisions(self):
        self.queue['decisions']={'a':{'action':'remove'}}
        f=self.run_forecast([self.race('a',20),self.race('b',30)])
        self.assertEqual(f['races']['a']['outcome'],'removed')
        self.assertEqual(f['races']['b']['outcome'],'projected_arm')
        self.assertEqual(self.queue['decisions'],{'a':{'action':'remove'}})
    def test_learned_skip_and_keep_override(self):
        races=[self.race('a',20),self.race('b',23)]
        profiles={key(races[0]):{'removals':3,'decisions':3,'race_days':2}}
        f=self.run_forecast(races,profiles=profiles)
        self.assertEqual(f['races']['a']['outcome'],'learned_skip')
        self.assertEqual(f['sequence'][0]['market_id'],'b')
        self.queue['decisions']={'a':{'action':'keep'}}
        self.assertEqual(self.run_forecast(races,profiles=profiles)['sequence'][0]['market_id'],'a')
    def test_priority_uses_live_scorer_and_restores_clock(self):
        self.config.update(enabled=True,track_scores={'strong':100})
        clock=self.e.utc_now
        f=self.run_forecast([self.race('a',20,'weak'),self.race('b',20,'strong')])
        self.assertEqual(f['sequence'][0]['market_id'],'b')
        self.assertIs(self.e.utc_now,clock)
    def test_actual_target_is_kept_even_if_removed_and_cannot_rearm_when_locked(self):
        self.config.update(enabled=True,rearm_enabled=True,track_scores={'strong':100})
        a,b=self.race('a',25,'weak'),self.race('b',25,'strong')
        self.queue['decisions']={'a':{'action':'remove'}}
        f=self.run_forecast([a,b],active=a,active_locked=True,target={'status':'armed','market':a})
        self.assertEqual(f['races']['a']['outcome'],'active')
        self.assertEqual(f['races']['b']['outcome'],'timing_blocked')
        f=self.run_forecast([a,b],active=a,active_locked=False,target={'status':'armed','market':a})
        self.assertEqual(f['races']['a']['outcome'],'replaced_before_lock')
        self.assertEqual(f['sequence'][0]['action'],'rearm')
    def test_known_worldwide_queue_blocks_card_without_entering_au_sequence(self):
        self.queue['races']=[dict(self.race('nz',20),country_code='NZ')]
        f=self.run_forecast([self.race('au',30)])
        self.assertEqual(f['sequence'],[])
        self.assertEqual(f['races']['au']['blockers'],['nz'])
    def test_boundaries_elapsed_and_filtered_card_keep_full_sequence(self):
        f=self.run_forecast([self.race('old',-1),self.race('a',20),self.race('b',38)])
        self.assertEqual(f['races']['old']['outcome'],'elapsed_or_closed')
        self.assertEqual(f['races']['b']['timing_clashes'],[])
        self.assertEqual(f['counts']['projected'],2)
    def test_decision_changes_make_saved_forecast_stale(self):
        race=self.race('a',20)
        saved=card.merge_card({},[race],[{'marketId':'a','status':'OPEN','inplay':False}],self.now,discovered=True)
        saved['emitter_forecast']=self.run_forecast([race])
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);(p/card.FILE).write_text(json.dumps(saved));(p/'tb_race_queue.json').write_text(json.dumps(self.queue))
            data=card.summary(p,now=self.now)
            self.assertTrue(data['emitter_forecast']['fresh'])
            self.queue['decisions']={'a':{'action':'remove'}}
            (p/'tb_race_queue.json').write_text(json.dumps(self.queue))
            data=card.summary(p,now=self.now)
            self.assertFalse(data['emitter_forecast']['fresh'])
            self.assertFalse(data['races'][0]['forecast_fresh'])
            self.assertEqual(data['races'][0]['selection_status'],'removed')

if __name__=='__main__':unittest.main()
