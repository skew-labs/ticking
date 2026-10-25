"""Exact-identity equity comparison. Prices never substitute for token candles."""
import json, math, time, threading, urllib.request, urllib.parse, sqlite3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
_cache={};_locks={};_guard=threading.Lock()
RH='https://api.robinhood.com/rhj/'
UNITS='/srv/skew/solana-mingle-20261007/data/hyperliquid-reference/units.sqlite3'

def cached(key,ttl,read):
    with _guard: lock=_locks.setdefault(key,threading.Lock())
    with lock:
        hit=_cache.get(key)
        if hit and time.monotonic()-hit[0]<ttl:return hit[1]
        val=read();_cache[key]=(time.monotonic(),val)
        return val

def get(url):
    with urllib.request.urlopen(urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'Ticking/1.0'}),timeout=4) as r:
        return json.loads(r.read(8*1024*1024))

def assets():return cached('assets',300,lambda:get(RH+'assets')['assets'])
def deployment(a,address):return any(str(d.get('contractAddress','')).lower()==address.lower() and d.get('chainId')==4663 for d in a.get('deployments',[]))
def ts(s):return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()*1000)
def positive(n):return isinstance(n,(int,float)) and math.isfinite(n) and n>0

def solana_multiplier(ent,at):
    def read():
        q={'jsonrpc':'2.0','id':1,'method':'getAccountInfo','params':[ent['address'],{'encoding':'jsonParsed','commitment':'finalized'}]}
        req=urllib.request.Request('https://api.mainnet-beta.solana.com',data=json.dumps(q).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=4) as r:d=json.load(r)['result']
        val=d['value'];parsed=val['data']['parsed'];assert val['owner']=='TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb' and parsed['type']=='mint'
        ext=parsed['info']['extensions'];meta=next(e['state'] for e in ext if e['extension']=='tokenMetadata')
        assert meta['mint']==ent['address'] and meta['symbol']==ent['symbol']
        state=next(e['state'] for e in ext if e['extension']=='scaledUiAmountConfig')
        return {**state,'slot':d['context']['slot'],'observed':int(time.time())}
    d=cached(('units',ent['address']),60,read);effective=int(d['newMultiplierEffectiveTimestamp'])
    # Current account state proves the new multiplier from its activation, not
    # the unknown activation date of a superseded value.
    if at<effective:return None
    return {'multiplier':float(d['newMultiplier']),'unit_effective':effective,'unit_source':'Solana finalized Scaled UI Amount extension','unit_slot':d['slot']}

def solana_onchain(ent):
    q=urllib.parse.urlencode({'network':'solana','mint':ent['address'],'interval':'1m','limit':1,'aggregation':'token'})
    d=get('https://api.ticking.dev/api/public/engine/candles?'+q);ident=d.get('identity',{})
    assert d.get('mint')==ent['address'] and ident.get('baseMint')==ent['address'] and ident.get('network')=='solana'
    b=(d.get('candles')or[])[-1];quote=d.get('currency') or b.get('quoteCurrency')
    if quote not in ('USDC','USDT','USD','SOL'):return {'onchain_reason':'unsupported_quote_currency'}
    stamp=b.get('sourceTime')or b['time'];price=b['close'];out={'onchain_price':price,'onchain_ts':stamp,'onchain_quote':quote,'onchain_source':'Ticking recorded 1m token bar close'}
    if quote=='USD':return {**out,'onchain_currency':'USD'}
    def fx():
        data=get('https://api.kraken.com/0/public/Ticker?pair='+quote+'USD')
        assert not data.get('error');row=next(iter(data['result'].values()));bid,ask=float(row['b'][0]),float(row['a'][0]);assert 0<bid<=ask
        return {'value':(bid+ask)/2,'observed':int(time.time()*1000)}
    try:rate=cached(('fx',quote),10,fx)
    except Exception:return out
    value,rt=rate['value'],rate['observed']
    out.update(quote_usd_rate=value,quote_usd_ts=rt,quote_usd_source='Kraken '+quote+'/USD observed bid/ask midpoint',onchain_price=price*value,onchain_currency='USD')

    return out

def reference(ent,legacy):
    if ent['network']=='robinhood':
        a=next((a for a in assets() if deployment(a,ent['address'])),None)
        if not a:return {'reason':'issuer_identity_unavailable'}
        symbol=a['tokenSymbol'];data=cached(('rh',symbol),10,lambda:get(RH+'prices/'+urllib.parse.quote(symbol)))
        q=next((q for q in data.get('quotes',[]) if deployment(q,ent['address'])),None)
        if not q:return {'reason':'issuer_quote_unavailable'}
        multiplier=float(a['currentMultiplier']);bid=float(q['bid']);ask=float(q['ask'])
        if not all(positive(n) for n in [multiplier,bid,ask]) or ask<bid or q.get('currency')!='USD':return {'reason':'invalid_issuer_quote'}
        # Prefer the issuer's own token-equivalent quote; check multiplier consistency.
        tb=float(q.get('tokenBid') or bid*multiplier);ta=float(q.get('tokenAsk') or ask*multiplier)
        if abs(tb/bid/multiplier-1)>1e-5 or abs(ta/ask/multiplier-1)>1e-5:return {'reason':'issuer_unit_transition'}
        return {'ref_price':(bid+ask)/2,'ref_bid':bid,'ref_ask':ask,'ref_ts':ts(q['generatedAt']),
                'token_ref_price':(tb+ta)/2,'token_ref_bid':tb,'token_ref_ask':ta,'multiplier':multiplier,'currency':'USD',
                'source':'Robinhood issuer bid/ask midpoint','source_url':'https://docs.robinhood.com/chain/stock-token-apis/',
                'unit_source':'Robinhood currentMultiplier / tokenBid / tokenAsk','ref_halted':q.get('isTradingHalt',False),
                'unit_scope':'current quote only','reason':None}
    r=None
    if ent.get('underlyingIsin'):
        try:
            a=next((a for a in assets() if a.get('isin')==ent['underlyingIsin']),None)
            if a:
                data=cached(('rh',a['tokenSymbol']),10,lambda:get(RH+'prices/'+urllib.parse.quote(a['tokenSymbol'])))
                q=next(q for q in data.get('quotes',[]) if q.get('tokenSymbol')==a['tokenSymbol'] and q.get('currency')=='USD')
                bid,ask=float(q['bid']),float(q['ask']);assert 0<bid<=ask
                r={'ref_price':(bid+ask)/2,'ref_ts':ts(q['generatedAt']),'ref_bid':bid,'ref_ask':ask,'ref_halted':q.get('isTradingHalt',False),'reason':None,'source':'Robinhood underlying bid/ask midpoint · exact ISIN match','currency':'USD'}
        except Exception:pass
    if r is None:r=legacy.ref_quote(ent['underlying'])
    out={**r,'source':r.get('source','Yahoo Finance underlying quote (may be delayed)'),'currency':r.get('currency')or r.get('research',{}).get('currency')}
    if ent['network']=='solana':
        try:
            at=int((r.get('ref_ts') or 0)/1000)
            with sqlite3.connect('file:'+UNITS+'?mode=ro',uri=True,timeout=.2) as c:
                row=c.execute('SELECT multiplier,effective,valid_until FROM xstock_units WHERE asset=? AND effective<=? AND valid_until>=? ORDER BY effective DESC LIMIT 1',(ent['address'],at,at)).fetchone()
            if row and positive(r.get('ref_price')):out.update(multiplier=row[0],token_ref_price=row[0]*r['ref_price'],unit_source='xStocks recorded issuer multiplier',unit_effective=row[1],unit_valid_until=row[2])
        except (sqlite3.Error,OSError):pass
        if not out.get('token_ref_price') and r.get('ref_ts'):
            try:
                unit=solana_multiplier(ent,int(r['ref_ts']/1000))
                if unit:out.update(**unit,token_ref_price=unit['multiplier']*r['ref_price'])
            except Exception:pass
    if not out.get('token_ref_price'):out['reason']='issuer_multiplier_unavailable'
    return out

def compare(ent,legacy):
    def run():
        with ThreadPoolExecutor(max_workers=2) as ex:
            a=ex.submit(solana_onchain,ent) if ent['network']=='solana' else ex.submit(legacy.onchain,ent);b=ex.submit(reference,ent,legacy)
            try:on=a.result()
            except Exception:on={'onchain_reason':'token_price_unavailable'}
            try:r=b.result()
            except Exception:r={'reason':'underlying_reference_unavailable'}
        out={**ent,**on,**r,'premium_bps':None,'comparison_state':'unavailable','server_ts':int(time.time()*1000)}
        o=out.get('onchain_price');ref=out.get('token_ref_price');ot=out.get('onchain_ts');rt=out.get('ref_ts');reason=r.get('reason')
        # Solana market observations must carry an explicit measured USD basis.
        usd=ent['network']!='solana' or out.get('onchain_currency')=='USD'
        if not usd:reason='token_quote_conversion_unavailable'
        if not reason and positive(o) and positive(ref) and ot and rt:
            delta=abs(ot-rt)/1000;out['time_skew_s']=round(delta,3)
            if delta>120:
                reason='price_times_not_aligned';out['indicative_premium_bps']=(o/ref-1)*10000;out['comparison_state']='unaligned';out['indicative_basis']='Last token trade versus current underlying reference; different observation times'
            elif out.get('ref_halted'):reason='underlying_halted'
            else:
                out['premium_bps']=(o/ref-1)*10000
                age=(out['server_ts']-min(ot,rt))/1000
                out['comparison_state']='live' if -5<=age<=120 else 'historical'
                out['premium_vs']='issuer-adjusted underlying midpoint' if ent['network']=='robinhood' else 'issuer-adjusted underlying quote'
        out['comparison_reason']=reason or (None if out['premium_bps'] is not None else 'price_unavailable')
        return out
    return cached(('compare',ent['id']),2,run)
