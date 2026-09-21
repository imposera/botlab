"""Collect published Racing Australia acceptance handicap ratings independently."""
import argparse
from datetime import datetime, timezone, date
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit, parse_qs
from urllib.request import Request, urlopen
import fcntl
from tb_queue_summary import read_state, parse_time
from tb_race_queue import atomic_write_json

ROOT='https://www.racingaustralia.horse'
STATES=('NSW','VIC','QLD','SA','WA','TAS','NT','ACT')
ALIASES={'rosehill':'rosehill gardens','randwick':'royal randwick','belmont':'belmont park','morphettville parks':'morphettville parks'}
FILE='tb_ra_ratings.json'


def norm(value):
    value=re.sub(r'\s*\([A-Za-z]{2,4}\)\s*$','',str(value or ''))
    return re.sub(r'[^a-z0-9]','',unicodedata.normalize('NFKD',value).encode('ascii','ignore').decode().lower())


class Page(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[];self.rows=[];self.race=None;self.cells=None;self.cell=None;self.text=[];self.horse_link=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='a':
            if a.get('href'):self.links.append(a['href'])
            match=re.fullmatch(r'Race(\d+)',a.get('name',''))
            if match:self.race=int(match[1])
            if self.cell=='horse' and 'Horse' in a.get('href',''):self.horse_link=a['href']
        if tag=='tr':self.cells={};self.horse_link=None
        if tag=='td' and self.cells is not None:self.cell=a.get('class','');self.text=[]
    def handle_data(self,data):
        if self.cell is not None:self.text.append(data)
    def handle_endtag(self,tag):
        if tag=='td' and self.cell is not None:
            self.cells[self.cell]=' '.join(''.join(self.text).split());self.cell=None
        if tag=='tr' and self.cells is not None:
            if self.cells.get('horse') and 'hcp' in self.cells:
                raw=self.cells['hcp'];self.rows.append({'race':self.race,'horse':self.cells['horse'],
                    'rating':float(raw) if re.fullmatch(r'\d+(?:\.\d+)?',raw) else None,'raw_rating':raw,'horse_url':self.horse_link})
            self.cells=None


def parse(text):
    page=Page();page.feed(text);return page


def meeting_links(text, day):
    expected=date.fromisoformat(day).strftime('%Y%b%d')
    result={}
    for link in parse(text).links:
        absolute=urljoin(ROOT+'/FreeFields/',link);parts=urlsplit(absolute)
        if parts.hostname not in ('www.racingaustralia.horse','racingaustralia.horse') or not parts.path.endswith('/Acceptances.aspx'):continue
        key=parse_qs(parts.query).get('Key',[''])[0].split(',')
        if len(key)==3 and key[0]==expected:result[(key[1],norm(key[2]))]=absolute
    return result


def match_runners(market, rows):
    match=re.match(r'R(\d+)\b',market.get('market_name',''))
    if not match:return {}
    race=int(match[1]);result={}
    for runner in market.get('runners',[]):
        candidates=[r for r in rows if r['race']==race and norm(r['horse'])==norm(runner.get('runner_name'))]
        result[str(runner['selection_id'])]=candidates[0] if len(candidates)==1 else {'rating':None,'match_status':'ambiguous' if candidates else 'unmatched'}
    return result


def collect(base, fetch, now=None):
    now=now or datetime.now(timezone.utc);base=Path(base);card=read_state(base/'state/tb_today_card.json')
    if not card or not card.get('date'):raise ValueError('Australian card unavailable')
    day=card['date'];state_path=base/'state'/FILE
    if state_path.exists() and read_state(state_path) is None:raise ValueError('Invalid saved ratings')
    saved=read_state(state_path) or {'schema':'tb_ra_ratings/v1','days':{}}
    daily=saved['days'].setdefault(day,{'races':{},'sources':{},'errors':[]});daily['errors']=[]
    links={}
    for region in STATES:
        try:links.update(meeting_links(fetch(ROOT+'/FreeFields/Calendar.aspx?State='+region),day))
        except Exception:daily['errors'].append(region+': calendar unavailable')
    groups={}
    for market in card['races']:
        if market.get('country_code')=='AU':groups.setdefault(market['track'],[]).append(market)
    for track,markets in groups.items():
        canonical=norm(ALIASES.get(track.lower(),track));options=[(state,url) for (state,name),url in links.items() if name==canonical]
        if len(options)!=1:
            daily['errors'].append(track+': acceptance page unmatched');continue
        region,url=options[0]
        try:
            page=fetch(url);rows=parse(page).rows
            if not rows:raise ValueError('No rating rows')
            daily['sources'][track]={'url':url,'collected_at':now.isoformat(),'rows':len(rows)}
        except Exception:
            daily['errors'].append(track+': ratings unavailable');continue
        for market in markets:
            mid=str(market['market_id']);ratings=daily['races'].setdefault(mid,{})
            for sid,row in match_runners(market,rows).items():
                # Freeze the first available rating. Later updates never rewrite the baseline.
                if ratings.get(sid,{}).get('rating') is not None:continue
                ratings[sid]={**row,'source':'Racing Australia acceptances','source_url':url,'jurisdiction':region,
                    'meeting_date':day,'collected_at':now.isoformat(),'track':track,
                    'match_status':row.get('match_status','matched'),
                    'pre_race':now < parse_time(market['market_start_time'])}
    saved['updated_at']=now.isoformat();atomic_write_json(state_path,saved)
    atomic_write_json(base/'state/ra_ratings'/f'{day}.json',daily)
    return daily


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--base-dir',type=Path,default=Path.home()/'botlab/totebot');args=parser.parse_args()
    base=args.base_dir; (base/'state').mkdir(parents=True,exist_ok=True)
    if (read_state(base/'state'/FILE) or {}).get('access_status') == 'verification_required':
        raise SystemExit('Racing Australia requires human verification; automatic collection is paused')
    blocked=False
    def fetch(url):
        nonlocal blocked
        if blocked:raise RuntimeError('Website verification required')
        time.sleep(5)
        with urlopen(Request(url,headers={'User-Agent':'ToteBot rating collector/1.0'}),timeout=25) as response:
            text=response.read().decode('utf-8-sig',errors='replace')
            if 'Are you human?' in text or '/__zenedge/' in text:
                blocked=True
                raise RuntimeError('Website verification required')
            return text
    with (base/'state/.tb_ra_ratings.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=collect(base,fetch)
        if blocked:
            saved=read_state(base/'state'/FILE)
            saved['access_status']='verification_required'
            atomic_write_json(base/'state'/FILE,saved)
        print(json.dumps({'matched_ratings':sum(r.get('rating') is not None for rows in result['races'].values() for r in rows.values()),'errors':result['errors']}))

if __name__=='__main__':main()
