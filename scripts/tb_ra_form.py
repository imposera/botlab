"""Manual recent-form import; baseline = mean published Rtg in latest 3 rated races."""
import argparse
from datetime import datetime, timezone, date
from html.parser import HTMLParser
from pathlib import Path
from collections import Counter
from urllib.parse import urlsplit, parse_qs
import hashlib
import fcntl
import json
import re
from tb_ra_import import identify, meeting_name
from tb_ra_ratings import norm
from tb_au_observer import read_strict
from tb_queue_summary import parse_time
from tb_race_queue import atomic_write_json

from tb_racing_fundamentals import extract, merge

FILE='tb_ra_form.json'

class Node:
    def __init__(self,tag='',attrs=()):self.tag=tag;self.attrs=dict(attrs);self.children=[]
    def text(self):return ' '.join(' '.join(x if isinstance(x,str) else x.text() for x in self.children).split())
    def find(self,cls):
        return [n for n in self.walk() if cls in n.attrs.get('class','').split()]
    def walk(self):
        for n in self.children:
            if isinstance(n,Node):yield n;yield from n.walk()

class Tree(HTMLParser):
    def __init__(self):super().__init__();self.root=Node();self.stack=[self.root]
    def handle_starttag(self,t,a):
        n=Node(t,a);n.parent=self.stack[-1];self.stack[-1].children.append(n)
        if t not in ('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'):self.stack.append(n)
    def handle_endtag(self,t):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==t:del self.stack[i:];break
    def handle_data(self,d):self.stack[-1].children.append(d)


def parse_form(text):
    day,region,track,_,url=identify(text)
    tree=Tree();tree.feed(text);race=None;rows=[]
    for n in tree.root.walk():
        m=re.fullmatch(r'Race(\d+)',n.attrs.get('name',''))
        if n.tag=='a' and m:race=int(m[1])
        if n.tag!='table' or 'horse-form-table' not in n.attrs.get('class','').split():continue
        names=n.find('horse-name')
        if len(names)!=1 or not race:continue
        horse_links=[a for a in names[0].walk() if a.tag=='a' and 'HorseFullForm.aspx' in a.attrs.get('href','')]
        horse_code=parse_qs(urlsplit(horse_links[0].attrs['href']).query).get('horsecode',[''])[0] if horse_links else ''
        starts=[];race_starts=[];fundamental_starts=[]
        for cell in n.find('remain'):
            raw=cell.text()
            # Race identity/date come from the meeting link; Rtg only before result text.
            links=[a for a in cell.walk() if a.tag=='a' and '/Meeting.aspx?' in a.attrs.get('href','')]
            if not links:continue
            dm=re.search(r'\b(\d{2}[A-Za-z]{3}\d{2})\b',links[0].text())
            if not dm:continue
            try:run_day=datetime.strptime(dm[1],'%d%b%y').date().isoformat()
            except ValueError:continue
            if run_day>=day:continue
            if re.search(r'OPEN-BT|\bBT\b|trial|jump\s*out',raw,re.I):continue
            row=cell.parent
            while row.tag!='tr' and hasattr(row,'parent'):row=row.parent
            positions=row.find('Pos');pos=positions[0].text() if positions else ''
            if re.match(r'^[TJ]\s*\d',pos,re.I):continue
            pm=re.match(r'^(\d+)\s*(?:=|DH)?\s+of\s+\d+',pos,re.I)
            distance=re.search(r'\b(\d{3,4})m\b',raw)
            race_starts.append({'date':run_day,'venue':links[0].text(),'source_url':links[0].attrs['href'],
                                'position':int(pm[1]) if pm else None,'position_raw':pos,
                                'distance_m':int(distance[1]) if distance else None})
            fundamental_starts.append({**extract(raw,links[0].text(),pos),'date':run_day,'source_url':links[0].attrs['href']})
            rt=re.search(r'\bRtg\s+(\d+(?:\.\d+)?)\b',raw.split('1st ')[0].split('2nd ')[0])
            if not rt:continue
            starts.append({'date':run_day,'rating':float(rt[1]),'venue':links[0].text(),'source_url':links[0].attrs['href'],'raw':raw})
        unique={s['source_url']:s for s in starts}
        recent=sorted(unique.values(),key=lambda s:s['date'],reverse=True)[:3]
        last_races=sorted({r['source_url']:r for r in race_starts}.values(),key=lambda r:r['date'],reverse=True)[:3]
        rows.append({'fundamental_starts':fundamental_starts,'recent_races':last_races,'recent_win':any(r['position']==1 for r in last_races),'race':race,'horse':names[0].text(),'baseline':round(sum(s['rating'] for s in recent)/len(recent),1) if recent else None,'starts':recent,'all_starts':list(unique.values()),'horse_code':horse_code})
    if not rows:raise ValueError('No recent-form runner sections found')
    return day,track,rows


def prepare(base,path):
    text=path.read_text();day,track,rows=parse_form(text)
    jobs=(read_strict(base/'state/tb_au_observations.json') or {}).get('races',{})
    candidates={}
    for mid,j in jobs.items():
        m=j['market'];rn=re.match(r'R(\d+)\b',m.get('market_name',''))
        if j['date']!=day or m.get('country_code')!='AU' or meeting_name(m.get('track'))!=meeting_name(track) or not rn:continue
        for r in m.get('runners',[]):candidates.setdefault((int(rn[1]),norm(r['runner_name'])),[]).append((mid,str(r['selection_id']),m))
    if not candidates:raise ValueError('No matching Australian meeting/date')
    counts=Counter((r['race'],norm(r['horse'])) for r in rows)
    report={'date':day,'track':track,'rated':0,'no_rated_history':0,'unmatched':[],'ambiguous':[]};records=[]
    now=datetime.now(timezone.utc);digest=hashlib.sha256(text.encode()).hexdigest()
    for r in rows:
        key=(r['race'],norm(r['horse']));matches=candidates.get(key,[])
        if not matches:report['unmatched'].append(r['horse']);continue
        if len(matches)!=1 or counts[key]!=1:report['ambiguous'].append(r['horse']);continue
        mid,sid,m=matches[0]
        report['rated' if r['baseline'] is not None else 'no_rated_history']+=1
        records.append((mid,sid,{**r,'race_context':{'country':'AU','track':m.get('track'),'date':day,'race_number':r['race'],'name':m.get('market_name'),'scheduled_start':m.get('market_start_time')},'method':'mean_latest_3_rated_races/v1','meeting_date':day,'imported_at':now.isoformat(),'pre_race':now<parse_time(m['market_start_time']),'file_sha256':digest,'filename':path.name}))
    return report,records,text,digest


def update_runner(database, mid, sid, record):
    """Update stable horse history without changing frozen per-race imports."""
    key='ra:'+record['horse_code'] if record.get('horse_code') else 'betfair:'+sid
    horse=database.setdefault(key,{'horse':record['horse'],'horse_code':record.get('horse_code'),
                                  'selection_ids':[],'starts':{},'imports':{},'revisions':[]})
    identity=record['file_sha256']+':'+mid+':'+sid
    if identity in horse['imports']:
        horse['imports'][identity].setdefault('recent_races',record.get('recent_races',[]))
        return 0
    horse['selection_ids']=sorted(set(horse['selection_ids']+[sid]))
    stamp=(record['meeting_date'],record['imported_at'])
    for start in record.get('all_starts',record.get('starts',[])):
        race_code=parse_qs(urlsplit(start['source_url']).query).get('racecode',[''])[0]
        start_key=race_code or start['source_url']
        previous=horse['starts'].get(start_key)
        if previous and tuple(previous['source_order'])>stamp:continue
        if previous and previous['rating']!=start['rating']:
            horse['revisions'].append({'start_id':start_key,'previous':previous,'replaced_by_import':identity})
        horse['starts'][start_key]={**start,'source_order':list(stamp),'file_sha256':record['file_sha256']}
    horse['imports'][identity]={k:record.get(k) for k in ('meeting_date','imported_at','filename','file_sha256','pre_race','recent_races')}
    latest=sorted(horse['starts'].values(),key=lambda x:(x['date'],x['source_url']),reverse=True)[:3]
    horse.update(baseline=round(sum(x['rating'] for x in latest)/len(latest),1) if latest else None,
                 baseline_starts=latest,method=record['method'],rated_start_count=len(horse['starts']),
                 updated_at=record['imported_at'])
    return 1


def run(base,path,commit=False):
    report,records,text,digest=prepare(base,path)
    if commit:return commit_prepared(base,report,records,text,digest)
    return report


def commit_prepared(base,report,records,text,digest):
    with (base/'state/.tb_ra_form.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        target=base/'state'/FILE
        state=read_strict(target) or {'schema':'tb_ra_form/v1','days':{}}
        if state.get('schema')!='tb_ra_form/v1' or not isinstance(state.get('days'),dict):raise ValueError('Invalid form state')
        database=state.setdefault('runners',{})
        database_updates=0
        imported=0
        fundamental_updates=0
        for mid,sid,r in records:
            historical=state['days'].get(report['date'],{}).get(mid,{}).get(sid,{})
            evidence=dict(r)
            if historical.get('file_sha256')==digest:
                evidence.update(imported_at=historical['imported_at'],pre_race=historical['pre_race'])
            fundamental_updates+=merge(state,mid,sid,evidence)
            database_updates+=update_runner(database,mid,sid,r)
            dest=state['days'].setdefault(report['date'],{}).setdefault(mid,{})
            if sid in dest:
                if dest[sid].get('file_sha256')==digest and 'recent_races' not in dest[sid]:
                    dest[sid].update(recent_races=r.get('recent_races',[]),recent_win=r.get('recent_win',False))
                continue
            dest[sid]=r;imported+=1
        archive=base/'imports/racing_australia';archive.mkdir(parents=True,exist_ok=True)
        (archive/(digest+'.html')).write_text(text)
        state['updated_at']=datetime.now(timezone.utc).isoformat();atomic_write_json(target,state)
        report['imported_runners']=imported
        report['database_updates']=database_updates
        report['database_runners']=len(database)
        report['fundamental_updates']=fundamental_updates
        report['historical_starts']=len(state.get('fundamentals',{}).get('starts',{}))
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('file',type=Path,nargs='?');p.add_argument('--database',action='store_true');p.add_argument('--base-dir',type=Path,default=Path.home()/'botlab/totebot');p.add_argument('--commit',action='store_true');a=p.parse_args()
    if a.database:
        state=read_strict(a.base_dir/'state'/FILE) or {}
        print(json.dumps([{ 'runner_id':k, **{f:v.get(f) for f in ('horse','baseline','rated_start_count','updated_at')} } for k,v in state.get('runners',{}).items()],indent=2))
    elif a.file:
        print(json.dumps(run(a.base_dir,a.file,a.commit),indent=2))
    else:p.error('Supply a saved recent-form page or --database')
