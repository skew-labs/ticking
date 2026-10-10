"""Bounded, non-mutating chart validation. Flat prices are not data corruption."""
import math,statistics
from collections import Counter
from chart_intervals import bucket_start

class CandleIntegrityError(RuntimeError):pass

def assess_candles(bars,period,now=None,*,calendar_origin=None):
    if type(period)!=int or period<1:raise ValueError('Invalid quality interval')
    if calendar_origin is not None and type(calendar_origin)!=int:raise ValueError('Invalid calendar origin')
    start=lambda stamp:bucket_start(stamp,period) if calendar_origin is None else (stamp-calendar_origin)//period*period+calendar_origin
    errors=[];good=[];previous=None;holes=0;bad_indices=set()
    for i,b in enumerate(bars):
        reasons=[];stamp=b.get('time')
        if type(stamp)!=int or stamp<0 or start(stamp)!=stamp:reasons.append('time_alignment')
        else:
            if previous is not None:
                if stamp<=previous:reasons.append('time_order')
                else:holes+=max(0,(stamp-previous)//period-1)
            previous=stamp
            if now is not None and stamp>start(int(now)):reasons.append('future_bar')
        numeric=all(type(b.get(k)) in (float,int) and math.isfinite(b[k]) for k in ('open','high','low','close','volume'))
        if not numeric:reasons.append('nonfinite_or_missing_price_volume')
        elif not 0<b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high'] or b['volume']<0:reasons.append('ohlcv_bounds')
        for k in ('count','baseVolume','quoteVolume'):
            v=b.get(k)
            if v is not None and (type(v) not in (int,float) or not math.isfinite(v) or v<0):reasons.append('invalid_'+k)
        if b.get('referenceOnly') and (b.get('count',0)!=0 or b.get('volume',0)!=0):reasons.append('reference_execution_leak')
        if b.get('synthetic') and numeric and (len({b[k] for k in ('open','high','low','close')})!=1 or b.get('count',0)!=0 or b['volume']!=0):reasons.append('invalid_carried_bar')
        if reasons:
            bad_indices.add(i)
            if len(errors)<16:errors.append(dict(index=i,time=stamp,reasons=reasons))
        else:good.append(b)
    nonflat=[(b['high']-b['low'])/b['close'] for b in good if b['high']>b['low']]
    median=statistics.median(nonflat) if nonflat else 0
    wick=[b['time'] for b in good if b['high']>b['low'] and (b['high']-b['low'])/b['close']>=max(.005,8*median) and 1-abs(b['open']-b['close'])/(b['high']-b['low'])>=.8]
    reconstructed=sum(b.get('estimateMethod') in ('linear_between_recorded_endpoints','previous_reference_close_to_recorded_price') for b in good)
    counts=dict(bars=len(bars),invalidBars=len(bad_indices),flatBars=sum(b['high']==b['low'] for b in good),carriedBars=sum(bool(b.get('synthetic')) for b in good),estimatedBars=sum(bool(b.get('estimated')) for b in good),reconstructedBars=reconstructed,referenceBars=sum(bool(b.get('referenceOnly')) for b in good),timestampHoles=holes,wickCandidates=len(wick))
    flags=[]
    if not bars:flags.append('no_records')
    if bad_indices:flags.append('integrity_failure')
    if holes:flags.append('missing_timestamps')
    if len(good)>=30 and counts['flatBars']>=.9*len(good):flags.append('sparse_price_observations')
    if len(good)>=30 and counts['carriedBars']>=.2*len(good):flags.append('carried_history')
    if len(good)>=30 and reconstructed>=.2*len(good):flags.append('reconstructed_history')
    if wick:flags.append('range_review')
    return dict(policy='chart_quality_v1',**counts,flags=flags,integrityPassed=not bad_indices,state='invalid' if bad_indices else 'unavailable' if not bars else 'needs_review' if flags else 'checked',sourceCompleteness='not_proven',errors=errors,wickTimes=wick[:16],sourceCounts=dict(Counter(s for b in good for s in b.get('sources',[]))))

def attach_quality(data,period,now=None):
    identity=data.get('identity',{})
    native=identity.get('network')=='exchange' and identity.get('venue')=='hyperliquid_public' and identity.get('historyPolicy')=='hyperliquid_retained_v1'
    quality=assess_candles(data.get('candles',[]),period,now,calendar_origin=0 if native else None)
    if not quality['integrityPassed']:raise CandleIntegrityError('Chart integrity failure: '+','.join(quality['errors'][0]['reasons']))
    return {**data,'coverage':{**data.get('coverage',{}),'chartQuality':quality}}

def recovery_assets(issues,catalog,routes,pins,existing):
    """Keep explicit routes; add exact identities from the validated mark plan."""
    result={a['mint']:dict(a) for a in existing}
    route_map={r['mint']:r for r in routes};available={a['mint']:a for a in catalog if a.get('mint')}
    for mint in issues:
        if mint not in available or mint not in route_map:continue
        route=route_map[mint];pin=pins.get(mint)
        quote=pin[0] if pin else route.get('quoteMint')
        pools=[pin[1]] if pin else []
        pools+=result.get(mint,{}).get('pools',[])
        pools += [p['pool'] for p in route.get('pools',[]) if (p.get('mint'),p.get('quoteMint')) in ((mint,quote),(quote,mint))]
        pools=list(dict.fromkeys(pools))[:8]
        priority=issues.get(mint,1) if isinstance(issues,dict) else 1
        if pools and quote:result[mint]={**result.get(mint,{}),'id':available[mint]['id'],'mint':mint,'quote':quote,'pools':pools,'repairPriority':priority,'admission':'exact_mint_quote_validated_mark_plan; provider identity rechecked before storage'}
    return list(result.values())

def recovery_lane(prior,now,history_from):
    """Alternate latest/history under a long round; neither lane can starve."""
    latest=now-prior.get('latestAt',0)>=600
    history=not prior.get('historyBoundary') and prior.get('cursor',now)>history_from
    if history and (not latest or prior.get('lastLane')=='latest'):return 'history'
    return 'latest' if latest else None

def recovery_request(prior,now,history_from,windows=()):
    """Fetch the actual incident window once, alternating with live/history work."""
    done=prior.get('requestedWindows',{})
    pending=sorted({int(t) for t in windows if type(t)==int and history_from<t<=now and str(t) not in done})
    if pending and prior.get('lastLane')!='repair':return 'repair',pending[0]
    lane=recovery_lane(prior,now,history_from)
    if lane:return lane,now//60*60 if lane=='latest' else prior.get('cursor',now//60*60)
    return ('repair',pending[0]) if pending else None

def fair_recovery_order(jobs):
    """One urgent targeted request, then oldest checks; no flood of priority work."""
    ordered=sorted(jobs,key=lambda j:(j[0],j[1],j[4]))
    continuity=lambda j:j[-2]=='latest' and j[2].get('quoteContinuity',False)
    urgent=[j for j in ordered if continuity(j) or j[-2]=='repair' and j[2].get('repairPriority',1)>=2]
    if urgent:
        first=min(urgent,key=lambda j:(not continuity(j),j[1],j[0],j[4]))
        ordered.remove(first);ordered.insert(0,first)
    return ordered

def update_incident(previous,current,pinned,now):
    """Only the exact pinned window may resolve an incident, with a receipt."""
    old=dict(previous) if previous else None;resolved=None
    def bad(window):return bool(window and ('error' in window or window.get('quality',{}).get('flags')))
    if old and old['state']!='resolved' and pinned and pinned['before']==old['before']:
        old.update(lastCheckedAt=now,quality=pinned.get('quality'))
        if bad(pinned):old['state']='unresolved'
        else:
            old.update(state='resolved',resolvedAt=now);resolved=dict(old)
    if bad(current):
        if not old or old['state']=='resolved':old=dict(before=current['before'],openedAt=now,state='unresolved')
        old.update(lastSeen=now)
    return old,resolved
