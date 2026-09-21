import unittest
from tb_ra_form import parse_form

class FormTests(unittest.TestCase):
 def test_rating_dates_trials_and_average(self):
  head='<a name="Race1"></a><tr><td class="horse"><a href="/HorseFullForm.aspx?stage=FinalFields&amp;Key=2026Sep14,NSW,Corowa">Horse</a></td><td class="hcp">60</td></tr>'
  def row(d,r,kind='BM58'):
   return f'<tr><td class="remain"><a href="/Meeting.aspx?racecode={d}">COR {d}</a> 1200m {kind} Rtg {r}<br>1st Other 58kg</td></tr>'
  page=head+'<table class="horse-form-table"><span class="horse-name">Horse</span><table>'+''.join([row('01Aug26',50),row('10Aug26',56),row('01Sep26',60),row('10Sep26',62),row('12Sep26',99,'OPEN-BT'),row('14Sep26',100),row('15Sep26',100)])+'</table></table>'
  _,_,r=parse_form(page);self.assertEqual(r[0]['baseline'],59.3);self.assertEqual([s['rating'] for s in r[0]['starts']],[62,60,56])
 def test_no_history_stays_blank(self):
  text='<a name="Race1"></a><tr><td class="horse"><a href="/HorseFullForm.aspx?stage=FinalFields&amp;Key=2026Sep14,NSW,Corowa">Horse</a></td><td class="hcp">60</td></tr><table class="horse-form-table"><span class="horse-name">Horse</span></table>'
  self.assertIsNone(parse_form(text)[2][0]['baseline'])

 def test_import_is_idempotent(self):
  import tempfile,json
  from pathlib import Path
  from tb_ra_form import run,FILE
  from tb_race_queue import atomic_write_json
  with tempfile.TemporaryDirectory() as tmp:
   base=Path(tmp);page=base/'form.html'
   page.write_text('<a name="Race1"></a><tr><td class="horse"><a href="/HorseFullForm.aspx?stage=FinalFields&amp;Key=2026Sep14,NSW,Corowa">Horse</a></td><td class="hcp">60</td></tr><table class="horse-form-table"><span class="horse-name">Horse</span></table>')
   atomic_write_json(base/'state/tb_au_observations.json',{'races':{'1.1':{'date':'2026-09-14','market':{'track':'Corowa','country_code':'AU','market_name':'R1','market_start_time':'2026-09-14T04:00:00Z','runners':[{'selection_id':1,'runner_name':'Horse'}]}}}})
   self.assertEqual(run(base,page)['no_rated_history'],1);self.assertFalse((base/'state'/FILE).exists())
   self.assertEqual(run(base,page,True)['imported_runners'],1)
   self.assertEqual(run(base,page,True)['imported_runners'],0)

class DatabaseTests(unittest.TestCase):
 def test_updates_deduplicates_and_older_import_cannot_regress(self):
  from tb_ra_form import update_runner
  def record(day,digest,ratings):
   return {'horse':'Horse','horse_code':'stable-id','meeting_date':day,'imported_at':day+'T10:00:00Z','file_sha256':digest,'method':'mean_latest_3_rated_races/v1','all_starts':[{'date':d,'rating':r,'source_url':'https://example/Meeting.aspx?racecode='+d} for d,r in ratings]}
  db={};r=record('2026-09-14','one',[('2026-09-01',60),('2026-09-02',62)])
  self.assertEqual(update_runner(db,'1.1','12',r),1);self.assertEqual(update_runner(db,'1.1','12',r),0)
  self.assertEqual(db['ra:stable-id']['baseline'],61)
  update_runner(db,'1.2','12',record('2026-09-20','two',[('2026-09-02',64),('2026-09-15',66)]))
  h=db['ra:stable-id'];self.assertEqual(h['baseline'],63.3);self.assertEqual(h['rated_start_count'],3);self.assertEqual(len(h['revisions']),1)
  update_runner(db,'1.0','12',record('2026-09-10','old',[('2026-09-02',50)]))
  self.assertEqual(h['baseline'],63.3);self.assertEqual(len(h['imports']),3)

class RecentWinTests(unittest.TestCase):
 def test_unrated_win_trials_and_last_three(self):
  from tb_ra_form import parse_form
  head='<a name="Race1"></a><tr><td class="horse"><a href="/HorseFullForm.aspx?stage=FinalFields&amp;Key=2026Sep16,NSW,Test">Horse</a></td><td class="hcp">60</td></tr><table class="horse-form-table"><span class="horse-name">Horse</span><table>'
  def row(day,pos,kind='MDN'):
   return f'<tr><td class="Pos">{pos} of 10</td><td class="remain"><a href="/Meeting.aspx?racecode={day}">TEST {day}Sep26</a> 1200m {kind}</td></tr>'
  rows=row('01','1')+row('05','4')+row('07','3')+row('09','2')+row('10','T1','OPEN-BT')
  r=parse_form(head+rows+'</table></table>')[2][0];self.assertFalse(r['recent_win']);self.assertEqual(len(r['recent_races']),3)
  r=parse_form(head+rows+row('11','1')+'</table></table>')[2][0];self.assertTrue(r['recent_win']);self.assertIsNone(r['baseline']);self.assertEqual(r['recent_races'][0]['distance_m'],1200)
