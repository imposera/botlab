from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from tb_au_observer import tick, artifact
from tb_au_coverage import summary, FILE, panel


class AustralianObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); (self.base/'state').mkdir()
        self.now = datetime(2026,9,11,0,tzinfo=timezone.utc)
        self.markets = [dict(market_id='1.'+str(i),country_code='AU',track=track,
                             market_name='R1',market_start_time=(self.now+timedelta(minutes=15)).isoformat(), runners=[])
                        for i,track in enumerate(['Geelong','Tuncurry','Goulburn'],1)]
        self.card = {'date':'2026-09-11','races':self.markets,'discovery_updated_at':self.now.isoformat()}
        self.save_card()
    def save_card(self):
        (self.base/'state/tb_today_card.json').write_text(json.dumps(self.card))
    def fetch(self,ids):
        return [{'marketId':mid,'status':'OPEN','inplay':False,'runners':[]} for mid in ids]
    def test_all_tracks_and_removed_races_capture_without_target_writes(self):
        (self.base/'state/tb_race_queue.json').write_text('{"decisions":{"1.2":{"action":"remove"}}}')
        result=tick(self.base,self.fetch,now=self.now)
        self.assertEqual(len(result['races']),3)
        for mid,job in result['races'].items():
            self.assertEqual(job['slots']['T15']['status'],'captured')
            self.assertTrue(artifact(self.base,job,mid,'T15').exists())
        self.assertFalse((self.base/'state/betfair_t15_target.json').exists())
        self.assertEqual(summary(self.base/'state',now=self.now)['counts']['observing'],3)
    def test_restart_does_not_duplicate_capture_and_recovers_committed_artifact(self):
        first=tick(self.base,self.fetch,now=self.now)
        first['races']['1.1']['slots']['T15']={'status':'pending','attempts':0,'due_at':self.now.isoformat()}
        (self.base/'state'/FILE).write_text(json.dumps(first))
        def forbidden(ids):raise AssertionError('Already captured')
        second=tick(self.base,forbidden,now=self.now+timedelta(seconds=10))
        self.assertEqual(second['races']['1.1']['slots']['T15']['status'],'captured')
        self.assertEqual(second['races']['1.2']['slots']['T15']['attempts'],1)
    def test_missing_response_retries_then_records_gap(self):
        first=tick(self.base,lambda ids:[],now=self.now)
        self.assertEqual(first['races']['1.1']['slots']['T15']['status'],'pending')
        self.assertEqual(summary(self.base/'state',now=self.now)['counts']['unavailable'],3)
        second=tick(self.base,self.fetch,now=self.now+timedelta(seconds=20))
        self.assertEqual(second['races']['1.1']['slots']['T15']['late_seconds'],20)
        third=tick(self.base,lambda ids:[],now=self.now+timedelta(minutes=6,seconds=1))
        self.assertEqual(third['races']['1.1']['slots']['T10']['status'],'missed')
        self.assertEqual(summary(self.base/'state',now=self.now+timedelta(minutes=6,seconds=1))['counts']['incomplete'],3)
    def test_closed_and_inplay_never_saved_as_preplay(self):
        for raw in [{'status':'CLOSED','inplay':False},{'status':'OPEN','inplay':True}]:
            with self.subTest(raw=raw):
                result=tick(self.base,lambda ids:[dict(raw,marketId=i) for i in ids],now=self.now)
                self.assertEqual(result['races']['1.1']['slots']['T15']['status'],'missed')
                self.assertFalse(artifact(self.base,result['races']['1.1'],'1.1','T15').exists())
                (self.base/'state'/FILE).unlink()
    def test_discovery_omission_retains_jobs_and_rollover(self):
        tick(self.base,self.fetch,now=self.now)
        self.card['races']=[];self.card['date']='2026-09-12';self.save_card()
        result=tick(self.base,self.fetch,now=self.now+timedelta(days=1))
        self.assertEqual(len(result['races']),3)
        self.assertEqual(result['races']['1.1']['slots']['T30']['status'],'missed')
    def test_corrupt_state_is_not_overwritten(self):
        path=self.base/'state'/FILE;path.write_text('{bad')
        with self.assertRaises(ValueError):tick(self.base,self.fetch,now=self.now)
        self.assertEqual(path.read_text(),'{bad')
    def test_coverage_missing_stale_and_html_escaping(self):
        self.assertEqual(summary(self.base/'state',now=self.now)['freshness'],'unavailable')
        self.markets[0]['track']='<script>';self.save_card()
        tick(self.base,self.fetch,now=self.now)
        data=summary(self.base/'state',now=self.now+timedelta(seconds=100))
        self.assertEqual(data['freshness'],'stale')
        self.assertNotIn('<script>',panel(data))
    def test_all_five_slots_complete_and_no_late_backfill(self):
        for offset in [0,300,600,780,870]:
            tick(self.base,self.fetch,now=self.now+timedelta(seconds=offset))
        self.assertEqual(summary(self.base/'state',now=self.now+timedelta(seconds=870))['counts']['complete'],3)
        self.card['races'].append(dict(self.markets[0],market_id='1.9'));self.save_card()
        result=tick(self.base,self.fetch,now=self.now+timedelta(seconds=900))
        self.assertTrue(all(s['status']=='missed' for s in result['races']['1.9']['slots'].values()))

if __name__=='__main__':unittest.main()

class CoverageHTTPTests(unittest.TestCase):
    def test_wall_endpoint_and_live_only_panel(self):
        from unittest.mock import Mock
        import tb_wall as wall
        with tempfile.TemporaryDirectory() as root:
            base=Path(root)
            handler=object.__new__(wall.WallHandler)
            handler.base_dir=base;handler.path='/api/au-coverage';handler.send_text=Mock()
            handler.do_GET()
            data=json.loads(handler.send_text.call_args.args[0])
            self.assertEqual(data['schema'],'tb_au_coverage/v1')
            self.assertEqual(handler.send_text.call_args.kwargs['status'],503)
            self.assertIn('data-poll="au-coverage"',wall.html_page(base))

    def test_authoritative_endpoint(self):
        from http.server import ThreadingHTTPServer
        import threading
        from urllib.request import urlopen
        from urllib.error import HTTPError
        from tb_race_queue_wall import make_handler
        with tempfile.TemporaryDirectory() as root:
            state=Path(root)
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(state))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(f'http://127.0.0.1:{server.server_port}/api/au-coverage')
                self.assertEqual(caught.exception.code,503)
                self.assertEqual(json.loads(caught.exception.read())['schema'],'tb_au_coverage/v1')
            finally:
                server.shutdown();server.server_close();thread.join()
