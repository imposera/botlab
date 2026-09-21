"""Australian daily card: authoritative collection, read-only summaries and panel."""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, timezone
import html
import json
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from tb_queue_summary import read_state, parse_time
from tb_card_forecast import build_forecast, decision_hash, target_key
from tb_track_priority import load as load_track_priority, order_meetings, info as track_info

ZONE = ZoneInfo('Australia/Melbourne')
FILE = 'tb_today_card.json'
DISCOVERY_SECONDS = 300
STATUS_STALE_SECONDS = 180
RACE_TYPES = ['Flat', 'Steeple', 'Hurdle']


def day_bounds(now):
    day = now.astimezone(ZONE).date()
    start = datetime.combine(day, time.min, ZONE)
    end = datetime.combine(day + timedelta(days=1), time.min, ZONE)
    return day.isoformat(), start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def iso(now):
    return now.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def fresh(stamp, now, seconds):
    dt = parse_time(stamp)
    return dt is not None and -5 <= (now-dt).total_seconds() <= seconds


def valid_card(value):
    return (isinstance(value, dict) and isinstance(value.get('races'), list)
            and all(isinstance(r, dict) and r.get('market_id') and parse_time(r.get('market_start_time'))
                    for r in value['races']))


def discover(api, token, now):
    from betfair_gateway import market_summary
    day, start, end = day_bounds(now)
    filters = {'eventTypeIds': ['7'], 'marketCountries': ['AU'],
               'marketTypeCodes': ['WIN'], 'raceTypes': RACE_TYPES,
               'marketStartTime': {'from': iso(start), 'to': iso(end)}}
    events = api('listEvents', {'filter': filters}, token)
    races = []
    for event in events:
        event_id = str(event['event']['id'])
        catalogue = api('listMarketCatalogue', {
            'filter': {**filters, 'eventIds': [event_id]},
            'marketProjection': ['EVENT', 'MARKET_START_TIME', 'RUNNER_DESCRIPTION', 'RUNNER_METADATA'],
            'sort': 'FIRST_TO_START', 'maxResults': 100}, token)
        if len(catalogue) >= 100:
            raise RuntimeError('Meeting catalogue may be truncated')
        for item in catalogue:
            row = market_summary(item)
            metadata = {str(r.get('selectionId')): r.get('metadata') or {} for r in item.get('runners', [])}
            for runner in row.get('runners', []):
                runner['metadata'] = metadata.get(str(runner.get('selection_id')), {})
            stamp = parse_time(row.get('market_start_time'))
            if row.get('market_id') and row.get('country_code') == 'AU' and stamp and start <= stamp < end:
                row['meeting_id'] = str(row.get('event_id') or event_id)
                races.append(row)
    return races


def merge_card(prior, markets, books, now, *, discovered=False):
    day, _, _ = day_bounds(now)
    same_day = prior and prior.get('date') == day
    card = dict(prior) if same_day else {'schema': 'tb_today_card/v1', 'date': day,
        'timezone': 'Australia/Melbourne', 'first_discovered_at': iso(now), 'races': []}
    by_id = {r['market_id']: dict(r) for r in card['races']}
    for market in markets:
        mid = market['market_id']
        by_id[mid] = {**by_id.get(mid, {}), **market}
        by_id[mid]['catalogue_seen_at'] = iso(now)
    for book in books:
        mid = book.get('marketId')
        if mid not in by_id:
            continue
        row = by_id[mid]
        row.update(market_status=book.get('status'), inplay=book.get('inplay'),
                   status_updated_at=iso(now), is_market_data_delayed=book.get('isMarketDataDelayed'),
                   total_matched=book.get('totalMatched'), runner_count=book.get('numberOfActiveRunners'))
        names = {str(r.get('selection_id')): r.get('runner_name') for r in row.get('runners', [])}
        winners = [{'selection_id': r['selectionId'], 'runner_name': names.get(str(r['selectionId']))}
                   for r in book.get('runners', []) if r.get('status') == 'WINNER']
        if winners:
            row['winners'] = winners
    card['races'] = sorted(by_id.values(), key=lambda r: (r['market_start_time'], r['market_id']))
    if discovered:
        card['discovery_updated_at'] = iso(now)
        card['discovery_error'] = None
    card['status_checked_at'] = iso(now)
    card['updated_at'] = iso(now)
    return card


def refresh(state_dir, secrets, *, now=None):
    import fcntl
    from betfair_gateway import betting_api, betfair_login, load_secrets
    from tb_race_queue import atomic_write_json
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir/'.tb_today_card.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = now or datetime.now(timezone.utc)
        day, _, _ = day_bounds(now)
        prior = read_state(state_dir/FILE)
        if (state_dir/FILE).exists() and not valid_card(prior):
            raise RuntimeError('Invalid saved daily card; refusing to discard retained races')
        if prior and prior.get('date') != day:
            atomic_write_json(state_dir/'today_cards'/f"{prior['date']}.json", prior)
        current = prior if prior and prior.get('date') == day else {}
        load_secrets(Path(secrets).expanduser())
        token = betfair_login()
        discovered = not fresh(current.get('discovery_updated_at'), now, DISCOVERY_SECONDS)
        markets = []
        discovery_error = None
        if discovered:
            try:
                markets = discover(betting_api, token, now)
            except Exception as exc:
                discovery_error = f'Discovery failed ({type(exc).__name__})'
                if not current:
                    raise RuntimeError(discovery_error) from None
                discovered = False
        card = merge_card(current, markets, [], now, discovered=discovered)
        books, missing, status_error = [], [], None
        ids = [r['market_id'] for r in card['races'] if r.get('market_status') != 'CLOSED']
        try:
            for offset in range(0, len(ids), 40):
                batch = ids[offset:offset+40]
                result = betting_api('listMarketBook', {'marketIds': batch}, token)
                books.extend(result)
                # Mixed open/closed requests return only open markets. Retry
                # missing IDs individually to confirm closures, never infer them.
                for mid in sorted(set(batch)-{r.get('marketId') for r in result}):
                    closed = betting_api('listMarketBook', {'marketIds': [mid]}, token)
                    books.extend(closed)
                    if not any(r.get('marketId') == mid for r in closed):
                        missing.append(mid)
        except Exception as exc:
            status_error = f'Market status failed ({type(exc).__name__})'
        card = merge_card(card, [], books, now)
        card.update(discovery_error=discovery_error, status_error=status_error, status_missing_ids=missing)
        try:
            card['track_priority'] = load_track_priority(state_dir.parent/'config')
            card['track_priority_error'] = None
        except (ValueError, OSError) as exc:
            card['track_priority_error'] = f'Australian track priority unavailable ({type(exc).__name__}); retaining last card order.'
        try:
            card['emitter_forecast'] = build_forecast(state_dir, card, now)
        except Exception as exc:
            card['emitter_forecast'] = {'error': f'Emitter forecast unavailable ({type(exc).__name__})', 'generated_at': iso(now)}
        atomic_write_json(state_dir/FILE, card)
        atomic_write_json(state_dir/'today_cards'/f'{day}.json', card)
        return card


def decision_race(state_dir, market_id, now=None):
    """Validate a current, future card race before allowing a shared decision."""
    now = now or datetime.now(timezone.utc)
    card = read_state(Path(state_dir)/FILE)
    if not valid_card(card) or card.get('date') != day_bounds(now)[0] or not fresh(card.get('discovery_updated_at'), now, 600):
        raise ValueError('Daily card is unavailable or stale; reload before deciding')
    for row in card.get('races', []):
        if str(row.get('market_id')) == str(market_id):
            if (not fresh(row.get('status_updated_at'), now, STATUS_STALE_SECONDS) or
                    row.get('market_status') not in ('OPEN', 'SUSPENDED') or row.get('inplay') is not False or
                    not parse_time(row.get('market_start_time')) or parse_time(row['market_start_time']) <= now):
                raise ValueError('Race has started or its current status is unconfirmed')
            return row
    raise ValueError('Race is not on today\'s Australian card')


def summary(state_dir, *, now=None, meeting='', view='all'):
    now = now or datetime.now(timezone.utc)
    state_dir = Path(state_dir)
    day = day_bounds(now)[0]
    card = read_state(state_dir/FILE)
    output = {'schema': 'tb_today_card_summary/v1', 'date': day, 'timezone': str(ZONE),
              'generated_at': iso(now), 'ok': False, 'freshness': 'unavailable',
              'discovery_updated_at': None, 'status_checked_at': None, 'meetings': [], 'races': [],
              'meeting_count': 0, 'race_count': 0, 'meeting_filter': meeting, 'view': view}
    if not valid_card(card) or card.get('date') != day:
        return {**output, 'message': 'Today\'s Australian card is unavailable'}
    queue = read_state(state_dir/'tb_race_queue.json')
    if queue and (not isinstance(queue.get('decisions'), dict) or not isinstance(queue.get('races'), list)):
        queue = None
    decisions = queue.get('decisions', {}) if queue else {}
    queue_rows = {str(r.get('market_id')): r for r in (queue or {}).get('races', [])}
    target = read_state(state_dir/'betfair_t15_target.json') or {}
    armed_id = str((target.get('market') or {}).get('market_id')) if target.get('status') == 'armed' else None
    discovery_fresh = fresh(card.get('discovery_updated_at'), now, 600)
    status_fresh = all(r.get('market_status') == 'CLOSED' or fresh(r.get('status_updated_at'), now, STATUS_STALE_SECONDS) for r in card['races'])
    output.update(discovery_updated_at=card.get('discovery_updated_at'), status_checked_at=card.get('status_checked_at'),
                  first_discovered_at=card.get('first_discovered_at'), discovery_error=card.get('discovery_error'),
                  status_error=card.get('status_error'), decisions_available=queue is not None,
                  status_missing_ids=card.get('status_missing_ids', []),
                  ok=bool(discovery_fresh and status_fresh and not card.get('discovery_error') and not card.get('status_error') and not card.get('status_missing_ids')),
                  discovery_fresh=discovery_fresh, status_fresh=status_fresh)
    output['freshness'] = 'fresh' if output['ok'] else 'stale'
    output['message'] = 'Card current' if output['ok'] else 'Cached card; check discovery and status ages'
    projection = card.get('emitter_forecast') or {}
    projection_fresh = bool(projection.get('races') is not None and
        fresh(projection.get('generated_at'), now, STATUS_STALE_SECONDS) and
        projection.get('decision_hash') == decision_hash(decisions) and
        projection.get('target_key') == target_key(target) and output['ok'])
    output['emitter_forecast'] = {**projection, 'fresh': projection_fresh}
    meetings = {}
    races = []
    for row in card['races']:
        start = parse_time(row.get('market_start_time'))
        if not start or not row.get('market_id'):
            continue
        mid = str(row['market_id'])
        event = str(row.get('meeting_id') or row.get('event_id') or row.get('track'))
        meetings.setdefault(event, {'meeting_id': event, 'track': row.get('track'), 'race_count': 0})['race_count'] += 1
        action = decisions.get(mid, {}).get('action')
        clue = queue_rows.get(mid, {})
        clash = clue.get('clash_skip') if fresh((queue or {}).get('updated_at'), now, 120) and action not in ('keep', 'remove') else None
        selection = 'removed' if action == 'remove' else 'clash_skip' if clash else 'eligible' if queue is not None else 'unknown'
        row_fresh = fresh(row.get('status_updated_at'), now, STATUS_STALE_SECONDS)
        if row.get('market_status') == 'CLOSED':
            state = 'completed' if row.get('winners') else 'closed'
        elif not row_fresh:
            state = 'unconfirmed'
        elif row.get('inplay'):
            state = 'in_play'
        elif row.get('market_status') == 'SUSPENDED':
            state = 'suspended'
        elif start <= now:
            state = 'awaiting_start'
        else:
            state = 'upcoming'
        if mid == armed_id and fresh(target.get('updated_at'), now, STATUS_STALE_SECONDS) and state not in ('closed', 'completed', 'in_play'):
            state = 'armed'
        race = {k: row.get(k) for k in ('market_id','track','market_name','market_start_time','country_code','runner_count','total_matched','winners','status_updated_at','is_market_data_delayed')}
        race.update(meeting_id=event, seconds_to_jump=int((start-now).total_seconds()), race_state=state,
                    selection_status=selection, human_action=action, human_reason=decisions.get(mid, {}).get('reason'),
                    clash_skip=clash, decision_allowed=bool(discovery_fresh and row_fresh and start > now and
                        row.get('market_status') in ('OPEN','SUSPENDED') and row.get('inplay') is False))
        race['emitter_forecast'] = (projection.get('races') or {}).get(mid)
        race['forecast_fresh'] = projection_fresh
        races.append(race)
    track_priority = card.get('track_priority') or {'tracks':[], 'apply_to_emitter':False}
    ordered_meetings = order_meetings(list(meetings.values()), track_priority)
    meeting_order = {m['meeting_id']:i for i,m in enumerate(ordered_meetings)}
    for race in races:
        race.update(track_info(race.get('track'), track_priority))
    races.sort(key=lambda r: (meeting_order[r['meeting_id']], r['market_start_time'], r['market_id']))
    output.update(meetings=ordered_meetings, meeting_count=len(meetings), race_count=len(races),
                  track_priority=track_priority, track_priority_error=card.get('track_priority_error'))
    def visible(r):
        if meeting and r['meeting_id'] != meeting:
            return False
        if view == 'upcoming':
            return r['race_state'] not in ('completed','closed','in_play')
        if view == 'completed':
            return r['race_state'] in ('completed','closed')
        if view == 'review':
            return r['selection_status'] in ('removed','clash_skip')
        if view == 'clashes':
            return bool((r.get('emitter_forecast') or {}).get('timing_clashes'))
        return True
    output['races'] = [r for r in races if visible(r)]
    return output


def forecast_panel(projection):
    e = lambda x: html.escape(str(x if x is not None else '—'), quote=True)
    if 'counts' not in projection:
        return f'<p class="small">{e(projection.get("error", "Emitter forecast pending"))}</p>'
    counts = projection['counts']
    stale = '' if projection.get('fresh') else 'Cached / awaiting refresh · '
    warnings = ''.join(f'<p class="small">{e(w)}</p>' for w in projection.get('warnings', []))
    sequence = ''.join(f'<li>{e(parse_time(r["projected_arm_at"]).astimezone(ZONE).strftime("%H:%M"))} arm → {e(parse_time(r["market_start_time"]).astimezone(ZONE).strftime("%H:%M"))} jump · {e(r.get("track"))} · {e(r.get("market_name"))}</li>' for r in projection.get('sequence', []))
    return f'''<div class="card-forecast">
<p><strong>{e(stale)}Emitter forecast: {counts['projected']} projected targets · {counts['blocked']} timing blocks · {counts.get('track_priority_skips', 0)} track-priority skips · {counts['delay_risk']} delay-sensitive</strong></p>
<p class="small">{e(projection['selection_mode'])} selection · T−{projection['target_minutes']+projection['window_minutes']} to T−{projection['target_minutes']-projection['window_minutes']} arming window · {projection['poll_seconds']}s model ticks. Red = projected timing block; amber = overlapping windows or delay risk.</p>
{warnings}<details data-poll-key="emitter-sequence"><summary>Projected Australian sequence ({len(projection.get('sequence', []))})</summary><ol>{sequence or '<li>No projected targets.</li>'}</ol></details>
<p class="small">Forecast only. {e(projection['scope'])} {e(projection['assumption'])} The three-minute delay scenario is a sensitivity check, not the maximum possible delay.</p></div>'''


def panel(data, *, blackbook_only=False, sort_by='price'):
    e = lambda x: html.escape(str(x if x is not None else '—'), quote=True)
    options = '<option value="">All meetings</option>' + ''.join(
        f'<option value="{e(m["meeting_id"])}" {"selected" if data["meeting_filter"] == m["meeting_id"] else ""}>{e(m.get("track_label", m["track"]))}</option>' for m in data['meetings'])
    views = ''.join(f'<option value="{key}" {"selected" if data["view"] == key else ""}>{label}</option>' for key,label in [('all','All races'),('upcoming','Upcoming'),('clashes','Timing clashes'),('review','Review / removed'),('completed','Completed / closed')])
    groups = []
    for meeting in data['meetings']:
        races = [r for r in data['races'] if r['meeting_id'] == meeting['meeting_id']]
        if not races:
            continue
        links = []
        for row in races:
            stamp = parse_time(row['market_start_time']).astimezone(ZONE).strftime('%H:%M')
            status = row['race_state'].replace('_',' ').title()
            choice = row['selection_status'].replace('_',' ').title()
            reason = row.get('clash_skip', {}).get('reason') if row.get('clash_skip') else row.get('human_reason')
            winners = ', '.join(str(w.get('runner_name') or w['selection_id']) for w in row.get('winners') or [])
            detail = (' · Winner: '+winners) if winners else ''
            plan = row.get('emitter_forecast') or {}
            forecast_label = f'Projected #{plan["sequence"]}' if plan.get('sequence') else str(plan.get('outcome', 'forecast pending')).replace('_',' ').title()
            if not row.get('forecast_fresh'):
                forecast_label = 'Cached: '+forecast_label
            paint = 'border-color:#dd7777;background:#482525;' if plan.get('outcome') == 'timing_blocked' else 'border-color:#b48c3e;background:#382e1b;' if plan.get('timing_clashes') or plan.get('delay_risk') else ''
            if plan.get('timing_clashes'):
                forecast_label += f' · {len(plan["timing_clashes"])} clash(es)'
            forecast_tip = plan.get('reason') or ''
            url = 'http://core7070:8792/today?' + urlencode({'market': row['market_id']})
            links.append(f'<a class="navbtn" style="{paint}" data-poll-key="card:{e(row["market_id"])}" href="{e(url)}" title="{e(forecast_tip or reason or status)}">{e(stamp)} · {e(row["market_name"])}<br><small>{e(status)} · {e(choice)}{e(detail)}</small><br><small>{e(forecast_label)}</small></a>')
            links.append(f'<a class="navbtn" href="/au-captures?{e(urlencode(dict(date=data["date"],market=row["market_id"] )))}">Open captures · {e(row["market_name"])}</a>')
        rank = f'{meeting["track_priority"]}. ' if meeting.get('track_priority') else ''
        groups.append(f'<div data-poll-key="meeting:{e(meeting["meeting_id"])}"><h3>{e(rank)}{e(meeting.get("track_label", meeting["track"]))}</h3><div class="strip">{"".join(links)}</div></div>')
    opened = ' open' if data['meeting_filter'] or data['view'] != 'all' else ''
    hidden = '<input type="hidden" name="blackbook" value="1">' if blackbook_only else ''
    priority_label = ' → '.join(f'{r["track"]} {r["state"]}' for r in data.get('track_priority', {}).get('tracks', []))
    return f'''<section class="volume-panel" data-poll="today-card"><details data-poll-key="today-card-disclosure"{opened}>
<summary style="cursor:pointer;font-weight:650">Today's Australian card · {e(data['date'])} · {data['meeting_count']} meetings · {data['race_count']} races</summary>
<p><a class="navbtn" href="http://core7070:8792/import-ra">Import Racing Australia pages</a> <span class="small">Upload saved Acceptances HTML, preview matches, then import ratings.</span></p>
<p class="small">Track priority: {e(priority_label or 'First jump order')}. Other meetings follow. {e(data.get('track_priority_error') or '')}</p>
<p class="small">{e(data['freshness'])} · {e(data['message'])}. Melbourne times. Discovery: {e(data['discovery_updated_at'])} · Status checked: {e(data['status_checked_at'])}</p>
{forecast_panel(data.get('emitter_forecast', {}))}
<form method="get" class="filters" data-poll-form>{hidden}<input type="hidden" name="sort" value="{e(sort_by)}"><label>Meeting <select name="card_meeting" data-poll-options>{options}</select></label><label>View <select name="card_view">{views}</select></label><button>Apply</button></form>
{''.join(groups) or '<p>No races in this view.</p>'}
<p class="small">Click a race to view or change its shared queue decision. Closed races retained since collection began; earlier closed markets may be absent. The worldwide arming policy is unchanged.</p>
</details></section>'''


def main():
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'botlab/totebot/state')
    parser.add_argument('--secrets', type=Path, default=Path(os.environ.get('BETFAIR_SECRETS_FILE','/opt/betfair/secrets.env')))
    args = parser.parse_args()
    card = refresh(args.state_dir, args.secrets)
    print(json.dumps({k: card.get(k) for k in ['date','discovery_updated_at','status_checked_at','discovery_error','status_error']}))
    return 1 if card.get('discovery_error') or card.get('status_error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
