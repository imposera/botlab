import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import tb_race_queue as q
from concurrent.futures import ThreadPoolExecutor
import subprocess

now=datetime(2026,9,7,12,tzinfo=timezone.utc)

class QueueTests(unittest.TestCase):
 def setUp(self):
  clock=patch.object(q,"utc_now",return_value=now);clock.start();self.addCleanup(clock.stop)
  secrets=patch.object(q,"load_secrets");secrets.start();self.addCleanup(secrets.stop)
  temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.base=Path(temp.name)
  self.market={'market_id':'1.2','market_start_time':(now+timedelta(minutes=10)).isoformat(),'market_name':'R1 1200m Mdn','runners':[]}
  self.queue={'decisions':{},'races':[q.row_for_market(self.market,1,q.default_priority_config(),{},None)]}
  q.atomic_write_json(self.base/q.QUEUE_FILE_NAME,self.queue)
 def refresh(self,callback=None):
  with patch.object(q,'discover_markets',side_effect=callback or (lambda _: [self.market])):
   return q.refresh_queue(self.base,self.base,self.base/'unused',10,4)
 def test_advice_does_not_auto_remove(self):
  self.assertEqual(self.queue['races'][0]['recommendation'],'PRUNE?')
  self.assertTrue(self.queue['races'][0]['eligible'])
 def test_serial_remove_preserved_and_restore(self):
  q.update_decision(self.base,'1.2','remove','manual')
  self.assertFalse(self.refresh()['races'][0]['eligible'])
  self.assertTrue(q.update_decision(self.base,'1.2',None,None)['races'][0]['eligible'])
 def test_next_rejects_elapsed_cached_countdown(self):
  with patch.object(q,'utc_now',return_value=now+timedelta(minutes=20)):
   self.assertIsNone(q.next_eligible(self.queue))
 def test_refresh_preserves_remove_during_discovery(self):
  def during(_):
   q.update_decision(self.base,'1.2','remove','must remain removed')
   return [self.market]
  refreshed=self.refresh(during)
  self.assertFalse(refreshed['races'][0]['eligible'])
 def test_corrupt_queue_does_not_discard_logged_remove(self):
  q.update_decision(self.base,'1.2','remove','manual')
  (self.base/q.QUEUE_FILE_NAME).write_text('{broken')
  try:refreshed=self.refresh()
  except RuntimeError:return
  self.assertFalse(refreshed['races'][0]['eligible'])
 def test_specific_starter_allowance_rule(self):
  score,_=q.score_race_quality({'market_name':'Starter allowance'})
  self.assertEqual(score,5)
 def test_actual_grade_one_label(self):
  score,_=q.score_race_quality({'market_name':'R4 7f Grd 1'})
  self.assertEqual(score,80)

 def test_simultaneous_decisions_preserved(self):
  with ThreadPoolExecutor(max_workers=2) as pool:
   list(pool.map(lambda mid:q.update_decision(self.base,mid,'remove','test'),['1.2','1.3']))
  saved=q.read_json(self.base/q.QUEUE_FILE_NAME)
  self.assertEqual(set(saved['decisions']),{'1.2','1.3'})
  self.assertEqual(len((self.base/q.DECISION_LOG_FILE_NAME).read_text().splitlines()),2)
 def test_deleted_queue_with_audit_log_is_not_reset(self):
  q.update_decision(self.base,'1.2','remove','test')
  (self.base/q.QUEUE_FILE_NAME).unlink()
  with self.assertRaises(RuntimeError):self.refresh()
  self.assertFalse((self.base/q.QUEUE_FILE_NAME).exists())
 def test_invalid_decision_structure_is_not_reset(self):
  self.queue['decisions']=['invalid']
  q.atomic_write_json(self.base/q.QUEUE_FILE_NAME,self.queue)
  with self.assertRaises(RuntimeError):self.refresh()
 def test_next_skips_bad_timestamp_and_returns_current_time(self):
  self.queue['races'].insert(0,{'eligible':True,'market_start_time':'invalid'})
  with patch.object(q,'utc_now',return_value=now+timedelta(minutes=5)):
   row=q.next_eligible(self.queue)
   self.assertEqual(row['seconds_to_jump'],300)
  self.assertEqual(self.queue['races'][1]['seconds_to_jump'],600)
 def test_offline_cli_without_site_packages(self):
  result=subprocess.run([sys.executable,'-S',str(Path(q.__file__)),'--state-dir',str(self.base),'show'],capture_output=True,text=True)
  self.assertEqual(result.returncode,0,result.stderr)
  self.assertIn('ROLLING QUEUE',result.stdout)
 def test_class_matching_uses_boundaries(self):
  self.assertEqual(q.score_race_quality({'market_name':'R1 G10'})[0],0)

if __name__ == '__main__':
 unittest.main(verbosity=2)
