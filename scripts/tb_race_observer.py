#!/usr/bin/env python3
"""Read-only Betfair companion: rolling captures through scheduled-start delays.

Runs alongside the existing scheduled capture service. Writes only state and
observations/, never scheduled history. Requires the existing betfair_gateway.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import os
from pathlib import Path
import time

from tb_blackbook import atomic_write_json, read_json
from tb_race_lifecycle import (VERSION, TERMINAL, evaluate, observation_dir, policy,
                               select_offsets, stamp)


def normalize(raw, market, now):
    names = {str(r.get('selection_id', r.get('selectionId'))): r
             for r in market.get('runners', []) if isinstance(r, dict)}
    rows = []
    for runner in raw.get('runners', []) or []:
        if not isinstance(runner, dict):
            continue
        sid = runner.get('selectionId')
        metadata = names.get(str(sid), {})
        rows.append({'selection_id': sid, 'runner_name': metadata.get('runner_name', metadata.get('runnerName')),
                     'cloth_number': metadata.get('cloth_number'), 'status': runner.get('status'),
                     'last_price_traded': runner.get('lastPriceTraded'),
                     'total_matched': runner.get('totalMatched'),
                     'adjustment_factor': runner.get('adjustmentFactor'),
                     'back_levels': (runner.get('ex') or {}).get('availableToBack', []),
                     'lay_levels': (runner.get('ex') or {}).get('availableToLay', [])})
    return {'schema': 'totebot_rolling_book/v1', 'timing_basis': 'received_observation',
            'captured_at': now.isoformat(),
            'market': {**{k: v for k, v in market.items() if k != 'runners'},
                       'market_status': raw.get('status'), 'inplay': raw.get('inplay'),
                       'is_market_data_delayed': raw.get('isMarketDataDelayed'),
                       'total_matched': raw.get('totalMatched')}, 'runners': rows}


def observe(market, raw, now, old, rolling, settings):
    """Pure transition; received timestamps are not official race-off times."""
    lifecycle = dict(old)
    captures = list(rolling.get('captures', []))
    lifecycle.update(schema='totebot_race_lifecycle/v1', version=VERSION,
                     market_id=str(market['market_id']), scheduled_start=market['market_start_time'])
    if lifecycle.get('state') in TERMINAL:
        return lifecycle, {'captures': captures}, None
    if not isinstance(raw, dict) or str(raw.get('marketId')) != str(market['market_id']):
        lifecycle['feed_error'] = True
        decision = evaluate(market, lifecycle, now, settings)
        lifecycle.update(state=decision['state'], reason=decision['reason'], updated_at=now.isoformat())
        return lifecycle, {'captures': captures}, None
    book = normalize(raw, market, now)
    prior_seen = lifecycle.get('last_received_at')
    lifecycle.update(last_received_at=now.isoformat(), updated_at=now.isoformat(), feed_error=False,
                     delayed_feed=raw.get('isMarketDataDelayed'), latest_book=book)
    events = list(lifecycle.get('events', []))
    removed = {str(r['selection_id']) for r in book['runners'] if str(r.get('status', '')).startswith('REMOVED')}
    known = set(lifecycle.get('removed_ids', []))
    for sid in sorted(removed - known):
        events.append({'type': 'runner_removed', 'selection_id': sid, 'observed_at': now.isoformat(),
                       'price_series_break': True})
    lifecycle['events'] = events
    lifecycle['removed_ids'] = sorted(known | removed)
    derived = None
    if raw.get('status') == 'CLOSED':
        lifecycle.update(state='closed', reason='closed_without_confirmed_start')
    elif raw.get('inplay') is True:
        lifecycle.update(state='observed_start', observed_start_at=now.isoformat(),
                         start_source='first_received_inplay_true',
                         previous_observation_at=prior_seen,
                         timing_uncertainty='Receive-time estimate; feed and polling delay unknown')
        selected = select_offsets(captures, now, tolerance=settings['poll_seconds'] * 2)
        derived = {'schema': 'totebot_observed_start_captures/v1', 'market_id': str(market['market_id']),
                   'timing_basis': 'observed_start', 'observed_start_at': now.isoformat(),
                   'source': lifecycle['start_source'], 'delayed_feed': raw.get('isMarketDataDelayed'),
                   'timing_uncertainty': lifecycle['timing_uncertainty'], 'events': events, 'captures': selected}
    elif raw.get('status') == 'SUSPENDED':
        lifecycle.update(state='suspended', reason='start_unconfirmed')
    elif raw.get('status') == 'OPEN' and raw.get('inplay') is False:
        lifecycle.update(state='delayed' if now >= stamp(market['market_start_time']) else 'preplay')
        if not captures or captures[-1]['captured_at'] != book['captured_at']:
            captures.append(book)
        lifecycle['last_preplay_at'] = now.isoformat()
    else:
        lifecycle.update(state='unknown', feed_error=True)
    decision = evaluate(market, lifecycle, now, settings)
    if not decision['hold'] and lifecycle['state'] not in TERMINAL:
        lifecycle.update(state='timed_out', reason=decision['reason'])
    transitions = list(lifecycle.get('status_events', []))
    if lifecycle.get('state') != old.get('state'):
        transitions.append({'state': lifecycle['state'], 'observed_at': now.isoformat()})
    lifecycle['status_events'] = transitions[-200:]
    if lifecycle['state'] not in TERMINAL:
        cutoff = now - timedelta(seconds=settings['buffer_seconds'])
        captures = [b for b in captures if stamp(b['captured_at']) >= cutoff]
    return lifecycle, {'schema': 'totebot_rolling_captures/v1', 'market_id': str(market['market_id']),
                       'timing_basis': 'rolling_receive_time', 'captures': captures}, derived


def tick(base, fetch, now=None):
    supplied_time = now is not None
    now = now or datetime.now(timezone.utc)
    settings = policy(base)
    target = read_json(base / 'state' / 'betfair_t15_target.json') or {}
    if target.get('status') != 'armed':
        return 'idle'
    market = target.get('market') or {}
    directory = observation_dir(base, market)
    if directory is None:
        return 'invalid_target'
    old = read_json(directory / 'lifecycle.json') or {}
    rolling = read_json(directory / 'rolling.json') or {}
    if old.get('state') in TERMINAL:
        return old['state']
    committed_start = read_json(directory / 'observed_start.json') or {}
    if (str(committed_start.get('market_id')) == str(market['market_id'])
            and stamp(committed_start.get('observed_start_at'))):
        # Recover a crash after the start artifact was saved but before lifecycle.
        old.update(market_id=str(market['market_id']), state='observed_start',
                   observed_start_at=committed_start['observed_start_at'],
                   start_source=committed_start.get('source'))
        atomic_write_json(directory / 'lifecycle.json', old)
        return 'observed_start'
    decision = evaluate(market, old, now, settings)
    # Still make one attempt after a stale timeout to allow recovery until max hold.
    if decision['reason'] == 'maximum_delay_reached':
        old.update(market_id=str(market['market_id']), state='timed_out', reason=decision['reason'])
        atomic_write_json(directory / 'lifecycle.json', old)
        return 'timed_out'
    try:
        raw = fetch(str(market['market_id']))
    except Exception:
        # Never persist gateway exceptions; they may contain credentials.
        raw = None
    if not supplied_time:
        now = datetime.now(timezone.utc)
    lifecycle, rolling, derived = observe(market, raw, now, old, rolling, settings)
    atomic_write_json(directory / 'rolling.json', rolling)
    if derived is not None:
        atomic_write_json(directory / 'observed_start.json', derived)
    atomic_write_json(directory / 'lifecycle.json', lifecycle)
    return lifecycle['state']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-dir', type=Path, default=Path.home() / 'botlab' / 'totebot')
    parser.add_argument('--secrets-file', type=Path,
                        default=Path(os.environ.get('BETFAIR_SECRETS_FILE', '/opt/betfair/secrets.env')))
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--version', action='version', version=VERSION)
    args = parser.parse_args()
    from betfair_gateway import betfair_login, betting_api, load_secrets
    load_secrets(args.secrets_file.expanduser())
    base = args.base_dir.expanduser()
    (base / 'state').mkdir(parents=True, exist_ok=True)
    with (base / 'state' / '.race_observer.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session = None
        def fetch(mid):
            nonlocal session
            if session is None:
                session = betfair_login()
            try:
                books = betting_api('listMarketBook', {'marketIds': [mid],
                    'priceProjection': {'priceData': ['EX_BEST_OFFERS']}}, session)
            except Exception:
                session = None
                raise
            return next((book for book in books if str(book.get('marketId')) == mid), None)
        while True:
            print(f'RACE_OBSERVER state={tick(base, fetch)}', flush=True)
            if args.once:
                break
            time.sleep(policy(base)['poll_seconds'])


if __name__ == '__main__':
    main()
