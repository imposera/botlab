import json
import os
from pathlib import Path
import sys
import contextlib
import io
from unittest.mock import patch
import tempfile
import unittest
import tb_review as r

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
    def race(self, date='2026-09-01', mid='1.1', complete=True):
        p=self.base/date/mid
        p.mkdir(parents=True)
        (p/'closed.json').write_text(json.dumps({'complete':complete,'has_result':complete}))
        (p/'result.json').write_text(json.dumps({'result_status':'winner_found' if complete else 'pending','winners':[{'selection_id':1,'runner_name':'Winner'}] if complete else []}))
        return p
    def snapshots(self,p,values):
        for (stage,file), price in zip(r.SNAPSHOT_FILES,values):
            (p/file).write_text(json.dumps({'market':{'market_start_time':p.parent.name+'T10:00:00Z'},'runners':[{'selection_id':1,'runner_name':'Winner','last_price_traded':price}]}))
    def test_basic_winner_and_price_move(self):
        p=self.race();self.snapshots(p,[10,9,8,7,5])
        result=r.build_review(p)
        self.assertTrue(result['winner']['winner'])
        self.assertEqual(result['winner']['move_pct'],-50)
        self.assertEqual(result['winner']['shape_class'],'steady_firm')
    def test_pending_result_not_completed(self):
        p=self.race(complete=False)
        self.assertEqual(r.discover_completed_races(self.base),[])
    def test_latest_ignores_sync_mtime(self):
        old=self.race();new=self.race('2026-09-02','1.2')
        self.snapshots(old,[10,5]);self.snapshots(new,[10,5])
        os.utime(old,(2000,2000));os.utime(new,(1000,1000))
        self.assertEqual(r.discover_completed_races(self.base)[0],new)
    def test_roundtrip_whipsaw_not_flat(self):
        self.assertEqual(r.shape_class({'T-15':10,'T-10':5,'T-5':15,'T-2':5,'T-30':10}),'whipsaw')
    def test_all_firmers_have_no_drifter(self):
        p=self.race();self.snapshots(p,[10,9,8,7,5])
        self.assertIsNone(r.build_review(p)['extremes']['largest_drifter'])
    def test_one_price_movement_unknown(self):
        p=self.race();self.snapshots(p,[10])
        self.assertEqual(r.build_review(p)['winner']['movement_class'],'unknown')
    def test_corrupt_files_not_complete(self):
        p=self.race()
        for _,file in r.SNAPSHOT_FILES:(p/file).write_text('{broken')
        self.assertNotEqual(r.build_review(p)['race']['capture'],'complete')
    def test_winners_array_preserves_identity_without_snapshots(self):
        p=self.race()
        self.assertEqual(r.build_review(p)['winner']['name'],'Winner')

    def test_large_roundtrip_v_shape_and_quiet_hold(self):
        self.assertEqual(r.shape_class({'T-15':10,'T-5':5,'T-30':10}), 'v_shape')
        self.assertEqual(r.shape_class({'T-15':10,'T-5':10.1,'T-30':10}), 'flat_hold')

    def test_all_drifters_have_no_firmer_and_flat_has_neither(self):
        p=self.race();self.snapshots(p,[5,6,7,8,10])
        self.assertIsNone(r.build_review(p)['extremes']['largest_firmer'])
        self.snapshots(p,[10,10,10,10,10])
        review=r.build_review(p)
        self.assertIsNone(review['extremes']['largest_firmer'])
        self.assertIsNone(review['extremes']['largest_drifter'])

    def test_dead_heat_details_without_snapshots(self):
        p=self.race()
        (p/'result.json').write_text(json.dumps({'winners':[
            {'selection_id':1,'runner_name':'First'}, {'selection_id':2,'runner_name':'Second'}],
            'market':{'track':'Test Track'}}))
        review=r.build_review(p)
        self.assertIsNone(review['winner'])
        self.assertEqual({w['name'] for w in review['winners']}, {'First','Second'})
        self.assertEqual(review['race']['track'],'Test Track')

    def test_same_day_sort_normalises_timezones(self):
        early=self.race(mid='1.9');late=self.race(mid='1.10')
        for folder,start in [(early,'2026-09-01T12:00:00+10:00'), (late,'2026-09-01T03:00:00Z')]:
            result=r.read_json(folder/'result.json')
            result['market']={'market_start_time':start}
            (folder/'result.json').write_text(json.dumps(result))
        self.assertEqual(r.discover_completed_races(self.base)[0],late)

    def test_legacy_closed_result_and_invalid_marker(self):
        p=self.race()
        (p/'closed.json').unlink()
        self.assertFalse(r.race_completed(p))
        result=r.read_json(p/'result.json');result['market_status']='CLOSED'
        (p/'result.json').write_text(json.dumps(result))
        self.assertTrue(r.race_completed(p))
        (p/'closed.json').write_text('{broken')
        self.assertFalse(r.race_completed(p))

    def test_unusable_snapshot_and_partial_capture(self):
        p=self.race();self.snapshots(p,[10])
        (p/'market_book_t10.json').write_text('{"runners":null}')
        (p/'market_book_t5.json').write_text('{"runners":[]}')
        review=r.build_review(p)
        self.assertEqual(review['race']['capture'],'partial')
        self.assertEqual(review['race']['stages_present'],['T-15'])

    def test_cli_rejects_pending_and_preserves_json_when_writing(self):
        p=self.race(complete=False)
        # The fixture's base is the history directory; expose it under a base dir.
        base=self.base/'installation';base.mkdir()
        (base/'history').symlink_to(self.base, target_is_directory=True)
        args=['tb_review.py','--base-dir',str(base),'--market-id','1.1','--json','--write']
        with patch.object(sys,'argv',args), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(r.main(),1)
        self.assertFalse((base/'review').exists())
        (p/'closed.json').write_text('{"complete":true,"has_result":true}')
        (p/'result.json').write_text('{"market_status":"CLOSED","winners":[{"selection_id":1,"runner_name":"Winner"}]}')
        self.snapshots(p,[10,5])
        with patch.object(sys,'argv',args), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(r.main(),0)
        parsed=json.loads(output.getvalue())
        self.assertEqual(parsed['version'],r.VERSION)
        self.assertEqual(json.loads(r.review_output_path(base,p).read_text()),parsed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
