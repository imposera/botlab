from datetime import datetime, timedelta, timezone
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import tempfile
import threading
import types
import sys
import unittest
from unittest.mock import Mock, patch

import tb_today_card as card
import tb_race_queue as queue
import tb_wall as wall
from tb_race_queue_wall import make_handler


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)/'state'
        self.state.mkdir()
        self.now = datetime(2026, 9, 11, 0, tzinfo=timezone.utc)
        self.race = {'market_id':'1.99','market_name':'R1 1000m Hcap','track':'Example',
                     'meeting_id':'99','event_id':'99','country_code':'AU',
                     'market_start_time':(self.now+timedelta(hours=1)).isoformat(),
                     'runners':[{'selection_id':7,'runner_name':'Winner Name'}]}
        self.book = {'marketId':'1.99','status':'OPEN','inplay':False,'numberOfActiveRunners':8,
                     'isMarketDataDelayed':False,'runners':[{'selectionId':7,'status':'ACTIVE'}]}
        self.saved = card.merge_card({}, [self.race], [self.book], self.now, discovered=True)
        self.write()
        queue.atomic_write_json(self.state/queue.QUEUE_FILE_NAME, {'decisions':{},'races':[]})

    def write(self):
        queue.atomic_write_json(self.state/card.FILE, self.saved)

    def gateway(self, api):
        return types.SimpleNamespace(betting_api=api, betfair_login=Mock(return_value='test'),
                                     load_secrets=Mock(),market_summary=lambda row:row)

    def test_melbourne_calendar_day_and_dst(self):
        for stamp,hours in [('2026-10-04T02:00:00+00:00',23),('2026-04-05T02:00:00+00:00',25)]:
            day,start,end = card.day_bounds(datetime.fromisoformat(stamp))
            self.assertEqual((end-start).total_seconds(),hours*3600)
            self.assertEqual(start.astimezone(card.ZONE).hour,0)
        day,_,_ = card.day_bounds(datetime(2026,9,10,15,tzinfo=timezone.utc))
        self.assertEqual(day,'2026-09-11')

    def test_discovery_filters_and_day_boundary(self):
        next_day = dict(self.race,market_id='later',market_start_time='2026-09-11T14:00:00Z')
        api = Mock(side_effect=[[{'event':{'id':'99'}}],[self.race,next_day,dict(self.race,country_code='NZ')]])
        with patch.dict(sys.modules,betfair_gateway=self.gateway(api)):
            rows = card.discover(api,'test',self.now)
        self.assertEqual([r['market_id'] for r in rows],['1.99'])
        filters = api.call_args_list[0].args[1]['filter']
        self.assertEqual(filters['marketCountries'],['AU'])
        self.assertEqual(filters['raceTypes'],['Flat','Steeple','Hurdle'])
        self.assertEqual(filters['marketTypeCodes'],['WIN'])
        self.assertEqual(filters['marketStartTime']['from'],'2026-09-10T14:00:00Z')

    def test_completed_retained_after_catalogue_omits_it(self):
        closed = dict(self.book,status='CLOSED',runners=[{'selectionId':7,'status':'WINNER'}])
        self.saved = card.merge_card(self.saved,[],[closed],self.now+timedelta(hours=2),discovered=True)
        self.write()
        data=card.summary(self.state,now=self.now+timedelta(hours=2),view='completed')
        self.assertEqual(data['races'][0]['race_state'],'completed')
        self.assertEqual(data['races'][0]['winners'][0]['runner_name'],'Winner Name')
        self.assertFalse(data['races'][0]['decision_allowed'])

    def test_elapsed_is_not_completed_and_old_status_stays_stale(self):
        later=self.now+timedelta(hours=2)
        self.saved=card.merge_card(self.saved,[],[],later,discovered=True)
        self.write()
        data=card.summary(self.state,now=later)
        self.assertFalse(data['status_fresh'])
        self.assertEqual(data['races'][0]['race_state'],'unconfirmed')
        self.assertEqual(data['races'][0]['status_updated_at'],card.iso(self.now))
        self.assertEqual(card.summary(self.state,now=later,view='completed')['races'],[])

    def test_retry_omitted_book_to_confirm_closure(self):
        closed=dict(self.book,status='CLOSED',runners=[{'selectionId':7,'status':'WINNER'}])
        api=Mock(side_effect=[[],[closed]])
        with patch.dict(sys.modules,betfair_gateway=self.gateway(api)):
            result=card.refresh(self.state,self.state/'unused',now=self.now+timedelta(seconds=60))
        self.assertEqual(result['races'][0]['market_status'],'CLOSED')
        self.assertEqual(result['discovery_updated_at'],card.iso(self.now))
        self.assertEqual(api.call_count,2)

    def test_refresh_failure_retains_evidence_and_archives_rollover(self):
        api=Mock(side_effect=RuntimeError('private gateway details'))
        with patch.dict(sys.modules,betfair_gateway=self.gateway(api)):
            result=card.refresh(self.state,self.state/'unused',now=self.now+timedelta(seconds=400))
        self.assertEqual(result['races'][0]['status_updated_at'],card.iso(self.now))
        self.assertIn('failed',result['discovery_error'])
        self.assertNotIn('private gateway',json.dumps(result))
        api=Mock(side_effect=[[]])
        with patch.dict(sys.modules,betfair_gateway=self.gateway(api)):
            result=card.refresh(self.state,self.state/'unused',now=self.now+timedelta(days=1))
        self.assertEqual(result['date'],'2026-09-12')
        self.assertEqual(result['races'],[])
        archive=json.loads((self.state/'today_cards/2026-09-11.json').read_text())
        self.assertEqual(archive['races'][0]['market_id'],'1.99')

    def test_shared_decision_outside_queue_preserves_snapshot_and_freshness(self):
        before=(self.state/card.FILE).read_bytes()
        snapshot=card.decision_race(self.state,'1.99',self.now)
        with patch.object(queue,'utc_now',return_value=self.now):
            result=queue.update_decision(self.state,'1.99','remove','pass',race_snapshot=snapshot)
        self.assertEqual(result['races'],[])
        event=json.loads((self.state/queue.DECISION_LOG_FILE_NAME).read_text())
        self.assertEqual(event['race_snapshot']['track'],'Example')
        self.assertEqual(card.summary(self.state,now=self.now)['races'][0]['selection_status'],'removed')
        self.assertEqual((self.state/card.FILE).read_bytes(),before)
        with patch.object(queue,'utc_now',return_value=self.now):
            queue.update_decision(self.state,'1.99',None,None,race_snapshot=snapshot)
        self.assertIsNone(card.summary(self.state,now=self.now)['races'][0]['human_action'])
        with self.assertRaises(ValueError):card.decision_race(self.state,'1.99',self.now+timedelta(minutes=4))
        with self.assertRaises(ValueError):card.decision_race(self.state,'unknown',self.now)

    def test_filters_readonly_endpoint_and_panel_escape(self):
        self.saved['races'][0]['track']='<script>bad</script>'
        self.write()
        self.assertEqual(card.summary(self.state,now=self.now,meeting='absent')['races'],[])
        self.assertEqual(card.summary(self.state,now=self.now,view='review')['races'],[])
        markup=card.panel(card.summary(self.state,now=self.now))
        self.assertNotIn('<script>',markup)
        self.assertIn('&lt;script&gt;',markup)
        self.assertNotIn(' open>',markup)
        before=(self.state/card.FILE).read_bytes()
        handler=object.__new__(wall.WallHandler)
        handler.base_dir=self.state.parent;handler.path='/api/today-card?meeting=99';handler.send_text=Mock()
        with patch.object(wall, 'today_card_summary', side_effect=lambda state_dir, **kw: card.summary(state_dir, now=self.now, **kw)):
            handler.do_GET()
        self.assertEqual(handler.send_text.call_args.kwargs['status'],200)
        self.assertEqual(json.loads(handler.send_text.call_args.args[0])['schema'],'tb_today_card_summary/v1')
        self.assertEqual((self.state/card.FILE).read_bytes(),before)
        (self.state/card.FILE).unlink();handler.do_GET()
        self.assertEqual(handler.send_text.call_args.kwargs['status'],503)


class CardHTTPTests(unittest.TestCase):
    def test_card_decision_authentication_and_shared_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp);now=datetime.now(timezone.utc)
            race={'market_id':'1.card','track':'Example','country_code':'AU','market_name':'R1 Hcap',
                  'market_start_time':(now+timedelta(minutes=10)).isoformat(),'meeting_id':'event'}
            saved=card.merge_card({},[race],[{'marketId':'1.card','status':'OPEN','inplay':False}],now,discovered=True)
            queue.atomic_write_json(state/card.FILE,saved)
            queue.atomic_write_json(state/queue.QUEUE_FILE_NAME,{'decisions':{},'races':[]})
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(state))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                def request(method,path,data=None,token=None):
                    c=http.client.HTTPConnection(*server.server_address,timeout=3)
                    headers={'Content-Type':'application/json'}
                    if token:headers['X-Queue-Token']=token
                    c.request(method,path,json.dumps(data) if data else None,headers)
                    r=c.getresponse();result=r.status,r.read().decode();c.close();return result
                status,page=request('GET','/today')
                self.assertEqual(status,200)
                token=re.search(r"'X-Queue-Token':'([^']+)'",page).group(1)
                self.assertEqual(request('POST','/api/card-decision',{'market_id':'1.card','action':'remove'})[0],403)
                for action in ['remove','keep','restore']:
                    status,body=request('POST','/api/card-decision',{'market_id':'1.card','action':action},token)
                    self.assertEqual(status,200,body)
                    self.assertEqual(json.loads(body)['races'][0]['human_action'],None if action=='restore' else action)
                status,_=request('POST','/api/card-decision',{'market_id':'unknown','action':'remove'},token)
                self.assertEqual(status,400)
                self.assertEqual(len((state/queue.DECISION_LOG_FILE_NAME).read_text().splitlines()),3)
                self.assertEqual(json.loads((state/queue.QUEUE_FILE_NAME).read_text())['races'],[])
            finally:
                server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
