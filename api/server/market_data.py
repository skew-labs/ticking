"""Cached public market observations. Separate from the sandbox customer feed."""
import asyncio
import hashlib
import json
import math
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import HTTPException,Response
from .store import DB_PATH
from .market_cache import ProviderCache
from . import token_icons
from .price_routes import price_support
from . import global_data
from . import market_sparklines

CATALOG = json.loads(Path(__file__).with_name('catalog.json').read_text())
RESEARCH = json.loads(Path(__file__).with_name('asset_research.json').read_text())
CATALOG_POLICY = json.loads(Path(__file__).with_name('catalog_policy.json').read_text())
REVIEW_POLICY = json.loads(Path(__file__).with_name('reviewed_markets.json').read_text())
REVIEWED_MARKETS = {t['id']:t for t in REVIEW_POLICY['markets']}

def market_review(t):
    """Pin editorial approval to the exact mint and provider ID, never a ticker."""
    record=REVIEWED_MARKETS.get(t.get('id'))
    if record and record['mint']==t.get('mint') and record['coingecko']==t.get('coingecko'):
        return {'status':'reviewed','id':t['id'],'mint':t['mint'],
                'label':REVIEW_POLICY['badge_label'],'scope':REVIEW_POLICY['badge_scope'],
                'reviewed_at':record['reviewed_at'],'basis':'curated_identity_review'}
    # This metadata is produced by the private operator listing workflow.
    approval=t.get('verification',{})
    if t.get('id')==t.get('mint') and approval.get('basis')=='operator_listing_review' and approval.get('status')=='reviewed' and approval.get('mint')==t.get('mint') and approval.get('id')==t.get('id'):
        return approval
    return None

def ranking_eligible(row):
    # A selected-pool percentage is not a global 24-hour token return.
    return bool(row.get('verification') and row.get('source')=='CoinGecko' and
                row.get('coingecko') and not row.get('stale') and
                number(row.get('price')) is not None and row['price']>0 and
                number(row.get('change')) is not None and row['change']>=-100)

def reviewed_universe(count):
    return {**CATALOG_POLICY,'title':'ticking reviewed markets','target_count':count,
            'rank_basis':'Only ticking-reviewed token identities appear in public Markets. The original 100 use the attributed 2026-10-08 selection; new listings require private operator approval. Current observations determine the table order.',
            'included':'Reviewed Solana mint identities, issuer-bound RWA tokens and explicit global exchange instruments. Sparse unsupported references are retained outside public discovery.',
            'coverage':'The checkmark means token identity and listing-source review. Solana volume, global market cap and attributed price references remain separate. Underlying stock prices are not token executions. Market-breadth comparisons exclude selected-pool changes and unavailable observations.'}

def number(x):
    return x if isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) else None

def timestamp(value):
    try: return int(datetime.fromisoformat(value.replace('Z','+00:00')).timestamp())
    except (ValueError,AttributeError,TypeError): return None

def decimal(value):
    try:return number(float(value))
    except (ValueError,TypeError):return None

def table_row(row):
    """Display-only envelope sampling; retain exact observed values and x positions."""
    fields=('id','symbol','name','mint','icon','category','coingecko','price','change','volume','cap','updated_at','stale','verification','ranking_eligible','network','marketType','market','source','venue','quote','rankOrder','volumeScope','volumeUpdatedAt','volumeStale','volumePoolCount','capSource','capUpdatedAt','capStale','priceBasis','sources','changeSource','changeUpdatedAt','changeStale')
    result={k:row.get(k) for k in fields}
    for key in ('issuer','underlying','priceUnit','underlyingReferencePolicy','venueBaseVolume24h','kind','subtags','turnover','turnover_usd'):
        if key in row:result[key]=row[key]
    for key in ('sparklineQuoteMint','sparklineSource','sparklineSince','sparklineThrough'):
        if key in row:result[key]=row[key]
    points=row['sparkline'];size=len(points)
    indices={0,size-1} if size else set()
    for start in range(1,max(1,size-1),max(1,math.ceil(max(0,size-2)/24))):
        bucket=range(start,min(size-1,start+max(1,math.ceil(max(0,size-2)/24))))
        if bucket:
            indices.update((min(bucket,key=points.__getitem__),max(bucket,key=points.__getitem__)))
    indices=sorted(indices)
    result.update(sparkline=[points[i] for i in indices],sparkline_indices=indices,sparkline_length=size)
    return result

def gecko_pairs(data,mint):
    """Adapt verified base-token relationships; do not assign quote-token prices."""
    if not isinstance(data,dict):return []
    tokens={t['id']:t.get('attributes',{}) for t in data.get('included',[]) if t.get('type')=='token'}
    rows=[]
    for p in data.get('data',[]):
        a=p.get('attributes',{});r=p.get('relationships',{})
        base=r.get('base_token',{}).get('data',{}).get('id');quote=r.get('quote_token',{}).get('data',{}).get('id')
        if base!='solana_'+mint:continue
        names=a.get('name','').split(' / ')
        rows.append({'chainId':'solana','pairAddress':a.get('address'),'dexId':r.get('dex',{}).get('data',{}).get('id'),
                     'baseToken':{'address':mint,'symbol':tokens.get(base,{}).get('symbol') or names[0]},
                     'quoteToken':{'symbol':tokens.get(quote,{}).get('symbol') or (names[1].split(' ')[0] if len(names)>1 else 'Unknown')},
                     'priceUsd':a.get('base_token_price_usd'),'liquidity':{'usd':decimal(a.get('reserve_in_usd'))},
                     'volume':{'h24':decimal(a.get('volume_usd',{}).get('h24'))},
                     'priceChange':{'h24':decimal(a.get('price_change_percentage',{}).get('h24'))},
                     'marketCap':decimal(a.get('market_cap_usd')),'fdv':decimal(a.get('fdv_usd')),'source':'GeckoTerminal'})
    return rows

def normalize_pairs(data,mint):
    rows={}
    if not isinstance(data,list): raise ValueError('Invalid pool response')
    for p in data:
        if p.get('chainId')!='solana' or p.get('baseToken',{}).get('address')!=mint: continue
        address=p.get('pairAddress','')
        if not address or address in rows: continue
        try: price=float(p.get('priceUsd'))
        except (ValueError,TypeError): price=None
        rows[address]={'address':address,'venue':p.get('dexId'), 'base':p.get('baseToken',{}).get('symbol'),
                       'quote':p.get('quoteToken',{}).get('symbol'),'price':number(price),
                       'liquidity':number((p.get('liquidity') or {}).get('usd')),'volume':number((p.get('volume') or {}).get('h24')),
                       'change':number((p.get('priceChange') or {}).get('h24')),'market_cap':number(p.get('marketCap')),
                       'fdv':number(p.get('fdv')),'url':('https://www.geckoterminal.com/solana/pools/' if p.get('source')=='GeckoTerminal' else 'https://dexscreener.com/solana/')+address}
    return sorted(rows.values(),key=lambda x:x['liquidity'] or 0,reverse=True)[:20]

def daily_series(data, now=None, allow_negative=False):
    """Only completed UTC days; never label a partial current day as daily revenue."""
    today=int(now or time.time())//86400*86400
    result={}
    for point in data.get('totalDataChart',[]) if isinstance(data,dict) else []:
        if not isinstance(point,list) or len(point)!=2: continue
        day,value=point
        if number(day) is None or day%86400 or day<1230768000 or day>=today or number(value) is None or (value<0 and not allow_negative): continue
        result[int(day)]=value
    return result


def research_for(ident):
    profile=RESEARCH.get(ident)
    if not profile: return {'status':'pending','message':'An editorial profile has not been published for this listing.'}
    reviewed=timestamp(profile.get('reviewed_at'))
    return {**profile,'status':'published','review_age_days':max(0,int((time.time()-reviewed)/86400)),
            'review_due':time.time()-reviewed>7*86400}


class MarketData(ProviderCache):
    def __init__(self,path=None):
        super().__init__(path or DB_PATH.parent/'market-cache.json')
        self.snapshots={}
        self.snapshot_lock=asyncio.Lock()
        self.batch_cursor=0
        self.cursor=0
        self.cycle=0
        self.bootstrap_until=time.monotonic()+600

    def missing_history(self,t):
        return not any(k.startswith('candles:'+t['mint']+':') and k.endswith(':hour') and
            v.get('data',{}).get('data',{}).get('attributes',{}).get('ohlcv_list') for k,v in self.cache.items())

    async def fetch(self,key,url,ttl,params=None):
        # Give first observations priority over refreshing already populated views.
        # Staleness remains visible; no observation timestamp is extended.
        if self.background and time.monotonic()<self.bootstrap_until and 'api.geckoterminal.com/' in url and 'data' in self.cache.get(key,{}) and any(self.missing_history(t) for t in CATALOG):
            return self.cache[key]
        return await super().fetch(key,url,ttl,params)

    def catalog(self):
        from .listings import approved
        return CATALOG+approved()

    def token(self,ident):
        t=next((t for t in self.catalog() if t['id']==ident or t['mint']==ident),None)
        if not t: raise HTTPException(404,'This market is not listed.')
        return t

    async def prices(self):
        ids=list(dict.fromkeys(t['coingecko'] for t in [*CATALOG,*global_data.ASSETS.values()] if t.get('coingecko')))
        if len(ids)<=250:
            return await self.fetch('prices','https://api.coingecko.com/api/v3/coins/markets',180,{'vs_currency':'usd','ids':','.join(ids),'sparkline':'true','per_page':250,'page':1,'order':'market_cap_desc'})
        # Each cache key identifies its exact membership. A single page silently
        # drops assets beyond the provider's page bound when the catalog grows.
        wanted=set(ids);merged={r['id']:r for r in self.cache.get('prices',{}).get('data',[]) if isinstance(r,dict) and r.get('id') in wanted}
        observations=[]
        for start in range(0,len(ids),100):
            batch=','.join(ids[start:start+100]);key='prices:'+hashlib.sha256(batch.encode()).hexdigest()[:20]
            d=await self.fetch(key,'https://api.coingecko.com/api/v3/coins/markets',180,{'vs_currency':'usd','ids':batch,'sparkline':'true','per_page':100,'page':1,'order':'market_cap_desc'})
            observations.append(d)
            merged.update({r['id']:r for r in d.get('data',[]) if isinstance(r,dict) and r.get('id') in wanted})
        result={'data':list(merged.values()),'fetched_at':min((d.get('fetched_at',0) for d in observations),default=0)}
        if any(d.get('error') for d in observations):result['error']='Some market observations are delayed; previous observations retained.'
        return result

    async def pools(self,t):
        dex=await self.fetch('pools:'+t['mint'],'https://api.dexscreener.com/token-pairs/v1/solana/'+t['mint'],180)
        if normalize_pairs(dex.get('data',[]),t['mint']):return {**dex,'source':'DEX Screener'}
        gecko=await self.fetch('gecko-pools:'+t['mint'],'https://api.geckoterminal.com/api/v2/networks/solana/tokens/'+t['mint']+'/pools',180,{'include':'base_token,quote_token'})
        return {**gecko,'data':gecko_pairs(gecko.get('data',{}),t['mint']),'source':'GeckoTerminal'}

    def solana_volume(self,mints):
        """Deduplicate observed Solana pools. Missing coverage is not zero flow."""
        pools={};times=[];delayed=False
        for mint in mints:
            entry=self.cache.get('pools:'+mint,{})
            raw=entry.get('data',[])
            if not raw:
                entry=self.cache.get('gecko-pools:'+mint,{})
                raw=gecko_pairs(entry.get('data',{}),mint)
            stamp=entry.get('fetched_at',0)
            for p in raw if isinstance(raw,list) else []:
                if p.get('chainId')!='solana' or mint not in (p.get('baseToken',{}).get('address'),p.get('quoteToken',{}).get('address')):continue
                value=number((p.get('volume') or {}).get('h24'));address=p.get('pairAddress')
                if address and value is not None and value>=0:pools[address]=value
            if stamp:times.append(stamp)
            delayed=delayed or bool(entry.get('error'))
        at=min(times) if times else None
        return dict(volume=math.fsum(pools.values()) if pools else None,volumeScope='solana_observed_pools_24h',volumePoolCount=len(pools),volumeUpdatedAt=at,
                    volumeStale=delayed or not at or time.time()-at>600,volumeSource='DEX Screener / GeckoTerminal',volumeComplete=False)

    def enrich_global(self,row,by_id):
        if row.get('source')=='hyperliquid_public':return row
        r=by_id.get(global_data.ASSETS.get(row['id'],{}).get('coingecko'),{})
        at=timestamp(r.get('last_updated'))
        row.update(cap=number(r.get('market_cap')),fdv=number(r.get('fully_diluted_valuation')),supply=number(r.get('circulating_supply')),global_rank=number(r.get('market_cap_rank')),
                   capSource='CoinGecko global circulating market cap',capUpdatedAt=at,capStale=not at or time.time()-at>900)
        change=number(r.get('price_change_percentage_24h'))
        row.update(changeSource='CoinGecko global 24h' if change is not None else None,changeUpdatedAt=at,changeStale=not at or not 0<=time.time()-at<=900)
        if change is not None:row['change']=change
        row['ranking_eligible']=bool(change is not None and change>=-100 and not row['changeStale'] and not row.get('stale',True) and number(row.get('price')) is not None and row['price']>0)
        mints=global_data.ASSETS.get(row['id'],{}).get('solanaMints',[])
        row.update(self.solana_volume(mints));return row

    async def global_detail(self,ident):
        row=await asyncio.to_thread(global_data.detail,ident)
        for mint in global_data.ASSETS.get(ident,{}).get('solanaMints',[]):await self.pools({'mint':mint})
        source=await self.prices();by_id={r['id']:r for r in source.get('data',[]) if isinstance(r,dict) and r.get('id')}
        return self.enrich_global(row,by_id)

    async def warm_pools_batch(self,tokens,wait=False):
        """Amortize public metadata requests; own chart candles stay separate."""
        mints=list(dict.fromkeys(t['mint'] for t in tokens))
        if not 1<=len(mints)<=30:raise ValueError('Use 1-30 mints per metadata batch')
        members=','.join(mints);key='pools-batch:'+hashlib.sha256(members.encode()).hexdigest()[:20]
        result=await (self._fetch if wait else self.fetch)(key,'https://api.dexscreener.com/tokens/v1/solana/'+members,180)
        data=result.get('data',[]);stamp=result.get('fetched_at',0)
        if not isinstance(data,list):return 0
        grouped={mint:[] for mint in mints}
        for pair in data:
            if not isinstance(pair,dict) or pair.get('chainId')!='solana':continue
            base=pair.get('baseToken')
            mint=base.get('address') if isinstance(base,dict) else None
            if mint in grouped:grouped[mint].append(pair)
        changed=0
        for mint,rows in grouped.items():
            old=self.cache.get('pools:'+mint,{})
            if rows and self.valid('pools:'+mint,rows) and stamp>old.get('fetched_at',0):
                self.cache['pools:'+mint]={'data':rows,'fetched_at':stamp,**({'error':result['error']} if result.get('error') else {})};changed+=1
        self.save();return changed

    async def snapshot(self,ident=None,compact=False,reviewed_only=False):
        if reviewed_only:
            if ident is not None:return await self._snapshot(ident,compact,reviewed_only=True)
            key=('reviewed',compact)
            async with self.snapshot_lock:
                old=self.snapshots.get(key)
                if old and time.monotonic()-old[0]<5:return old[1]
                result=await self._snapshot(None,compact,reviewed_only=True)
                self.snapshots[key]=(time.monotonic(),result)
                return result
        if ident is not None:return await self._snapshot(ident,compact)
        async with self.snapshot_lock:
            old=self.snapshots.get(compact)
            if old and time.monotonic()-old[0]<5:return old[1]
            result=await self._snapshot(None,compact)
            self.snapshots[compact]=(time.monotonic(),result)
            return result

    async def _snapshot(self,ident=None,compact=False,reviewed_only=False):
        source=await self.prices();now=int(time.time())
        data=source.get('data',[]);by_id={r['id']:r for r in data if isinstance(r,dict) and r.get('id')} if isinstance(data,list) else {}
        rows=[]
        for t in self.catalog():
            if ident is not None and t['id']!=ident:continue
            verification=market_review(t)
            if reviewed_only and not verification:continue
            r=by_id.get(t.get('coingecko'),{});observed=timestamp(r.get('last_updated'))
            points=[x for x in (r.get('sparkline_in_7d') or {}).get('price',[]) if number(x) is not None and x>0]
            row={**t,'icon':token_icons.icon_url(t['mint']) or t.get('icon'),'price':number(r.get('current_price')),'change':number(r.get('price_change_percentage_24h')),
                 'volume':number(r.get('total_volume')),'cap':number(r.get('market_cap')),'fdv':number(r.get('fully_diluted_valuation')),
                 'high':number(r.get('high_24h')),'low':number(r.get('low_24h')),'supply':number(r.get('circulating_supply')),
                 'total_supply':number(r.get('total_supply')),'max_supply':number(r.get('max_supply')),'global_rank':number(r.get('market_cap_rank')),
                 'sparkline':points,'research_reviewed_at':RESEARCH.get(t['id'],{}).get('reviewed_at'),'updated_at':observed,'fetched_at':source.get('fetched_at'),
                 'source':'CoinGecko','source_url':'https://www.coingecko.com/en/coins/'+t['coingecko'] if t.get('coingecko') else None,
                 'stale':bool(source.get('error') or not observed or now-observed>900 or observed>now+60)}
            if not t.get('coingecko'):
                p=await self.pools(t);pairs=normalize_pairs(p.get('data',[]),t['mint']);best=next((x for x in pairs if (x['liquidity'] or 0)>0),{})
                support=price_support(pairs,p.get('fetched_at'),now,bool(p.get('error')))
                row['price_support']=support
                row.update(price=support['price'] if support['state']=='available' else best.get('price'),change=best.get('change'),volume=best.get('volume'),cap=best.get('market_cap'),
                           fdv=best.get('fdv'),source=p.get('source','Pool data')+' · selected pool',source_url=best.get('url'),updated_at=p.get('fetched_at'),
                           stale=bool(p.get('error') or not p.get('fetched_at') or now-p['fetched_at']>600))
            row['verification']=verification
            if not row['sparkline']:
                preview=market_sparklines.reference(t['mint'],now)
                if preview:row.update(sparkline=preview['points'],sparklineQuoteMint=preview['quoteMint'],sparklineSource=preview['source'],sparklineSince=preview['actualStart'],sparklineThrough=preview['actualEnd'])
            row.update(self.solana_volume([t['mint']]))
            row.update(capSource='CoinGecko global circulating market cap' if t.get('coingecko') else row['source'],capUpdatedAt=observed if t.get('coingecko') else row.get('updated_at'),capStale=row['stale'])
            row['ranking_eligible']=ranking_eligible(row)
            rows.append(table_row(row) if compact else row)
        if reviewed_only and ident is None and global_data.CATALOG:
            extra=await asyncio.to_thread(global_data.snapshot)
            extra=[self.enrich_global(r,by_id) for r in extra]
            rows.extend(table_row(r) if compact else r for r in extra)
        return {'tokens':rows,'generated_at':now,'refresh_seconds':60,
                'universe':reviewed_universe(len(rows)) if reviewed_only else CATALOG_POLICY,
                'scope':'Reviewed public markets only. Token trading volumes can overlap; not total Solana volume.' if reviewed_only else 'Listed assets only. Token trading volumes can overlap; not total Solana volume.'}

    async def detail(self,ident):
        t=self.token(ident)
        snapshot,p=await asyncio.gather(self.snapshot(ident=t['id']),self.pools(t));row=snapshot['tokens'][0]
        row['pools']={'items':normalize_pairs(p.get('data',[]),t['mint']),'updated_at':p.get('fetched_at'),
                      'stale':bool(p.get('error') or int(time.time())-p.get('fetched_at',0)>600),'source_url':p.get('url'),'source':p.get('source')}
        row['price_support']=price_support(row['pools']['items'],p.get('fetched_at'),int(time.time()),bool(p.get('error')))
        row['research']=research_for(t['id'])
        row['revenue']=await self.revenue(t)
        row['quality']={
            'price':'unavailable' if row['price'] is None else 'delayed' if row['stale'] else 'current',
            'pools':'unavailable' if not row['pools']['items'] else 'delayed' if row['pools']['stale'] else 'current',
            'research':'review_due' if row['research'].get('review_due') else row['research']['status'],
            'scope':'Public provider observations. Prices may differ across venues; this is not the ticking execution feed.'}
        row['chain']=self.chain_observation(t)
        from . import engine_data
        try:
            resolved=await asyncio.to_thread(engine_data.resolve,mint=t['mint'])
            self.cache['_owned-pools:'+t['mint']]={'data':resolved,'fetched_at':int(time.time())}
        except HTTPException as error:
            if error.status_code<500:raise
            # Optional pool discovery must not prevent the independent token
            # history from mounting. Keep only previously verified identities.
            prior=self.cache.get('_owned-pools:'+t['mint'],{})
            resolved=prior.get('data',{'ready':False,'state':'pool_details_delayed','preferredPool':None,'pools':[]})
            row['pools']['stale']=True;row['quality']['pools']='delayed'
            row['pools']['updated_at']=prior.get('fetched_at')
            row['pools']['error']='Verified pool details are temporarily delayed. Token history remains available.'
        row['chart']={'ready':resolved['ready'],'state':resolved['state'],'preferredPool':resolved['preferredPool'],'source':'ticking execution store'}
        observed={p['address']:p for p in row['pools']['items']}
        selected=resolved['preferredPool'];ordered=sorted(resolved['pools'],key=lambda p:(p['pool']!=(selected or {}).get('pool'),-p['minuteCandles'],p['pool']))
        owned=[]
        labels={'675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8':'Raydium','CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK':'Raydium CLMM','CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C':'Raydium CPMM','LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo':'Meteora DLMM','pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA':'PumpSwap'}
        for p in ordered[:20]:
            prior=observed.get(p['pool'],{})
            owned.append({**prior,'address':p['pool'],'venue':labels.get(p['program'],'Verified pool'),'base':t['symbol'],'quote':engine_data.QUOTES.get(p['quoteMint'],p['quoteMint'][:6]+'…'),'url':'https://explorer.solana.com/address/'+p['pool'],'history_candles':p['minuteCandles']})
        row['pools']['items']=owned
        row['pools']['source']='ticking verified pool identities · attributed USD observations where available'
        return row

    async def revenue(self,t):
        if not t.get('revenue'):
            return {'status':'not_applicable' if t['category']=='Memes' else 'not_covered'}
        revenue,fees=await asyncio.gather(
            self.fetch('revenue:'+t['revenue'],'https://api.llama.fi/summary/fees/'+t['revenue'],3600,{'dataType':'dailyRevenue'}),
            self.fetch('fees:'+t['revenue'],'https://api.llama.fi/summary/fees/'+t['revenue'],3600,{'dataType':'dailyFees'}))
        d=revenue.get('data',{});f=fees.get('data',{})
        rev=daily_series(d,allow_negative=t.get('revenue_signed',False));fee=daily_series(f,allow_negative=t.get('revenue_signed',False));last=max(rev,default=None)
        history=[{'day':day,'revenue':rev[day],'fees':fee.get(day)} for day in sorted(rev)[-30:]]
        now=int(time.time())
        return {'status':'available' if last is not None else 'unavailable',
                'name':d.get('name',t['name']),'value':rev.get(last),'fees':fee.get(last),
                'data_day':datetime.fromtimestamp(last,timezone.utc).strftime('%Y-%m-%d') if last else None,
                'history':history,'period':'Completed UTC day','updated_at':revenue.get('fetched_at'),
                'label':t.get('revenue_label','Revenue (1d)'),
                'fees_updated_at':fees.get('fetched_at'),
                'stale':bool(revenue.get('error') or not last or now-last>3*86400 or now-revenue.get('fetched_at',0)>7200),
                'fees_stale':bool(fees.get('error') or last not in fee or now-fees.get('fetched_at',0)>7200),
                'definition':t.get('revenue_definition','Revenue retained by the protocol and token holders after supply-side fees. Not profit or a token-holder yield.'),
                'source':'DefiLlama','source_url':'https://defillama.com/fees/'+t['revenue'],
                'methodology':d.get('methodology'),'chains':d.get('chains',[])}

    def chain_observation(self,t):
        observation=self.cache.get('_chain',{})
        row=observation.get('tokens',{}).get(t['mint'])
        if not row: return {'status':'unavailable','source':'Solana RPC'}
        return {**row,'status':'observed','fetched_at':observation.get('fetched_at'),
                'stale':bool(observation.get('error') or time.time()-observation.get('fetched_at',0)>86400),
                'slot':row.get('slot',observation.get('slot')),'commitment':'finalized','source':'Solana RPC',
                'source_url':'https://explorer.solana.com/address/'+t['mint'],
                'native':t['id']=='SOL'}

    async def observe_chain(self):
        from .listings import TOKEN_PROGRAMS
        old=self.cache.get('_chain',{});now=int(time.time())
        complete=all(t['mint'] in old.get('tokens',{}) for t in CATALOG)
        if (complete and now-old.get('fetched_at',0)<21600) or now<old.get('retry_at',0): return
        # Solana getMultipleAccounts accepts at most 100 addresses per request.
        # Commit a complete generation only; one failed batch retains the old one.
        try:
            rows={};slots=[]
            async with httpx.AsyncClient(timeout=12) as client:
                for start in range(0,len(CATALOG),100):
                    batch=CATALOG[start:start+100]
                    if start:await asyncio.sleep(1)
                    r=await client.post('https://api.mainnet-beta.solana.com',json={'jsonrpc':'2.0','id':1,'method':'getMultipleAccounts',
                        'params':[[t['mint'] for t in batch],{'encoding':'jsonParsed','commitment':'finalized'}]})
                    r.raise_for_status();result=r.json()['result'];accounts=result['value'];slot=result['context']['slot'];slots.append(slot)
                    if len(accounts)!=len(batch): raise ValueError('Incomplete mint observation')
                    for t,account in zip(batch,accounts):
                        if not isinstance(account,dict) or account.get('owner') not in TOKEN_PROGRAMS: continue
                        parsed=account.get('data',{}).get('parsed',{});info=parsed.get('info',{})
                        if parsed.get('type')!='mint' or not info.get('isInitialized'): continue
                        decimals=info.get('decimals');supply=info.get('supply')
                        if not isinstance(decimals,int) or not 0<=decimals<=255 or not isinstance(supply,str) or not supply.isdigit(): continue
                        rows[t['mint']]={'program':account['owner'],'decimals':decimals,'supply_raw':supply,'slot':slot,
                                         'mint_authority':info.get('mintAuthority'),'freeze_authority':info.get('freezeAuthority')}
            if len(rows)!=len(CATALOG): raise ValueError('An expected mint could not be decoded')
            self.cache['_chain']={'tokens':rows,'fetched_at':now,'slot':min(slots),'slot_range':[min(slots),max(slots)]}
        except (httpx.HTTPError,KeyError,ValueError,TypeError):
            self.cache['_chain']={**old,'retry_at':now+900,'error':'Finalized mint observations are temporarily unavailable.'}
        self.save()

    async def candles(self,ident,frame,pool=None):
        t=self.token(ident)
        if frame not in {'minute','hour','day'}: raise HTTPException(422,'Supported source intervals: minute, hour, day.')
        source=await self.pools(t)
        pairs=[p for p in normalize_pairs(source.get('data',[]),t['mint']) if (p['liquidity'] or 0)>0]
        p=next((p for p in pairs if p['address']==pool),None) if pool else next(iter(pairs),None)
        if pool and not p: raise HTTPException(422,'This pool is no longer in the observed markets for this token. Select an available pool.')
        if not p: return {'candles':[],'message':'No funded pool observation is available for this chart. The data provider may be delayed.'}
        key='candles:'+t['mint']+':'+p['address']+':'+frame
        legacy=self.cache.get('candles:'+p['address']+':'+frame)
        if key not in self.cache and legacy and self.valid(key,legacy.get('data')):
            self.cache[key]=dict(legacy)  # Same verified base mint/pool, original observation time.
        result=await self.fetch(key,
            'https://api.geckoterminal.com/api/v2/networks/solana/pools/'+p['address']+'/ohlcv/'+frame,300,
            {'aggregate':1,'limit':1000,'currency':'usd','token':t['mint']})
        raw=result.get('data',{}).get('data',{}).get('attributes',{}).get('ohlcv_list',[]);bars={}
        now=int(time.time());seconds={'minute':60,'hour':3600,'day':86400}[frame]
        for b in raw:
            if not isinstance(b,list) or len(b)<6 or any(number(v) is None for v in b[:6]): continue
            if b[0]<1230768000 or b[0]>now or b[0]%seconds or b[5]<0 or b[3]<=0 or b[2]<max(b[1],b[4]) or b[3]>min(b[1],b[4]): continue
            bars[b[0]]={'time':b[0]*1000,'open':b[1],'high':b[2],'low':b[3],'close':b[4],'volume':b[5]}
        rows=[bars[k] for k in sorted(bars)]
        return {'candles':rows,'source':'GeckoTerminal','source_url':'https://www.geckoterminal.com/solana/pools/'+p['address'],
                'pool':p['address'],'venue':p['venue'],'quote':p['quote'],'currency':'USD','interval':frame,'updated_at':result.get('fetched_at'),
                'stale':bool(result.get('error') or int(time.time())-result.get('fetched_at',0)>600),
                'message':result.get('error') if not rows else None}

    async def run(self):
        from .listings import analyze_pending
        self.background=True
        try:
            while True:
                try:
                    await self.prices()
                    await self.observe_chain()
                    tokens=self.catalog()+[{'mint':mint} for a in global_data.ASSETS.values() for mint in a.get('solanaMints',[])]
                    # One rate-aware request serves 30 mints, independent of visitors.
                    start=self.batch_cursor%len(tokens)
                    await self.warm_pools_batch(tokens[start:start+30],wait=True)
                    self.batch_cursor=(start+30)%len(tokens)
                    # Rotate through every listing, independent of page visits.
                    missing=[t for t in tokens if not self.cache.get('gecko-pools:'+t['mint'],{}).get('data',{}).get('data') and not self.cache.get('pools:'+t['mint'],{}).get('data')]
                    no_history=[t for t in tokens if self.missing_history(t)]
                    urgent=missing or no_history
                    candidates=urgent if urgent and (time.monotonic()<self.bootstrap_until or self.cycle%3==0) else tokens
                    self.cycle+=1
                    t=candidates[self.cursor%len(candidates)]
                    if 'id' not in t:t=self.catalog()[self.cursor%len(self.catalog())]
                    if time.time()>=self.cache.get('_provider:api.geckoterminal.com',{}).get('retry_at',0): self.cursor+=1
                    await self.pools(t)
                    await self.revenue(t)
                    # Chart history is owned by the execution/archive engine.
                    await asyncio.to_thread(analyze_pending)
                except Exception as error:
                    logging.getLogger(__name__).warning('Public market refresh failed: %s',type(error).__name__)
                await asyncio.sleep(3)
        finally:
            await self.close()

markets=MarketData()

def install(app):
    token_icons.install(app)
    from . import engine_data
    def public_selection(mint=None,pool=None,market=None,preview=False,aggregation='pool'):
        if market:
            t=markets.token(market.removesuffix('-USDC'));mint=t['mint']
        if not any(t['mint']==mint for t in markets.catalog()):
            if preview:
                from .chart_watches import authorize_preview
                return authorize_preview(mint,aggregation,pool)
            raise HTTPException(404,'Choose a listed Solana mint. Arbitrary verified addresses are available through the authenticated API.')
        if pool:engine_data.pinned(pool,mint)
        return mint

    @app.get('/api/public/engine/candles')
    def engine_candles(response:Response,mint:str|None=None,pool:str|None=None,market:str|None=None,interval:str='1m',limit:int=300,before:int|None=None,gap_policy:str='recorded',aggregation:str='pool',history:str='executions'):
        if global_data.chosen(market):
            if mint or pool:raise HTTPException(422,'Select an exchange market without a Solana address.')
            response.headers['Access-Control-Allow-Origin']='*'
            return global_data.candles(market,interval,limit,before,gap_policy)
        mint=public_selection(mint,pool,market,preview=True,aggregation=aggregation);response.headers['Access-Control-Allow-Origin']='*'
        return engine_data.candles(mint=mint,pool_pin=pool,interval=interval,limit=limit,before=before,gap_policy=gap_policy,aggregation=aggregation,history=history)

    @app.get('/api/public/engine/trades')
    def engine_trades(response:Response,mint:str|None=None,pool:str|None=None,market:str|None=None,limit:int=50,aggregation:str='pool'):
        if global_data.chosen(market):
            if mint or pool:raise HTTPException(422,'Select one exchange market.')
            response.headers['Access-Control-Allow-Origin']='*';return global_data.trades(market)
        mint=public_selection(mint,pool,market);response.headers['Access-Control-Allow-Origin']='*'
        return engine_data.trades(mint=mint,pool=pool,base_mint=mint if pool else None,limit=limit,aggregation=aggregation)

    @app.get('/api/public/engine/coverage')
    def engine_coverage(aggregation:str='pool'):return engine_data.markets_data(aggregation)

    @app.get('/api/public/engine/analytics')
    def engine_analytics(aggregation:str='pool'):return engine_data.analytics(aggregation)

    @app.get('/api/public/markets')
    async def snapshot(view:str='full'): return await markets.snapshot(compact=view=='table',reviewed_only=True)

    @app.get('/api/public/markets/{ident}')
    async def detail(ident:str):
        if global_data.chosen(ident):return await markets.global_detail(ident)
        if not market_review(markets.token(ident)):raise HTTPException(404,'This token has not been reviewed for public Markets.')
        return await markets.detail(ident)

    @app.get('/api/public/markets/{ident}/candles')
    def candles(ident:str,frame:str='hour',pool:str|None=None,interval:str|None=None,before:int|None=None,gap_policy:str='recorded'):
        if interval is None and frame not in {'minute','hour','day'}:raise HTTPException(422,'Use minute, hour or day, or an explicit interval.')
        t=markets.token(ident)
        return engine_data.candles(mint=t['mint'],pool_pin=pool,interval=interval or {'minute':'1m','hour':'1h','day':'1d'}.get(frame,'1h'),before=before,gap_policy=gap_policy)
