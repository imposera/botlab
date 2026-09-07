#!/usr/bin/env python3
"""Build and display historical pre-race matched-volume comparisons.

Run `python3 tb_market_volume.py scan` after history is synced. The wall only
reads the derived register; no history scans occur during HTTP requests.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import html
import math
from pathlib import Path
from statistics import median
from threading import Lock

from tb_blackbook import atomic_write_json, normalise_name, read_json
from tb_liquidity import market_total_matched

VERSION = '1.0.0'
STATE_FILE = 'tb_market_volume.json'
STAGES = {'t15': 900, 't10': 600, 't5': 300, 't2': 120, 't30': 30}
DEFAULT_BASE = Path.home() / 'botlab' / 'totebot'


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def amount(book):
    value = market_total_matched(book)
    return value if value is not None and math.isfinite(value) and value >= 0 else None


def cohort(book):
    market = book.get('market') or {}
    # This repository's legacy capture pipeline selects WIN markets only.
    return (normalise_name(market.get('track')),
            str(market.get('market_type') or market.get('marketType') or 'WIN').upper(),
            str(market.get('currency') or book.get('currency') or 'UNSPECIFIED').upper())


def capture(book, stage):
    if not isinstance(book, dict):
        return None
    market = book.get('market') or {}
    start = parse_time(market.get('market_start_time'))
    when = parse_time(book.get('captured_at'))
    value = amount(book)
    if (not start or not when or value is None or market.get('inplay') is not False
            or book.get('inplay') is True or when >= start):
        return None
    seconds = (start - when).total_seconds()
    tolerance = 15 if stage == 't30' else 45
    if abs(seconds - STAGES[stage]) > tolerance:
        return None
    return {'stage': stage, 'amount': value, 'captured_at': when.isoformat(),
            'delayed': bool(market.get('is_market_data_delayed') or book.get('is_market_data_delayed'))}


def quantile(values, fraction):
    ordered = sorted(values)
    pos = (len(ordered) - 1) * fraction
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (pos - lower)


def stats(values):
    values = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0]
    if not values:
        return {'n': 0, 'median': None, 'q25': None, 'q75': None}
    return {'n': len(values), 'median': median(values), 'q25': quantile(values, .25), 'q75': quantile(values, .75)}


def build_register(base, now=None):
    now = now or datetime.now(timezone.utc)
    records = {}
    skipped = 0
    for folder in sorted((base / 'history').glob('*/*')):
        closed = read_json(folder / 'closed.json') or {}
        if not closed.get('complete') or not closed.get('has_result'):
            skipped += 1
            continue
        slots = {}
        identity = None
        start = None
        for stage in STAGES:
            book = read_json(folder / f'market_book_{stage}.json')
            mark = capture(book, stage)
            if not mark:
                continue
            key = cohort(book)
            current_start = parse_time(book['market'].get('market_start_time'))
            if not key[0] or (identity is not None and (key != identity or current_start != start)):
                continue
            identity, start = key, current_start
            slots[stage] = mark['amount']
        if not slots or start >= now:
            skipped += 1
            continue
        mid = str(closed.get('market_id') or folder.name)
        records[mid] = {'market_id': mid, 'start': start.isoformat(), 'cohort': list(identity), 'slots': slots}
    records = sorted(records.values(), key=lambda r: r['start'])
    groups = {}
    for record in records:
        if parse_time(record['start']) < now - timedelta(days=60):
            continue
        key = tuple(record['cohort'])
        group = groups.setdefault(key, {stage: [] for stage in STAGES})
        for stage, value in record['slots'].items():
            group[stage].append(value)
    return {'schema': 'totebot_market_volume/v1', 'version': VERSION,
            'generated_at': now.isoformat(), 'window_days': 60,
            'records': records, 'record_count': len(records), 'skipped': skipped,
            'groups': [{'cohort': list(key), 'stages': {stage: stats(values) for stage, values in group.items()}}
                       for key, group in sorted(groups.items())]}


class VolumeCache:
    def __init__(self, path):
        self.path, self.signature = path, None
        self.lock = Lock()
        self.payload, self.groups = {}, {}

    def get(self):
        with self.lock:
            try:
                st = self.path.stat()
                signature = (st.st_ino, st.st_mtime_ns, st.st_size)
            except OSError:
                signature = None
            if signature != self.signature:
                payload = read_json(self.path) or {}
                records = payload.get('records')
                groups = {}
                if isinstance(records, list):
                    for record in records:
                        if not isinstance(record, dict) or not isinstance(record.get('cohort'), list) or len(record['cohort']) != 3:
                            continue
                        start = parse_time(record.get('start'))
                        if start and isinstance(record.get('slots'), dict):
                            key = tuple(str(v) for v in record['cohort'])
                            groups.setdefault(key, []).append((start, record))
                for key, rows in groups.items():
                    rows.sort(key=lambda r: r[0])
                    groups[key] = ([r[0] for r in rows], [r[1] for r in rows])
                self.payload = payload if isinstance(records, list) else {}
                self.groups = groups
                self.signature = signature
            return self.payload, self.groups


@lru_cache(maxsize=8)
def volume_cache(base):
    return VolumeCache(base / 'state' / STATE_FILE)


def baseline(groups, identity, cutoff, market_id, stage):
    times, records = groups.get(identity, ([], []))
    left = bisect_left(times, cutoff - timedelta(days=60))
    right = bisect_left(times, cutoff)
    return stats([r['slots'].get(stage) for r in records[left:right]
                  if str(r.get('market_id')) != str(market_id)])


def comparison(value, distribution):
    middle = distribution['median']
    if value is None or middle is None or middle <= 0:
        return None, 'grey', 'Unavailable'
    ratio = value / middle * 100
    if distribution['n'] < 20:
        return ratio, 'grey', 'Provisional: fewer than 20 races'
    if value < distribution['q25']:
        return ratio, 'amber', 'Below typical'
    if value > distribution['q75']:
        return ratio, 'green', 'Above typical'
    return ratio, 'blue', 'Typical'


def fmt(value):
    return '—' if value is None else f'{value:,.0f}'


def stage_label(stage):
    return 'T−30 seconds' if stage == 't30' else f'T−{STAGES[stage] // 60} minutes'


def bar(title, value, distribution, final=False):
    ratio, color, label = comparison(value, distribution)
    if final and ratio is not None and distribution['n'] >= 20:
        color, label = 'blue', 'Relative to typical pre-race volume'
    esc = html.escape
    if ratio is None:
        return f'<div class="volume-bar"><strong>{esc(title)}</strong><p>Comparison unavailable · {distribution["n"]} races</p></div>'
    maximum = max(200, ratio)
    width, marker = ratio / maximum * 100, 100 / maximum * 100
    return (f'<div class="volume-bar"><strong>{esc(title)}</strong>'
            f'<p>{fmt(value)} / {fmt(distribution["median"])} median · <b>{ratio:.0f}%</b> · {label}</p>'
            f'<div class="volume-track" role="img" aria-label="{esc(title)}: {ratio:.0f}% of historical median; {label}">'
            f'<span class="volume-fill {color}" style="width:{width:.2f}%"></span>'
            f'<span class="volume-median" style="left:{marker:.2f}%" title="100%: historical median"></span></div>'
            f'<p class="small">100% marker = median · {distribution["n"]} races · middle 50%: {fmt(distribution["q25"])}–{fmt(distribution["q75"])}</p></div>')


def panel(base, book, history_dir, live=True, now=None):
    now = now or datetime.now(timezone.utc)
    book = book or {}
    market = book.get('market') or {}
    start = parse_time(market.get('market_start_time'))
    payload, groups = volume_cache(base.resolve()).get()
    heading = '<section class="volume-panel"><h2>Market Volume</h2>'
    if not payload:
        return heading + '<p>Historical volume register unavailable.</p></section>'
    generated = parse_time(payload.get('generated_at'))
    age = max(0, int((now - generated).total_seconds())) if generated else None
    age_text = f'{age // 3600}h {(age % 3600) // 60}m' if age is not None else 'unknown'
    stale = age is None or age > 86400
    context = f'<p class="small">Register age: {age_text}{" · stale" if stale else ""}. Same track · preceding 60 days · recorded trading activity.</p>'
    if not start:
        return heading + context + '<p>Race start unavailable; comparison disabled.</p></section>'
    if live and (market.get('inplay') is not False or book.get('inplay') is True or now >= start):
        return heading + context + '<p>Pre-race comparison disabled: scheduled benchmark window ended or in-play status unavailable. Rolling observations use a separate time basis.</p></section>'
    identity = cohort(book)
    context += f'<p class="small">{html.escape(identity[0])} · {html.escape(identity[1])} · {html.escape(identity[2] if identity[2] != "UNSPECIFIED" else "Currency unspecified: same-source totals, unit consistency unverified")}</p>'
    marks = {}
    for stage in STAGES:
        snapshot = read_json(history_dir / f'market_book_{stage}.json') if history_dir else None
        mark = capture(snapshot, stage)
        if mark and cohort(snapshot) == identity and parse_time(snapshot['market'].get('market_start_time')) == start and (not live or parse_time(mark['captured_at']) <= now):
            marks[stage] = mark
    # A currently aligned active capture can be newer than the archived copy.
    for stage in STAGES:
        mark = capture(book, stage)
        if mark and parse_time(mark['captured_at']) <= now:
            marks[stage] = mark
    if not marks:
        return heading + context + '<p>No valid pre-race capture at a supported stage.</p></section>'
    last = max(marks.values(), key=lambda m: parse_time(m['captured_at']))
    cutoff = min(start, now) if live else start
    distributions = {stage: baseline(groups, identity, cutoff, market.get('market_id'), stage) for stage in STAGES}
    capture_age = max(0, int((now - parse_time(last['captured_at'])).total_seconds()))
    stamp = f'{capture_age // 60}m {capture_age % 60}s ago' if live else last['captured_at']
    context += f'<p>Latest stage capture: {stage_label(last["stage"])} · {html.escape(stamp)}{" · delayed feed" if last["delayed"] else ""}{" · stale capture" if live and capture_age > 120 else ""}</p>'
    stage_bar = bar(f'{stage_label(last["stage"])}: relative to this stage', last['amount'], distributions[last['stage']])
    live_amount = amount(book) if live else None
    active_time = parse_time(book.get('captured_at'))
    if live and active_time and active_time <= now and active_time < start and live_amount is not None:
        current = live_amount
        current_age = int((now - active_time).total_seconds())
        final_title = f'Latest reported total vs typical T−30 seconds ({current_age}s old{"; stale" if current_age > 120 else ""})'
    else:
        current = last['amount']
        final_title = f'{stage_label(last["stage"])} capture vs typical T−30 seconds'
    final_bar = bar(final_title, current, distributions['t30'], final=True)
    values = [m['amount'] for m in marks.values()] + [s['median'] for s in distributions.values() if s['median'] is not None]
    maximum = max(values + [1])
    actual, typical, labels = [], [], []
    for i, stage in enumerate(STAGES):
        x = 35 + i * 115
        label = 'T−30s' if stage == 't30' else f'T−{STAGES[stage] // 60}m'
        labels.append(f'<text x="{x}" y="115" text-anchor="middle">{label}</text>')
        if stage in marks:
            actual.append(f'{x},{95 - marks[stage]["amount"] / maximum * 75:.2f}')
        if distributions[stage]['median'] is not None:
            typical.append(f'{x},{95 - distributions[stage]["median"] / maximum * 75:.2f}')
    trend = (f'<svg class="volume-trend" viewBox="0 0 540 125" role="img" aria-label="Matched volume by capture stage. Scale zero to {fmt(maximum)}. Blue current race, dashed grey historical median.">'
             f'<polyline points="{" ".join(typical)}" fill="none" stroke="#aab5c4" stroke-dasharray="5 4" stroke-width="2"/>'
             f'<polyline points="{" ".join(actual)}" fill="none" stroke="#69b4ff" stroke-width="3"/>'
             + ''.join(f'<circle cx="{point.split(",")[0]}" cy="{point.split(",")[1]}" r="4" fill="#69b4ff"/>' for point in actual)
             + ''.join(labels) + '</svg>')
    rows = ''.join(f'<tr><td>{stage_label(stage)}</td><td>{fmt(marks.get(stage, {}).get("amount"))}</td><td>{fmt(distributions[stage]["median"])}</td><td>{distributions[stage]["n"]}</td></tr>' for stage in STAGES)
    return (heading + context + stage_bar + final_bar + trend +
            f'<p class="small">Trend: blue = race captures; dashed grey = historical median. Scale 0–{fmt(maximum)}. T−30 means 30 seconds before start.</p>'
            '<details><summary>Stage amounts and samples</summary><table><thead><tr><th>Stage</th><th>Race</th><th>Median</th><th>Races</th></tr></thead><tbody>' + rows + '</tbody></table></details></section>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-dir', type=Path, default=DEFAULT_BASE)
    parser.add_argument('--version', action='version', version=VERSION)
    sub = parser.add_subparsers(dest='command', required=True)
    scan = sub.add_parser('scan')
    scan.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    base = args.base_dir.expanduser()
    payload = build_register(base)
    path = base / 'state' / STATE_FILE
    if not args.dry_run:
        atomic_write_json(path, payload)
    print(f'VOLUME races={payload["record_count"]} groups={len(payload["groups"])} skipped={payload["skipped"]} file={path}')
    if args.dry_run:
        print('DRY RUN: no file written')


if __name__ == '__main__':
    main()
