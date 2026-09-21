"""Read-only queue summary and HTML panel; consumes saved policy, never trains it."""
from datetime import datetime, timezone
import html
import json
from pathlib import Path
from zoneinfo import ZoneInfo

STALE_SECONDS = 120


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def read_state(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def age(value, now):
    when = parse_time(value)
    return int((now - when).total_seconds()) if when else None


def freshness(seconds):
    if seconds is None or seconds < -5:
        return 'unknown'
    return 'stale' if seconds > STALE_SECONDS else 'fresh'


def summary(state_dir: Path, *, now=None, limit=5):
    now = now or datetime.now(timezone.utc)
    state_dir = Path(state_dir)
    target = read_state(state_dir / 'betfair_t15_target.json') or {}
    armed = None
    if target.get('status') == 'armed' and isinstance(target.get('market'), dict):
        market = target['market']
        target_age = age(target.get('updated_at'), now)
        armed = {k: market.get(k) for k in ('market_id', 'track', 'market_name', 'market_start_time')}
        armed.update(updated_at=target.get('updated_at'), age_seconds=target_age,
                     freshness=freshness(target_age), status='armed')
    result = {'schema': 'tb_queue_summary/v1', 'generated_at': now.isoformat(),
              'queue_updated_at': None, 'age_seconds': None, 'freshness': 'unavailable',
              'ok': False, 'armed': armed, 'next_eligible': None, 'eligible': [],
              'excluded': [], 'counts': {'eligible': 0, 'clash_skip': 0, 'removed': 0, 'unavailable': 0},
              'limit': max(1, min(int(limit), 10))}
    queue = read_state(state_dir / 'tb_race_queue.json')
    if not queue or not isinstance(queue.get('races'), list):
        return {**result, 'message': 'Queue unavailable'}
    result['queue_updated_at'] = queue.get('updated_at')
    result['age_seconds'] = age(queue.get('updated_at'), now)
    result['freshness'] = freshness(result['age_seconds'])
    result['ok'] = result['freshness'] == 'fresh'
    eligible, excluded = [], []
    for row in queue['races']:
        if not isinstance(row, dict) or not row.get('market_id') or not parse_time(row.get('market_start_time')):
            return {**result, 'ok': False, 'freshness': 'unavailable', 'message': 'Invalid queue rows'}
        seconds = int((parse_time(row['market_start_time']) - now).total_seconds())
        if seconds <= 0 or (armed and str(row['market_id']) == str(armed['market_id'])):
            continue
        clash = row.get('clash_skip') if isinstance(row.get('clash_skip'), dict) else None
        human = row.get('human_action')
        if human == 'remove':
            status, reason = 'removed', row.get('human_reason') or 'Explicit Remove'
        elif clash:
            status, reason = 'clash_skip', clash.get('reason') or 'Learned clash preference'
        elif row.get('arming_eligible', row.get('eligible')) is True:
            status, reason = 'eligible', 'Eligible for automatic selection; not an arming commitment'
        else:
            status, reason = 'unavailable', 'Arming eligibility unavailable'
        item = {k: row.get(k) for k in ('market_id', 'track', 'market_name', 'country_code', 'market_start_time')}
        item.update(seconds_to_jump=seconds, status=status, reason=reason, human_action=human,
                    alternative_market_id=clash.get('alternative_market_id') if clash else None)
        (eligible if status == 'eligible' else excluded).append(item)
        result['counts'][status] += 1
    # Preserve authoritative queue order, including deliberate future ordering changes.
    result['eligible'] = eligible[:result['limit']]
    result['excluded'] = excluded
    result['next_eligible'] = eligible[0] if eligible and result['ok'] else None
    result['message'] = 'Queue current' if result['ok'] else 'Cached queue; current selection unconfirmed'
    return result


def panel(data, manage_url='http://core7070:8792'):
    esc = lambda value: html.escape(str(value if value is not None else '—'), quote=True)
    name = lambda row: ' · '.join(str(row.get(k) or '') for k in ('track', 'market_name'))
    armed = data['armed']
    armed_text = (f"Armed target: {name(armed)} ({armed['freshness']} target state)" if armed else 'No armed target reported')
    seconds = data['age_seconds']
    age_label = f'{max(0, seconds)}s old' if seconds is not None else 'age unknown'
    rows = []
    for row in data['eligible']:
        start = parse_time(row['market_start_time']).astimezone(ZoneInfo('Australia/Melbourne'))
        countdown = f"T−{row['seconds_to_jump']//60}m {row['seconds_to_jump']%60:02d}s"
        rows.append(f'<tr data-poll-key="queue:{esc(row["market_id"])}"><td>{esc(start.strftime("%H:%M %Z"))} · {esc(countdown)}</td><td>{esc(name(row))}</td><td>{esc(row["country_code"])}</td><td>{"Eligible" if data["ok"] else "Cached eligible"}</td><td>{esc(row["human_action"] or "None")}</td></tr>')
    table = ('<div class="table-scroll"><table><thead><tr><th>Jump / Melbourne</th><th>Track / race</th><th>Country</th><th>Selection</th><th>Decision</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>') if rows else '<p>No upcoming eligible races reported.</p>'
    excluded = ''.join(f'<li data-poll-key="queue-excluded:{esc(row["market_id"])}"><strong>{esc(name(row))}</strong> · {esc(row["status"].replace("_", " ").title())}<br>{esc(row["reason"])}</li>' for row in data['excluded'])
    next_row = data['next_eligible']
    next_text = f"Next eligible: {name(next_row)}" if next_row else ('Next eligible: unconfirmed' if not data['ok'] else 'Next eligible: none')
    return f'''<section class="volume-panel queue-panel" data-poll="queue" aria-label="Next to go queue">
<details data-poll-key="queue-list"><summary style="cursor:pointer;font-weight:650">{esc(next_text)} · {len(data['eligible'])} races</summary>
<div class="top"><a class="navbtn" href="{esc(manage_url)}">Manage queue</a></div>
<p>{esc(armed_text)}</p><p><strong>{esc(next_text)}</strong></p>
<p class="small">Queue {esc(data['freshness'])} · {esc(age_label)} · {esc(data['message'])}. Showing {len(data['eligible'])} of {data['counts']['eligible']} eligible races.</p>
{table}<details><summary>Skipped / removed ({len(data['excluded'])})</summary><ul>{excluded or '<li>None</li>'}</ul></details>
<p class="small">Scheduled queue preview. The emitter selects the armed target. Decisions open in the authoritative queue wall.</p></details></section>'''
