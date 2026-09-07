"""Shared delayed-race policy and observation data (no network access)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import json

VERSION = '1.0.0'
TERMINAL = {'observed_start', 'closed', 'timed_out'}
DEFAULTS = {'poll_seconds': 10, 'fresh_seconds': 45, 'stale_hold_seconds': 180,
            'max_delay_seconds': 1800, 'buffer_seconds': 1200}
OFFSETS = {'t15': 900, 't10': 600, 't5': 300, 't2': 120, 't30': 30}


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def stamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def policy(base):
    supplied = read_json(base / 'config' / 'race_observation.json') or {}
    result = dict(DEFAULTS)
    bounds = {'poll_seconds': (5, 60), 'fresh_seconds': (15, 300),
              'stale_hold_seconds': (30, 900), 'max_delay_seconds': (120, 7200),
              'buffer_seconds': (1200, 3600)}
    for key, (low, high) in bounds.items():
        value = supplied.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = max(low, min(value, high))
    result['fresh_seconds'] = max(result['fresh_seconds'], result['poll_seconds'] * 2)
    result['stale_hold_seconds'] = max(result['stale_hold_seconds'], result['fresh_seconds'])
    return result


def observation_dir(base, market):
    mid = str(market.get('market_id') or '')
    start = stamp(market.get('market_start_time'))
    if not re.fullmatch(r'\d+\.\d+', mid) or not start:
        return None
    return base / 'observations' / start.date().isoformat() / mid


def load_lifecycle(base, market):
    directory = observation_dir(base, market)
    return (read_json(directory / 'lifecycle.json') or {}) if directory else {}


def evaluate(market, lifecycle, now, settings=None):
    settings = settings or DEFAULTS
    start = stamp(market.get('market_start_time'))
    if not start:
        return {'hold': False, 'state': 'unknown', 'reason': 'missing_scheduled_start'}
    mid = str(market.get('market_id'))
    state = lifecycle if str(lifecycle.get('market_id')) == mid else {}
    if state.get('state') in TERMINAL:
        return {'hold': False, 'state': state['state'], 'reason': state['state']}
    delayed = max(0, int((now - start).total_seconds()))
    if now >= start + timedelta(seconds=settings['max_delay_seconds']):
        return {'hold': False, 'state': 'timed_out', 'reason': 'maximum_delay_reached', 'delay_seconds': delayed}
    seen = stamp(state.get('last_received_at'))
    age = (now - seen).total_seconds() if seen else None
    fresh = age is not None and 0 <= age <= settings['fresh_seconds'] and not state.get('feed_error')
    if fresh and state.get('state') in {'preplay', 'delayed', 'suspended'}:
        label = 'suspended' if state['state'] == 'suspended' else 'delayed' if now >= start else 'preplay'
        return {'hold': True, 'state': label, 'reason': 'fresh_market_observation', 'delay_seconds': delayed}
    if now < start:
        return {'hold': True, 'state': 'unknown', 'reason': 'awaiting_fresh_observation', 'delay_seconds': 0}
    reference = max(start, seen) if seen and seen <= now else start
    hold = now < reference + timedelta(seconds=settings['stale_hold_seconds'])
    return {'hold': hold, 'state': 'unknown' if hold else 'timed_out',
            'reason': 'stale_feed' if hold else 'stale_feed_timeout', 'delay_seconds': delayed}


def select_offsets(captures, anchor, tolerance=30):
    """Select observations at/before each offset; leave gaps, never interpolate."""
    selected = {}
    valid = [(stamp(book.get('captured_at')), book) for book in captures]
    valid = [(when, book) for when, book in valid if when and book.get('market', {}).get('inplay') is False
             and book.get('market', {}).get('market_status') == 'OPEN']
    for stage, seconds in OFFSETS.items():
        target = anchor - timedelta(seconds=seconds)
        candidates = [(when, book) for when, book in valid if 0 <= (target - when).total_seconds() <= tolerance]
        if candidates:
            selected[stage] = max(candidates, key=lambda item: item[0])[1]
    return selected


def scratching_break(snapshots):
    """Detect removal events within a price path; reductions affect the field."""
    seen_removed = None
    for book in snapshots:
        if not book:
            continue
        runners = book.get('runners')
        if not isinstance(runners, list):
            continue
        removed = {str(r.get('selection_id', r.get('selectionId'))) for r in runners
                   if isinstance(r, dict) and str(r.get('status', '')).upper().startswith('REMOVED')}
        if seen_removed is not None and removed - seen_removed:
            return True
        seen_removed = (seen_removed or set()) | removed
    return False
