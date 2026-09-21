#!/usr/bin/env python3
"""Durable, read-only scheduled observations for every race in the AU registry.

Sole writer on basecamp. Never writes the emitter target, shared decisions,
existing history, or the active-target observer's artifacts.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import threading
import time
from tb_au_coverage import FILE
from tb_queue_summary import parse_time
from tb_race_queue import atomic_write_json

OFFSETS = {'T15':900, 'T10':600, 'T5':300, 'T2':120, 'T30':30}
GRACE_SECONDS = 60


def utc_now():
    return datetime.now(timezone.utc)


def read_strict(path):
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('Expected state object')
    return value


def reconcile(saved, card, now):
    result = saved or {'schema':'tb_au_observations/v1', 'races':{}}
    if result.get('schema') != 'tb_au_observations/v1' or not isinstance(result.get('races'), dict):
        raise ValueError('Invalid observation registry')
    if not isinstance(card.get('races'), list):
        raise ValueError('Invalid Australian card')
    for market in card['races']:
        if market.get('country_code') != 'AU':
            continue
        mid = str(market.get('market_id', ''))
        start = parse_time(market.get('market_start_time'))
        if not re.fullmatch(r'[0-9]+\.[0-9]+', mid) or not start:
            raise ValueError('Invalid Australian market identity or start')
        job = result['races'].setdefault(mid, {'date':card['date'], 'registered_at':now.isoformat(), 'slots':{}})
        job['market'] = market
        for name,offset in OFFSETS.items():
            due = (start-timedelta(seconds=offset)).isoformat()
            slot = job['slots'].setdefault(name, {'status':'pending','attempts':0,'due_at':due})
            if slot['status'] == 'pending':
                slot['due_at'] = due
    result.update(updated_at=now.isoformat(), registry_updated_at=card.get('discovery_updated_at'), error=None)
    return result


def artifact(base, job, mid, name):
    return base/'observations'/'au'/job['date']/mid/(name+'.json')


def tick(base, fetch, *, now=None):
    base = Path(base)
    supplied_time = now is not None
    now = now or utc_now()
    path = base/'state'/FILE
    saved = read_strict(path)
    card = read_strict(base/'state/tb_today_card.json')
    if card is None:
        raise ValueError('Australian card unavailable')
    state = reconcile(saved, card, now)
    due = {}
    for mid, job in state['races'].items():
        start = parse_time(job['market']['market_start_time'])
        for name,slot in job['slots'].items():
            if slot['status'] != 'pending':
                continue
            # The artifact is committed first. Recover a crash before job commit.
            capture = read_strict(artifact(base, job, mid, name))
            if capture and capture.get('due_at') == slot['due_at'] and capture.get('market_id') == mid:
                slot.update(status='captured', captured_at=capture['captured_at'],
                            late_seconds=capture['late_seconds'], delayed_feed=capture.get('book', {}).get('isMarketDataDelayed'), artifact=str(artifact(base, job, mid, name).relative_to(base)))
                slot.pop('last_error', None)
                continue
            scheduled = parse_time(slot['due_at'])
            if now > scheduled+timedelta(seconds=GRACE_SECONDS) or now >= start:
                slot.update(status='missed', reason=slot.pop('last_error', None) or 'snapshot_window_elapsed', finished_at=now.isoformat())
            elif now >= scheduled:
                due.setdefault(mid, []).append(name)
    # Jobs and missed windows survive process or network failure.
    atomic_write_json(path, state)
    ids = list(due)
    batches = [ids[i:i+5] for i in range(0,len(ids),5)]
    def request(batch):
        try:
            books = fetch(batch)
            if not isinstance(books, list):
                raise ValueError('Invalid market books')
            return batch, books, now if supplied_time else utc_now(), None
        except Exception:
            return batch, [], now if supplied_time else utc_now(), 'market_book_unavailable'
    with ThreadPoolExecutor(max_workers=2) as workers:
        for batch,books,received,error in workers.map(request,batches):
            by_id = {str(b.get('marketId')):b for b in books if isinstance(b,dict)}
            for mid in batch:
                job = state['races'][mid]; raw = by_id.get(mid)
                for name in due[mid]:
                    slot = job['slots'][name]; scheduled = parse_time(slot['due_at'])
                    slot['attempts'] += 1
                    if received > scheduled+timedelta(seconds=GRACE_SECONDS) or received >= parse_time(job['market']['market_start_time']):
                        slot.update(status='missed', reason='snapshot_window_elapsed', finished_at=received.isoformat())
                    elif raw and (raw.get('inplay') is True or raw.get('status') == 'CLOSED'):
                        slot.update(status='missed',reason='already_inplay_or_closed',finished_at=received.isoformat())
                    elif error or not raw or raw.get('status') != 'OPEN' or raw.get('inplay') is not False:
                        slot['last_error'] = error or 'market_omitted_or_not_open'
                    else:
                        late = round((received-scheduled).total_seconds(),3)
                        target = artifact(base,job,mid,name)
                        atomic_write_json(target, {'schema':'tb_au_snapshot/v1','market_id':mid,'slot':name,
                            'due_at':slot['due_at'],'captured_at':received.isoformat(),'late_seconds':late,
                            'timing_basis':'scheduled_start_receive_time','price_data':['EX_BEST_OFFERS','EX_TRADED'],'market':job['market'],'book':raw})
                        slot.update(status='captured',captured_at=received.isoformat(),late_seconds=late,
                                    delayed_feed=raw.get('isMarketDataDelayed'),
                                    artifact=str(target.relative_to(base)))
                        slot.pop('last_error', None)
                atomic_write_json(path,state)
    state['updated_at'] = (now if supplied_time else utc_now()).isoformat()
    atomic_write_json(path,state)
    return state


class Gateway:
    """Two workers share authentication and a maximum one-request/second gate.

    Five EX_BEST_OFFERS + EX_TRADED markets consume 100 of Betfair's 200 request points.
    This budget belongs to the AU observer; legacy services remain independent.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.token = None
        self.last = 0

    def __call__(self, ids):
        from betfair_gateway import betfair_login, betting_api
        with self.lock:
            if self.token is None:
                self.token = betfair_login()
            time.sleep(max(0, 1-(time.monotonic()-self.last)))
            self.last = time.monotonic()
            token = self.token
        try:
            return betting_api('listMarketBook', {'marketIds':ids,
                'priceProjection':{'priceData':['EX_BEST_OFFERS','EX_TRADED']}},token)
        except Exception:
            with self.lock:
                if self.token == token:
                    self.token = None
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-dir',type=Path,default=Path.home()/'botlab/totebot')
    parser.add_argument('--once',action='store_true')
    args = parser.parse_args()
    from betfair_gateway import load_secrets
    load_secrets(Path(os.environ.get('BETFAIR_SECRETS_FILE','/opt/betfair/secrets.env')))
    base = args.base_dir.expanduser(); (base/'state').mkdir(parents=True,exist_ok=True)
    gateway = Gateway()
    with (base/'state/.tb_au_observer.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            try:
                result = tick(base,gateway)
                print(f'AU_OBSERVER registered={len(result["races"])}',flush=True)
            except Exception:
                # Fail visibly; preserve the last state and let its heartbeat stale.
                print('AU_OBSERVER refresh_failed; retained previous jobs',flush=True)
                if args.once:
                    raise SystemExit(1)
            if args.once:
                return
            time.sleep(10)


if __name__ == '__main__':
    main()
