"""Transparent historical winner-result signals for Australian race review.

This module deliberately uses only pre-start stages T15, T10, T5 and T2.
T30 is retained for review/validation and is never a model feature.
"""
from __future__ import annotations

import math
import argparse
import json
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

from tb_au_price_review import classify
from tb_runner_shape import canonical, shape_symbols


STAGES = ('T15', 'T10', 'T5', 'T2')
PRICE_STATS_SLOTS = ('T5', 'T3', 'T30')
PRICE_BUCKETS = ('<2.00', '2.00–3.00', '3.00–5.00', '5.00–10.00', '10.00+')


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 1 else None


def price_bucket(value):
    value=_number(value)
    if value is None: return None
    if value < 2: return '<2.00'
    if value < 3: return '2.00–3.00'
    if value < 5: return '3.00–5.00'
    if value < 10: return '5.00–10.00'
    return '10.00+'


def wilson_interval(wins, starts, z=1.96):
    """Return a percentage Wilson 95% interval for a binomial rate."""
    n=int(starts or 0); w=int(wins or 0)
    if n <= 0: return (None, None)
    p=w/n; denominator=1+(z*z/n); centre=(p+(z*z/(2*n)))/denominator
    spread=(z*math.sqrt((p*(1-p)/n)+(z*z/(4*n*n))))/denominator
    return (round(max(0,centre-spread)*100,1),round(min(1,centre+spread)*100,1))


def movement_bucket(prices):
    """Return firming, drift, stable or unknown from T15 to T2/T5."""
    prices = canonical(prices or {})
    first = _number(prices.get('T15'))
    last = next((_number(prices.get(slot)) for slot in ('T2', 'T5', 'T10') if _number(prices.get(slot)) is not None), None)
    if first is None or last is None:
        return 'unknown'
    change = math.log(first / last)
    if change >= 0.05:
        return 'firming'
    if change <= -0.05:
        return 'drifting'
    return 'stable'


def shape_bucket(prices):
    """Return a compact pre-start shape path, with missing data explicit."""
    symbols = shape_symbols(canonical(prices or {}))
    return ''.join(symbols[:4]) or '?'


def shape_prefixes(prices):
    """Return each available pre-start shape prefix as it develops."""
    prices=canonical(prices or {})
    values=[prices[stage] for stage in STAGES if stage in prices and _number(prices[stage]) is not None]
    prefixes=[]
    for end in range(2,len(values)+1):
        prefixes.append(''.join('▲' if b>a else '▼' if b<a else '▬' for a,b in zip(values[:end],values[1:end])))
    return prefixes


def feature_key(prices, race_class):
    return movement_bucket(prices), shape_bucket(prices), str(race_class or 'Unknown')


def profile_key(prices, race_class):
    return price_bucket((prices or {}).get('T30')), shape_bucket(prices), str(race_class or 'Unknown')


def key_text(key):
    return '|'.join(str(part) for part in key)


def fit(records, prior_strength=4.0):
    """Fit smoothed winner rates from records with prices, class and winner.

    Records are dictionaries containing ``prices``, ``race_class`` and
    ``winner``. The result is deterministic and serialisable.
    """
    groups = defaultdict(lambda: [0, 0])
    components = {name: defaultdict(lambda: [0, 0]) for name in ('movement', 'shape', 'class')}
    class_groups = defaultdict(lambda: [0, 0, 0.0])
    prefix_groups = defaultdict(lambda: [0, 0])
    price_groups = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0, 0])))
    profile_groups = defaultdict(lambda: [0, 0])
    global_wins = global_starts = 0
    for record in records or ():
        if record.get('scratched'):
            continue
        key = feature_key(record.get('prices'), record.get('race_class'))
        groups[key][0] += 1
        groups[key][1] += bool(record.get('winner'))
        for name, value in zip(('movement', 'shape', 'class'), key):
            components[name][value][0] += 1
            components[name][value][1] += bool(record.get('winner'))
        class_groups[key[2]][0] += 1
        class_groups[key[2]][1] += bool(record.get('winner'))
        if record.get('winner'):
            winning_price=next((_number(record.get('prices',{}).get(slot)) for slot in ('T2','T5','T10','T15') if _number(record.get('prices',{}).get(slot)) is not None), None)
            if winning_price is not None: class_groups[key[2]][2] += winning_price
        for prefix in shape_prefixes(record.get('prices')):
            prefix_groups[prefix][0] += 1
            prefix_groups[prefix][1] += bool(record.get('winner'))
        for slot in PRICE_STATS_SLOTS:
            bucket=price_bucket((record.get('prices') or {}).get(slot))
            if bucket:
                price_groups[key[2]][slot][bucket][0] += 1
                price_groups[key[2]][slot][bucket][1] += bool(record.get('winner'))
        profile=profile_key(record.get('prices'),key[2])
        if profile[0]:
            profile_groups[profile][0] += 1
            profile_groups[profile][1] += bool(record.get('winner'))
        global_starts += 1
        global_wins += bool(record.get('winner'))
    global_rate = (global_wins + prior_strength * 0.1) / (global_starts + prior_strength) if global_starts else 0.1
    model={'prior_strength': prior_strength, 'global_rate': global_rate,
            'groups': {key: {'starts': n, 'wins': w} for key, (n, w) in groups.items()},
            'components': {name: {value: {'starts': n, 'wins': w} for value, (n, w) in values.items()}
                           for name, values in components.items()},
            'class_stats': {value: {'starts': n, 'wins': w, 'win_price_sum': round(price_sum, 4)} for value, (n, w, price_sum) in class_groups.items()},
            'shape_prefix_stats': {value: {'starts': n, 'wins': w} for value, (n, w) in prefix_groups.items()}}
    model['class_price_buckets']={name:{slot:{bucket:{'starts':n,'wins':w} for bucket,(n,w) in buckets.items()} for slot,buckets in slots.items()} for name,slots in price_groups.items()}
    model['winner_profiles']={key:{'starts':n,'wins':w} for key,(n,w) in profile_groups.items()}
    return model


def class_summary(model, minimum_starts=20):
    """Return ordered class statistics suitable for the wall panel."""
    prior=float((model or {}).get('prior_strength',4.0) or 4.0)
    overall=float((model or {}).get('global_rate',0.1) or 0.1)
    rows=[]
    for name,stats in ((model or {}).get('class_stats') or {}).items():
        starts=int(stats.get('starts',0) or 0); wins=int(stats.get('wins',0) or 0)
        if starts < minimum_starts: continue
        rate=(wins+prior*overall)/(starts+prior)
        price_sum=float(stats.get('win_price_sum',0) or 0)
        rows.append({'class':name,'starts':starts,'wins':wins,'rate':round(rate*100,1),
                     'average_winning_price':round(price_sum/wins,2) if wins and price_sum else None})
    return sorted(rows,key=lambda row:(-row['rate'],-row['starts'],row['class']))


def class_price_bucket(model, class_name, slot):
    """Return the most observed price bucket and its Wilson uncertainty."""
    buckets=((model or {}).get('class_price_buckets') or {}).get(class_name,{}).get(slot,{})
    if not buckets: return None
    bucket,stats=max(buckets.items(),key=lambda item:(int(item[1].get('starts',0) or 0),item[0]))
    starts=int(stats.get('starts',0) or 0); wins=int(stats.get('wins',0) or 0)
    low,high=wilson_interval(wins,starts)
    return {'bucket':bucket,'starts':starts,'wins':wins,'rate':round(wins/starts*100,1) if starts else None,'low':low,'high':high}


def profile_summary(model, minimum_starts=20):
    prior=float((model or {}).get('prior_strength',4.0) or 4.0)
    overall=float((model or {}).get('global_rate',0.1) or 0.1)
    rows=[]
    for key,stats in ((model or {}).get('winner_profiles') or {}).items():
        if isinstance(key,str): bucket,shape,class_name=key.split('|',2)
        else: bucket,shape,class_name=key
        starts=int(stats.get('starts',0) or 0); wins=int(stats.get('wins',0) or 0)
        if starts < minimum_starts: continue
        rate=(wins+prior*overall)/(starts+prior); low,high=wilson_interval(wins,starts)
        rows.append({'bucket':bucket,'shape':shape,'class':class_name,'starts':starts,'wins':wins,
                     'rate':round(rate*100,1),'low':low,'high':high})
    return sorted(rows,key=lambda row:(-row['rate'],-row['starts'],row['class']))


def profile_score(prices, race_class, model):
    key=profile_key(prices,race_class); groups=(model or {}).get('winner_profiles',{})
    stats=groups.get(key,groups.get(key_text(key),{})); starts=int(stats.get('starts',0) or 0); wins=int(stats.get('wins',0) or 0)
    low,high=wilson_interval(wins,starts)
    return {'bucket':key[0] or '—','shape':key[1],'class':key[2],'starts':starts,'wins':wins,
            'rate':round(wins/starts*100,1) if starts else None,'low':low,'high':high}


def emerging_shape(prices, model):
    """Score the latest available pre-start shape prefix."""
    prefixes=shape_prefixes(prices)
    prefix=prefixes[-1] if prefixes else '—'
    stats=((model or {}).get('shape_prefix_stats') or {}).get(prefix,{})
    starts=int(stats.get('starts',0) or 0); wins=int(stats.get('wins',0) or 0)
    prior=float((model or {}).get('prior_strength',4.0) or 4.0)
    overall=float((model or {}).get('global_rate',0.1) or 0.1)
    rate=(wins+prior*overall)/(starts+prior)
    return {'shape':prefix,'starts':starts,'wins':wins,'rate':round(rate*100,1),
            'confidence':'High' if starts>=40 else 'Medium' if starts>=12 else 'Low'}


def score(prices, race_class, model):
    """Return a 0-100 historical signal, evidence count and explanation."""
    key = feature_key(prices, race_class)
    groups=(model or {}).get('groups', {})
    group = groups.get(key, groups.get(key_text(key), {}))
    starts = int(group.get('starts', 0) or 0)
    wins = int(group.get('wins', 0) or 0)
    prior = float((model or {}).get('prior_strength', 4.0) or 4.0)
    prior_rate = float((model or {}).get('global_rate', 0.1) or 0.1)
    def posterior(stats):
        n=int(stats.get('starts', 0) or 0); w=int(stats.get('wins', 0) or 0)
        return (w + prior * prior_rate) / (n + prior)
    exact_rate=posterior(group)
    components=(model or {}).get('components', {})
    component_rates=[]
    for name, value in zip(('movement', 'shape', 'class'), key):
        component_rates.append(posterior(components.get(name, {}).get(value, {})))
    # Exact combinations carry the most weight; component histories prevent
    # sparse exact keys from collapsing every runner to the global rate.
    rate=(0.40*exact_rate)+(0.20*component_rates[0])+(0.15*component_rates[1])+(0.25*component_rates[2])
    component_starts=sum(int(components.get(name, {}).get(value, {}).get('starts', 0) or 0)
                        for name, value in zip(('movement', 'shape', 'class'), key))
    evidence=max(starts, component_starts)
    return {'score': round(max(0.0, min(100.0, rate * 100)), 1),
            'wins': wins, 'starts': starts, 'movement': key[0],
            'shape': key[1], 'class': key[2],
            'movement_score': round(component_rates[0]*100, 1),
            'shape_score': round(component_rates[1]*100, 1),
            'class_score': round(component_rates[2]*100, 1),
            'evidence': evidence,
            'confidence': 'High' if evidence >= 40 else 'Medium' if evidence >= 12 else 'Low'}


def build_records(base):
    """Read settled observation history and return model training records."""
    base=Path(base)
    observations=json.loads((base/'state/tb_au_observations.json').read_text()) if (base/'state/tb_au_observations.json').exists() else {}
    results=json.loads((base/'state/tb_au_results.json').read_text()) if (base/'state/tb_au_results.json').exists() else {}
    records=[]
    for mid,job in (observations.get('races') or {}).items():
        result=(results.get('races') or {}).get(mid,{})
        if result.get('status')!='settled': continue
        result_runners={str(r.get('selectionId')):r for r in result.get('book',{}).get('runners',[]) if r.get('selectionId') is not None}
        for item in (job.get('market') or {}).get('runners',[]):
            sid=str(item.get('selection_id'))
            rr=result_runners.get(sid,{})
            if rr.get('status') in ('REMOVED','REMOVED_VACANT'): continue
            prices={}
            for slot in (*STAGES, 'T3', 'T30'):
                path=base/'observations'/'au'/str(job.get('date'))/str(mid)/(slot+'.json')
                if not path.exists(): continue
                try: data=json.loads(path.read_text())
                except (OSError,ValueError): continue
                runner=next((r for r in (data.get('book') or {}).get('runners',[]) if str(r.get('selectionId'))==sid),None)
                if runner and runner.get('lastPriceTraded') is not None: prices[slot]=runner['lastPriceTraded']
            if prices:
                race_class=classify(prices,broken=False,threshold=3).get('label')
                records.append({'prices':prices,'race_class':race_class,'winner':rr.get('status')=='WINNER','date':job.get('date')})
    return records


def rebuild(base, destination=None):
    """Build and atomically write the offline model state."""
    base=Path(base); destination=Path(destination or base/'state/tb_historical_intelligence.json')
    records=build_records(base)
    model=fit(records)
    models_by_date={}
    for day in sorted({r.get('date') for r in records if r.get('date')}):
        prior=fit([r for r in records if r.get('date','') < day])
        prior['groups']={key_text(key): value for key, value in prior['groups'].items()}
        prior['winner_profiles']={key_text(key): value for key, value in prior['winner_profiles'].items()}
        models_by_date[day]=prior
    model['groups']={key_text(key): value for key, value in model['groups'].items()}
    model['winner_profiles']={key_text(key): value for key, value in model['winner_profiles'].items()}
    model.update({'schema':'tb_historical_intelligence/v1',
                  'updated_at':datetime.now(timezone.utc).isoformat(),
                  'training_records':len(records),
                  'models_by_date':models_by_date})
    destination.parent.mkdir(parents=True,exist_ok=True)
    tmp=destination.with_suffix(destination.suffix+'.tmp')
    tmp.write_text(json.dumps(model,sort_keys=True,indent=2)+'\n')
    tmp.replace(destination)
    return model


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='Rebuild the offline historical winner signal model')
    parser.add_argument('--base',default='.',help='Totebot base directory')
    parser.add_argument('--write',action='store_true',help='write state/tb_historical_intelligence.json')
    args=parser.parse_args()
    if args.write:
        model=rebuild(args.base)
        print(f"wrote {model['training_records']} training records")
    else:
        print(f"training records: {len(build_records(args.base))}")
