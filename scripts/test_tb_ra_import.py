from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from tb_ra_import import Importer,identify

PAGE='<a name="Race1"></a><tr><td class="horse"><a href="../InteractiveForm/HorseFullForm.aspx?stage=Acceptances&amp;Key=2026Sep12,NSW,Rosehill+Gardens">HORSE (NZ)</a></td><td class="hcp">75</td></tr>'
class ImportTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.state=Path(self.temp.name)/'state';self.state.mkdir()
  job={'date':'2026-09-12','market':{'market_id':'1.1','track':'Rosehill','country_code':'AU','market_name':'R1 1400m','market_start_time':'2026-09-12T05:00:00Z','runners':[{'selection_id':1,'runner_name':'Horse'}]}}
  (self.state/'tb_au_observations.json').write_text(json.dumps({'races':{'1.1':job}}))
  self.importer=Importer(self.state)
 def preview(self,page=PAGE):return self.importer.preview([{'name':'meeting.html','html':page}])
 def test_preview_commit_idempotent_and_no_preview_writes(self):
  p=self.preview();self.assertEqual(p['new_ratings'],1)
  self.assertFalse((self.state/'tb_ra_ratings.json').exists())
  self.assertEqual(self.importer.commit(p['preview_id'])['imported'],1)
  self.assertEqual(self.preview()['new_ratings'],0)
  with self.assertRaises(ValueError):self.importer.commit(p['preview_id'])
  data=json.loads((self.state/'tb_ra_ratings.json').read_text())
  self.assertEqual(data['days']['2026-09-12']['races']['1.1']['1']['rating'],75)
 def test_wrong_pages_and_wrong_day(self):
  for page in [PAGE.replace('Acceptances','Weights'),'Are you human?',PAGE.replace('2026Sep12','2026Sep13')]:
   p=self.preview(page);self.assertEqual(p['new_ratings'],0);self.assertIn('error',p['reports'][0])
 def test_conflicting_files_and_duplicate_files(self):
  p=self.importer.preview([{'html':PAGE},{'html':PAGE.replace('75','76')}])
  self.assertEqual(p['new_ratings'],0);self.assertEqual(p['conflicting_runners'],1)
  p=self.importer.preview([{'html':PAGE},{'html':PAGE}]);self.assertEqual(p['new_ratings'],1)
 def test_archive_and_paused_flag_preserved(self):
  (self.state/'tb_ra_ratings.json').write_text(json.dumps({'days':{},'access_status':'verification_required'}))
  self.importer.commit(self.preview()['preview_id'])
  self.assertEqual(json.loads((self.state/'tb_ra_ratings.json').read_text())['access_status'],'verification_required')
  self.assertEqual(len(list((self.state.parent/'imports/racing_australia').glob('*.html'))),1)
 def test_http_auth_and_workflow(self):
  from tb_race_queue_wall import make_handler
  from http.server import ThreadingHTTPServer
  import threading,re
  from urllib.request import urlopen,Request
  from urllib.error import HTTPError
  server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.state))
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   base=f'http://127.0.0.1:{server.server_port}'
   with urlopen(base+'/import-ra') as response:page=response.read().decode()
   token=re.search("const token='([^']+)'",page)[1]
   def post(endpoint,body,key):
    req=Request(base+'/api/ra-import/'+endpoint,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Queue-Token':key})
    with urlopen(req) as response:return json.load(response)
   with self.assertRaises(HTTPError) as error:post('preview',{'files':[]},'bad')
   self.assertEqual(error.exception.code,403)
   preview=post('preview',{'files':[{'name':'test.html','html':PAGE}]},token)
   self.assertEqual(post('commit',{'preview_id':preview['preview_id']},token)['imported'],1)
  finally:server.shutdown();server.server_close();thread.join()
