"""Read-only emitter rehearsal over the AU card and known worldwide queue races."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path

from tb_queue_summary import parse_time, read_state
from tb_queue_policy import annotate, learn
from tb_track_priority import clash_preferences
from tb_race_lifecycle import evaluate, load_lifecycle, policy


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def decision_hash(decisions):
    return hashlib.sha256(json.dumps(decisions, sort_keys=True).encode()).hexdigest()


def target_key(target):
    return str((target.get('market') or {}).get('market_id')) if target.get('status') == 'armed' else None


def simulate(markets, decisions, profiles, emitter, config, liquidity, now, *,
             target_minutes=15, window_minutes=3, poll_seconds=30, delay_seconds=0,
             active=None, active_locked=True, search_hours=4):
    """Reuse live selection/hold/rearm functions; assume release after scheduled off."""
    start_times = {r['market_id']: parse_time(r['market_start_time']) for r in markets}
    rows = {r['market_id']: {'market_id': r['market_id'], 'outcome': 'missed_window',
                           'blockers': [], 'policy_skip': None} for r in markets}
    for mid in rows:
        if decisions.get(str(mid), {}).get('action') == 'remove':
            rows[mid]['outcome'] = 'removed'
    if not markets:
        return rows, []
    clock = datetime.fromtimestamp(math.ceil(now.timestamp()/poll_seconds)*poll_seconds, timezone.utc)
    end = max(start_times.values())
    selected = set()
    events = []
    current = active
    release = None
    if current:
        scheduled = parse_time(current['market_start_time'])
        # Already delayed targets have no knowable future release. Use a bounded
        # illustrative hold, and expose this assumption in the forecast metadata.
        release = scheduled + timedelta(seconds=delay_seconds) if scheduled > now else now+timedelta(seconds=max(180,delay_seconds))
        selected.add(current['market_id'])
        if current['market_id'] in rows:
            rows[current['market_id']].update(outcome='active', projected_arm_at=None)
    old_clock = emitter.utc_now
    try:
        while clock <= end:
            emitter.utc_now = lambda: clock
            if current and clock >= release:
                current = None
            pool = [r for r in markets if r['market_id'] not in selected and
                    clock < start_times[r['market_id']] <= clock+timedelta(hours=search_hours)]
            pool.sort(key=lambda r: (start_times[r['market_id']], r['market_id']))
            pool = pool[:100]
            alternative = (lambda r: emitter.score_market(r, config)[0] > -9999) if config.get('enabled') else None
            learned = {r['market_id']: r for r in annotate(pool[:10], decisions, profiles, clock, alternative)}
            allowed = []
            for r in pool:
                mid = r['market_id']
                if decisions.get(str(mid), {}).get('action') == 'remove':
                    continue
                clue = learned.get(mid, {})
                if clue.get('clash_skip'):
                    if target_minutes-window_minutes <= (start_times[mid]-clock).total_seconds()/60 <= target_minutes+window_minutes:
                        rows[mid]['policy_skip'] = clue['clash_skip']
                    continue
                allowed.append(r)
            alternative = (lambda r: not emitter.priority_excluded(r, config)) if config.get('enabled') else None
            allowed, ranked_skips = clash_preferences(allowed, decisions,
                config.get('australian_card_priority', {}), clock,
                clash_seconds=(target_minutes+window_minutes)*60,
                minimum_alternative_seconds=(target_minutes-window_minutes)*60, alternative_allowed=alternative)
            for skip in ranked_skips:
                mid = skip['market_id']
                if target_minutes-window_minutes <= (start_times[mid]-clock).total_seconds()/60 <= target_minutes+window_minutes:
                    rows[mid]['policy_skip'] = skip
            candidate = None
            rearming = False
            if current:
                if config.get('enabled') and config.get('rearm_enabled'):
                    lock_name = config.get('hard_lock_snapshot', 'market_book_t15.json')
                    offset = {'market_book_t15.json':900,'market_book_t10.json':600,'market_book_t5.json':300,'market_book_t2.json':120,'market_book_t30.json':30}.get(lock_name,900)
                    is_actual = active and current['market_id'] == active['market_id']
                    locked = (is_actual and active_locked) or clock >= parse_time(current['market_start_time'])-timedelta(seconds=offset)
                    if not locked:
                        challenger, _ = emitter.choose_rearm_challenger(current, allowed, config, liquidity)
                        if challenger:
                            candidate = challenger['market']; rearming = True
                for r in allowed:
                    mid = r['market_id']
                    minutes = (start_times[mid]-clock).total_seconds()/60
                    if target_minutes-window_minutes <= minutes <= target_minutes+window_minutes:
                        if current['market_id'] not in rows[mid]['blockers']:
                            rows[mid]['blockers'].append(current['market_id'])
            else:
                candidate, _ = emitter.find_t15_candidate(allowed, target_minutes, window_minutes, config)
                if candidate and config.get('enabled') and config.get('hold_for_better_candidate'):
                    if emitter.choose_hold_challenger(candidate, allowed, config, liquidity):
                        candidate = None
            if candidate:
                mid = candidate['market_id']
                if rearming and current['market_id'] in rows:
                    rows[current['market_id']]['outcome'] = 'replaced_before_lock'
                selected.add(mid)
                rows[mid].update(outcome='projected_arm', projected_arm_at=iso(clock), policy_skip=None)
                events.append({'market_id':mid,'track':candidate.get('track'),'market_name':candidate.get('market_name'),
                               'projected_arm_at':iso(clock),'market_start_time':candidate['market_start_time'],
                               'action':'rearm' if rearming else 'arm'})
                current = candidate
                release = start_times[mid]+timedelta(seconds=delay_seconds)
            clock += timedelta(seconds=poll_seconds)
    finally:
        emitter.utc_now = old_clock
    for row in rows.values():
        if row['outcome'] == 'missed_window':
            if row['policy_skip']:
                row['outcome'] = 'track_priority_skip' if row['policy_skip'].get('preference_kind') == 'australian_track_priority' else 'learned_skip'
            elif row['blockers']:
                row['outcome'] = 'timing_blocked'
    return rows, events


def forecast(card, queue, target, emitter, config, profiles, liquidity, now, *,
             active=None, active_locked=True, warnings=None, config_source='defaults', settings=None):
    settings = settings or {}
    target_minutes = target.get('target_minutes_before_jump') or 15
    window_minutes = target.get('window_minutes') if target.get('window_minutes') is not None else 3
    if not (isinstance(target_minutes,(int,float)) and isinstance(window_minutes,(int,float)) and 0 <= window_minutes < target_minutes <= 120):
        raise ValueError('Invalid emitter arming parameters')
    decisions = queue['decisions']
    eligible_card = [r for r in card['races'] if r.get('market_status') != 'CLOSED' and not r.get('inplay') and parse_time(r['market_start_time']) > now]
    markets = {r['market_id']:dict(r) for r in eligible_card}
    queue_stamp = parse_time(queue.get('updated_at'))
    queue_fresh = queue_stamp and -5 <= (now-queue_stamp).total_seconds() <= 120
    if queue_fresh:
        for r in queue.get('races', []):
            stamp = parse_time(r.get('market_start_time'))
            if r.get('market_id') not in markets and stamp and stamp > now:
                # Queue retains field size rather than runner metadata. The live
                # scorer uses list length; these placeholders never leave here.
                markets[r['market_id']] = {**r,'runners':[None]*int(r.get('runner_count') or 0)}
    if active:
        markets.setdefault(active['market_id'], active)
    inputs = list(markets.values())
    nominal, events = simulate(inputs, decisions, profiles, emitter, config, liquidity, now,
        target_minutes=target_minutes, window_minutes=window_minutes, active=active, active_locked=active_locked)
    delayed, _ = simulate(inputs, decisions, profiles, emitter, config, liquidity, now,
        target_minutes=target_minutes, window_minutes=window_minutes, active=active, active_locked=active_locked, delay_seconds=180)
    card_ids = {r['market_id'] for r in card['races']}
    sequence = [e for e in events if e['market_id'] in card_ids]
    number = {e['market_id']:i+1 for i,e in enumerate(sequence)}
    names = {r['market_id']:f"{r.get('track','')} · {r.get('market_name','')}" for r in inputs}
    rows = {}
    for race in card['races']:
        mid = race['market_id'];start = parse_time(race['market_start_time'])
        row = dict(nominal.get(mid, {'outcome':'elapsed_or_closed','blockers':[],'policy_skip':None}))
        row.update(sequence=number.get(mid), window_open=iso(start-timedelta(minutes=target_minutes+window_minutes)),
                   window_close=iso(start-timedelta(minutes=target_minutes-window_minutes)),
                   timing_clashes=[], delay_risk=False)
        for other in eligible_card:
            if other['market_id'] == mid or start <= now:
                continue
            gap = abs((parse_time(other['market_start_time'])-start).total_seconds())
            if gap < (target_minutes+window_minutes)*60:
                row['timing_clashes'].append({'market_id':other['market_id'], 'race':names[other['market_id']],
                    'gap_seconds':int(gap), 'kind':'window_conflict' if gap < (target_minutes-window_minutes)*60 else 'tight_turnaround'})
        row['delay_risk'] = row['outcome'] == 'projected_arm' and delayed.get(mid,{}).get('outcome') != 'projected_arm'
        blockers = ', '.join(names.get(mid,mid) for mid in row['blockers'])
        if row['outcome'] == 'timing_blocked':
            reason = f'Arming window overlaps observation of {blockers}.'
        elif row['outcome'] == 'projected_arm':
            reason = 'Projected selection assuming on-time starts.'
            if row['delay_risk']:reason += ' A three-minute release delay changes this selection.'
        elif row['outcome'] in ('learned_skip', 'track_priority_skip'):
            reason = row['policy_skip']['reason']
        else:
            reason = {'removed':'Explicit Remove; excluded from automatic selection.',
                      'active':'Already armed; actual observation controls release.',
                      'elapsed_or_closed':'Already started, elapsed or closed; not forecast as a new target.',
                      'replaced_before_lock':'Projected to be replaced by a stronger race before the snapshot lock.',
                      'missed_window':'No projected selection inside the remaining arming window.'}.get(row['outcome'],'Unconfirmed')
        row['reason'] = reason
        rows[mid] = row
    notes = list(warnings or [])
    if not queue_fresh:notes.append('Worldwide rolling queue is stale; only the Australian card is modelled.')
    if active and parse_time(active['market_start_time']) <= now:
        notes.append('Current target is delayed; its release is uncertain (three-minute illustrative hold).')
    return {'schema':'tb_card_emitter_forecast/v1','generated_at':iso(now),'decision_hash':decision_hash(decisions),
            'target_key':target_key(target),'config_source':config_source,
            'track_priority': config.get('australian_card_priority', {}),
            'selection_mode':'priority' if config.get('enabled') else 'earliest',
            'target_minutes':target_minutes,'window_minutes':window_minutes,'poll_seconds':30,
            'delay_scenario_seconds':180,'max_observation_delay_seconds':settings.get('max_delay_seconds',1800),
            'scope':'Australian card plus fresh known worldwide queue races; other overseas races are not yet included.',
            'assumption':'Scheduled starts release the observer; 30-second model ticks omit service runtime and timer phase. Manual requests and future snapshots are not predictable.',
            'warnings':notes,'sequence':sequence,'races':rows,
            'counts':{'projected':sum(r['outcome']=='projected_arm' for r in rows.values()),
                      'track_priority_skips':sum(r['outcome']=='track_priority_skip' for r in rows.values()),
                      'blocked':sum(r['outcome']=='timing_blocked' for r in rows.values()),
                      'clashing':sum(bool(r['timing_clashes']) for r in rows.values()),
                      'delay_risk':sum(r['delay_risk'] for r in rows.values())}}


def build_forecast(state_dir, card, now):
    """Collector-only: load a private emitter module and never invoke its main/API."""
    from tb_race_queue import queue_lock, load_queue_for_update
    state_dir = Path(state_dir)
    path = Path(__file__).with_name('betfair_t15_emit.py')
    spec = importlib.util.spec_from_file_location('_tb_forecast_emitter',path)
    emitter = importlib.util.module_from_spec(spec);spec.loader.exec_module(emitter)
    warnings = []
    priority_path = state_dir.parent/'config/race_priority.json'
    source = 'defaults'
    if priority_path.exists():
        try:
            raw = json.loads(priority_path.read_text())
            if not isinstance(raw,dict):raise ValueError('Expected object')
            source = 'race_priority.json'
        except (ValueError,OSError) as exc:
            warnings.append(f'Priority configuration is invalid ({type(exc).__name__}); the emitter falls back to earliest-race defaults.')
    config = emitter.load_priority_config(state_dir.parent/'config')
    if config.get('australian_card_priority_error'):
        warnings.append(config['australian_card_priority_error'])
    liquidity = emitter.load_track_liquidity(state_dir, config)
    with queue_lock(state_dir):
        queue = load_queue_for_update(state_dir)
        if not queue:raise RuntimeError('Queue unavailable')
        profiles = learn(state_dir, now)
    target = read_state(state_dir/'betfair_t15_target.json') or {}
    active = target.get('market') if target.get('status') == 'armed' else None
    settings = policy(state_dir.parent)
    if active and not evaluate(active, load_lifecycle(state_dir.parent, active), now, settings)['hold']:
        active = None
    locked = emitter.active_target_is_hard_locked(state_dir, active, config)[0] if active else True
    return forecast(card, queue, target, emitter, config, profiles, liquidity, now,
                    active=active, active_locked=locked, warnings=warnings, config_source=source, settings=settings)
