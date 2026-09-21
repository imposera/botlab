"""Shared read-only Australian capture inputs and observation shortlist."""
from datetime import date, datetime, timezone
from pathlib import Path
import math
import re
from tb_queue_summary import read_state, parse_time
from tb_au_price_review import classify
from tb_race_lifecycle import scratching_break

SLOTS = ('T15', 'T10', 'T5', 'T2', 'T30')


def market_chances(captures, expected_ids=()):
    """Normalize one complete preplay book; never mix runner snapshot times."""
    for slot in SLOTS:
        data=captures.get(slot) or {};book=data.get('book') or {}
        if book.get('status')!='OPEN' or book.get('inplay') is not False:continue
        runners=book.get('runners') or []
        ids=[str(r.get('selectionId')) for r in runners]
        if len(set(ids))!=len(ids) or not set(expected_ids).issubset(ids):continue
        active=[r for r in runners if r.get('status')=='ACTIVE']
        if not active or any(r.get('status') not in ('ACTIVE','REMOVED','REMOVED_VACANT') for r in runners):continue
        if book.get('numberOfActiveRunners',len(active))!=len(active):continue
        prices=[number(r.get('lastPriceTraded')) for r in active]
        if any(p is None or p<=1 for p in prices):continue
        total=sum(1/p for p in prices)
        return slot,{str(r['selectionId']):{'chance':100/(p*total),'odds':p*total} for r,p in zip(active,prices)}
    return None,{}


def snapshot(base, day, mid, slot):
    if date.fromisoformat(day).isoformat() != day or not re.fullmatch(r'\d+\.\d+',mid) or slot not in SLOTS:
        raise ValueError('Invalid capture selection')
    root=(Path(base)/'observations/au').resolve()
    path=root/day/mid/(slot+'.json')
    if not path.resolve().is_relative_to(root):
        raise ValueError('Invalid capture path')
    data=read_state(path)
    if data and (data.get('market_id') != mid or data.get('slot') != slot or data.get('schema') != 'tb_au_snapshot/v1'):
        return None
    return data


def number(value):
    if isinstance(value, bool): return None
    try:
        result=float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError): return None


QUALIFYING = {'Steady firm', 'Steady drift', 'Late firm', 'Late drift',
              'Drift then recovery', 'Firm then rebound'}


def mapping(value):
    return value if isinstance(value, dict) else {}


def review_threshold(base):
    value = number((read_state(Path(base)/'state/au_price_review_config.json') or {}).get('stable_percent', 3))
    return value if value is not None and 0 < value <= 20 else 3.0


def analyse_race(base, day, mid, market, *, now=None, forms=None):
    """Rank descriptive shapes using only captures received before assessment/start.

    All runners must have a price at the same latest market snapshot. Coverage
    precedes path range in ranking, then selection ID breaks ties. No outcomes
    or historical winner model enter qualification.
    """
    now = now or datetime.now(timezone.utc)
    start = parse_time(market.get('market_start_time'))
    cutoff = min(now, start) if start else now
    captures, timing = {}, []
    previous = None
    for slot in SLOTS:
        try:
            data = snapshot(base, day, mid, slot)
        except (ValueError, TypeError):
            data = None
        book = mapping((data or {}).get('book'))
        received = parse_time((data or {}).get('captured_at'))
        runners = book.get('runners')
        valid = (start is not None and received is not None and received <= now and received < start
                 and book.get('status') == 'OPEN' and book.get('inplay') is False
                 and isinstance(runners, list) and bool(runners)
                 and all(isinstance(r, dict) and r.get('selectionId') is not None for r in runners))
        reason = 'Missing / invalid snapshot'
        if valid:
            ids = [str(r['selectionId']) for r in runners]
            valid = len(ids) == len(set(ids)) and (previous is None or received > previous)
            reason = 'Duplicate runners or non-increasing capture time'
        if valid:
            captures[slot] = data
            previous = received
        timing.append({'slot': slot, 'available': bool(valid),
                       'captured_at': (data or {}).get('captured_at'),
                       'due_at': (data or {}).get('due_at'),
                       'late_seconds': (data or {}).get('late_seconds'),
                       'delayed_feed': book.get('isMarketDataDelayed'),
                       'reason': None if valid else reason})
    broken = scratching_break([captures[s]['book'] for s in SLOTS if s in captures])
    catalogue_runners = market.get('runners')
    catalogue_runners = catalogue_runners if isinstance(catalogue_runners, list) else []
    names = {str(r['selection_id']): r.get('runner_name') for r in catalogue_runners
             if isinstance(r, dict) and r.get('selection_id') is not None}
    reference, chances = market_chances(captures, names)
    if broken:
        reference, chances = None, {}
    latest = next((s for s in reversed(SLOTS) if s in captures), None)
    books = {s: {str(r['selectionId']): r for r in c['book']['runners']} for s, c in captures.items()}
    if forms is None:
        state = read_state(Path(base)/'state/tb_ra_form.json') or {}
        forms = mapping(mapping(mapping(state.get('days')).get(day)).get(mid))
    threshold = review_threshold(base)
    rows = []
    for sid, runner in books.get(latest, {}).items():
        if runner.get('status') != 'ACTIVE':
            continue
        prices = {s: number(b[sid].get('lastPriceTraded')) for s, b in books.items()
                  if sid in b and b[sid].get('status') == 'ACTIVE'}
        prices = {s: p for s, p in prices.items() if p is not None and p > 1}
        review = classify(prices, broken=broken, threshold=threshold)
        if latest not in prices or review['label'] not in QUALIFYING:
            continue
        values = [prices[s] for s in SLOTS if s in prices]
        form = mapping(mapping(forms).get(sid))
        imported = parse_time(form.get('imported_at'))
        starts = form.get('starts')
        history_known = isinstance(starts, list) and bool(starts) and all(
            isinstance(r, dict) and parse_time(r.get('date')) and str(r['date']) < day for r in starts)
        base_value = number(form.get('baseline')) if history_known and imported and imported <= cutoff and imported < start and form.get('pre_race') is True else None
        probability = chances.get(sid, {})
        rows.append({'selection_id': sid, 'horse_name': names.get(sid) or sid,
                     'base': base_value, 'chance': probability.get('chance'),
                     'fair_odds': probability.get('odds'), 'reference_slot': reference,
                     'move': (values[-1]/values[0]-1)*100,
                     'class': review['label'], 'partial': review['partial'],
                     'missing': review['missing'], 'captured': len(values),
                     'range_percent': (max(values)-min(values))/values[0]*100,
                     'latest_price': prices[latest], 'latest_slot': latest,
                     'captured_at': captures[latest]['captured_at'],
                     'delayed_feed': any(c['book'].get('isMarketDataDelayed') is True for c in captures.values())})
    rows.sort(key=lambda r: (-r['captured'], -r['range_percent'], r['selection_id']))
    return {'runners': rows[:3], 'qualifier_count': len(rows), 'timing': timing,
            'scratching_break': broken, 'reference_slot': reference, 'latest_slot': latest,
            'threshold': threshold, 'assessed_at': cutoff.isoformat(),
            'message': 'Not comparable · scratching' if broken else
                       'No qualifying shapes yet' if not rows else 'Provisional shortlist' if any(r['partial'] for r in rows[:3]) else 'Complete snapshot shapes'}
