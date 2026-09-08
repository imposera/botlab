"""Read-only blackbook enrichment for the wall, with race-time cutoffs."""
from datetime import datetime, timezone
from functools import lru_cache
import html
from pathlib import Path
from threading import Lock
from urllib.parse import quote

from tb_blackbook import STATE_FILE_NAME, distance_metres, normalise_name, read_json


class RegisterCache:
    def __init__(self, path):
        self.path = path
        self.lock = Lock()
        self.signature = None
        self.payload = {}
        self.by_id = {}
        self.by_name = {}

    def get(self):
        with self.lock:
            try:
                stat = self.path.stat()
                signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            except OSError:
                signature = None
            if signature != self.signature:
                payload = read_json(self.path) or {}
                entries = payload.get('entries')
                self.payload = payload if isinstance(entries, list) else {}
                self.by_id, self.by_name = {}, {}
                for entry in entries if isinstance(entries, list) else []:
                    if not isinstance(entry, dict):
                        continue
                    ids = entry.get('selection_ids') or []
                    ids = list(ids) if isinstance(ids, list) else []
                    key = str(entry.get('key') or '')
                    if key.startswith('sid:'):
                        ids.append(key[4:])
                    for sid in set(str(sid) for sid in ids):
                        self.by_id.setdefault(sid, []).append(entry)
                    name = normalise_name(entry.get('runner_name'))
                    if name:
                        self.by_name.setdefault(name, []).append(entry)
                self.signature = signature
            return self.payload, self.by_id, self.by_name


@lru_cache(maxsize=8)
def register_cache(base_dir: Path):
    return RegisterCache(base_dir / 'state' / STATE_FILE_NAME)


def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def enrich_rows(rows, market, base_dir):
    payload, by_id, by_name = register_cache(base_dir.resolve()).get()
    cutoff = timestamp(market.get('market_start_time'))
    track = normalise_name(market.get('track'))
    distance = distance_metres(market.get('market_name'))
    for row in rows:
        sid = str(row.get('selection_id') or '')
        matches = by_id.get(sid, []) if sid else []
        if not matches:
            candidates = by_name.get(normalise_name(row.get('name')), [])
            # Names may only substitute for missing IDs, never conflicting IDs.
            matches = [e for e in candidates if not sid or
                       (not e.get('selection_ids') and not str(e.get('key', '')).startswith('sid:'))]
            if len(candidates) != 1:
                matches = []
        entry = matches[0] if len(matches) == 1 else {}
        wins = []
        for win in entry.get('wins', []) or []:
            if not isinstance(win, dict) or not cutoff:
                continue
            if str(win.get('market_id')) == str(market.get('market_id')):
                continue
            when = timestamp(win.get('market_start_time'))
            # Date-only records are safe only on an earlier calendar day.
            day = timestamp(win.get('date'))
            prior = when < cutoff if when else day is not None and day.date() < cutoff.date()
            if prior:
                wins.append(win)
        wins.sort(key=lambda w: timestamp(w.get('market_start_time')) or timestamp(w.get('date')) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        same_track = lambda w: bool(track) and normalise_name(w.get('track')) == track
        same_distance = lambda w: distance is not None and distance_metres(w.get('market_name')) == distance
        row['blackbook'] = {
            'wins': wins, 'w': len(wins),
            't': sum(same_track(w) for w in wins) if track else None,
            'd': sum(same_distance(w) for w in wins) if distance is not None else None,
            'td': sum(same_track(w) and same_distance(w) for w in wins) if track and distance is not None else None,
            'tags': entry.get('tags') if isinstance(entry.get('tags'), list) else [],
            'notes': str(entry.get('notes') or ''),
            'status': str(entry.get('status') or ''),
            'available': bool(payload) and cutoff is not None,
        }
    return payload


def cell(record):
    esc = lambda value: html.escape(str(value))
    labels = ' · '.join(f'{label} {record[key] if record[key] is not None else "—"}'
                        for label, key in [('W', 'w'), ('T', 't'), ('D', 'd'), ('T+D', 'td')])
    if not record['available']:
        labels = 'History unavailable'
    if record['tags']:
        labels = '★ ' + labels
    details = []
    if record['tags'] or record['notes'] or record['status'] not in ('', 'auto-winner'):
        details.append('<p>Current manual metadata: ' + esc(', '.join(str(t) for t in record['tags'])) +
                       ' · ' + esc(record['status']) + '<br>' + esc(record['notes']) + '</p>')
    for win in record['wins']:
        mid = quote(str(win.get('market_id') or ''), safe='')
        prices = win.get('prices') if isinstance(win.get('prices'), dict) else {}
        path = ' → '.join(f'{stage.upper()}: {prices[stage]}' for stage in ('t15', 't10', 't5', 't2', 't30') if stage in prices)
        distance = distance_metres(win.get('market_name'))
        details.append(f'<li><a href="/race/{mid}">{esc(win.get("date"))} · {esc(win.get("track"))} · {esc(win.get("market_name"))}</a>'
                       f'<br>{esc(win.get("country") or "Country unknown")} · {esc(f"{distance:g} m" if distance is not None else "Distance unknown")} · {esc(path or "Prices unavailable")}'
                       + matched_table(win) + '</li>')
    metadata = ''.join(d for d in details if d.startswith('<p>'))
    history = ''.join(d for d in details if d.startswith('<li>'))
    if not history:
        history = '<li>No earlier recorded wins.</li>' if record['available'] else '<li>Register or race start time unavailable.</li>'
    return '<details><summary>' + labels + '</summary>' + metadata + '<ul>' + history + '</ul></details>'


def matched_summary(win):
    marks = win.get('matched') or {}
    for stage in ('t30', 't2', 't5', 't10', 't15'):
        mark = marks.get(stage) or {}
        if mark.get('runner_matched') is not None and mark.get('market_matched') is not None:
            return matched_label(stage) + ': ' + matched_amount(mark['runner_matched']) + ' / ' + matched_amount(mark['market_matched']) + ' · ' + matched_share(mark.get('share_pct'))
    return 'Unavailable'


def matched_label(stage):
    return 'T−30s' if stage == 't30' else 'T−' + stage[1:] + 'm'


def matched_amount(value):
    return '—' if value is None else f'{value:,.2f}'


def matched_share(value):
    return '—' if value is None else f'{value:.1f}%'


def matched_table(win):
    marks = win.get('matched') or {}
    if not marks:
        return '<p>Matched amounts unavailable.</p>'
    rows = []
    for stage in ('t15', 't10', 't5', 't2', 't30'):
        if stage not in marks:
            continue
        mark = marks[stage]
        unit = str(mark.get('currency') or 'Currency unspecified')
        if mark.get('delayed'):
            unit += ' · delayed feed'
        rows.append('<tr><td>' + matched_label(stage) + '</td><td>' +
                    matched_amount(mark.get('runner_matched')) + '</td><td>' +
                    matched_amount(mark.get('market_matched')) + '</td><td>' +
                    matched_share(mark.get('share_pct')) + '</td><td>' + html.escape(unit) + '</td></tr>')
    return ('<p>Runner vs market matched · same capture</p><table><thead><tr><th>Stage</th>'
            '<th>Runner</th><th>Market</th><th>Share</th><th>Currency / feed</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table>')
