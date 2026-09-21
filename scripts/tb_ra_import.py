"""Manual acceptance-page import; parses HTML as data and never fetches URLs."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlencode
from tb_ra_ratings import parse, norm, ALIASES, ROOT, STATES, FILE, match_runners
from tb_queue_summary import read_state, parse_time
from tb_race_queue import atomic_write_json


def identify(text):
    if 'Are you human?' in text or '/__zenedge/' in text or 'captcha' in text.lower():
        raise ValueError('Verification page: save the actual Acceptances page after completing verification')
    page=parse(text); identities=set();stages=set()
    for row in page.rows:
        link=row.get('horse_url')
        if not link:continue
        query=parse_qs(urlsplit(link).query)
        stage=query.get('stage',[''])[0];stages.add(stage.casefold())
        key=query.get('Key',[''])[0].split(',')
        if len(key)==3 and stage.casefold() in ('acceptances','finalfields'):identities.add(tuple(key))
    if not stages or not stages.issubset({'acceptances','finalfields'}) or len(identities)!=1:
        raise ValueError('Expected one complete Acceptances meeting; weights/form pages are not accepted')
    stamp,region,track=next(iter(identities))
    if region not in STATES:raise ValueError('Invalid state')
    day=datetime.strptime(stamp,'%Y%b%d').date().isoformat()
    if not page.rows:raise ValueError('No Hcp Rating rows found')
    return day,region,track,page.rows,ROOT+'/FreeFields/Acceptances.aspx?'+urlencode({'Key':','.join((stamp,region,track))})


def meeting_name(track):
    name=str(track or "").lower()
    name={"bet365 hamilton":"hamilton", "sportsbet bowen":"bowen", "picklebet park wodonga":"wodonga", "aquis beaudesert":"beaudesert", "canterbury park":"canterbury", "caulfield heath":"caulfield"}.get(name,name)
    return norm(ALIASES.get(name,name))


def load_saved(state):
    path=state/FILE;saved=read_state(path)
    if path.exists() and (saved is None or not isinstance(saved.get('days'),dict)):
        raise ValueError('Saved ratings are invalid; repair them before importing')
    return saved or {'schema':'tb_ra_ratings/v1','days':{}}


def prepare(state, files):
    if not isinstance(files,list) or not 1<=len(files)<=20:raise ValueError('Select 1–20 HTML files')
    saved=load_saved(state)
    jobs=(read_state(state/'tb_au_observations.json') or {}).get('races',{})
    now=datetime.now(timezone.utc);reports=[];proposals={};sources={};seen=set();conflicts=set()
    for upload in files:
        if not isinstance(upload,dict) or not isinstance(upload.get('html'),str):raise ValueError('Expected HTML file contents')
        text=upload['html'];name=str(upload.get('name','page.html'))[:200]
        if len(text.encode())>2_000_000:raise ValueError('Each HTML file must be under 2 MB')
        digest=hashlib.sha256(text.encode()).hexdigest();report={'file':name,'new':0,'existing':0,'unmatched':0,'ambiguous':0,'missing_rating':0};reports.append(report)
        if digest in seen:report['error']='Duplicate file in this batch';continue
        seen.add(digest)
        try:day,region,track,rows,url=identify(text)
        except ValueError as exc:report['error']=str(exc);continue
        candidates=[(mid,job) for mid,job in jobs.items() if job.get('date')==day and job['market'].get('country_code')=='AU' and meeting_name(job['market'].get('track'))==meeting_name(track)]
        if not candidates:report['error']='No matching meeting/date in the Australian observation registry';continue
        report.update(date=day,track=track,state=region,source_url=url)
        sources[digest]={'html':text,'filename':name,'source_url':url}
        for mid,job in candidates:
            for sid,row in match_runners(job['market'],rows).items():
                if row.get('match_status') in ('unmatched','ambiguous'):
                    report[row['match_status']]+=1;continue
                if row.get('rating') is None:report['missing_rating']+=1;continue
                existing=saved['days'].get(day,{}).get('races',{}).get(mid,{}).get(sid,{})
                if existing.get('rating') is not None:report['existing']+=1;continue
                key=(day,mid,sid)
                record={**row,'source':'Racing Australia manual acceptance import','source_url':url,'jurisdiction':region,
                        'meeting_date':day,'collected_at':now.isoformat(),'imported_at':now.isoformat(),'track':job['market']['track'],
                        'match_status':'matched','pre_race':now<parse_time(job['market']['market_start_time']),
                        'file_sha256':digest,'import_filename':name}
                if key in proposals and proposals[key]['rating']!=record['rating']:conflicts.add(key)
                proposals[key]=record;report['new']+=1
    for key in conflicts:proposals.pop(key,None)
    return {'reports':reports,'new_ratings':len(proposals),'conflicting_runners':len(conflicts)},proposals,sources


class Importer:
    def __init__(self,state):self.state=Path(state);self.pending={};self.lock=threading.Lock()
    def preview(self,files):
        report,records,sources=prepare(self.state,files)
        with self.lock:
            self.pending={k:v for k,v in self.pending.items() if v[0]>time.monotonic()-900}
            if len(self.pending)>=5:raise ValueError('Too many pending previews; reload later')
            token=secrets.token_urlsafe(24);self.pending[token]=(time.monotonic(),records,sources)
        return {**report,'preview_id':token}
    def commit(self,token):
        with self.lock:
            data=self.pending.pop(token,None)
        if not data or data[0]<time.monotonic()-900:raise ValueError('Preview expired or already imported; preview again')
        _,records,sources=data;state=self.state;now=datetime.now(timezone.utc);count=0;existing=0
        with (state/'.tb_ra_ratings.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            saved=load_saved(state)
            for digest,source in sources.items():
                directory=state.parent/'imports/racing_australia';directory.mkdir(parents=True,exist_ok=True)
                target=directory/(digest+'.html')
                if not target.exists():target.write_text(source['html'])
            for (day,mid,sid),record in records.items():
                daily=saved['days'].setdefault(day,{'races':{},'sources':{},'errors':[]})
                rows=daily['races'].setdefault(mid,{})
                if rows.get(sid,{}).get('rating') is not None:existing+=1;continue
                rows[sid]={**record,'imported_at':now.isoformat()};count+=1
            saved['updated_at']=now.isoformat();atomic_write_json(state/FILE,saved)
            for day in {k[0] for k in records}:atomic_write_json(state/'ra_ratings'/f'{day}.json',saved['days'][day])
        return {'imported':count,'already_present':existing,'message':'Import complete. Existing numeric baselines were preserved.'}
