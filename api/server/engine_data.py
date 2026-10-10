"""Customer/public chart contract backed exclusively by our execution store."""
import json,math,os,re,time,threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor,Future,TimeoutError as FutureTimeout
from pathlib import Path
from urllib.parse import urlencode,urlsplit
import httpx
from fastapi import HTTPException
from . import global_data
from .changes import feed, dependencies

CATALOG=json.loads(Path(__file__).with_name('catalog.json').read_text())
SECRET_FILE=Path(os.environ.get('TICKING_BRIDGE_SECRET_FILE','./api/.state/engine-bridge.token'))
USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
QUOTES={USDC:'USDC','So11111111111111111111111111111111111111112':'SOL','Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB':'USDT'}
cache=OrderedDict();lock=threading.Lock();cache_bytes=0;MAX_CACHE_BYTES=16*1024**2
inflight={};MAX_INFLIGHT=128;RETAIN_SECONDS=120
RETAIN_PATHS={'/candles','/token/candles','/exchange/candles','/reference/candles','/token/resolve'}
client=httpx.Client(base_url=os.environ.get('TICKING_ENGINE_BRIDGE','http://127.0.0.1:19643'),timeout=8,limits=httpx.Limits(max_connections=8,max_keepalive_connections=4))
catalog_workers=ThreadPoolExecutor(max_workers=4,thread_name_prefix='ticking-catalog')
catalog_lock=threading.Lock();catalog_snapshot=None;token_catalog_snapshot=None

def address(value):return isinstance(value,str) and bool(re.fullmatch('[1-9A-HJ-NP-Za-km-z]{32,44}',value))
def origin(value):
    try:
        u=urlsplit(value)
        if u.username or u.password or u.path or u.query or u.fragment or u.scheme!='https' and not (u.scheme=='http' and u.hostname in {'localhost','127.0.0.1'}):raise ValueError()
        if not u.hostname or u.netloc.lower()!=u.netloc:raise ValueError()
        return value
    except (ValueError,TypeError):raise HTTPException(422,'Use an HTTPS app origin without a path.')

def get(path,**query):
    global cache_bytes
    key=path+'?'+urlencode(sorted((k,v) for k,v in query.items() if v is not None))
    generation=feed.fingerprint(dependencies(path,query))
    with lock:
        row=cache.get(key)
        ttl=1 if path in {'/watch/status','/token/resolve'} else .25 if path in {'/candles','/trades','/token/candles','/token/trades','/exchange/candles','/reference/candles'} and not query.get('before') else 5
        if row and row[2]==generation and time.monotonic()-row[0]<ttl:
            cache.move_to_end(key);return json.loads(row[1])
        flight_key=(key,generation);pending=inflight.get(flight_key);owner=pending is None
        if owner:
            if len(inflight)>=MAX_INFLIGHT:raise HTTPException(503,'Chart reads are busy. Retry shortly.',headers={'Retry-After':'1'})
            pending=Future();inflight[flight_key]=pending
    if not owner:
        try:return json.loads(pending.result(timeout=9))
        except FutureTimeout:raise HTTPException(503,'Chart read timed out. Retry shortly.',headers={'Retry-After':'1'})
    def retained():
        # Exact request only, no tape/credential/validation fallback. A restart
        # or config change invalidates identities even during a store outage.
        if path not in RETAIN_PATHS or not row or time.monotonic()-row[0]>RETAIN_SECONDS:return None
        current_generation=feed.fingerprint(dependencies(path,query))
        if row[2][0]!=current_generation[0] or dict(row[2][1]).get('config')!=dict(current_generation[1]).get('config'):return None
        previous=json.loads(row[1])
        if not previous.get('candles') and not (path=='/token/resolve' and previous.get('ready')):return None
        previous.update(stale=True,retainedSnapshot={'reason':'store_temporarily_unavailable','ageSeconds':round(time.monotonic()-row[0],3),'correctionPending':row[2]!=current_generation or not feed.connected})
        previous['live']=False
        return previous
    try:
        try:
            r=client.get(key,headers={'Authorization':'Bearer '+SECRET_FILE.read_text().strip()})
            if r.status_code==422:raise HTTPException(422,'Unsupported engine query or unverified pool.')
            r.raise_for_status();data=r.json()
            encoded=json.dumps(data,separators=(',',':'))
            with lock:
                # A slower old-generation read must not evict a correction
                # that already completed for the new committed generation.
                if feed.fingerprint(dependencies(path,query))==generation:
                    old=cache.pop(key,None)
                    if old:cache_bytes-=len(old[1])
                    cache[key]=(time.monotonic(),encoded,generation);cache_bytes+=len(encoded)
                    while len(cache)>512 or cache_bytes>MAX_CACHE_BYTES:
                        _,removed=cache.popitem(last=False);cache_bytes-=len(removed[1])
        except (httpx.HTTPError,OSError,ValueError) as error:
            transient=isinstance(error,(httpx.TransportError,OSError)) or isinstance(error,httpx.HTTPStatusError) and error.response.status_code>=500
            data=retained() if transient else None
            if data is None:raise HTTPException(503,'The execution store is temporarily unavailable. Retry shortly.',headers={'Retry-After':'10'})
            encoded=json.dumps(data,separators=(',',':'))
        pending.set_result(encoded)
        return json.loads(encoded)
    except BaseException as error:
        pending.set_exception(error);raise
    finally:
        with lock:inflight.pop(flight_key,None)

def resolve(mint=None,pool=None,market=None,quote=None,aggregation='pool'):
    if global_data.chosen(market):
        if mint or pool or quote:raise HTTPException(422,'Exchange instruments require an explicit market without a Solana selector.')
        t=global_data.chosen(market)
        return {**t,'identity':{'network':'exchange','market':t['market'],'venue':t['source']},'aggregation':'exchange','ready':True,'pools':[],'preferredPool':None}
    if market:
        if mint or pool:raise HTTPException(422,'Supply one mint, pool or market.')
        t=next((t for t in CATALOG if market in {t['id'],t['symbol'],t['symbol']+'-USDC'}),None)
        if t:mint=t['mint'];quote=USDC if market.endswith('-USDC') else quote
        elif address(market):mint=market
        else:raise HTTPException(404,'Unknown market. Use its Solana mint or pool address.')
    if bool(mint)==bool(pool) or not address(mint or pool) or quote and not address(quote):raise HTTPException(422,'Supply one valid Solana mint or pool address.')
    if aggregation=='token':
        if pool:raise HTTPException(422,'Token references require a mint.')
        r=get('/token/resolve',token=mint)
        if quote and r.get('quoteMint')!=quote:raise HTTPException(422,'The requested unit does not match this token reference.')
        return r
    if aggregation!='pool':raise HTTPException(422,'aggregation must be pool or token.')
    r=get('/resolve',token=mint,pool=pool,quoteMint=quote)
    if mint and not quote:
        ready=[p for p in r['pools'] if p['minuteCandles']]
        rank={k:i for i,k in enumerate(QUOTES)}
        if ready:r['preferredPool']=min(ready,key=lambda p:(0 if p.get('observedThrough',0) and time.time()-p['observedThrough']<120 else 1,rank.get(p['quoteMint'],99),-p['minuteCandles'],p['pool']))
    return r

def analytics(aggregation='pool'):
    if aggregation=='token':
        catalog={t['mint']:t for t in CATALOG};rows=[];volumes={};fills=0
        for r in get('/token/analytics')['tokens']:
            t=catalog.get(r['mint'])
            if not t:continue
            fills+=r['fills'];volumes[r['quoteMint']]=volumes.get(r['quoteMint'],0)+r['volume']
            rows.append({**r,'id':t['id'],'symbol':t['symbol'],'pool':None,'quote':QUOTES.get(r['quoteMint'],r['quoteMint'])})
        return {'mode':'executed_history','aggregation':'token','listed':len(catalog),'chartable':len(rows),'pending':len(catalog)-len(rows),'fills':fills,'pools':None,'tokens':rows,'volumes':[{'quote':QUOTES.get(k,k),'quoteMint':k,'volume':v} for k,v in volumes.items()],'scope':'Latest 300 recorded reference minutes per asset. Windows differ. Eligible physical pool legs; token-side totals can count a leg twice. Quote units stay separate. Partial retained coverage, not chain-wide or 24h volume.','live':False}
    catalog=markets_data();rows=[];volumes={};seen=set();fills=0
    for t in catalog['markets']:
        if not t['ready']:continue
        d=candles(mint=t['mint'],pool_pin=t['pool'],interval='1m',limit=300)
        bars=d['candles']
        if not bars:continue
        count=sum(b['count'] for b in bars);volume=math.fsum(b['volume'] for b in bars)
        if d['pool'] not in seen:
            seen.add(d['pool']);fills+=count
            volumes[d['quoteMint']]=volumes.get(d['quoteMint'],0)+volume
        rows.append({'id':t['id'],'symbol':t['symbol'],'mint':t['mint'],'pool':d['pool'],'quote':d['quote'],'quoteMint':d['quoteMint'],'volume':volume,'fills':count,'change':(bars[-1]['close']/bars[0]['open']-1)*100,'start':bars[0]['time'],'end':bars[-1]['time'],'stale':d['stale']})
    return {'mode':'executed_history','listed':len(catalog['markets']),'chartable':len(rows),'pending':len(catalog['markets'])-len(rows),'fills':fills,'pools':len(seen),'tokens':rows,
            'volumes':[{'quote':QUOTES.get(k,k),'quoteMint':k,'volume':v} for k,v in volumes.items()],
            'scope':'Latest 300 recorded minute candles per selected pool. Windows differ. Quote units are kept separate; duplicate selected pools count once. Partial coverage, not total Solana or 24h volume.','live':False}

def chosen(mint=None,pool=None,market=None,quote=None):
    r=resolve(mint,pool,market,quote)
    if not r['preferredPool']:raise HTTPException(409,{'code':'history_pending','state':r['state'],'pools':r['pools'],'message':'No recorded executions for this selection yet.'})
    return r['preferredPool']

def pinned(pool,mint=None):
    r=resolve(pool=pool)
    if not r['pools']:raise HTTPException(404,'Unknown pool.')
    p=r['pools'][0]
    if mint and mint not in {p['baseMint'],p['quoteMint']}:raise HTTPException(422,'Mint does not belong to this pool.')
    if mint==p['quoteMint']:p={**p,'baseMint':mint,'quoteMint':p['baseMint'],'inverted':True}
    return p

def candles(market=None,interval='1m',limit=300,mint=None,pool=None,before=None,quote=None,pool_pin=None,gap_policy='recorded',aggregation='pool',history='executions'):
    if global_data.chosen(market):
        if mint or pool or pool_pin or quote:raise HTTPException(422,'Do not combine exchange instruments with Solana selectors.')
        return global_data.candles(market,interval,limit,before,gap_policy)
    match=re.fullmatch(r'([1-9][0-9]{0,4})(s|m|h|d|w)',interval.lower()) if isinstance(interval,str) else None
    seconds=int(match[1])*{'s':1,'m':60,'h':3600,'d':86400,'w':604800}[match[2]] if match else 0
    if not 1<=seconds<=604800 or type(limit)!=int or not 1<=limit<=600:raise HTTPException(422,'Use 1s–1w and a page size of 1–600.')
    if before is not None and (type(before)!=int or not 0<before<2**53):raise HTTPException(422,'before is an exclusive Unix-millisecond cursor.')
    if gap_policy not in {'recorded','carry'}:raise HTTPException(422,'gap_policy must be recorded or carry.')
    if history not in {'full','executions'}:raise HTTPException(422,'history must be full or executions.')
    if aggregation=='token':
        if pool or pool_pin or seconds<60 or seconds%60:raise HTTPException(422,'Token references use mint selection and 1m–1w.')
        r=resolve(mint=mint,market=market,quote=quote,aggregation='token')
        if not r['ready']:raise HTTPException(409,{'code':'reference_pending','message':'No priced executions for this token reference yet.'})
        raw=get('/token/candles',token=r['mint'],interval=seconds,limit=limit,before=before//1000 if before else None,gapPolicy=gap_policy,history=history)
        rows=[{**b,'time':b['time']*1000,'sourceTime':b['sourceTime']*1000} for b in raw['candles']]
        end=raw['observedThrough'];asset=next((t for t in CATALOG if t['mint']==r['mint']),{})
        identity=dict(raw['identity'])
        if history=='full':
            # Archive availability can differ by interval (e.g. hour-only Alpha).
            # Keep the requested composition stable while exposing actual provenance.
            identity['historySourcePolicy']=identity.get('historyPolicy','solana_reference_only')
            identity['historyPolicy']='retained_asset_history_v1'
        unit=identity['quoteMint'];label=QUOTES.get(unit,unit)
        if unit!=r['quoteMint']:raise HTTPException(409,'Reference identity changed. Resolve again.')
        if raw.get('coverage',{}).get('poolStateMarkMinutes'):
            raw={**raw,'gapDisclosure':raw.get('gapDisclosure','')+' Pool-state marks are indicative observed prices with zero execution count and volume.'}
            grades=list(dict.fromkeys(g for b in rows for g in b.get('referenceQualities',[])))
            descriptions={'liquid_reference':'Funded pool reference; not an executable quote.','thin_reference':'Thin-liquidity reference; a trade may move the price materially.',
                          'quote_only':'Native quote units only; no verified USD conversion.','disputed_quote_only':'Pool prices disagree. One native pair is shown by quote reserves; no verified USD conversion.',
                          'disputed_pool_reference':'Pool prices disagree. One best-funded observed pool is shown; this is not a consensus or executable price.',
                          'legacy_reference':'Earlier reference policy; current grade was not recorded.'}
            raw['gapDisclosure']+=' '+' '.join(descriptions[g] for g in grades if g in descriptions)
            raw['coverage']={**raw['coverage'],'referenceQualities':grades}
        if raw.get('coverage',{}).get('displayPolicy')=='attributed_reference_composite_v1':
            raw['gapDisclosure']+=' '+raw['coverage']['referencePolicy']
        source_label=((raw['history'].get('sourceLabel','Binance')+' history + ticking Solana reference') if raw.get('history') else 'ticking token execution reference')+(' + indicative pool-state marks' if raw.get('coverage',{}).get('poolStateMarkMinutes') else '')
        if raw.get('coverage',{}).get('displayPolicy')=='attributed_reference_composite_v1':source_label='Ticking attributed composite reference; observed and reconstructed prices'
        return {'mode':'asset_history' if raw.get('history') or raw.get('coverage',{}).get('displayPolicy') or raw.get('coverage',{}).get('poolStateMarkMinutes') else 'executed_history','live':False,'aggregation':'token','observationAvailable':True,'refresh_seconds':2,'market':r['mint'],'pool':None,'mint':r['mint'],'symbol':asset.get('symbol'),'name':asset.get('name'),'quoteMint':unit,'quote':label,'identity':identity,'interval':interval,'candles':rows,'fillPolicy':raw['fillPolicy'],'carriedBars':raw['carriedBars'],'gapDisclosure':raw['gapDisclosure'],'revision':raw['sequence'],'cursor':raw['sequence'],'hasMore':raw['hasMore'],'nextBefore':raw['nextBefore']*1000 if raw['nextBefore'] else None,'coverage':raw['coverage'],'history':raw.get('history'),'source':source_label,'updated_at':end,'stale':bool(raw.get('retainedSnapshot')) or not end or time.time()-end>180,**({'retainedSnapshot':raw['retainedSnapshot']} if raw.get('retainedSnapshot') else {}),'currency':label,'venue':'All retained supported pools','complete':False}
    if aggregation!='pool':raise HTTPException(422,'aggregation must be pool or token.')
    p=pinned(pool_pin,mint) if pool_pin else chosen(mint,pool,market,quote)
    raw=get('/candles',pool=p['pool'],interval=seconds,limit=limit,before=before//1000 if before else None,gapPolicy=gap_policy)
    rows=[]
    for b in raw['candles']:
        row={**b,'time':b['time']*1000}
        if 'sourceTime' in b:row['sourceTime']=b['sourceTime']*1000
        if p['inverted']:row.update(open=1/b['open'],high=1/b['low'],low=1/b['high'],close=1/b['close'],baseVolume=b['volume'],volume=b['baseVolume'])
        rows.append(row)
    identity={**raw['identity'],'baseMint':p['baseMint'],'quoteMint':p['quoteMint'],'inverted':p['inverted']}
    end=raw.get('observedThrough') or raw.get('recordedThrough') or (rows[-1]['time']//1000 if rows else None)
    asset=next((t for t in CATALOG if t['mint']==p['baseMint']),{})
    return {'mode':'executed_history','live':False,'observationAvailable':raw.get('observationAvailable',False),'refresh_seconds':1 if raw.get('observationAvailable') else 30,'market':p['pool'],'pool':p['pool'],'mint':p['baseMint'],'symbol':asset.get('symbol'),'name':asset.get('name'),'quoteMint':p['quoteMint'],'quote':QUOTES.get(p['quoteMint'],p['quoteMint']),
            'identity':identity,'interval':interval,'candles':rows,'fillPolicy':gap_policy,'carriedBars':raw.get('carriedBars',0),'gapDisclosure':raw.get('gapDisclosure'),'revision':raw['sequence'],'cursor':raw['sequence'],'hasMore':raw['hasMore'],'nextBefore':raw['nextBefore']*1000 if raw['nextBefore'] else None,
            'coverage':raw['coverage'],'source':'ticking execution store','updated_at':end,'stale':bool(raw.get('retainedSnapshot')) or not end or time.time()-end>180,**({'retainedSnapshot':raw['retainedSnapshot']} if raw.get('retainedSnapshot') else {}),'currency':QUOTES.get(p['quoteMint'],p['quoteMint']),
            'venue':p['program'],'source_url':'https://explorer.solana.com/address/'+p['pool'],'complete':False}

def trades(mint=None,pool=None,market=None,limit=50,base_mint=None,aggregation='pool'):
    if global_data.chosen(market):
        if mint or pool or base_mint:raise HTTPException(422,'Do not combine exchange instruments with Solana selectors.')
        return global_data.trades(market,limit)
    if aggregation=='token':
        if pool or base_mint:raise HTTPException(422,'Token tapes require a mint without a pool.')
        r=resolve(mint=mint,market=market,aggregation='token')
        return {**get('/token/trades',token=r['mint'],limit=limit),'source':'ticking retained pool executions','latencySla':None}
    if aggregation!='pool':raise HTTPException(422,'aggregation must be pool or token.')
    p=pinned(pool,base_mint) if base_mint else chosen(mint,pool,market)
    data=get('/trades',pool=p['pool'],limit=limit)
    rows=[]
    for r in data['trades']:
        rows.append({**r,'price':1/r['price'] if p['inverted'] else r['price'],'base_volume':r['quote_volume'] if p['inverted'] else r['base_volume'],'quote_volume':r['base_volume'] if p['inverted'] else r['quote_volume']})
    return {**data,'identity':p,'trades':rows,'source':'ticking execution store','latencySla':None}

def markets_data(aggregation='pool'):
    global catalog_snapshot,token_catalog_snapshot
    if aggregation not in {'pool','token'}:raise HTTPException(422,'aggregation must be pool or token.')
    def row(t):
        if aggregation=='token':
            r=references.get(t['mint'])
            if r is None:raise HTTPException(503,'The market catalog is refreshing. Retry shortly.',headers={'Retry-After':'10'})
            return {**t,'market':t['mint'],'ready':r['ready'],'state':r['state'],'pool':None,'quoteMint':r.get('quoteMint'),'minuteCandles':r.get('minuteCandles',0),'coverage':r['coverage'],'aggregation':'token','live':False}
        r=resolve(mint=t['mint']);p=r['preferredPool']
        return {**t,'market':t['mint'],'ready':r['ready'],'state':r['state'],'pool':p['pool'] if p else None,'quoteMint':p['quoteMint'] if p else None,'minuteCandles':p['minuteCandles'] if p else 0,'live':False}
    # One bounded refresh per process; four bridge slots remain for chart reads.
    # Serialize the snapshot so a consumer cannot mutate another response.
    with catalog_lock:
        snapshot=token_catalog_snapshot if aggregation=='token' else catalog_snapshot
        if snapshot and time.monotonic()-snapshot[0]<15:return json.loads(snapshot[1])
        if aggregation=='token':
            references={r['mint']:r for r in get('/token/catalog')['tokens']}
            rows=[row(t) for t in CATALOG]
        else:rows=list(catalog_workers.map(row,CATALOG))
        if global_data.CATALOG:rows+=global_data.snapshot()
        data={'mode':'executed_history','live':False,'markets':rows,'entireSolanaComplete':False,'scope':'Listed Solana references and explicit exchange instruments; venue identities remain separate.'}
        data['aggregation']=aggregation
        snapshot=(time.monotonic(),json.dumps(data,separators=(',',':')))
        if aggregation=='token':token_catalog_snapshot=snapshot
        else:catalog_snapshot=snapshot
        return data
