"""Descriptive snapshot classification, not a prediction or trading signal."""
from tb_runner_shape import canonical, STAGES


def pct(prices,a,b):
    return (prices[b]/prices[a]-1)*100 if a in prices and b in prices else None


def classify(values, *, broken=False, threshold=3.0):
    prices={s:v for s,v in canonical(values).items() if s in STAGES}
    result={'label':'Insufficient data','early':None,'late':None,'partial':len(prices)<5,'tone':'neutral',
            'threshold':threshold,'missing':[s for s in STAGES if s not in prices]}
    if broken:
        result['label']='Not comparable · scratching';return result
    result.update(early=pct(prices,'T15','T5'),late=pct(prices,'T5','T30'))
    if len(prices)<2:return result
    adjacent=[(prices[b]-prices[a])/prices[a]*100 for a,b in zip(STAGES,STAGES[1:]) if a in prices and b in prices]
    if not adjacent:return result
    first=next(prices[s] for s in STAGES if s in prices)
    if (max(prices.values())-min(prices.values()))/first*100 < threshold:
        result['label']='Stable';return result
    directions=[1 if d>0 else -1 for d in adjacent if abs(d)>=threshold]
    if not directions:directions=[1 if d>0 else -1 for d in adjacent if d]
    changes=sum(a!=b for a,b in zip(directions,directions[1:]))
    if changes>=2:label='Mixed / volatile'
    elif changes==1:label='Firm then rebound' if directions[0]<0 else 'Drift then recovery'
    else:
        early,late=result['early'],result['late']
        if early is not None and late is not None and abs(late)>=threshold and abs(late)>=2*abs(early):
            label='Late firm' if late<0 else 'Late drift'
        else:label='Steady firm' if directions and directions[-1]<0 else 'Steady drift'
    result['label']=label
    result['tone']='firm' if label in ('Late firm','Steady firm') else 'drift' if label in ('Late drift','Steady drift') else 'neutral'
    return result


def shortlist_review(values, *, broken=False, threshold=3.0):
    """Evidence status for manual observation review; no selection or queue action."""
    review=classify(values,broken=broken,threshold=threshold)
    prices={s:v for s,v in canonical(values).items() if s in STAGES}
    closing=None if broken else pct(prices,'T2','T30')
    if broken:status='Not comparable';reason='A scratching interrupts this price sequence.'
    elif review['partial']:status='Incomplete';reason='Wait for all five snapshots before comparing the full shape.'
    else:
        status='Complete'
        if closing is not None and closing<=-threshold:reason='Firming into T30: inspect spread and traded-volume evidence.'
        elif closing is not None and closing>=threshold:reason='Drifting into T30: inspect spread and traded-volume evidence.'
        else:reason='Little closing movement: compare the earlier move and any reversal.'
    return {'status':status,'reason':reason,'closing':closing,'captured':len(prices)}
