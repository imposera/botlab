"""Versioned race/start fundamentals extracted only from manually saved form pages."""
import re
from urllib.parse import urlsplit,parse_qs

VERSION=1

def extract(raw,venue,position_raw):
    distance=re.search(r'\b(\d{3,4})m\b',raw)
    date_match=re.search(r'\b\d{2}[A-Za-z]{3}\d{2}\b',venue)
    track=venue[:date_match.start()].strip() if date_match else venue
    tail=raw[distance.end():] if distance else ''
    going=re.match(r'\s*(Firm|Good|Soft|Heavy|Synthetic)\s*(\d+)?\b',tail,re.I)
    class_text=None
    if going:
        class_part=tail[going.end():].split('$',1)
        if len(class_part)==2:class_text=class_part[0].strip() or None
    pos=re.match(r'^(\d+)\s*(?:=|DH)?\s+of\s+(\d+)',position_raw,re.I)
    weight=re.search(r'\b(\d+(?:\.\d+)?)kg\b',raw)
    carried=re.search(r'\(cd\s+(\d+(?:\.\d+)?)kg\)',raw)
    barrier=re.search(r'\bBarrier\s+(\d+)\b',raw)
    rating=re.search(r'\bRtg\s+(\d+(?:\.\d+)?)\b',raw)
    margin=re.search(r'\b(\d+(?:\.\d+)?)L\b',raw)
    # Country of a past start is not established by an Australian form page.
    return {'parser_version':VERSION,'country':None,'country_status':'not supplied',
            'track_code':track,'distance_m':int(distance[1]) if distance else None,
            'class_raw':class_text,'going':going[1].title() if going else None,
            'going_rating':int(going[2]) if going and going[2] else None,
            'surface':'Synthetic' if going and going[1].lower()=='synthetic' else None,
            'position':int(pos[1]) if pos else None,'field_size':int(pos[2]) if pos else None,
            'position_raw':position_raw,'weight_kg':float(weight[1]) if weight else None,
            'carried_weight_kg':float(carried[1]) if carried else None,
            'barrier':int(barrier[1]) if barrier else None,
            'handicap_rating':float(rating[1]) if rating else None,
            'margin_lengths':float(margin[1]) if margin else None,
            'raw':raw}


def merge(state,mid,sid,record):
    db=state.setdefault('fundamentals',{'schema':'tb_racing_fundamentals/v1','horses':{},'races':{},'starts':{},'imports':{},'revisions':[]})
    key='ra:'+record['horse_code'] if record.get('horse_code') else 'betfair:'+sid
    identity=record['file_sha256']+':'+mid+':'+sid
    if db['imports'].get(identity,{}).get('parser_version')==VERSION:return 0
    horse=db['horses'].setdefault(key,{'name':record['horse'],'selection_ids':[]})
    horse['selection_ids']=sorted(set(horse['selection_ids']+[sid]))
    source={k:record.get(k) for k in ('file_sha256','filename','meeting_date','imported_at','pre_race')}
    source.update(parser_version=VERSION,market_id=mid,selection_id=sid)
    db['imports'][identity]=source
    current=record.get('race_context',{})
    db['races'].setdefault('betfair:'+mid,{**current,'source_import':identity})
    for start in record.get('fundamental_starts',[]):
        racecode=parse_qs(urlsplit(start['source_url']).query).get('racecode',[''])[0]
        racekey='ra:'+(racecode or start['source_url'])
        startkey=key+'|'+racekey
        value={**start,'horse_id':key,'race_id':racekey,'source_import':identity}
        previous=db['starts'].get(startkey)
        if previous:
            prior=db['imports'][previous['source_import']]
            if (prior['meeting_date'],prior['imported_at'])>(source['meeting_date'],source['imported_at']):continue
            if previous!=value:db['revisions'].append({'start_id':startkey,'previous':previous})
        db['starts'][startkey]=value
        race={k:start.get(k) for k in ('date','country','country_status','track_code','class_raw','distance_m','going','going_rating','surface')}
        # Keep per-source race assertions; differing runner pages remain auditable.
        entry=db['races'].setdefault(racekey,{'observations':{}})
        entry['observations'][identity]=race
    return 1


def summary(db):
    starts=list(db.get('starts',{}).values())
    return {'schema':db.get('schema'),'horses':len(db.get('horses',{})),
            'races':len(db.get('races',{})),'historical_starts':len(starts),
            'imports':len(db.get('imports',{})),'revisions':len(db.get('revisions',[])),
            'field_coverage':{k:sum(s.get(k) is not None for s in starts) for k in
                              ('country','track_code','class_raw','distance_m','going','weight_kg','position','handicap_rating','margin_lengths')}}

if __name__=='__main__':
    import argparse,json
    from pathlib import Path
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-dir',type=Path,default=Path.home()/'botlab/totebot')
    p.add_argument('--horse',help='Case-insensitive horse name substring')
    args=p.parse_args();state=json.loads((args.base_dir/'state/tb_ra_form.json').read_text());db=state.get('fundamentals',{})
    if args.horse:
        ids={k for k,h in db.get('horses',{}).items() if args.horse.casefold() in h['name'].casefold()}
        result={'horses':{k:db['horses'][k] for k in ids},'starts':[s for s in db.get('starts',{}).values() if s['horse_id'] in ids]}
    else:result=summary(db)
    print(json.dumps(result,indent=2))
