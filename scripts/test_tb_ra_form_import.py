import unittest,tempfile,json
from pathlib import Path
from tb_ra_form_import import FormImporter
from tb_race_queue import atomic_write_json

class ImportTests(unittest.TestCase):
 def test_preview_immutable_commit_skip_duplicates(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Path(tmp);(b/'recent_form').mkdir()
   p=b/'recent_form/form.html';p.write_text('<a name="Race1"></a><tr><td class="horse"><a href="/HorseFullForm.aspx?stage=FinalFields&amp;Key=2026Sep16,NSW,Test">Horse</a></td><td class="hcp">60</td></tr><table class="horse-form-table"><span class="horse-name">Horse</span></table>')
   atomic_write_json(b/'state/tb_au_observations.json',{'races':{'1.1':{'date':'2026-09-16','market':{'track':'Test','country_code':'AU','market_name':'R1','market_start_time':'2026-09-16T04:00:00Z','runners':[{'selection_id':1,'runner_name':'Horse'}]}}}})
   obj=FormImporter(b);d=obj.preview();self.assertEqual(d['files_ready'],1);self.assertFalse((b/'state/tb_ra_form.json').exists())
   original=p.read_text();p.write_text('changed after preview')
   self.assertEqual(obj.commit(d['preview_id'])['imported_runners'],1)
   with self.assertRaises(ValueError):obj.commit(d['preview_id'])
   p.write_text(original);self.assertEqual(obj.preview()['skipped_files'],1)
 def test_bad_page_reported(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Path(tmp);(b/'recent_form').mkdir();(b/'recent_form/bad.html').write_text('Are you human?')
   d=FormImporter(b).preview();self.assertEqual(d['files_ready'],0);self.assertIn('error',d['reports'][0])
