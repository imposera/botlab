import unittest
from tb_ra_ratings import parse,match_runners,meeting_links
class RatingTests(unittest.TestCase):
 def test_matching_and_blank(self):
  page='<a name="Race1"></a><tr><td class="horse">HORSE (NZ)</td><td class="hcp">75</td></tr><tr><td class="horse">OTHER</td><td class="hcp"></td></tr>'
  rows=parse(page).rows
  market={'market_name':'R1 1200m','runners':[{'selection_id':1,'runner_name':'Horse'},{'selection_id':2,'runner_name':'Other'}]}
  result=match_runners(market,rows)
  self.assertEqual(result['1']['rating'],75);self.assertIsNone(result['2']['rating'])
  self.assertEqual(match_runners(market,rows+rows)['1']['match_status'],'ambiguous')
  market['market_name']='R2';self.assertEqual(match_runners(market,rows)['1']['match_status'],'unmatched')
 def test_links_date(self):
  p='<a href="Acceptances.aspx?Key=2026Sep12,NSW,Rosehill+Gardens">View</a>'
  self.assertEqual(len(meeting_links(p,'2026-09-12')),1)
  self.assertEqual(meeting_links(p,'2026-09-13'),{})

class CollectionTests(unittest.TestCase):
 def test_freeze_and_failed_refresh_preserve_baseline(self):
  from tempfile import TemporaryDirectory
  from pathlib import Path
  from datetime import datetime,timezone
  import json
  from tb_ra_ratings import collect
  with TemporaryDirectory() as root:
   base=Path(root);(base/'state').mkdir()
   card={'date':'2026-09-12','races':[{'market_id':'1.1','track':'Rosehill','country_code':'AU','market_name':'R1 1200m','market_start_time':'2026-09-12T05:00:00Z','runners':[{'selection_id':1,'runner_name':'Horse'}]}]}
   (base/'state/tb_today_card.json').write_text(json.dumps(card))
   rating=[75]
   def fetch(url):
    if 'Calendar' in url:return '<a href="Acceptances.aspx?Key=2026Sep12,NSW,Rosehill+Gardens">View</a>'
    return f'<a name="Race1"></a><tr><td class="horse">HORSE</td><td class="hcp">{rating[0]}</td></tr>'
   now=datetime(2026,9,12,tzinfo=timezone.utc)
   self.assertTrue(collect(base,fetch,now)['races']['1.1']['1']['pre_race'])
   rating[0]=90
   self.assertEqual(collect(base,fetch,now)['races']['1.1']['1']['rating'],75)
   def failure(url):raise OSError('unavailable')
   self.assertEqual(collect(base,failure,now)['races']['1.1']['1']['rating'],75)
