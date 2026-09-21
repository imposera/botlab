import unittest
from copy import deepcopy
from tb_racing_fundamentals import extract,merge
class FundamentalsTests(unittest.TestCase):
 def test_fields_and_missing(self):
  r=extract('GOUL 01Sep26 1400m Soft5 BM64 $30,000 J Smith 59kg (cd 57kg) Barrier 3 Rtg 62 1st Other 60kg 1:22.0, 2.30L','GOUL 01Sep26','3 of 10')
  for k,v in {'track_code':'GOUL','distance_m':1400,'class_raw':'BM64','going':'Soft','going_rating':5,'weight_kg':59,'carried_weight_kg':57,'barrier':3,'handicap_rating':62,'position':3,'field_size':10,'margin_lengths':2.3,'country':None}.items():self.assertEqual(r[k],v,k)
  r=extract('unknown','unknown','DNF');self.assertIsNone(r['position']);self.assertIsNone(r['distance_m'])
 def test_idempotency_and_correction_history(self):
  state={};r={'horse_code':'horse','horse':'Horse','file_sha256':'a','meeting_date':'2026-09-16','imported_at':'2026-09-16T00:00:00Z','fundamental_starts':[{'source_url':'https://example/Meeting.aspx?racecode=r1','date':'2026-09-01','handicap_rating':60}]}
  self.assertEqual(merge(state,'1.1','1',r),1);self.assertEqual(merge(state,'1.1','1',r),0)
  newer=deepcopy(r);newer.update(file_sha256='b',meeting_date='2026-09-17');newer['fundamental_starts'][0]['handicap_rating']=61
  merge(state,'1.2','1',newer);db=state['fundamentals'];self.assertEqual(len(db['starts']),1);self.assertEqual(len(db['revisions']),1);self.assertEqual(next(iter(db['starts'].values()))['handicap_rating'],61)
  old=deepcopy(r);old['file_sha256']='c';merge(state,'1.3','1',old);self.assertEqual(next(iter(db['starts'].values()))['handicap_rating'],61)
