"""Searchable read-only view of the complete winner register."""
import html
from urllib.parse import urlencode

from tb_blackbook import normalise_name, race_sort_key
from tb_blackbook_wall import cell, register_cache, matched_summary


def page(base_dir, query):
    payload, _, _ = register_cache(base_dir.resolve()).get()
    esc = lambda value: html.escape(str(value), quote=True)
    get = lambda key, default='': query.get(key, [default])[0]
    def integer(key, default, floor=1):
        try:
            return max(floor, int(get(key, str(default))))
        except (ValueError, TypeError):
            return default
    search = get('q').strip()
    track = get('track')
    country = get('country')
    tagged = get('tagged') == '1'
    minimum = integer('min_wins', 0, 0)
    sort = get('sort', 'recent')
    entries = payload.get('entries', [])
    tracks = sorted({str(t) for e in entries for t in e.get('tracks_seen', [])})
    countries = sorted({w.get('country') or 'Unknown' for e in entries for w in e.get('wins', [])})
    selected = []
    for entry in entries:
        haystack = ' '.join(str(entry.get(k) or '') for k in
                            ('runner_name', 'key', 'tags', 'notes', 'status'))
        if normalise_name(search) not in normalise_name(haystack):
            continue
        if track and track not in entry.get('tracks_seen', []):
            continue
        if country and not any((w.get('country') or 'Unknown') == country for w in entry.get('wins', [])):
            continue
        if tagged and not entry.get('tags'):
            continue
        if len(entry.get('wins', [])) < minimum:
            continue
        selected.append(entry)
    if sort == 'name':
        selected.sort(key=lambda e: normalise_name(e.get('runner_name')))
    elif sort == 'wins':
        selected.sort(key=lambda e: (-len(e.get('wins', [])), normalise_name(e.get('runner_name'))))
    else:
        sort = 'recent'
        selected.sort(key=lambda e: race_sort_key(e.get('last_win') or {}), reverse=True)
    pages = max(1, (len(selected) + 49) // 50)
    current = min(integer('page', 1), pages)
    rows = []
    for entry in selected[(current - 1) * 50:current * 50]:
        wins = sorted(entry.get('wins', []), key=race_sort_key, reverse=True)
        record = dict(wins=wins, w=len(wins), t=None, d=None, td=None,
                      tags=entry.get('tags') or [], notes=entry.get('notes') or '',
                      status=entry.get('status') or '', available=True)
        last = entry.get('last_win') or {}
        rows.append(f'<tr><td><strong>{esc(entry.get("runner_name") or entry.get("key"))}</strong>'
                    f'<br><small>{esc(entry.get("key"))}</small></td><td>{len(wins)}</td>'
                    f'<td>{esc(last.get("date") or "—")}<br>{esc(last.get("track") or "—")}</td>'
                    f'<td>{esc(last.get("country") or "Unknown")}</td>'
                    f'<td>{esc(matched_summary(last))}</td>'
                    f'<td>{cell(record)}</td></tr>')
    def link(number, label):
        params = dict(q=search, track=track, country=country, tagged='1' if tagged else '',
                      min_wins=minimum, sort=sort, page=number)
        return f'<a href="/blackbook?{esc(urlencode(params))}">{label}</a>'
    navigation = (link(current - 1, '← Previous') if current > 1 else '')
    navigation += f' <span>Page {current} of {pages}</span> '
    navigation += link(current + 1, 'Next →') if current < pages else ''
    track_options = ''.join(f'<option value="{esc(t)}" {"selected" if t == track else ""}>{esc(t)}</option>' for t in tracks)
    country_options = ''.join(f'<option value="{esc(c)}" {"selected" if c == country else ""}>{esc(c)}</option>' for c in countries)
    sort_options = ''.join(f'<option value="{key}" {"selected" if key == sort else ""}>{label}</option>'
                           for key, label in [('recent', 'Latest win'), ('wins', 'Most wins'), ('name', 'Runner name')])
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ToteBot Blackbook</title><style>
body{{background:#101722;color:#e6edf5;font:16px system-ui;margin:24px auto;padding:0 18px;max-width:1200px}}
a{{color:#9fcaff}}h1{{margin-bottom:8px}}small,.muted{{color:#aab8ca}}
form{{display:flex;flex-wrap:wrap;gap:14px;align-items:end;background:#1a2535;padding:18px;border-radius:10px}}
label{{display:flex;flex-direction:column;gap:5px}}input,select,button{{font:inherit;padding:8px;background:#101722;color:#e6edf5;border:1px solid #526176;border-radius:5px}}
button{{cursor:pointer;background:#234875}}table{{width:100%;border-collapse:collapse;margin:20px 0}}td,th{{text-align:left;padding:14px 10px;border-bottom:1px solid #334155;vertical-align:top}}
summary{{cursor:pointer}}li{{margin:12px 0}}.table{{overflow-x:auto}}nav{{margin:20px 0;display:flex;gap:20px}}
</style></head><body><a href="/">← Race wall</a><h1>ToteBot Blackbook</h1>
<p>{len(entries):,} runners · {sum(len(e.get('wins', [])) for e in entries):,} recorded wins</p>
<p class="muted">{'Register updated ' + esc(payload.get('generated_at', 'unknown')) if payload else 'Register unavailable'} · Refresh service runs every five minutes. Reload this page for updates.</p>
<form method="get" action="/blackbook">
<label>Search runner, ID, tags or notes<input name="q" value="{esc(search)}" type="search"></label>
<label>Track<select name="track"><option value="">All tracks</option>{track_options}</select></label>
<label>Country<select name="country"><option value="">All countries</option>{country_options}</select></label>
<label>Minimum wins<input name="min_wins" type="number" min="0" value="{minimum}" style="width:80px"></label>
<label>Sort<select name="sort">{sort_options}</select></label>
<label><span>Tagged only</span><input type="checkbox" name="tagged" value="1" {'checked' if tagged else ''}></label>
<button>Apply</button><a href="/blackbook">Reset</a></form>
<p>{len(selected):,} matching runners. Counts cover recorded wins only; expand history for prices and race links. T/D counts apply on individual race pages.</p>
<p class="muted">Runner / market matched shows the latest available paired capture for the last win, with runner share of that market total. Expand history for every stage. Amounts are in the source currency (unspecified where absent); — means unavailable.</p>
<p class="muted">Country is the race location. The filter matches any recorded win; the country column refers to the last win.</p>
<nav>{navigation}</nav><div class="table"><table><thead><tr><th>Runner</th><th>Wins</th><th>Last win</th><th>Country</th><th>Runner / market matched</th><th>History and notes</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="6">No matching runners.</td></tr>'}</tbody></table></div><nav>{navigation}</nav>
</body></html>'''
