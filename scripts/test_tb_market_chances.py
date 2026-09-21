import unittest
from tb_au_capture_wall import market_chances

def capture(prices):
 return {'book':{'status':'OPEN','inplay':False,'numberOfActiveRunners':len(prices),'runners':[{'selectionId':i,'status':'ACTIVE','lastPriceTraded':p} for i,p in enumerate(prices)]}}
class ChanceTests(unittest.TestCase):
 def test_normalization_and_earliest_complete(self):
  slot,rs=market_chances({'T15':capture([2,None]),'T10':capture([2,4]),'T5':capture([2,2])},['0','1'])
  self.assertEqual(slot,'T10');self.assertAlmostEqual(sum(r['chance'] for r in rs.values()),100);self.assertAlmostEqual(rs['0']['chance'],200/3);self.assertAlmostEqual(rs['0']['odds'],1.5)
 def test_missing_runner_inplay_and_invalid_prices(self):
  self.assertEqual(market_chances({'T15':capture([2,3])},['0','1','2']),(None,{}))
  for price in (None,0,1,float('nan')):self.assertEqual(market_chances({'T15':capture([2,price])}),(None,{}))
  c=capture([2,3]);c['book']['inplay']=True;self.assertEqual(market_chances({'T15':c}),(None,{}))
 def test_scratched_excluded_from_snapshot(self):
  c=capture([2,4]);c['book']['runners'].append({'selectionId':2,'status':'REMOVED'})
  slot,rs=market_chances({'T15':c},['0','1','2']);self.assertEqual(slot,'T15');self.assertNotIn('2',rs)
