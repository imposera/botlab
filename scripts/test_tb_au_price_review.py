import unittest
from tb_au_price_review import classify
from tb_runner_shape import STAGES
class ReviewTests(unittest.TestCase):
 def c(self,values,**kwargs):return classify(dict(zip(STAGES,values)),**kwargs)
 def test_patterns(self):
  for values,label in [([8,7.8,7.6,6.2,5.5],'Late firm'),([5,5.1,5.2,6,7],'Late drift'),([10,9,8,7,6],'Steady firm'),([6,7,8,9,10],'Steady drift'),([10,8,6,8,9],'Firm then rebound'),([5,7,9,7,6],'Drift then recovery'),([5,8,4,7,5],'Mixed / volatile'),([5,5.01,5.02,5,5],'Stable')]:
   with self.subTest(label=label):self.assertEqual(self.c(values)['label'],label)
 def test_exact_windows_partial_and_break(self):
  r=classify({'T15':10,'T5':8,'T30':6})
  self.assertTrue(r['partial']);self.assertAlmostEqual(r['early'],-20);self.assertAlmostEqual(r['late'],-25)
  self.assertEqual(r['label'],'Insufficient data')
  r=self.c([10,9,8,7,6],broken=True);self.assertIsNone(r['early']);self.assertIn('scratching',r['label'])
  self.assertIsNone(classify({'T10':5,'T5':4})['early'])
 def test_threshold(self):
  self.assertEqual(self.c([10,10.1,10.2,10.3,10.4],threshold=5)['label'],'Stable')

class ShortlistReviewTests(unittest.TestCase):
 def test_closing_direction_and_quality(self):
  from tb_au_price_review import shortlist_review
  full=dict(zip(STAGES,[10,8,6,5,6]))
  r=shortlist_review(full);self.assertEqual(r['status'],'Complete');self.assertAlmostEqual(r['closing'],20);self.assertIn('Drifting',r['reason'])
  r=shortlist_review({'T2':5,'T30':4});self.assertEqual(r['status'],'Incomplete');self.assertAlmostEqual(r['closing'],-20)
  r=shortlist_review(full,broken=True);self.assertEqual(r['status'],'Not comparable');self.assertIsNone(r['closing'])
