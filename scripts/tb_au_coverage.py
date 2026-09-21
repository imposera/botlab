"""Read-only coverage summary for independent Australian race observations."""
from collections import Counter
from datetime import datetime, timezone
import html
from pathlib import Path
from zoneinfo import ZoneInfo
from tb_au_capture_wall import url as capture_url
from tb_queue_summary import age, read_state

FILE = 'tb_au_observations.json'


def summary(state_dir, *, now=None):
    now = now or datetime.now(timezone.utc)
    state_dir = Path(state_dir)
    saved = read_state(state_dir/FILE)
    card = read_state(state_dir/'tb_today_card.json') or {}
    day = now.astimezone(ZoneInfo('Australia/Melbourne')).date().isoformat()
    seconds = age((saved or {}).get('updated_at'), now)
    discovery_age = age(card.get('discovery_updated_at'), now)
    fresh = saved is not None and seconds is not None and -5 <= seconds <= 90
    registry_fresh = card.get('date') == day and discovery_age is not None and -5 <= discovery_age <= 360 and not card.get('discovery_error')
    races = []
    jobs = (saved or {}).get('races', {})
    for market in card.get('races', []) if card.get('date') == day else []:
        if market.get('country_code') != 'AU':
            continue
        job = jobs.get(str(market['market_id']), {})
        slots = list(job.get('slots', {}).values())
        counts = Counter(s.get('status') for s in slots)
        if not slots:
            status = 'unavailable'
        elif counts['missed']:
            status = 'incomplete'
        elif counts['captured'] == len(slots):
            status = 'complete'
        elif any(s.get('last_error') for s in slots if s.get('status') == 'pending'):
            status = 'unavailable'
        elif counts['captured']:
            status = 'observing'
        else:
            status = 'scheduled'
        races.append({**{k: market.get(k) for k in ('market_id','track','market_name','market_start_time')},
                      'status': status, 'slots': job.get('slots', {}), 'counts': dict(counts),
                      'delayed_feed': any(s.get('delayed_feed') is True for s in slots),
                      'reason': 'Awaiting observer registration' if not slots else None})
    counts = {k: 0 for k in ('scheduled','observing','complete','incomplete','unavailable')}
    counts.update(Counter(r['status'] for r in races))
    return {'schema':'tb_au_coverage/v1', 'date':day,
            'freshness':'unavailable' if saved is None else 'fresh' if fresh else 'stale',
            'updated_at':(saved or {}).get('updated_at'), 'age_seconds':seconds,
            'registry_fresh':registry_fresh, 'registry_error':card.get('discovery_error'),
            'discovered':len(races), 'counts':counts, 'races':races,
            'observer_error':(saved or {}).get('error'),
            'scope':'Australian WIN thoroughbred races published in the daily registry; independent of target priority and queue decisions',
            'timing':'Scheduled-start snapshots at T15, T10, T5, T2 and T30 seconds; up to 60 seconds late, never after scheduled start or confirmed in-play'}


def panel(data):
    e = lambda value: html.escape(str(value))
    counts = data['counts']
    lines = []
    for race in data['races']:
        gaps = (['Betfair reports delayed data'] if race.get('delayed_feed') else []) + [f"{name}: {slot.get('reason') or slot.get('last_error')}" for name,slot in race['slots'].items()
                if slot.get('reason') or slot.get('last_error')]
        link = e(capture_url(date=data['date'], market=race['market_id']))
        lines.append(f'<tr><td><a href="{link}">{e(race["track"])} · {e(race["market_name"])}</a></td><td>{e(race["status"])}</td><td>{race["counts"].get("captured",0)}/5</td><td>{e("; ".join(gaps) or race.get("reason") or "—")}</td></tr>')
    return f'''<section data-poll="au-coverage"><details data-poll-key="au-coverage-disclosure">
<summary>Australian coverage: {data['discovered']} races · {counts['complete']} complete · {counts['incomplete']} incomplete · {counts['unavailable']} unavailable</summary>
<p><a href="/au-captures">Open Australian Capture Wall</a></p>
<p class="small">Observer: {e(data['freshness'])} · Registry: {'fresh' if data['registry_fresh'] else 'stale / unavailable'}. Every Australian race is observed independently of track priority. {e(data.get('observer_error') or '')}</p>
<p class="small">{e(data['timing'])}. Captures from before this service started cannot be recovered.</p>
<div class="table-scroll"><table><thead><tr><th>Race</th><th>Coverage</th><th>Captured</th><th>Gaps</th></tr></thead><tbody>{''.join(lines)}</tbody></table></div>
</details></section>'''
