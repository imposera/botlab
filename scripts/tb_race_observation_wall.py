"""Presentation for separately stored rolling race observations."""
import html
from datetime import datetime, timezone

from tb_blackbook import price_from_runner, read_json
from tb_race_lifecycle import evaluate, load_lifecycle, observation_dir, policy, select_offsets, stamp


def timing_label(base, market, now=None):
    now = now or datetime.now(timezone.utc)
    lifecycle = load_lifecycle(base, market)
    if not lifecycle:
        return None
    decision = evaluate(market, lifecycle, now, policy(base))
    state = decision['state']
    if state == 'delayed':
        seconds = decision['delay_seconds']
        return f'Delayed +{seconds // 60}m{seconds % 60:02d}s'
    return {'preplay': 'Pre-play', 'suspended': 'Suspended · start unconfirmed',
            'unknown': 'Status unknown · stale feed', 'observed_start': 'Start observed',
            'closed': 'Closed', 'timed_out': 'Observation timed out'}.get(state, state)


def panel(base, market, live=True, now=None):
    now = now or datetime.now(timezone.utc)
    directory = observation_dir(base, market)
    if directory is None:
        return ''
    lifecycle = load_lifecycle(base, market)
    if not lifecycle:
        return ''
    esc = lambda value: html.escape(str(value))
    label = timing_label(base, market, now)
    seen = stamp(lifecycle.get('last_received_at'))
    age = max(0, int((now - seen).total_seconds())) if seen else None
    freshness = f'{age}s old' if age is not None else 'unavailable'
    if live and (age is None or age > policy(base)['fresh_seconds'] or lifecycle.get('feed_error')):
        freshness += ' · stale'
    events = lifecycle.get('events', [])
    event_html = ''.join(f'<li>Runner {esc(e.get("selection_id"))} removed · observed {esc(e.get("observed_at"))} · price-series break</li>' for e in events)
    rolling = read_json(directory / 'rolling.json') or {}
    books = rolling.get('captures', [])
    header = ('<section class="volume-panel"><h2>Race observation · ' + esc(label) + '</h2>'
              f'<p>Scheduled: {esc(market.get("market_start_time"))} · Feed observation: {esc(freshness)}'
              f'{" · delayed feed" if lifecycle.get("delayed_feed") else ""}</p>')
    if lifecycle.get('observed_start_at'):
        header += f'<p>Start first observed: {esc(lifecycle["observed_start_at"])}. Receive-time estimate, not an official off time.</p>'
    if not books:
        return header + '<p>No rolling pre-play captures available.</p><ul>' + event_html + '</ul></section>'
    latest = books[-1]
    anchor = stamp(latest['captured_at'])
    selected = select_offsets(books, anchor, policy(base)['poll_seconds'] * 2)
    latest_prices = {str(r.get('selection_id')): r for r in latest.get('runners', [])}
    removed = {str(e.get('selection_id')) for e in events}
    starts = [stamp(b['captured_at']) for b in selected.values()]
    # A removal can change odds for every remaining runner through reduction factors.
    broken = any(stamp(e.get('observed_at')) and min(starts or [anchor]) <= stamp(e['observed_at']) <= anchor for e in events)
    def price(book, sid):
        for runner in book.get('runners', []):
            if str(runner.get('selection_id')) == sid:
                return price_from_runner(runner)
        return None
    def fmt(value):
        return '—' if value is None else f'{value:g}'
    rows = []
    for sid, runner in latest_prices.items():
        current = price_from_runner(runner)
        first = price(selected.get('t15', {}), sid)
        move = 'Scratching break' if broken or sid in removed else f'{(current-first)/first*100:+.1f}%' if first and current else '—'
        cells = ''.join(f'<td>{fmt(price(selected.get(stage, {}), sid))}</td>' for stage in ('t15','t10','t5','t2','t30'))
        rows.append(f'<tr><td class="horse">{esc(runner.get("runner_name") or sid)}</td>{cells}<td>{fmt(current)}</td><td>{move}</td></tr>')
    return (header + f'<p>Rolling lookbacks from {esc(latest["captured_at"])} · market matched {fmt(latest.get("market", {}).get("total_matched"))}.</p>'
            '<p class="small">These are elapsed lookbacks, not time to jump. Scheduled volume benchmarks remain separate. Gaps mean no capture near that offset.</p>'
            '<details><summary>Rolling prices · last 15 minutes</summary><div class="table-scroll"><table><thead><tr><th>Runner</th><th>15m ago</th><th>10m ago</th><th>5m ago</th><th>2m ago</th><th>30s ago</th><th>Latest capture</th><th>15m change</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div></details><ul>' + event_html + '</ul></section>')
