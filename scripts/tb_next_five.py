"""Read-only next five Australian races, with descriptive runner shortlists."""
from datetime import datetime, timezone
import html
import json
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from tb_au_snapshot_analysis import analyse_race, mapping
from tb_queue_summary import parse_time, read_state
from tb_today_card import summary as card_summary

ZONE = ZoneInfo('Australia/Melbourne')


def summary(base, *, now=None):
    base = Path(base)
    now = now or datetime.now(timezone.utc)
    try:
        card = card_summary(base/'state', now=now)
    except (TypeError, ValueError, AttributeError, KeyError):
        card = {'freshness': 'unavailable', 'races': [], 'message': 'Australian card invalid'}
    raw = read_state(base/'state/tb_today_card.json') or {}
    markets = {str(r.get('market_id')): r for r in raw.get('races', []) if isinstance(r, dict)} if isinstance(raw.get('races'), list) else {}
    observer = read_state(base/'state/tb_au_observations.json') or {}
    seen = parse_time(observer.get('updated_at'))
    observer_fresh = bool(seen and 0 <= (now-seen).total_seconds() <= 90 and not observer.get('error'))
    day = now.astimezone(ZONE).date().isoformat()
    forms = mapping(mapping((read_state(base/'state/tb_ra_form.json') or {}).get('days')).get(day))
    races = []
    for row in card['races']:
        market = markets.get(row['market_id'], {})
        start = parse_time(row.get('market_start_time'))
        if (not market or row.get('country_code') != 'AU' or not start or start <= now
                or market.get('inplay') is True or market.get('market_status') == 'CLOSED'):
            continue
        races.append(row)
    races.sort(key=lambda r: (parse_time(r['market_start_time']), r['market_id']))
    selected = []
    for race in races[:5]:
        mid = race['market_id']
        analysis = analyse_race(base, day, mid, markets[mid], now=now, forms=mapping(forms.get(mid)))
        selected.append({**race, **analysis, 'capture_url': '/au-captures?'+urlencode({'date': day, 'market': mid})})
    return {'schema': 'tb_next_five/v1', 'date': day, 'generated_at': now.isoformat(),
            'freshness': card['freshness'], 'message': card['message'],
            'discovery_updated_at': card.get('discovery_updated_at'),
            'status_checked_at': card.get('status_checked_at'),
            'observer_fresh': observer_fresh, 'observer_updated_at': observer.get('updated_at'),
            'observer_error': observer.get('error'), 'races': selected}


def escape(value):
    return html.escape(str(value if value is not None else '—'), quote=True)


def formatted(value, places=1, suffix=''):
    return '—' if value is None else f'{value:.{places}f}{suffix}'


def render(data):
    rows = []
    for race in data['races']:
        start = parse_time(race['market_start_time']).astimezone(ZONE).strftime('%H:%M')
        label = f'{race.get("track")} · {race.get("market_name")} · {start}'
        flags = [race['message'], race.get('race_state'), 'Queue: '+str(race.get('selection_status'))]
        if race.get('is_market_data_delayed'):
            flags.append('Delayed feed')
        timing = ''.join('<li>'+escape(t['slot'])+' · '+
                         (escape(t['captured_at'])+' · late '+escape(t['late_seconds'])+'s' if t['available'] else escape(t['reason']))+
                         ' · due '+escape(t['due_at'])+
                         (' · Delayed feed' if t['delayed_feed'] is True else ' · Feed delay unknown' if t['delayed_feed'] is None else '')+'</li>'
                         for t in race['timing'])
        rows.append(f'<tr class="race" data-market="{escape(race["market_id"])}"><th colspan="8"><a href="{escape(race["capture_url"])}">{escape(label)}</a>'
                    f'<small>{escape(" · ".join(str(f) for f in flags if f))} · Chance reference: {escape(race["reference_slot"])}</small>'
                    f'<details><summary>Snapshot times and gaps</summary><ul>{timing}</ul></details></th></tr>')
        for runner in race['runners']:
            latest = f'{runner["latest_price"]:g} – {runner["latest_slot"]}'
            quality = ('Partial · ' if runner['partial'] else '')+f'{runner["captured"]}/5'
            if runner['delayed_feed']:
                quality += ' · Delayed feed'
            rows.append('<tr class="qualifier">'+''.join(f'<td>{escape(v)}</td>' for v in (
                runner['horse_name'], formatted(runner['base']), formatted(runner['chance'], suffix='%'),
                formatted(runner['fair_odds'], 2), formatted(runner['move'], suffix='%'),
                runner['class'], latest, quality))+'</tr>')
        if not race['runners']:
            rows.append('<tr><td colspan="8">'+escape(race['message'])+' · Runner values —</td></tr>')
    content = ''.join(rows) or '<tr><td colspan="8">No upcoming Australian races available on today’s card.</td></tr>'
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TB / Next five Australian races</title>
<style>body{font:16px system-ui;background:#101820;color:#e6edf3;margin:24px}a{color:#8ecaff}header{display:flex;gap:24px;align-items:center;flex-wrap:wrap}h1{font-size:24px}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;border-bottom:1px solid #405265;white-space:nowrap}.scroll{overflow:auto}.race{background:#1d2b38}.qualifier td:first-child{color:#ffd27a;box-shadow:inset 4px 0 #ffd27a}small{display:block;color:#a9b8c7;margin-top:5px}details{font-weight:normal;margin:8px 0}.warning{color:#ffd27a}button{padding:8px;background:#1d2b38;color:#e6edf3;border:1px solid #405265;border-radius:5px}li{white-space:normal}#status{min-height:1.5em}</style></head><body>
<header><h1>Next five Australian races</h1><nav><a href="/">TB Wall / Queue</a> · <a href="/au-captures">Australian Capture Wall</a></nav><button onclick="location.reload()">Reload</button><label><input id="refresh" type="checkbox" checked> Refresh every 10 seconds</label></header>
<p id="status" role="status"></p><main id="next-five-content">'''+fragment(data, content)+'''</main>
<script>
let busy=false;
setInterval(async()=>{
 if(busy||document.hidden||!document.getElementById('refresh').checked)return;
 busy=true;
 try{
  const response=await fetch('/next-five',{cache:'no-store',signal:AbortSignal.timeout(8000)});
  if(!response.ok)throw Error('unavailable');
  const doc=new DOMParser().parseFromString(await response.text(),'text/html');
  const replacement=doc.getElementById('next-five-content');
  if(!replacement)throw Error('invalid');
  const open=new Set([...document.querySelectorAll('tr[data-market] details[open]')].map(d=>d.closest('tr').dataset.market));
  replacement.querySelectorAll('tr[data-market] details').forEach(d=>d.open=open.has(d.closest('tr').dataset.market));
  replacement.querySelector('details').open=document.querySelector('#next-five-content > details').open;
  document.getElementById('next-five-content').replaceWith(replacement);
  document.getElementById('status').textContent='';
 }catch(error){document.getElementById('status').textContent='Refresh failed — showing cached information. Last successful refresh time is below.';}
 finally{busy=false;}
},10000);
</script></body></html>'''


def fragment(data, content):
    e = escape
    return f'''<p class="warning">{e(data['message'])} · {e(data['freshness'])}. Observer: {'current' if data['observer_fresh'] else 'stale / unavailable'} · {e(data['observer_error'])}</p>
<p>Updated {e(data['generated_at'])} · Discovery {e(data['discovery_updated_at'])} · Status {e(data['status_checked_at'])} · Observer {e(data['observer_updated_at'])}</p>
<details><summary>How the shortlist works</summary><p>Races follow scheduled Melbourne times, irrespective of queue Keep/Remove decisions. Up to three runners qualify through a clear firm, drift or reversal using adjacent saved snapshots. Stable, mixed and scratching-broken histories do not qualify. Rank uses more captured prices first, then greater price range as a percentage of the first price; selection ID breaks ties. Partial shapes can change as new snapshots arrive. These are observations, not winner predictions.</p>
<p>Base is the imported mean historical handicap rating, shown only when the form was imported before assessment and scheduled start. Chance is market-implied probability normalized to 100% from one complete snapshot; Fair odds is 100 / Chance. It is not a Base-derived prediction. Move is first-to-latest price change; negative means firming. Firm then rebound means firm then drift. Classification uses the capture wall’s configured threshold (default 3%). Missing data is —. All shortlisted runners have a price at the same latest market snapshot; Latest is a saved last-traded price, whose trade age is unknown. Delayed-feed flags and actual received times appear in snapshot details.</p></details>
<div class="scroll"><table><thead><tr><th>Horse name</th><th>Base</th><th>Chance</th><th>Fair odds</th><th>Move</th><th>Class</th><th>Latest</th><th>Coverage / feed</th></tr></thead><tbody>{content}</tbody></table></div>'''


def response(base, path, query=None):
    data = summary(base)
    if path == '/api/next-five':
        return json.dumps(data), 'application/json; charset=utf-8', 503 if data['freshness'] == 'unavailable' else 200
    # Keep the page and refresh control available during transient state failures.
    return render(data), 'text/html; charset=utf-8', 200
