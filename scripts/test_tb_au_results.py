import unittest
import tempfile
from pathlib import Path
from datetime import datetime,timezone,timedelta
from tb_race_queue import atomic_write_json
from tb_au_results import tick,FILE

class ResultsTest(unittest.TestCase):
 def test_settlement_retry_and_retention(self):
  with tempfile.TemporaryDirectory() as tmp:
   base=Path(tmp);now=datetime(2026,9,13,6,tzinfo=timezone.utc)
   atomic_write_json(base/'state/tb_au_observations.json',{'races':{'1.1':{'date':'2026-09-13','market':{'market_start_time':(now-timedelta(minutes=10)).isoformat()}}}})
   state=tick(base,lambda ids:[],now);self.assertIn('error',state['races']['1.1'])
   book={'marketId':'1.1','status':'CLOSED','runners':[{'selectionId':1,'status':'WINNER'},{'selectionId':2,'status':'WINNER'},{'selectionId':3,'status':'REMOVED'}]}
   calls=[]
   def fetch(ids):calls.append(ids);return [book]
   state=tick(base,fetch,now+timedelta(minutes=6));self.assertEqual(calls,[['1.1']]);self.assertEqual(state['races']['1.1']['status'],'settled');self.assertEqual(state['races']['1.1']['book'],book)
   tick(base,fetch,now+timedelta(minutes=7));self.assertEqual(len(calls),1)
   state=tick(base,fetch,now+timedelta(days=8));self.assertEqual(state['races']['1.1']['status'],'settled');self.assertEqual(len(calls),1)
 def test_future_not_requested(self):
  with tempfile.TemporaryDirectory() as tmp:
   base=Path(tmp);now=datetime.now(timezone.utc)
   atomic_write_json(base/'state/tb_au_observations.json',{'races':{'1.1':{'date':'2026-09-13','market':{'market_start_time':now.isoformat()}}}})
   def fetch(ids):self.fail('Premature result request')
   self.assertFalse(tick(base,fetch,now)['races'])
 def test_wall_joins_results_by_selection_and_escapes_names(self):
  from tb_au_capture_wall import render
  with tempfile.TemporaryDirectory() as tmp:
   base=Path(tmp)
   atomic_write_json(base/'state/tb_au_observations.json',{'races':{'1.1':{'date':'2026-09-13','slots':{},'market':{'track':'Test','market_name':'R1','runners':[{'selection_id':2,'runner_name':'Other'},{'selection_id':1,'runner_name':'Winner <one>'}]}}}})
   query={'date':['2026-09-13'],'market':['1.1']}
   self.assertIn('Awaiting result',render(base,query))
   atomic_write_json(base/'state'/FILE,{'races':{'1.1':{'status':'settled','book':{'runners':[{'selectionId':1,'status':'WINNER'},{'selectionId':2,'status':'LOSER'}]}}}})
   page=render(base,query)
   self.assertIn('Winner: Winner &lt;one&gt;',page);self.assertIn('class="runner-result">Non-winner',page);self.assertNotIn('Winner <one>',page)
