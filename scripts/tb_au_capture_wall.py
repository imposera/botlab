"""Read-only Australian snapshot browser; serves only validated capture identities."""
from datetime import date
import html
import json
import unicodedata
from pathlib import Path
import re
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from tb_queue_summary import read_state, parse_time
from tb_au_price_review import classify, shortlist_review
from tb_price_threshold import threshold as price_threshold
from tb_runner_shape import canonical, first_last, shape_symbols
from tb_race_lifecycle import scratching_break
from tb_historical_intelligence import emerging_shape, fit as fit_historical, profile_score, score as score_historical

from tb_au_snapshot_analysis import SLOTS, market_chances, snapshot, number, analyse_race
ZONE = ZoneInfo('Australia/Melbourne')


def t30_refresh_script(start_ms):
    return """<script>
(()=>{
 const start=START_MS;
 async function check(){
  const now=Date.now();
  if(now>start+600000)return;
  if(now<start-45000||document.hidden){setTimeout(check,10000);return;}
  try{
   const response=await fetch(location.href,{cache:'no-store',signal:AbortSignal.timeout(8000)});
   if(response.ok){
    const page=new DOMParser().parseFromString(await response.text(),'text/html');
    const detail=page.querySelector('#race-detail');
    const t2Ready=page.querySelector('#t2-state')?.dataset.ready==='true';
    if(t2Ready && detail?.dataset.t30Ready==='true'){
     location.reload();return;
    }
   }
  }catch(error){}
  setTimeout(check,10000);
 }
 setTimeout(check,1000);
})();
</script>""".replace('START_MS',str(int(start_ms)))


def action_stages(prices, fair_odds, reference_slot, broken=False):
    fair=number(fair_odds)
    if broken or fair is None or fair<=2 or reference_slot not in SLOTS:return []
    return [slot for slot in ('T5','T2') if SLOTS.index(slot)>SLOTS.index(reference_slot)
            and (price:=number(prices.get(slot))) is not None and 1<price<=fair/2]


def drift_stage(prices, fair_odds, reference_slot, broken=False):
    fair=number(fair_odds)
    if broken or fair is None or fair<=1 or reference_slot not in SLOTS:return None
    target=fair*1.15
    for slot in ('T2','T5'):
        price=number(prices.get(slot))
        if price is None or price<=1:continue
        if SLOTS.index(slot)<=SLOTS.index(reference_slot):return None
        return slot if price>=target-1e-10 else None
    return None


def url(**params):
    return '/au-captures?'+urlencode(params)


def meeting_code(day, track):
    name=unicodedata.normalize('NFKD', track).encode('ascii','ignore').decode().lower()
    slug=re.sub(r'[^a-z0-9]+','-',name).strip('-') or 'meeting'
    return date.fromisoformat(day).strftime('%y%m%d')+'-'+slug


def clock(value):
    stamp=parse_time(value)
    return stamp.astimezone(ZONE).strftime('%H:%M:%S') if stamp else '—'


def compact_person(value):
    if value is None:return None
    name=' '.join(str(value).split())
    if len(name)<=25:return name
    def shorten(part):
        words=re.sub(r'^(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+', '', part.strip(), flags=re.I).split()
        return words[0][0]+'. '+words[-1] if len(words)>1 else ' '.join(words)
    return ' & '.join(shorten(part) for part in re.split(r'\s+(?:&|and)\s+',name))


def historical_model(base, jobs=None, results=None, cutoff_day=None, threshold=None):
    """Read the offline historical model; page rendering never fits it."""
    state=read_state(Path(base)/'state/tb_historical_intelligence.json') or {}
    by_date=state.get('models_by_date') or {}
    if cutoff_day:
        eligible=[day for day in by_date if day < cutoff_day]
        if eligible: return by_date[max(eligible)]
    return fit_historical([])


def liquidity(runner, capture):
    ex=runner.get('ex') or {}
    back=[p for p in ex.get('availableToBack',[]) if number(p.get('price')) is not None]
    lay=[p for p in ex.get('availableToLay',[]) if number(p.get('price')) is not None]
    best_back=max(back,key=lambda p:float(p['price']),default={})
    best_lay=min(lay,key=lambda p:float(p['price']),default={})
    bp=number(best_back.get('price'));lp=number(best_lay.get('price'))
    traded=None
    if 'EX_TRADED' in capture.get('price_data',[]) and isinstance(ex.get('tradedVolume'),list):
        sizes=[number(p.get('size')) for p in ex['tradedVolume']]
        if all(n is not None for n in sizes):traded=round(sum(sizes),2)
    return dict(back=bp,back_size=number(best_back.get('size')),lay=lp,lay_size=number(best_lay.get('size')),
                spread=round(lp-bp,3) if lp is not None and bp is not None else None,traded=traded)


def move_and_shape(prices, broken=False):
    prices=canonical(prices)
    first,last=first_last(prices)
    move=None if broken or len(prices)<2 else (last-first)/first*100
    symbols=shape_symbols(prices)
    title=('Scratching changed the field; net move suppressed. ' if broken else '')+'Available stages: '+(', '.join(s for s in SLOTS if s in prices) or 'none')
    if len(prices)<5:title+=' · partial captures'
    painted=[]
    for i,symbol in enumerate(symbols):
        style={'▲':'early-drift','▼':'early-firm','▬':'early-flat'}.get(symbol)
        painted.append(f'<span class="{style}">{symbol}</span>' if style and i<3 and not broken else html.escape(symbol))
    move_label='—' if move is None else f'{move:+.1f}%' if move>0 else f'{move:.1f}%'
    return f'<td>{move_label}</td><td><span class="shape-badge" tabindex="0" title="{html.escape(title,quote=True)}">{"".join(painted)}</span></td>'


def review_cells(prices, books, captures, sid, broken, threshold):
    review=classify(prices,broken=broken,threshold=threshold)
    e=lambda value:html.escape(str(value),quote=True)
    fmt=lambda value:'—' if value is None else f'{value:+.1f}%'
    label=review['label']+(' · partial' if review['partial'] else '')
    assessment=shortlist_review(prices,broken=broken,threshold=threshold)
    evidence=[f'<p><strong>Review: {e(assessment["status"])}</strong> · {assessment["captured"]}/5 snapshots. {e(assessment["reason"])}</p>',f'<p>Closing T2 → T30: {fmt(assessment["closing"])}</p>']
    for title,a,b in [('Early','T15','T5'),('Late','T5','T30'),('Closing','T2','T30')]:
        pair=[]
        for stage in (a,b):
            runner=books[stage].get(sid)
            pair.append(liquidity(runner,captures[stage]) if runner and captures[stage] else None)
        x,y=pair
        delta=None if broken or not x or not y or x['traded'] is None or y['traded'] is None else y['traded']-x['traded']
        amount='—' if delta is None else 'Reset / reduction' if delta<0 else f'+{delta:,.2f}'
        evidence.append(f'<p>{title} {a} → {b}: traded increase {e(amount)}; spread {e(x["spread"] if x else "—")} → {e(y["spread"] if y else "—")}</p>')
    delayed=any((c or {}).get('book',{}).get('isMarketDataDelayed') for c in captures.values())
    note='Betfair delayed feed. ' if delayed else ''
    note+='Missing: '+(', '.join(review['missing']) or 'none')+'. '
    note+=f'Stable range threshold {threshold:g}%. Late means the late percentage move is at least twice the early move and meets the threshold. Complete means price coverage, not reliable liquidity. Check the actual received times in Snapshot observations. Descriptive only; manual observation review.'
    return f'<td class="price-review"><details><summary class="review-{review["tone"]}">{e(label)}</summary><div>{"".join(evidence)}<p>{e(note)}</p></div></details></td><td>{fmt(review["early"])}</td><td>{fmt(review["late"])}</td>'


def render(base, query):
    base=Path(base); jobs=(read_state(base/'state/tb_au_observations.json') or {}).get('races',{})
    days=sorted({j['date'] for j in jobs.values()},reverse=True)
    day=query.get('date',[''])[0] or (days[0] if days else '')
    if day and date.fromisoformat(day).isoformat()!=day:raise ValueError('Invalid date')
    races=sorted(((mid,j) for mid,j in jobs.items() if j.get('date')==day),key=lambda pair:(pair[1]['market'].get('track',''),pair[1]['market'].get('market_start_time','')))
    mid=query.get('market',[''])[0]
    if mid and not re.fullmatch(r'\d+\.\d+',mid):raise ValueError('Invalid market')
    selected=next((j for key,j in races if key==mid),None)
    e=lambda v:html.escape(str(v if v is not None else '—'),quote=True)
    options=''.join(f'<option value="{e(d)}" {"selected" if d==day else ""}>{e(d)}</option>' for d in days)
    meeting=query.get('meeting',[''])[0]
    if not meeting and selected:meeting=selected['market'].get('track','')
    meetings={}
    for key,job in races:meetings.setdefault(job['market'].get('track','Unknown meeting'),[]).append((key,job))
    if selected and selected['market'].get('track') != meeting:selected=None
    cards=[]
    for track,items in meetings.items():
        cards.append(f'<a class="meeting {"selected" if track==meeting else ""}" aria-current="{"page" if track==meeting else "false"}" href="{e(url(date=day,meeting=track))}">{e(track)} <code>{e(meeting_code(day,track))}</code><small>{len(items)} races</small></a>')
    def race_order(item):
        match=re.match(r'R(\d+)\b',item[1]['market'].get('market_name',''))
        return (int(match[1]) if match else 999,item[1]['market'].get('market_start_time',''))
    results=(read_state(base/'state/tb_au_results.json') or {}).get('races',{})
    race_links=[]
    for key,job in sorted(meetings.get(meeting,[]),key=race_order):
        market=job['market'];count=sum(slot.get('status')=='captured' for slot in job['slots'].values())
        match=re.match(r'R\d+\b',market.get('market_name',''))
        race_label=match[0] if match else market.get('market_name','Race')
        race_links.append(f'<a class="race {"selected" if key==mid else ""}" aria-current="{"page" if key==mid else "false"}" href="{e(url(date=day,meeting=meeting,market=key))}" title="{e(market.get("market_name"))}">{e(race_label)}<small>{clock(market.get("market_start_time"))} · {count}/5 · {e(results.get(key,{}).get("status","pending").title())}</small></a>')
    race_nav=f'<nav class="race-nav" aria-label="Races at selected meeting"><h2 class="filename-title">{e(meeting_code(day,meeting))}_rf</h2><div class="race-strip">{"".join(race_links)}</div></nav>' if meeting in meetings else ''
    detail='<h2>Select a race</h2><p>Choose a race above to view its information and snapshots.</p>' if meeting in meetings else '<h2>Select a meeting</h2><p>Choose a meeting from the left to see its races.</p>'
    if meeting and meeting not in meetings:detail='<h2>Meeting unavailable</h2><p>No races for this meeting on the selected date.</p>'
    if mid and not selected:detail='<h2>Race unavailable</h2><p>This race is not registered for the selected meeting and date.</p>'
    auto_refresh=''
    t30_ready=False
    t2_ready=False
    latest_snapshot=''
    if selected:
        market=selected['market']; captures={s:snapshot(base,day,mid,s) for s in SLOTS}
        t30_ready=bool(captures.get('T30'))
        t2_ready=bool(captures.get('T2'))
        latest_snapshot=next((slot for slot in reversed(SLOTS) if captures.get(slot)), 'T15')
        if not t2_ready or not t30_ready:
            start=parse_time(market.get('market_start_time'))
            if start:
                auto_refresh=t30_refresh_script(start.timestamp()*1000)
        names={str(r.get('selection_id')):r.get('runner_name') for r in market.get('runners',[])}
        result=results.get(mid,{})
        result_runners={str(r['selectionId']):r for r in result.get('book',{}).get('runners',[]) if 'selectionId' in r}
        result_labels={'WINNER':'Winner','LOSER':'Non-winner','REMOVED':'Scratched','REMOVED_VACANT':'Removed','PLACED':'Placed'}
        winners=[names.get(sid,sid) for sid,r in result_runners.items() if r.get('status')=='WINNER']
        result_text=('Winner: '+', '.join(winners) if winners else 'Closed · no winner reported') if result.get('status')=='settled' else 'Result unavailable' if result.get('status')=='unavailable' else 'Awaiting result'
        result_summary=f'<p class="race-result"><strong>{e(result_text)}</strong> · Betfair win-market result' + (f' · Saved {clock(result.get("captured_at"))}' if result.get('captured_at') else '') + (' · Latest check failed' if result.get('error') else '') + '</p>'
        chance_slot,chances=market_chances(captures,names)
        chance_note=('Market-implied chance · '+chance_slot+' last traded prices, normalized to 100%; fair decimal odds.' + (' Delayed feed.' if captures[chance_slot].get('book',{}).get('isMarketDataDelayed') else '')) if chance_slot else 'Market-implied chance unavailable: no complete pre-race price snapshot.'
        result_summary+=f'<p class="chance-source">{e(chance_note)}</p>'
        runner_ids=list(dict.fromkeys([*names,*result_runners]))
        books={}
        timing=[]
        for slot,data in captures.items():
            job=selected['slots'].get(slot,{})
            books[slot]={str(r['selectionId']):r for r in (data or {}).get('book',{}).get('runners',[]) if 'selectionId' in r}
            for sid in books[slot]:
                if sid not in runner_ids:runner_ids.append(sid)
            status='Saved' if data else 'File not synced / unavailable' if job.get('status')=='captured' else job.get('status','unavailable').title()
            raw='/api/au-capture?'+urlencode(dict(date=day,market=mid,slot=slot))
            timing.append(f'<tr><th>{slot}</th><td>{e(status)}</td><td>{clock((data or job).get("due_at"))}</td><td>{clock((data or {}).get("captured_at"))}</td><td>{e((data or {}).get("late_seconds"))}</td><td>{"Delayed" if (data or {}).get("book",{}).get("isMarketDataDelayed") else "—"}</td><td>{f"<a href={e(raw)}>JSON</a>" if data else e(job.get("reason") or job.get("last_error") or "—")}</td></tr>')
        baseline=((read_state(base/'state/tb_ra_ratings.json') or {}).get('days',{}).get(day,{}) .get('races',{}).get(mid,{}))
        form_baselines=(read_state(base/'state/tb_ra_form.json') or {}).get('days',{}).get(day,{}).get(mid,{})
        threshold=number((read_state(base/'state/au_price_review_config.json') or {}).get('stable_percent',3))
        threshold=threshold if threshold is not None and 0 < threshold <= 20 else 3.0
        historical=historical_model(base,jobs,results,day,threshold)
        broken=scratching_break([(captures[slot] or {}).get('book') for slot in SLOTS])
        qualifying={r['selection_id'] for r in analyse_race(base,day,mid,market)['runners']}
        rows=[]
        metadata={str(r.get('selection_id')):r.get('metadata') or {} for r in market.get('runners',[])}
        latest_status={}
        for slot in SLOTS:
            latest_status.update({sid:r.get('status') for sid,r in books[slot].items() if r.get('status')})
        latest_status.update({sid:r.get('status') for sid,r in result_runners.items() if r.get('status')})
        runner_ids=[sid for sid in runner_ids if latest_status.get(sid) not in ('REMOVED','REMOVED_VACANT')]
        runner_ids.sort(key=lambda sid:chances.get(sid,{}).get('odds',float('inf')))
        for sid in runner_ids:
            row_class='winner-row' if latest_status.get(sid)=='WINNER' else ''
            if sid in qualifying:row_class += ' shape-qualifier'
            cells=[]; previous=None; prices={}
            for slot in SLOTS:
                runner=books[slot].get(sid)
                if runner is None:cells.append(f'<td class="snapshot-column snapshot-{slot}" data-snapshot="{slot}">—</td>');previous=None;continue
                prices[slot]=runner.get('lastPriceTraded')
                values=liquidity(runner, captures[slot])
                delta=None if previous is None or values['traded'] is None else values['traded']-previous
                change='—' if delta is None else 'Reset / reduction' if delta < 0 else '+'+format(delta, ',.2f')
                previous=values['traded'] if runner.get('status')=='ACTIVE' else None
                cells.append(f'<td class="snapshot-column snapshot-{slot} snapshot-price" data-snapshot="{slot}"><details><summary>{e(runner.get("lastPriceTraded"))}</summary><small>Back {e(values["back"])} × {e(values["back_size"])}<br>Lay {e(values["lay"])} × {e(values["lay_size"])}<br>Spread {e(values["spread"])}<br>Traded {e(values["traded"])}<br>Change {e(change)}<br>{e(runner.get("status"))}</small></details></td>')
            meta=metadata.get(sid,{})
            label=f'Barrier {e(meta.get("STALL_DRAW"))} · Weight {e(meta.get("WEIGHT_VALUE"))} {e(meta.get("WEIGHT_UNITS"))}<br>Jockey {e(meta.get("JOCKEY_NAME"))}<br>Trainer {e(meta.get("TRAINER_NAME"))}'
            rating=baseline.get(sid,{})
            rating_label=e(rating.get('rating'))
            if rating.get('source_url'):
                rating_label=f'<a href="{e(rating["source_url"])}">{rating_label}</a>'
            label += f'<br>RA handicap: {rating_label}'
            if rating.get('collected_at'):
                label += f'<br>Collected {e(rating["collected_at"])}' + (' · after scheduled start' if not rating.get('pre_race') else '')
            weight=meta.get('WEIGHT_VALUE')
            score=rating.get('rating')
            if isinstance(score,(int,float)):score=format(score,'g')
            rating_cell=e(score)
            if rating.get('source_url'):
                tip='Collected '+str(rating.get('collected_at') or '—')+(' · after scheduled start' if not rating.get('pre_race') else '')
                rating_cell=f'<a href="{e(rating["source_url"])}" title="{e(tip)}">{e(score)}</a>'
            probability=chances.get(sid,{})
            chance_cell=format(probability['chance'],'.1f')+'%' if probability else '—'
            odds_cell=format(probability['odds'],'.2f') if probability else '—'
            hist_class=classify(prices,broken=broken,threshold=threshold).get('label')
            historical_signal=score_historical(prices,hist_class,historical)
            emerging=emerging_shape(prices,historical)
            profile=profile_score(prices,hist_class,historical)
            profile_text=f' · T30/Shape/Class profile {profile["rate"]:.1f}% ({profile["wins"]}/{profile["starts"]}) {profile["low"]:.1f}–{profile["high"]:.1f}%' if profile["rate"] is not None else ' · T30/Shape/Class profile unavailable'
            hist_cell=f'<span title="{e(historical_signal["confidence"])} confidence · {e(historical_signal["wins"])} exact wins from {e(historical_signal["starts"])} exact matches · evidence {e(historical_signal["evidence"])} · movement {historical_signal["movement_score"]:.1f}% · shape {historical_signal["shape_score"]:.1f}% · class {historical_signal["class_score"]:.1f}% · emerging shape {e(emerging["shape"])} {emerging["rate"]:.1f}% ({emerging["wins"]}/{emerging["starts"]}){profile_text}">{historical_signal["score"]:.1f}%</span>'
            target_odds=price_threshold(probability.get('odds'))
            target_cell=format(target_odds,'.2f') if target_odds is not None else '—'
            row_style=''
            form=form_baselines.get(sid,{})
            form_cell='—'
            if form.get('baseline') is not None or form.get('recent_win'):
                evidence=''.join(f'<div>{e(start["venue"])} · Rtg {e(start["rating"])}</div>' for start in form.get('starts',[]))
                recent_wins=''.join(f'<div>Won: {e(r["venue"])} · {e(r.get("distance_m"))}m</div>' for r in form.get('recent_races',[]) if r.get('position')==1)
                arrow_class='recent-win' if form.get('recent_win') else ''
                form_cell=f'<details><summary class="{arrow_class}" title="{"Won within last three races" if recent_wins else "Historical baseline"}">{e(form.get("baseline"))}</summary><small>Mean of {len(form.get("starts",[]))} rated races{evidence}{recent_wins}<div>Imported {e(form.get("imported_at"))}</div>' + ('After scheduled start · retrospective' if not form.get('pre_race') else 'Imported before scheduled start') + '</small></details>'
            rows.append(f'<tr class="{row_class}"><th scope="row"><span class="horse-name" title="{e(names.get(sid) or sid)} ({e(meta.get("STALL_DRAW"))})">{e(names.get(sid) or sid)} ({e(meta.get("STALL_DRAW"))})</span></th><td><span class="person-name" title="{e(meta.get("JOCKEY_NAME"))}">{e(compact_person(meta.get("JOCKEY_NAME")))}</span></td><td><span class="person-name" title="{e(meta.get("TRAINER_NAME"))}">{e(compact_person(meta.get("TRAINER_NAME")))}</span></td><td title="{e(meta.get("WEIGHT_UNITS"))}">{e(weight)}</td><td>{rating_cell}</td><td class="form-baseline">{form_cell}</td><td class="market-chance">{chance_cell}</td><td class="fair-odds">{odds_cell}</td><td class="historical-signal">{hist_cell}</td><td class="runner-result">{e(result_labels.get(result_runners.get(sid,{}).get("status"),"—"))}</td>{"".join(cells)}{move_and_shape(prices,broken)}{review_cells(prices,books,captures,sid,broken,threshold)}</tr>')
        detail=f'<h2>{e(market.get("track"))} · {e(market.get("market_name"))}</h2><p>Scheduled {clock(market.get("market_start_time"))} Melbourne time · Market {e(mid)}</p>{result_summary}<details class="snapshot-observations"><summary>Snapshot observations</summary><div class="scroll"><table><thead><tr><th>Snapshot</th><th>Status</th><th>Due</th><th>Received</th><th>Late (s)</th><th>Feed</th><th>Details</th></tr></thead><tbody>{"".join(timing)}</tbody></table></div></details><h3>Runner price comparison</h3><p>Gold names mark the top three qualifying price shapes. <a href="/next-five">Next five races and shortlist guide</a>.</p><button type="button" id="top-half" aria-pressed="false" aria-controls="runner-comparison-table">Top half</button><span id="cohail-status" role="status"></span><details class="runner-comparison"><summary>Price comparison guide</summary><p>Each snapshot shows last traded price. Click a price for liquidity details. Move is the percentage change from first to latest available price; negative means firming. Shape: ▲ drift, ▼ firm, ▬ unchanged, ? missing interval. Early intervals are blue/red; the final interval is uncoloured. A scratching suppresses Move and shape colours. Traded change compares adjacent saved snapshots; missing data is —. Runner details use the latest retained catalogue. Older captures may lack traded volume. Target odds applies your smooth decimal-odds curve to the earliest complete snapshot Fair odds. Inputs outside 2.40–5.00 remain blank. Blue Drift runners have a latest T2 price (T5 fallback) at least 15% above Fair odds, across the full odds range. Their badge shows whether the price is still drifting, holding or easing back versus the previous scheduled snapshot. Target odds remains a separate reference curve. A later drift takes precedence over an earlier red Action flag. Red Action runners have a T5 or T2 last-traded price at or below half Fair odds, using only snapshots later than the reference snapshot. Scratching breaks suppress the flag. These are saved pre-start prices, not actual post-time quotes. Top half retains the first half of the full unscratched field in Fair odds order, rounding up for odd fields. This is a market-reference observation threshold, not a baseline-derived morning line or an executable post-time quote.</p></details><div class="scroll"><table class="runner-table" id="runner-comparison-table"><thead><tr><th>Horse (br)</th><th>Jcky</th><th>Trnr</th><th>Wght</th><th>Rtg</th><th title="Mean of latest three available previous race handicap ratings">Base</th><th title="Normalized market-implied probability, not a baseline model prediction">Chance %</th><th title="Decimal odds from normalized market-implied probability">Fair odds</th><th title="User table curve applied to unrounded Fair odds; an observation threshold, not an independent value assessment">Target odds</th><th>Result</th>{"".join(f"<th>{slot}</th>" for slot in SLOTS)}<th>Move</th><th>Shape</th><th>Class</th><th>Early %</th><th>Late %</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    if selected:
        selected_start=parse_time(selected['market'].get('market_start_time'))
        upcoming=[]
        for key,job in races:
            start=parse_time(job['market'].get('market_start_time'))
            if selected_start and start and (start,key)>(selected_start,mid):
                upcoming.append((start,key,job))
        next_links=[]
        for start,key,job in sorted(upcoming,key=lambda item:(item[0],item[1]))[:5]:
            market=job['market'];track=market.get('track','Unknown meeting')
            match=re.match(r'R\d+\b',market.get('market_name',''))
            race_label=match[0] if match else market.get('market_name','Race')
            count=sum(slot.get('status')=='captured' for slot in job['slots'].values())
            next_links.append(f'<a class="race" href="{e(url(date=day,meeting=track,market=key))}" title="{e(market.get("market_name"))}">{e(track)} · {e(race_label)}<small>{clock(market.get("market_start_time"))} · {count}/5 · {e(results.get(key,{}).get("status","pending").title())}</small></a>')
        detail += '<nav class="next-races" aria-label="Next five races"><h3>Next five races</h3><div class="race-strip">'+(''.join(next_links) or '<p>No later races on this card.</p>')+'</div></nav>'
    # Keep the runner review focused on the Class and colour evidence.
    detail = detail.replace('<button type="button" id="top-half" aria-pressed="false" aria-controls="runner-comparison-table">Top half</button>', '')
    detail = detail.replace('<span id="cohail-status" role="status"></span>', '')
    detail = detail.replace('Blue Drift runners have a latest T2 price (T5 fallback) at least 15% above Fair odds, across the full odds range. Their badge shows whether the price is still drifting, holding or easing back versus the previous scheduled snapshot. Target odds remains a separate reference curve. A later drift takes precedence over an earlier red Action flag. Red Action runners have a T5 or T2 last-traded price at or below half Fair odds, using only snapshots later than the reference snapshot. Scratching breaks suppress the flag. These are saved pre-start prices, not actual post-time quotes. Top half retains the first half of the full unscratched field in Fair odds order, rounding up for odd fields.', 'Red Action, Fair-odds drift and Target odds highlighting are disabled. These are saved pre-start observations, not actual post-time quotes.')
    detail = detail.replace('<th title="Decimal odds from normalized market-implied probability">Fair odds</th>', '<th title="Decimal odds from normalized market-implied probability">Fair odds</th><th title="Historical pre-start winner signal from prior settled races">Hist</th>')
    for slot in SLOTS:
        detail = detail.replace(f'<th>{slot}</th>', f'<th class="snapshot-column" data-snapshot="{slot}" title="Click to expand or collapse saved snapshots">{slot}</th>')
    detail = detail.replace('class="snapshot-column" data-snapshot="T2"', 'class="snapshot-column" data-snapshot="T2" style="background:#5a3d12;color:#ffd27a"')
    detail = detail.replace('class="snapshot-column snapshot-T2 snapshot-price"', 'class="snapshot-column snapshot-T2 snapshot-price" style="background:#5a3d12;color:#ffd27a"')
    detail = detail.replace('class="snapshot-column snapshot-T2"', 'class="snapshot-column snapshot-T2" style="background:#5a3d12;color:#ffd27a"')
    if latest_snapshot:
        detail = detail.replace('<table class="runner-table" id="runner-comparison-table">', f'<table class="runner-table snapshots-collapsed" data-latest-snapshot="{latest_snapshot}" id="runner-comparison-table">')
    detail = re.sub(r'<th[^>]*>\s*Target odds\s*</th>', '', detail)
    detail = re.sub(r'<td class="target-odds"[^>]*>.*?</td>', '', detail)
    detail = detail.replace('<th>Result</th>', '')
    detail = detail.replace('<th>Early %</th><th>Late %</th>', '<th>Early %</th><th>Late %</th><th>Result</th>')
    detail = re.sub(r'(<tr[^>]*>)(.*?)(<td class="runner-result">.*?</td>)(.*?)(</tr>)', r'\1\2\4\3\5', detail, flags=re.S)
    detail = detail.replace('class="horse-name"', 'class="horse-name" style="width:30ch"')
    detail += f'<span id="t2-state" data-ready="{str(t2_ready).lower()}" hidden></span>'
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Australian Capture Wall</title><style>
body{{font:16px system-ui;background:#101820;color:#e6edf3;margin:24px}}a{{color:#8ecaff}}header{{display:flex;align-items:center;gap:16px;white-space:nowrap;overflow-x:auto;margin-bottom:20px;padding-bottom:4px}}header h1{{font-size:20px;margin:0}}header p,header form{{margin:0}}header form{{display:flex;align-items:center;gap:8px}}header>*{{flex-shrink:0}}main{{display:grid;grid-template-columns:240px minmax(0,1fr);gap:24px}}.meeting,.race{{display:block;padding:12px;margin:6px 0;background:#1d2b38;border:1px solid #405265;border-radius:7px;text-decoration:none}}.selected{{border-color:#8ecaff;background:#294158}}.race-strip{{display:flex;gap:8px;overflow-x:auto;padding-bottom:10px}}.race-strip .race{{flex:0 0 auto;min-width:95px;text-align:center}}.race-nav h2{{margin-top:0}}.filename-title{{user-select:all;overflow-wrap:anywhere}}code{{font-size:.8em;color:#a9c6df;overflow-wrap:anywhere;white-space:normal}}.meeting code{{display:inline}}.save-name{{font-size:14px;color:#a9b8c7}}aside h2{{margin-top:0}}small{{display:block;color:#a9b8c7;font-size:12px;margin-top:5px}}table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;text-align:left;border-bottom:1px solid #405265;white-space:nowrap}}.price-review{{text-align:left!important}}.price-review summary{{cursor:pointer}}.price-review details>div{{white-space:normal;min-width:280px;max-width:400px}}.review-firm{{color:#ff9b9b}}.review-drift{{color:#8bc4ff}}.shape-badge{{white-space:nowrap;color:#c0c7d0}}.shape-badge span{{border-radius:3px;padding:1px 3px}}.early-drift{{color:#8bc4ff;background:#173b60}}.early-firm{{color:#ff9b9b;background:#562626}}.early-flat{{background:transparent}}.runner-table .shape-qualifier>th{{box-shadow:inset 4px 0 #ffd27a}}.runner-table .shape-qualifier .horse-name{{color:#ffd27a}}.runner-table .winner-row{{background:#184b36}}.runner-table .winner-row>th{{box-shadow:inset 4px 0 #62db9c}}.runner-table .winner-row .runner-result{{color:#8ff0b7;font-weight:700}}.runner-table .action-runner{{background:#562626}}.runner-table .action-runner .horse-name,.runner-table .action-runner .action-odds{{color:#ff9b9b;font-weight:700}}.runner-table .drift-runner{{background:#173b60}}.runner-table .drift-runner .horse-name,.runner-table .drift-runner .target-odds{{color:#8bc4ff;font-weight:700}}.form-baseline summary.recent-win::marker{{color:#62db9c}}.form-baseline summary.recent-win::-webkit-details-marker{{color:#62db9c}}#top-half[aria-pressed="true"]{{background:#562626;color:#ffb4b4;border:1px solid #ff9b9b}}.runner-table.top-half tbody tr.outside-half{{display:none}}.runner-table{{font-size:14px;width:max-content}}.horse-name,.person-name{{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.horse-name{{width:35ch}}.person-name{{width:25ch}}.runner-table td,.runner-table th{{padding:8px}}.runner-table td:nth-child(n+4){{text-align:right;font-variant-numeric:tabular-nums}}.snapshot-price summary{{cursor:pointer;list-style:none}}.snapshot-price summary::-webkit-details-marker{{display:none}}.snapshot-price details[open] small{{text-align:left}}.scroll{{overflow:auto}}button,select{{padding:8px}}@media(max-width:800px){{main{{grid-template-columns:1fr}}aside{{max-height:280px;overflow:auto}}}}</style></head><body><header><nav><a href="/next-five">Next five</a> · <a href="/">TB Wall / Queue</a> · <a href="{e(url(date=day,meeting=meeting,market=mid))}">Reload captures</a></nav><h1>Australian Capture Wall</h1><p><a href="http://core7070:8792/import-form">Import new files</a> · <a href="http://core7070:8792/import-ra">Import Racing Australia pages</a></p><form><label>Race date <select name="date">{options}</select></label> <button>Open card</button></form></header><main><aside aria-label="Meetings"><h2>Meetings</h2>{''.join(cards) or 'No registered Australian races yet.'}</aside><article>{race_nav}<section id="race-detail" data-t30-ready="{str(t30_ready).lower()}">{detail}</section></article></main><script>
(()=>{{
 const half=document.getElementById('top-half');
 const table=document.getElementById('runner-comparison-table');
 if(!half||!table)return;
 const rows=[...table.querySelectorAll('tbody tr')];
 const keep=Math.ceil(rows.length/2);
 rows.forEach((row,i)=>row.classList.toggle('outside-half',i>=keep));
 function update(){{
  const top=half.getAttribute('aria-pressed')==='true';
  table.classList.toggle('top-half',top);
  const visible=rows.filter((r,i)=>(!top||i<keep)).length;
  document.getElementById('cohail-status').textContent=' Showing '+visible+' of '+rows.length+' runners'+(top?' · top '+keep+' by Fair odds':'')+'.';
 }}
 [half].forEach(control=>control.addEventListener('click',()=>{{
  control.setAttribute('aria-pressed',String(control.getAttribute('aria-pressed')!=='true'));
  update();
 }}));
}})();
</script><script>
(()=>{{
 const table=document.getElementById('runner-comparison-table');
 if(!table)return;
 const headers=[...table.querySelectorAll('th.snapshot-column')];
 const columns=[...table.querySelectorAll('.snapshot-column')];
 const latest=table.dataset.latestSnapshot;
 function apply(collapsed){{
  table.classList.toggle('snapshots-collapsed',collapsed);
  columns.forEach(cell=>{{cell.hidden=collapsed && cell.dataset.snapshot!==latest;}});
  headers.forEach(header=>{{header.setAttribute('aria-expanded',String(!collapsed));}});
 }}
 apply(true);
 headers.forEach(header=>header.addEventListener('click',()=>apply(table.classList.contains('snapshots-collapsed')===false)));
}})();
</script>{auto_refresh}</body></html>'''


def response(base, path, query):
    try:
        if path=='/au-captures':return render(base,query),'text/html; charset=utf-8',200
        data=snapshot(base,query.get('date',[''])[0],query.get('market',[''])[0],query.get('slot',[''])[0])
        return json.dumps(data or {'error':'Capture unavailable or not yet synced'}),'application/json',200 if data else 404
    except (ValueError,TypeError):
        return 'Invalid capture selection','text/plain',400
