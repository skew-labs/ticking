"""Attributed EVM display composite. Read-only; execution tape is never rewritten.

Same contract DEX fills + pool observations + explicitly bound venue references.
CEX/quote OHLC is a display reference with zero owned executions/volume. Empty
buckets may carry the previous close, only inside an observed window.
"""
import bisect, copy, hashlib, json, math, os, sqlite3, threading, time
from pathlib import Path
from statistics import median
from contextlib import closing

ROOT=Path(os.environ.get('TICKING_REFERENCE_ROOT','/srv/skew/solana-mingle-20261007/data'))
SECONDS=Path(os.environ.get('TICKING_SECONDS_ROOT','/srv/skew/em-realtime/sec-bars/out'))
BINDINGS=json.loads(Path(__file__).with_name('evm_reference_bindings.json').read_text())
POLICY='evm_attributed_composite_v1'
_cache={};_lock=threading.Lock()
FIELDS=('open','high','low','close')

def valid(b):
    return all(type(b.get(k)) in (int,float) and math.isfinite(b[k]) and b[k]>0 for k in FIELDS) and b['high']>=max(b['open'],b['close']) and b['low']<=min(b['open'],b['close']) and b['low']<=b['high']

def sql(path,query,args=()):
    if not path.exists():return []
    key=(str(path),query,tuple(args));now=time.monotonic()
    with _lock:
        hit=_cache.get(key)
        if hit and now-hit[0]<.7:return hit[1]
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as c:
            c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');deadline=time.monotonic()+.8
            c.set_progress_handler(lambda:int(time.monotonic()>deadline),5000)
            rows=[dict(x) for x in c.execute(query,args)]
    except (sqlite3.Error,OSError):return []
    with _lock:
        for expired in [k for k,v in _cache.items() if now-v[0]>2]:_cache.pop(expired,None)
        if len(rows)<=8000:_cache[key]=(now,rows)
        while sum(len(v[1]) for v in _cache.values())>20000 or len(_cache)>64:_cache.pop(next(iter(_cache)))
    return rows

def fx_rows(start,end):
    rows=sql(ROOT/'price-support/support.sqlite3',"SELECT asset,time,close,digest,received FROM candles WHERE asset IN ('FX:USDCUSDT','FX:USDCUSD') AND time>=? AND time<? ORDER BY time,received",(start-120,end))
    result={}
    for a in ('FX:USDCUSDT','FX:USDCUSD'):
        result[a]={r['time']:r for r in rows if r['asset']==a and .5<r['close']<1.5}
    return result

def usd_factor(fx,stamp,quote='USDT',live=False):
    minute=stamp//60*60;parts=[]
    for key in (('FX:USDCUSD','FX:USDCUSDT') if quote=='USDT' else ('FX:USDCUSD',)):
        values=fx.get(key,{})
        row=values.get(minute)
        if row is None and live:
            keys=sorted(values);i=bisect.bisect_right(keys,minute)-1
            if i>=0 and minute-keys[i]<=120:row=values[keys[i]]
        if row is None:return None
        parts.append(row)
    factor=parts[0]['close']/(parts[1]['close'] if quote=='USDT' else 1)
    return factor,{'receipts':[r['digest'] for r in parts],'observedAt':min(r['time'] for r in parts),'quote':quote,'usdRate':factor}

def reference(b,source,**extra):
    return {**b,'volume':0,'baseVolume':0,'quoteVolume':0,'count':0,'sources':[source],'source':source,'synthetic':False,'referenceOnly':True,'estimated':True,'quoteCurrency':'USD',**extra}

def rollup(records,period):
    groups={}
    for r in sorted(records,key=lambda x:x['time']):
        if not valid(r):continue
        t=int(r['time'])//period*period;b=groups.get(t)
        if b is None:groups[t]={**r,'time':t,'sourceTime':r.get('sourceTime',r['time'])};continue
        b.update(high=max(b['high'],r['high']),low=min(b['low'],r['low']),close=r['close'],sourceTime=max(b.get('sourceTime',0),r.get('sourceTime',r['time'])))
        for k in ('count','volume','baseVolume','quoteVolume','referenceSamples'):b[k]=b.get(k,0)+r.get(k,0)
        b['sources']=list(dict.fromkeys(b.get('sources',[])+r.get('sources',[])))
        b['provisional']=b.get('provisional',False) or r.get('provisional',False)
        # A one-minute conversion receipt cannot describe a multi-minute bar.
        if period>60:b.pop('conversionReceipt',None)
    return groups

def observations(mid,start,end,period):
    """One source range per bucket. No joining exchange and pool extrema."""
    native=sql(SECONDS/'bars_1s_onchain.sqlite3',"SELECT time,open,high,low,close,volume,quote_volume,count FROM bars_1s WHERE market=? AND time>=? AND time<? AND source='tip_hot' ORDER BY time DESC LIMIT 24000",(mid,start*1000,end*1000))
    fills=[{**r,'time':r['time']//1000,'sourceTime':r['time']//1000,'volume':r['quote_volume'],'baseVolume':r['volume'],'quoteVolume':r['quote_volume'],'sources':['onchain_dex_swaps'],'source':'tip_hot','synthetic':False} for r in native if r['count']>0]
    # Corrected, independent FastFeed records: never read legacy mixed/clamped rows.
    ff=sql(SECONDS/'fastfeed_observations.sqlite3',"SELECT time,open,high,low,close,samples,last,receipt FROM bars WHERE market=? AND time>=? AND time<? ORDER BY time DESC LIMIT 24000",(mid,start,end))
    quotes=[reference(r,'fastfeed',sourceTime=r['last']/1000,referenceSamples=r['samples'],provisional=True,estimateMethod='observed_pool_state') for r in ff]
    sources=[rollup(quotes,period)];binding=BINDINGS.get(mid)
    if not binding:return rollup(fills,period),sources
    fx=fx_rows(start,end);asset=binding['referenceAsset'];identity=binding['identityReceipt']
    # Retained native exchange candles and their original receipt digests.
    rows=sql(ROOT/'price-support/support.sqlite3',"SELECT provider,market,quote,time,open,high,low,close,digest,received FROM candles WHERE asset=? AND time>=? AND time<? ORDER BY time DESC LIMIT 70000",(asset,start,end)) if asset in ('GLOBAL:ETH','GLOBAL:BTC') else []
    by_time={}
    for r in rows:
        if r['quote'] not in ('USD','USDT','USDC'):continue
        converted=(1,None) if r['quote']=='USD' else usd_factor(fx,r['time'],r['quote'])
        if converted is None:continue
        factor,receipt=converted;b=reference({**r,**{k:r[k]*factor for k in FIELDS}},r['provider']+'_spot',sourceTime=r['time'],receipt=r['digest'],conversionReceipt=receipt,identityReceipt=identity,referenceSamples=1,estimateMethod='same_underlying_spot')
        if valid(b):by_time.setdefault(r['time'],[]).append(b)
    chosen=[]
    for stamp,candidates in by_time.items():
        anchor=median(b['close'] for b in candidates);peers=[b for b in candidates if abs(b['close']/anchor-1)<=.015]
        if not peers:continue
        b=min(peers,key=lambda x:abs(x['close']-anchor));chosen.append({**b,'sources':list(dict.fromkeys(x['source'] for x in peers))})
    sources.append(rollup(chosen,period))
    # Closed/WS exchange seconds only for pinned contract→underlying bindings.
    # A venue row contributes a reference price, never a token execution.
    if period<60 or not rows:
        raw=sql(SECONDS/'bars_1s.sqlite3',"SELECT time,open,high,low,close,source FROM bars_1s WHERE market=? AND time>=? AND time<? AND source IN ('binance_1s','binance','binance_ws') ORDER BY time DESC LIMIT 24000",(mid,start*1000,end*1000));exchange=[]
        for r in raw:
            stamp=r['time']//1000;converted=usd_factor(fx,stamp,live=end>=int(time.time())-120)
            if converted is None:continue
            factor,receipt=converted;exchange.append(reference({**r,'time':stamp,**{k:r[k]*factor for k in FIELDS}},'binance_spot',sourceTime=stamp,conversionReceipt=receipt,identityReceipt=identity,referenceSamples=1,estimateMethod='same_underlying_spot'))
        sources.append(rollup(exchange,period))
    # Actual retained orderbook midpoint ranges (not bid/ask extrema).
    book_bind=sql(ROOT/'book-reference/current.sqlite3','SELECT market,identity FROM bindings WHERE asset=?',(asset,))
    books=[]
    if period>=60:
        for bound in book_bind:
            for r in sql(ROOT/'book-reference/current.sqlite3','SELECT time,o,h,l,c,samples,last,receipts FROM bars WHERE market=? AND time>=? AND time<? ORDER BY time LIMIT 10081',(bound['market'],start,end)):
                converted=usd_factor(fx,r['time'])
                if converted is None:continue
                factor,receipt=converted;books.append(reference({'time':r['time'],**{k:r[v]*factor for k,v in zip(FIELDS,('o','h','l','c'))}},'okx_orderbook',sourceTime=r['last']/1000,receipt=r['receipts'],identityReceipt=bound['identity'],conversionReceipt=receipt,referenceSamples=r['samples'],estimateMethod='observed_depth_midpoints'))
    if end>=int(time.time())-10:
        for bound in book_bind:
            for r in sql(ROOT/'book-reference/current.sqlite3',"SELECT time,quote,state FROM books WHERE market=?",(bound['market'],)):
                stamp=r['time']//1000
                if r['state']!='available' or stamp<start or stamp>=end or time.time()-stamp>10:continue
                try:q=json.loads(r['quote']);price=float(q['mid'])
                except (ValueError,KeyError,TypeError):continue
                converted=usd_factor(fx,stamp,live=True)
                if converted is None:continue
                factor,receipt=converted;books.append(reference({'time':stamp,**{k:price*factor for k in FIELDS}},'okx_orderbook',sourceTime=stamp,identityReceipt=bound['identity'],conversionReceipt=receipt,referenceSamples=1,estimateMethod='observed_depth_midpoints'))
    sources.append(rollup(books,period))
    return rollup(fills,period),sources

def choose(native,candidates,period=60,market=None):
    refs=[b for b in candidates if valid(b)]
    if not refs:return native
    # Healthy local execution ranges keep the token's actual premium/discount.
    if native and native.get('count',0)>1 and native['high']>native['low']:
        recent=[b for b in refs if b.get('sourceTime',0)>native.get('sourceTime',native['time']) and time.time()-b.get('sourceTime',0)<10 and abs(b['close']/native['close']-1)<=.02]
        if not recent:return native
        quote=max(recent,key=lambda b:b['sourceTime']);close=quote['close']
        return {**native,'close':close,'high':max(native['high'],close),'low':min(native['low'],close),'sourceTime':quote['sourceTime'],'sources':list(dict.fromkeys(native.get('sources',['onchain_dex_swaps'])+quote.get('sources',[]))),'provisional':True,'mixedSource':True,'priceObservation':{'source':quote['source'],'time':quote['sourceTime'],'price':close,'receipt':quote.get('receipt'),'conversionReceipt':quote.get('conversionReceipt')},'estimateMethod':'live_observed_quote_over_execution_bar'}
    peers=refs
    if native:
        peers=[b for b in refs if abs(b['close']/native['close']-1)<=.1]
        if not peers:return native
    moving=[b for b in peers if b['high']>b['low']];peers=moving or peers
    result=max(peers,key=lambda b:(b.get('sourceTime',b['time']),b.get('referenceSamples',0)))
    # Retain independently observed token volume when the display uses a reference.
    if native:
        result={**result,'observedExecutionCount':native.get('count',0),'observedBaseVolume':native.get('baseVolume',0)}
        # The SDK's independent-volume contract represents minute-or-longer
        # retained buckets. Sub-minute execution receipts stay in the tape.
        if period>=60:
            result['volumeObservation']={'policy':'independent_retained_volume_v1','basis':'pinned_venue_trades','baseVolume':native.get('baseVolume',0),'quoteVolume':native.get('quoteVolume',native.get('volume',0)),'count':native.get('count',0),'recordedMinutes':None,'sourcePeriodSeconds':period,'complete':False,'coverage':'retained_partial','priceIndependent':True,'sources':['onchain_dex_swaps'],'quoteCurrencies':['USD'],'market':market}

    return result

def continuous(bars,period,limit,carry=True):
    if not bars:return []
    bars=sorted(bars,key=lambda b:b['time']);end=bars[-1]['time'];lower=max(bars[0]['time'],end-(limit-1)*period);by_time={b['time']:b for b in bars};out=[];previous=None
    for b in bars:
        if b['time']<=lower:previous=b
        else:break
    for stamp in range(lower,end+period,period):
        b=by_time.get(stamp)
        if b is not None:previous=b;out.append(b)
        elif previous and carry:
            out.append({'time':stamp,**{k:previous['close'] for k in FIELDS},'volume':0,'baseVolume':0,'quoteVolume':0,'count':0,'synthetic':True,'referenceOnly':True,'estimated':False,'sourceTime':previous.get('sourceTime',previous['time']),'sources':['display_carry'],'source':'display_carry','gapKind':'unverified_no_record','quoteCurrency':'USD'})
    return out[-limit:]

def compose(raw,network,mint,period,limit,before=None,gap='carry',history='full'):
    """Input/output engine timestamps in seconds. Stable requested identity."""
    if network not in ('arbitrum','robinhood'):return raw
    mid=network+':'+mint.lower();now=int(time.time());end=(before or now+period);end=min(end,now+period)
    # Query latest bounded suffix; older native history stays available untouched.
    start=max(0,end-min(7*86400,max(120,period*(limit+1))))
    fills,sources=observations(mid,start,end,period)
    native={int(b['time']):{**b,'sources':b.get('sources') or ['onchain_dex_swaps']} for b in raw.get('candles',[]) if not b.get('synthetic') and valid(b) and (not before or b['time']<before)}
    if period<60:native=fills
    elif fills:
        # Only this page's open bucket may be advanced by the finer execution store.
        t=max(fills);b=fills[t];old=native.get(t)
        if t>=now//period*period and (not old or b.get('sourceTime',t)>old.get('sourceTime',t)):native[t]=b
    if history=='executions':selected=list(native.values())
    else:
        stamps=set(native)
        for source in sources:stamps.update(source)
        selected=[choose(native.get(t),[s[t] for s in sources if t in s],period,mid) for t in sorted(stamps)]
    selected=[b for b in selected if b and (not before or b['time']<before)]
    original_count=len(selected);bars=continuous(selected,period,limit,carry=gap=='carry')
    latest=max([b.get('sourceTime',b['time']) for b in selected],default=0)
    coverage={**raw.get('coverage',{}),'displayPolicy':POLICY,'referenceBinding':BINDINGS.get(mid),'referenceBars':sum(bool(b.get('estimated')) for b in bars),'carriedBars':sum(bool(b.get('synthetic')) for b in bars),'sources':sorted({s for b in bars for s in b.get('sources',[])}),'referencePolicy':'Same-contract DEX executions and pool observations with explicitly bound, USD-converted spot and orderbook references. Reference/carry candles do not add token executions or volume. Carry ends at the latest observed record.'}
    return {**raw,'candles':bars,'observedThrough':latest or raw.get('observedThrough'),'sequence':int(time.time()*1000),'hasMore':bool(raw.get('hasMore')) or original_count>len(bars),'nextBefore':bars[0]['time'] if bars else None,'fillPolicy':gap,'carriedBars':coverage['carriedBars'],'coverage':coverage,'gapDisclosure':coverage['referencePolicy'],'observationAvailable':bool(bars)}
