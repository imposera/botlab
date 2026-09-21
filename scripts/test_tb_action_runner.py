import unittest
from tb_au_capture_wall import action_stages
class ActionTests(unittest.TestCase):
 def test_either_window_and_boundary(self):
  self.assertEqual(action_stages({'T5':3,'T2':4},6,'T15'),['T5'])
  self.assertEqual(action_stages({'T5':4,'T2':2.9},6,'T15'),['T2'])
  self.assertEqual(action_stages({'T5':3,'T2':3},6,'T15'),['T5','T2'])
 def test_invalid_and_noncomparable(self):
  self.assertEqual(action_stages({'T5':2,'T2':2},6,'T30'),[])
  self.assertEqual(action_stages({'T5':2,'T2':2},6,'T5'),['T2'])
  self.assertEqual(action_stages({'T5':2},6,'T15',True),[])
  self.assertEqual(action_stages({'T5':0,'T2':float('nan')},6,'T15'),[])

class DriftTests(unittest.TestCase):
 def test_fifteen_percent_and_latest(self):
  from tb_au_capture_wall import drift_stage
  self.assertEqual(drift_stage({'T2':2.99},2.6,'T15'),'T2')
  self.assertIsNone(drift_stage({'T2':2.98},2.6,'T15'))
  self.assertEqual(drift_stage({'T5':3},2.6,'T15'),'T5')
  self.assertIsNone(drift_stage({'T5':5,'T2':2.7},2.6,'T15'))
  self.assertIsNone(drift_stage({'T2':5},2.6,'T15',True))
  self.assertIsNone(drift_stage({'T2':5},2.6,'T30'))
  self.assertEqual(drift_stage({'T2':11.5},10,'T15'),'T2')
  self.assertIsNone(drift_stage({'T2':20},None,'T15'))
