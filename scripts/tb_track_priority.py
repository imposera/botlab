"""Explicit Australian meeting order, shared as metadata by the daily card."""
import json
from pathlib import Path
from tb_queue_summary import parse_time

FILE = 'australian_card_priority.json'
STATES = {'ACT', 'NSW', 'NT', 'QLD', 'SA', 'TAS', 'VIC', 'WA'}


def normalise(value):
    return ' '.join(str(value or '').casefold().split())


def validate(value):
    if not isinstance(value, dict) or not isinstance(value.get('tracks'), list):
        raise ValueError('Australian track priority must contain a tracks list')
    seen = set()
    for row in value['tracks']:
        if not isinstance(row, dict) or not isinstance(row.get('track'), str) or not normalise(row['track']):
            raise ValueError('Every priority entry needs a track name')
        if row.get('state') not in STATES:
            raise ValueError('Every priority entry needs an Australian state code')
        name = normalise(row['track'])
        if name in seen:
            raise ValueError('Duplicate track in Australian priority list')
        seen.add(name)
    regions = value.get('other_track_states', {})
    if not isinstance(regions, dict) or any(v not in STATES for v in regions.values()):
        raise ValueError('Invalid state mapping')
    if not isinstance(value.get('apply_to_emitter', False), bool):
        raise ValueError('apply_to_emitter must be true or false')
    return {**value, 'schema': 'tb_australian_track_priority/v1'}


def load(config_dir):
    path = Path(config_dir)/FILE
    if not path.exists():
        return {'schema':'tb_australian_track_priority/v1','tracks':[], 'apply_to_emitter':False}
    return validate(json.loads(path.read_text()))


def info(track, priority):
    name = normalise(track)
    state = next((v for k,v in priority.get('other_track_states', {}).items() if normalise(k) == name), None)
    rank = None
    for i, row in enumerate(priority.get('tracks', []), 1):
        if normalise(row.get('track')) == name:
            rank, state = i, row.get('state')
            break
    label = f'{track} {state}' if state else str(track or 'Unknown track')
    return {'track_priority':rank,'state_code':state,'track_label':label}


def order_meetings(meetings, priority):
    # Unlisted meetings follow named priorities, retaining their original
    # first-jump order. Regions label tracks; they do not rank whole states.
    rows = [{**m, **info(m.get('track'), priority)} for m in meetings]
    return sorted(rows, key=lambda m: m['track_priority'] if m['track_priority'] is not None else len(priority.get('tracks', []))+1)


def clash_preferences(markets, decisions, priority, now, *, clash_seconds=1080, minimum_alternative_seconds=720, alternative_allowed=None):
    """Prefer ranked AU tracks only where scheduled observation windows overlap.

    Resolve higher ranks first so a race already skipped cannot suppress another
    race. An explicit Keep exempts a market from this skip rule.
    """
    if not priority.get('apply_to_emitter') or not priority.get('tracks'):
        return list(markets), []
    default_rank = len(priority['tracks'])+1
    candidates = []
    for row in markets:
        start = parse_time(row.get('market_start_time'))
        if (row.get('country_code') != 'AU' or not start or (start-now).total_seconds() < minimum_alternative_seconds or
                decisions.get(str(row.get('market_id')), {}).get('action') == 'remove' or
                (alternative_allowed and not alternative_allowed(row))):
            continue
        candidates.append((info(row.get('track'), priority)['track_priority'] or default_rank, start, row))
    candidates.sort(key=lambda item: (item[0], item[1], str(item[2]['market_id'])))
    accepted, skipped = [], {}
    for rank, start, row in candidates:
        mid = str(row['market_id'])
        rivals = [(r, t, m) for r,t,m in accepted if r < rank and abs((start-t).total_seconds()) < clash_seconds]
        if rivals and decisions.get(mid, {}).get('action') != 'keep':
            other_rank, _, other = rivals[0]
            label = info(other.get('track'), priority)['track_label']
            skipped[mid] = {'market_id':mid,'preference_kind':'australian_track_priority',
                'alternative_market_id':other['market_id'],'track_priority':rank,'preferred_track_priority':other_rank,
                'reason':f'Track priority: prefer {label} {other.get("market_name", "")} (rank {other_rank}) where observation windows overlap within {clash_seconds/60:g} minutes.'}
        else:
            accepted.append((rank, start, row))
    return [r for r in markets if str(r['market_id']) not in skipped], list(skipped.values())
