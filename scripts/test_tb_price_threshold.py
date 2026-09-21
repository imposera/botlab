import unittest
from tb_price_threshold import POINTS,threshold
class ThresholdTests(unittest.TestCase):
 def test_anchors(self):
  for x,y in POINTS:self.assertAlmostEqual(threshold(x),y)
 def test_monotone_and_no_overshoot(self):
  values=[threshold(2.4+i*2.6/1000) for i in range(1001)]
  self.assertTrue(all(b>=a-1e-10 for a,b in zip(values,values[1:])))
  for a,b in zip(POINTS,POINTS[1:]):
   for i in range(101):self.assertTrue(a[1]-1e-10<=threshold(a[0]+i*(b[0]-a[0])/100)<=b[1]+1e-10)
 def test_no_invented_extrapolation(self):
  for x in (None,True,2,6,60,float('nan'),float('inf')):self.assertIsNone(threshold(x))
