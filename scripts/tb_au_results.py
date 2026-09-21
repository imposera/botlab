#!/usr/bin/env python3
"""Persist Betfair win-market settlement independently of snapshot collection."""
from datetime import timedelta
from pathlib import Path
import fcntl
import os
import time
from tb_au_observer import read_strict, utc_now
from tb_queue_summary import parse_time
from tb_race_queue import atomic_write_json

FILE = 'tb_au_results.json'


def tick(base, fetch, now=None):
    base = Path(base); now = now or utc_now()
    registry = read_strict(base/'state/tb_au_observations.json') or {}
    path = base/'state'/FILE
    state = read_strict(path) or {'schema':'tb_au_results/v1','races':{}}
    if state.get('schema') != 'tb_au_results/v1' or not isinstance(state.get('races'),dict):
        raise ValueError('Invalid result state')
    due=[]
    for mid, job in registry.get('races',{}).items():
        start=parse_time(job['market'].get('market_start_time'))
        if not start or now < start+timedelta(minutes=2): continue
        result=state['races'].setdefault(mid,{'status':'pending','date':job['date']})
        if now > start+timedelta(days=7):
            if result['status'] != 'settled': result['status']='unavailable'
            continue
        if result['status']=='settled' and now > start+timedelta(days=1): continue
        next_check=parse_time(result.get('next_check'))
        if not next_check or next_check<=now: due.append((result.get('checked_at',''),mid))
    for _,mid in sorted(due)[:20]:
        result=state['races'][mid]
        result.update(checked_at=now.isoformat(),next_check=(now+timedelta(minutes=5)).isoformat())
        try:
            # Never mix open and closed markets: Betfair can omit closed books.
            books=fetch([mid])
            book=next((b for b in books if str(b.get('marketId'))==mid),None)
            if not book: raise ValueError('Market omitted')
            result.pop('error',None)
            result['market_status']=book.get('status')
            if book.get('status')=='CLOSED' and book.get('runners'):
                result.update(status='settled',captured_at=now.isoformat(),book=book,
                              next_check=(now+timedelta(hours=1)).isoformat())
            elif result['status']=='settled':
                result['status']='pending';result.pop('book',None)
        except Exception:
            result['error']='Result request unavailable; retry scheduled'
        state['updated_at']=now.isoformat()
        atomic_write_json(path,state)
    state['updated_at']=now.isoformat();atomic_write_json(path,state)
    return state


class Gateway:
    def __init__(self): self.token=None;self.last=0
    def __call__(self,ids):
        from betfair_gateway import betfair_login, betting_api
        time.sleep(max(0,2-(time.monotonic()-self.last)));self.last=time.monotonic()
        try:
            if self.token is None:self.token=betfair_login()
            return betting_api('listMarketBook',{'marketIds':ids},self.token)
        except Exception:
            self.token=None;raise


def main():
    from betfair_gateway import load_secrets
    load_secrets(Path(os.environ.get('BETFAIR_SECRETS_FILE','/opt/betfair/secrets.env')))
    base=Path.home()/'botlab/totebot';gateway=Gateway()
    with (base/'state/.tb_au_results.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            try:
                state=tick(base,gateway)
                print('AU_RESULTS settled='+str(sum(r['status']=='settled' for r in state['races'].values())),flush=True)
            except Exception: print('AU_RESULTS refresh_failed',flush=True)
            time.sleep(30)

if __name__=='__main__':main()
