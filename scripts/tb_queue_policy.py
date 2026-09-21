"""Shared, clash-only learning from explicit queue decisions; no model dependencies."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

CLASH_SECONDS = 300


def timestamp(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def race_class(row):
    name = str(row.get('market_name') or '').lower()
    for label, pattern in [
        ('stakes', r'\b(group|grp|grade|grd|listed|stakes|stks)\b'),
        ('maiden', r'\b(maiden|mdn|msw)\b'),
        ('claiming', r'\b(claiming|claim|clm)\b'),
        ('allowance', r'\b(allowance|allw|alw)\b'),
        ('handicap', r'\b(handicap|hcap)\b'),
    ]:
        if re.search(pattern, name):
            return label
    return 'other'


def key(row):
    return (str(row.get('country_code') or '').strip().upper(),
            ' '.join(str(row.get('track') or '').casefold().split()), race_class(row))


def learn(state_dir, now):
    path = Path(state_dir) / 'tb_race_queue_decisions.jsonl'
    if not path.exists():
        return {}
    latest = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        mid = event['market_id']
        when = timestamp(event['recorded_at'])
        if mid not in latest or when >= timestamp(latest[mid]['recorded_at']):
            latest[mid] = event
    groups = defaultdict(list)
    for event in latest.values():
        when = timestamp(event['recorded_at'])
        row = event.get('race_snapshot') or {}
        if (event.get('action') not in ('keep', 'remove') or
                not now - timedelta(days=30) <= when <= now or not key(row)[1]):
            continue
        # Distinct race dates, rather than click dates, prevent repeated clicks
        # on a single meeting from manufacturing multi-day evidence.
        try:
            day = timestamp(row['market_start_time']).date().isoformat()
        except (KeyError, ValueError, TypeError):
            continue
        groups[key(row)].append((event['action'], day))
    profiles = {}
    for group, events in groups.items():
        removes = [day for action, day in events if action == 'remove']
        if len(removes) >= 3 and len(set(removes)) >= 2 and len(removes) / len(events) >= .8:
            profiles[group] = {'removals': len(removes), 'decisions': len(events),
                               'race_days': len(set(removes))}
    return profiles


def annotate(markets, decisions, profiles, now, alternative_allowed=None):
    rows = []
    for market in markets:
        row = dict(market)
        action = decisions.get(str(row.get('market_id')), {}).get('action')
        row.update(human_action=action, arming_eligible=action != 'remove',
                   clash_skip=None)
        rows.append(row)
    future = []
    for row in rows:
        try:
            if timestamp(row['market_start_time']) > now:
                future.append(row)
        except (KeyError, ValueError, TypeError):
            continue
    # Alternatives must survive independently: two disfavoured races cannot
    # eliminate one another, and an explicit Keep always defeats learning.
    preferred = [r for r in future if r['arming_eligible'] and
                 (alternative_allowed is None or alternative_allowed(r)) and
                 (r['human_action'] == 'keep' or key(r) not in profiles)]
    for row in future:
        evidence = profiles.get(key(row))
        if not evidence or not row['arming_eligible'] or row['human_action'] == 'keep':
            continue
        alternatives = [r for r in preferred if r['market_id'] != row['market_id'] and
                        abs((timestamp(r['market_start_time']) - timestamp(row['market_start_time'])).total_seconds()) <= CLASH_SECONDS]
        if alternatives:
            other = min(alternatives, key=lambda r: (timestamp(r['market_start_time']), str(r['market_id'])))
            row['arming_eligible'] = False
            row['clash_skip'] = {**evidence, 'alternative_market_id': other['market_id'],
                'reason': f"Clash preference: {evidence['removals']}/{evidence['decisions']} removals for this track/{race_class(row)} across {evidence['race_days']} race dates; prefer {other.get('track')} {other.get('market_name')} (within 5 minutes)."}
    return rows


def filter_automatic(markets, state_dir, now, alternative_allowed=None):
    """Apply human decisions globally, and learned clashes within the next ten."""
    path = Path(state_dir) / 'tb_race_queue.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    decisions = state.get('decisions', {})
    profiles = learn(state_dir, now)
    upcoming = sorted((m for m in markets if timestamp(m['market_start_time']) > now),
                      key=lambda m: timestamp(m['market_start_time']))
    annotated = annotate(upcoming[:10], decisions, profiles, now, alternative_allowed)
    by_id = {r['market_id']: r for r in annotated}
    allowed, skipped = [], []
    for market in markets:
        row = by_id.get(market['market_id'])
        if decisions.get(str(market['market_id']), {}).get('action') == 'remove':
            skipped.append({'market_id': market['market_id'], 'reason': 'human_remove'})
        elif row and row['clash_skip']:
            skipped.append({'market_id': market['market_id'], **row['clash_skip']})
        else:
            allowed.append(market)
    return allowed, skipped
