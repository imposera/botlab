import json
from pathlib import Path
import tempfile
import unittest
from tb_au_capture_wall import response, render

class CaptureWallTests(unittest.TestCase):
    def test_saved_and_missing_snapshots_and_validation(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);(base/'state').mkdir()
            market={'track':'Geelong <test>','market_name':'R1','runners':[{'selection_id':1,'runner_name':'Horse & Co'}]}
            (base/'state/tb_au_observations.json').write_text(json.dumps({'races':{'1.2':{'date':'2026-09-11','market':market,'slots':{'T15':{'status':'captured'}}}}}))
            p=base/'observations/au/2026-09-11/1.2';p.mkdir(parents=True)
            (p/'T15.json').write_text(json.dumps({'schema':'tb_au_snapshot/v1','market_id':'1.2','slot':'T15','book':{'isMarketDataDelayed':True,'runners':[{'selectionId':1,'lastPriceTraded':4.2}]}}))
            q={'date':['2026-09-11'],'market':['1.2'],'slot':['T15']}
            page=render(base,q)
            for term in ('Horse &amp; Co','4.2','Delayed'):self.assertIn(term,page)
            self.assertNotIn('Geelong <test>',page)
            self.assertEqual(response(base,'/api/au-capture',q)[2],200)
            q['slot']=['T10'];self.assertEqual(response(base,'/api/au-capture',q)[2],404)
            q['market']=['../../state'];self.assertEqual(response(base,'/api/au-capture',q)[2],400)
    def test_empty(self):
        with tempfile.TemporaryDirectory() as root:self.assertIn('No registered Australian races',render(Path(root),{}))

class LiquidityTests(unittest.TestCase):
    def test_missing_zero_depth_and_volume(self):
        from tb_au_capture_wall import liquidity
        runner={'ex':{'availableToBack':[{'price':3,'size':40},{'price':4,'size':0}], 'availableToLay':[{'price':5,'size':20}], 'tradedVolume':[{'price':4,'size':50},{'price':3,'size':25}]}}
        self.assertIsNone(liquidity(runner,{})['traded'])
        values=liquidity(runner,{'price_data':['EX_TRADED']})
        self.assertEqual(values['traded'],75)
        self.assertEqual(values['back_size'],0)
        self.assertEqual(values['spread'],1)
        self.assertEqual(liquidity({'ex':{'tradedVolume':[]}}, {'price_data':['EX_TRADED']})['traded'],0)

class NavigationTests(unittest.TestCase):
    def test_meeting_race_navigation_and_direct_links(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);(base/'state').mkdir()
            jobs={}
            for mid,track,race in [('1.1','Flemington','R1'),('1.2','Flemington','R2'),('1.3','Rosehill','R1')]:
                jobs[mid]={'date':'2026-09-12','slots':{},'market':{'track':track,'market_name':race,'runners':[]}}
            (base/'state/tb_au_observations.json').write_text(json.dumps({'races':jobs}))
            page=render(base,{'date':['2026-09-12'],'meeting':['Flemington']})
            nav=page.split('<nav class="race-nav"')[1].split('</nav>')[0]
            self.assertIn('market=1.1',nav);self.assertIn('market=1.2',nav);self.assertNotIn('market=1.3',nav)
            self.assertIn('Choose a race above',page)
            direct=render(base,{'date':['2026-09-12'],'market':['1.2']})
            self.assertIn('Runner price comparison',direct)
            self.assertIn('meeting=Flemington',direct)
            mismatch=render(base,{'date':['2026-09-12'],'meeting':['Rosehill'],'market':['1.2']})
            self.assertIn('Race unavailable',mismatch)

class MoveShapeTests(unittest.TestCase):
    def test_move_shape_missing_and_scratching(self):
        from tb_au_capture_wall import move_and_shape
        result=move_and_shape({'T15':10,'T10':8,'T5':6,'T2':5,'T30':4})
        self.assertIn('-60.0%',result);self.assertEqual(result.count('early-firm'),3)
        self.assertIn('+20.0%',move_and_shape({'T15':10,'T30':12}))
        self.assertIn('????',move_and_shape({'T15':10,'T30':12}))
        self.assertNotIn('%',move_and_shape({'T15':10}))
        broken=move_and_shape({'T15':10,'T10':5},True)
        self.assertNotIn('-50.0%',broken);self.assertNotIn('early-firm',broken)
