"""Shared token reference from retained pool executions; originals stay intact.

One-second causal multi-pool reference samples form minute OHLC. Tiny quantized flows
stay in the execution tape but cannot price the reference. Quote conversion
uses an actual completed USDC minute, at most five minutes old, never a future
price or an assumed stablecoin peg. Assets without a measured USDC path use a
pinned, explicitly identified native quote. The source's provisional/finality limits
remain explicit. This does not merge trading liquidity or create wallet swaps.
"""
import bisect,collections,fcntl,json,math,os,sqlite3,time
from functools import lru_cache
from pathlib import Path
from contextlib import closing
from chart_intervals import bucket_start,WEEK
USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
POLICY='pool_execution_causal_pool_median_reference_v10'
SOL_MINT='So11111111111111111111111111111111111111112'
USDT_MINT='Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB'
REFERENCE_BRIDGES={SOL_MINT,USDT_MINT}
from reference_sample import PoolReferenceWindow
from market_limits import MAX_MARKET_ASSETS
MAX_REFERENCE_ASSETS=MAX_MARKET_ASSETS

def merge_windows(windows):
    merged=[]
    for start,end in sorted(windows):
        if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else:merged.append((start,end))
    return merged

def precise(base,quote,cfg,inverted=False):
    bd,qd=(cfg['quoteDecimals'],cfg['baseDecimals']) if inverted else (cfg['baseDecimals'],cfg['quoteDecimals'])
    return base>0 and quote>0 and base*10**bd>=(100 if bd>=2 else 1)-1e-6 and quote*10**qd>=(100 if qd>=2 else 1)-1e-6

def quote_rate(anchors,mint,stamp):
    if mint==USDC:return 1.0
    series=anchors.get(mint)
    if not series:return None
    for entry in ([series.get('direct'),series.get('indirect')] if isinstance(series,dict) else [series]):
        if not entry:continue
        times,prices=entry;i=bisect.bisect_right(times,stamp)-1
        if i>=0 and stamp-times[i]<=300:return prices[i]
    return None

class TokenReferenceBuilder:
    def __init__(self,reader,directory,targets,replay_floor=0,bounded_targets=False,admission_floor=None):
        self.reader=reader;self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'reference.sqlite3';self.targets=tuple(targets);self.replay_floor=int(replay_floor)//60*60;self.bounded_targets=bounded_targets;self.admission_floor=admission_floor
        if not 1<=len(set(targets))==len(targets)<=MAX_REFERENCE_ASSETS:raise ValueError('Bounded unique token catalog required')
        self.lockfd=os.open(self.directory/'writer.lock',os.O_CREAT|os.O_RDWR,0o600)
        try:fcntl.flock(self.lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:os.close(self.lockfd);raise
        with closing(sqlite3.connect(self.path)) as c, c:
            c.executescript('''PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS bars(mint TEXT,time INTEGER,open REAL,high REAL,low REAL,close REAL,base_volume REAL,quote_volume REAL,count INTEGER,samples INTEGER,PRIMARY KEY(mint,time)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS tape(mint TEXT,identity TEXT,time INTEGER,slot INTEGER,tx_order INTEGER,event_order INTEGER,pool TEXT,quote_mint TEXT,price REAL,base_volume REAL,quote_volume REAL,reference_price REAL,reference_eligible INTEGER,PRIMARY KEY(mint,identity)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS tape_order ON tape(mint,slot,tx_order,event_order);
            CREATE INDEX IF NOT EXISTS reference_bar_time ON bars(time);
            CREATE INDEX IF NOT EXISTS reference_tape_time ON tape(time);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS coverage(mint TEXT PRIMARY KEY,value TEXT);''')
    def close(self):os.close(self.lockfd)
    def update(self):
        started=time.monotonic();targetset=set(self.targets)
        with closing(sqlite3.connect(self.path)) as dest, dest:
            prev={r[0]:json.loads(r[1]) for r in dest.execute('SELECT key,value FROM state')}
            prior_coverage={r[0]:json.loads(r[1]) for r in dest.execute('SELECT mint,value FROM coverage')}
            prior_ready={r[0] for r in dest.execute('SELECT DISTINCT mint FROM bars')}
        with self.reader.connect() as c:
            c.execute('BEGIN')
            coldseq=c.execute('SELECT coalesce(max(seq),0) FROM main.fills').fetchone()[0]
            observedrow=c.execute('SELECT coalesce(max(rowid),0) FROM observed.fills').fetchone()[0]
            dead=c.execute('SELECT count(*),coalesce(max(slot),0),coalesce(sum(slot),0) FROM observed.source WHERE dead=1').fetchone()
            source=[coldseq,observedrow,*dead]
            union_policy=getattr(self.reader,'source_union_policy','legacy')
            same_policy=prev.get('report',{}).get('policy')==POLICY and prev.get('report',{}).get('sourceUnionPolicy')==union_policy
            if same_policy and prev.get('source')==source and prev.get('targets')==list(self.targets):return prev.get('report')
            lower=self.replay_floor;incremental=False
            scope_change=self.bounded_targets and prev.get('targets')!=list(self.targets)
            if same_policy and prev.get('source') and (prev.get('targets')==list(self.targets) or scope_change) and source[0]>=prev['source'][0] and source[1]>=prev['source'][1] and list(dead)==prev['source'][2:]:
                lower=max(self.replay_floor,int(prev['report']['sourceThrough'])-300)//60*60;incremental=True
            # Actual traded pools first: no scan of 100k+ untraded identities.
            configs={}
            # Jump to the next indexed pool instead of scanning every execution
            # in the two ever-growing ledgers. This is still the same snapshot;
            # a fork removing a pool's final fill disappears immediately.
            for r in c.execute('''WITH RECURSIVE
              cold(pool) AS (SELECT min(pool) FROM main.fills UNION ALL
                SELECT (SELECT min(pool) FROM main.fills WHERE pool>cold.pool) FROM cold WHERE pool IS NOT NULL),
              hot(pool) AS (SELECT min(pool) FROM observed.fills UNION ALL
                SELECT (SELECT min(pool) FROM observed.fills WHERE pool>hot.pool) FROM hot WHERE pool IS NOT NULL),
              available AS MATERIALIZED (SELECT pool FROM cold WHERE pool IS NOT NULL UNION SELECT pool FROM hot WHERE pool IS NOT NULL)
              SELECT p.pool,p.base,p.quote,p.config FROM available a JOIN main.pools p ON p.pool=a.pool'''):
                cfg=json.loads(r['config']);configs[r['pool']]=(r['base'],r['quote'],cfg)
            # Customer charts read only their exact pools plus causal quote
            # anchors. The public builder keeps its original broad policy.
            if self.bounded_targets:
                direct={q if b==USDC else b for b,q,cfg in configs.values() if USDC in (b,q)}
                own={p for p,(b,q,cfg) in configs.items() if {b,q}&targetset}
                needed=targetset|{m for p in own for m in configs[p][:2]}|REFERENCE_BRIDGES
                admitted=own|{p for p,(b,q,cfg) in configs.items() if USDC in (b,q) and (q if b==USDC else b) in needed}
                admitted|={p for p,(b,q,cfg) in configs.items() if b in needed-direct and q in direct or q in needed-direct and b in direct}
                if len(admitted)<=4096:configs={p:cfg for p,cfg in configs.items() if p in admitted}
            pool_args=list(configs) if self.bounded_targets and len(configs)<=4096 else []
            pool_where=' AND pool IN ('+','.join('?' for _ in pool_args)+')' if pool_args else (' AND 0' if self.bounded_targets and not configs else '')
            windows=[(lower,2**62)]
            if incremental and scope_change and set(self.targets)-set(prev.get('targets',[])):
                lower=max(self.replay_floor,int(self.admission_floor if self.admission_floor is not None else time.time()-3600))//60*60
                windows=merge_windows([*windows,(lower,2**62)])
            if incremental:
                dirty=set();new_direct=False
                # Retained backfill affects its minute and the bounded causal
                # conversion horizon, not every later minute. Include 16 minutes
                # for completed-minute anchors, two conversion legs and pool
                # windows. The live suffix remains independently refreshed.
                for f in c.execute('''SELECT pool,time,base_volume,quote_volume FROM main.fills WHERE seq>?'''+pool_where+'''
                  UNION ALL SELECT pool,time,base_volume,quote_volume FROM observed.fills WHERE rowid>?'''+pool_where,(prev['source'][0],*pool_args,prev['source'][1],*pool_args)):
                    minute=f['time']//60*60
                    if self.replay_floor<=minute<lower:dirty.add(minute)
                    item=configs.get(f['pool'])
                    if not item:continue
                    base,quote,cfg=item
                    if quote==USDC:mint=base;bv=f['base_volume'];qv=f['quote_volume'];inverted=False
                    elif base==USDC:mint=quote;bv=f['quote_volume'];qv=f['base_volume'];inverted=True
                    else:continue
                    if mint in targetset and (not scope_change or mint in prev.get('targets',[])) and not prior_coverage.get(mint,{}).get('directQuoteObserved') and qv>=.01 and precise(bv,qv,cfg,inverted):new_direct=True
                if new_direct:
                    # Discovering a token's first direct quote can change its
                    # historical eligibility, so retain the full replay path.
                    windows=[(self.replay_floor,2**62)];incremental=False
                else:
                    windows=merge_windows([*windows,*[(t,t+960) for t in dirty]])
                    if len(windows)>32:windows=[(windows[0][0],2**62)]
                lower=windows[0][0]
            starts=[a for a,b in windows]
            def writes(stamp):
                i=bisect.bisect_right(starts,stamp)-1
                return i>=0 and stamp<windows[i][1]
            coverage={mint:{'observedPools':[],'pricedPools':[],'unpricedQuotePools':[],'precisionExcluded':0,'unpricedExecutions':0,'conversionOutliers':0,'lowNotionalExcluded':0,'unanchoredPriceSeconds':0,'poolExecutionLegs':0} for mint in self.targets}
            for pool,(base,quote,cfg) in configs.items():
                for mint in {base,quote}&targetset:coverage[mint]['observedPools'].append(pool)
            anchor_bins={};rows=0
            # Bounded recent windows reuse a single ordered SQLite read for all
            # three causal passes. Large/full rebuilds keep streaming instead.
            cached_rows=None
            if incremental:
                reads=merge_windows([(max(0,a-1200),b) for a,b in windows])
                where=' OR '.join('(time>=? AND time<?)' for _ in reads)
                cursor=c.execute('SELECT * FROM fills WHERE ('+where+')'+pool_where+' ORDER BY time,slot,tx_order,event_order,identity',([v for pair in reads for v in pair]+pool_args))
                cached_rows=cursor.fetchmany(200001);cursor.close()
                if len(cached_rows)>200000:cached_rows=None
            def source_rows(warmup):
                reads=merge_windows([(max(0,a-warmup),b) for a,b in windows])
                if cached_rows is not None:
                    def selected():
                        at=0
                        for row in cached_rows:
                            stamp=row['time']
                            while at<len(reads) and stamp>=reads[at][1]:at+=1
                            if at==len(reads):return
                            if stamp>=reads[at][0]:yield row
                    return selected()
                where=' OR '.join('(time>=? AND time<?)' for _ in reads)
                return c.execute('SELECT * FROM fills WHERE ('+where+')'+pool_where+' ORDER BY time,slot,tx_order,event_order,identity',([v for pair in reads for v in pair]+pool_args))
            # Anchor value is published at minute END; no look-ahead conversion.
            for f in source_rows(1200):
                item=configs.get(f['pool'])
                if not item:continue
                base,quote,cfg=item
                if quote==USDC:mint=base;b=f['base_volume'];q=f['quote_volume'];invert=False
                elif base==USDC:mint=quote;b=f['quote_volume'];q=f['base_volume'];invert=True
                else:continue
                if q<.01 or not precise(b,q,cfg,invert):continue
                key=(mint,f['time']//60*60+60);s=anchor_bins.setdefault(key,[0.0,0.0]);s[0]+=b;s[1]+=q
            grouped=collections.defaultdict(list)
            for (mint,t),(b,q) in anchor_bins.items():grouped[mint].append((t,q/b))
            anchors={mint:tuple(map(list,zip(*sorted(points)))) for mint,points in grouped.items()}
            direct_anchors=anchors
            # Prices for a mint at a given second are immutable within this
            # source snapshot. Bound the memo independently of feed cardinality.
            direct_rate=lru_cache(maxsize=16384)(lambda mint,stamp:quote_rate(direct_anchors,mint,stamp))
            direct_quote_known={mint for mint in self.targets if mint in direct_anchors or prior_coverage.get(mint,{}).get('directQuoteObserved')}
            # One extra actual pool leg supports e.g. USD1/SOL -> SOL/USDC.
            # These samples also become visible only after their minute ends.
            indirect={}
            for f in source_rows(840):
                item=configs.get(f['pool'])
                if not item:continue
                base,quote,cfg=item
                if USDC in (base,quote):continue
                for mint,other,bv,qv,inverted in [(base,quote,f['base_volume'],f['quote_volume'],False),(quote,base,f['quote_volume'],f['base_volume'],True)]:
                    rate=direct_rate(other,f['time'])
                    if rate is None or not precise(bv,qv,cfg,inverted):continue
                    key=(mint,f['time']//60*60+60)
                    if key in anchor_bins:continue
                    s=indirect.setdefault(key,[0.0,0.0]);s[0]+=bv;s[1]+=qv*rate
            grouped=collections.defaultdict(list)
            for (mint,t),(b,q) in indirect.items():grouped[mint].append((t,q/b))
            indirect_anchors={mint:tuple(map(list,zip(*sorted(points)))) for mint,points in grouped.items()}
            # A fresh direct USDC observation outranks a newer two-leg estimate.
            anchors={mint:{'direct':direct_anchors.get(mint),'indirect':indirect_anchors.get(mint)} for mint in set(direct_anchors)|set(indirect_anchors)}
            reference_rate=lru_cache(maxsize=16384)(lambda mint,stamp:quote_rate(anchors,mint,stamp))
            target_quotes={m:prior_coverage.get(m,{}).get('quoteMint',USDC) for m in self.targets}
            fallback={m for m in self.targets if target_quotes[m]==USDC and m not in prior_ready}
            native_bars={};native_second={};native_volume=collections.Counter();native_pools=collections.defaultdict(set)
            bars={};second={};direct_second={};bridge_second={};stamp=None;seen=set();duplicates=0;through=0
            price_windows={};last_direct={}
            tapes={mint:collections.deque(maxlen=500) for mint in self.targets};priced=collections.defaultdict(set);unpriced=collections.defaultdict(set)
            def flush():
                for mint,s in second.items():
                    direct=direct_second.get(mint)
                    prior=last_direct.get(mint)
                    prior=prior[1] if prior and 0<=stamp-prior[0]<=300 else None
                    bridge=bridge_second.get(mint)
                    if direct or bridge:
                        # One causal window across already converted pools.
                        # Separate windows made interleaved USDC and SOL ticks
                        # forget each other's still-fresh samples and oscillate.
                        window=price_windows.get(mint)
                        if window is None:window=price_windows[mint]=PoolReferenceWindow()
                        price=window.update(stamp,{**(bridge or {}),**(direct or {})})
                        # Keep the existing direct-anchor eligibility horizon;
                        # a bridge update must not admit previously unanchored
                        # unrelated cross-token legs or change traded volume.
                        if direct:last_direct[mint]=(stamp,price)
                        if bridge:coverage[mint]['bridgePriceSeconds']=coverage[mint].get('bridgePriceSeconds',0)+1
                    # A token's old USDC pool must not permanently suppress its
                    # active SOL/USDT pool. Only an exact known bridge with a
                    # fresh DIRECT executed USDC anchor may take over. Arbitrary
                    # illiquid cross-token routes stay excluded.
                    if not direct and not bridge:
                        if prior is None and target_quotes[mint]==USDC and mint in direct_quote_known:
                            coverage[mint]['unanchoredPriceSeconds']+=1
                            continue
                        price=prior if prior is not None else s[1]/s[0]
                    if not writes(stamp):continue
                    key=(mint,stamp//60*60);b=bars.get(key)
                    if b is None:bars[key]=[price,price,price,price,s[0],s[1],s[2],1]
                    else:b[1]=max(b[1],price);b[2]=min(b[2],price);b[3]=price;b[4]+=s[0];b[5]+=s[1];b[6]+=s[2];b[7]+=1
                second.clear();direct_second.clear();bridge_second.clear();seen.clear()
                for key,s in native_second.items():
                    mint,quote=key;price=s[1]/s[0];bar_key=(mint,quote,stamp//60*60);b=native_bars.get(bar_key)
                    if b is None:native_bars[bar_key]=[price,price,price,price,s[0],s[1],s[2],1]
                    else:b[1]=max(b[1],price);b[2]=min(b[2],price);b[3]=price;b[4]+=s[0];b[5]+=s[1];b[6]+=s[2];b[7]+=1
                native_second.clear()
            # Seed the causal pool window and last direct price before the
            # replaced suffix. Incremental and full rebuilds see the same past.
            for f in source_rows(360):
                through=max(through,f['time']);rows+=1;item=configs.get(f['pool'])
                if not item:continue
                base,quote,cfg=item
                if stamp is not None and f['time']!=stamp:flush()
                stamp=f['time'];sig=f['signature'];identity=(f['slot'],sig,f['pool'],f['event_order'])
                if identity in seen:duplicates+=1;continue
                seen.add(identity)
                for mint in {base,quote}&targetset:
                    inverted=mint==quote;other=base if inverted else quote
                    bv,qv=(f['quote_volume'],f['base_volume']) if inverted else (f['base_volume'],f['quote_volume'])
                    if not bv or not qv:continue
                    info=coverage[mint];info['poolExecutionLegs']+=1
                    precision_ok=precise(bv,qv,cfg,inverted)
                    if mint in fallback and precision_ok and (other!=USDC or qv>=.01):
                        key=(mint,other);native_volume[key]+=bv;native_pools[key].add(f['pool']);s=native_second.setdefault(key,[0.0,0.0,0]);s[0]+=bv;s[1]+=qv;s[2]+=1
                    target_quote=target_quotes[mint]
                    rate=1.0 if other==target_quote else reference_rate(other,stamp)
                    target_rate=reference_rate(target_quote,stamp)
                    if other!=target_quote:rate=rate/target_rate if rate is not None and target_rate is not None else None
                    prior_base=reference_rate(mint,stamp)
                    proposed=qv*rate/bv if rate is not None else None
                    expected=prior_base/target_rate if prior_base is not None and target_rate is not None else None
                    outlier=other!=target_quote and expected is not None and proposed is not None and not expected/2<=proposed<=expected*2
                    low_notional=target_quote==USDC and rate is not None and (qv*rate<.01 or prior_base is not None and bv*prior_base<.01)
                    eligible=precision_ok and rate is not None and not outlier and not low_notional
                    if outlier:info['conversionOutliers']+=1
                    if low_notional:info['lowNotionalExcluded']+=1
                    if not precision_ok:info['precisionExcluded']+=1
                    if rate is None:info['unpricedExecutions']+=1;unpriced[mint].add(f['pool'])
                    reference_price=qv*rate/bv if eligible else None
                    if writes(stamp):tapes[mint].append((mint,':'.join(map(str,identity)),stamp,f['slot'],f['tx_order'],f['event_order'],f['pool'],other,qv/bv,bv,qv,reference_price,int(eligible)))
                    if not eligible:continue
                    priced[mint].add(f['pool']);s=second.setdefault(mint,[0.0,0.0,0]);s[0]+=bv;s[1]+=qv*rate;s[2]+=1
                    if other==target_quote:
                        d=direct_second.setdefault(mint,{}).setdefault(f['pool'],[0.0,0.0]);d[0]+=bv;d[1]+=qv
                    elif mint!=SOL_MINT and target_quote==USDC and other in REFERENCE_BRIDGES:
                        bridge=direct_rate(other,stamp)
                        if bridge is not None:
                            d=bridge_second.setdefault(mint,{}).setdefault(f['pool'],[0.0,0.0]);d[0]+=bv;d[1]+=qv*bridge
            if second or native_second:flush()
            # An asset with no measured USDC path still has a valid native quote.
            # Pin that quote once; never pretend an unknown token equals USD.
            bars={key:value for key,value in bars.items() if writes(key[1])}
            native_bars={key:value for key,value in native_bars.items() if writes(key[2])}
            for mint in fallback:
                if any(m==mint for m,t in bars):continue
                choices=[(amount,q) for (m,q),amount in native_volume.items() if m==mint]
                if not choices:continue
                quote=max(choices,key=lambda row:(row[0],row[1]))[1];target_quotes[mint]=quote
                for (m,q,t),b in native_bars.items():
                    if m==mint and q==quote:bars[(mint,t)]=b
                priced[mint]=native_pools[(mint,quote)];unpriced[mint]-=priced[mint]
                coverage[mint]['unpricedExecutions']-=sum(b[6] for (m,q,t),b in native_bars.items() if m==mint and q==quote)
                revised=collections.deque(maxlen=500)
                for row in tapes[mint]:
                    base,other,cfg=configs[row[6]];inverted=mint==other
                    eligible=row[7]==quote and precise(row[9],row[10],cfg,inverted) and (quote!=USDC or row[10]>=.01)
                    revised.append((*row[:-2],row[8] if eligible else None,int(eligible)))
                tapes[mint]=revised
        for mint in self.targets:
            info=coverage[mint]
            if lower:priced[mint].update(prior_coverage.get(mint,{}).get('pricedPools',[]))
            info['pricedPools']=sorted(priced[mint]);info['unpricedQuotePools']=sorted(unpriced[mint]);info['observedPools'].sort()
        report={'policy':POLICY,'sourceUnionPolicy':union_policy,'sourceThrough':max(through,prev.get('report',{}).get('sourceThrough',0)),'rebuiltFrom':lower,'rebuiltWindows':[[a,None if b==2**62 else b] for a,b in windows],'incrementalWindows':incremental,'sourceRows':rows,'barUpdates':len(bars),'seconds':round(time.monotonic()-started,3),'duplicateLegsRemoved':duplicates,'processedSuffixProvisional':True,'fullSolanaPoolCoverage':False,'fullLifetimeHistoryComplete':False,'shredJoinVerified':False,'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
        with closing(sqlite3.connect(self.path)) as dest, dest:
            where=' OR '.join('(time>=? AND time<?)' for _ in windows);bounds=[v for pair in windows for v in pair]
            if self.bounded_targets:
                where='('+where+') AND mint IN ('+','.join('?' for _ in self.targets)+')';bounds+=list(self.targets)
            dest.execute('BEGIN IMMEDIATE');dest.execute('DELETE FROM bars WHERE '+where,bounds)
            dest.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)',[(mint,t,*values) for (mint,t),values in bars.items()])
            # Tape is rebuilt from the recent source window and contains ALL
            # flows, including dust and records without a conversion anchor.
            dest.execute('DELETE FROM tape WHERE '+where,bounds)
            dest.executemany('INSERT OR REPLACE INTO tape VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',[row for tape in tapes.values() for row in tape])
            for mint,tape in tapes.items():
                if tape:dest.execute('''DELETE FROM tape WHERE mint=? AND (slot,tx_order,event_order,identity)<
                  (SELECT slot,tx_order,event_order,identity FROM tape WHERE mint=?
                   ORDER BY slot DESC,tx_order DESC,event_order DESC,identity DESC LIMIT 1 OFFSET 499)''',(mint,mint))
            for mint,info in coverage.items():
                info.update(quoteMint=target_quotes[mint],policy=POLICY,directQuoteObserved=mint in direct_quote_known,priceSampling='Causal pool median: trailing 5-second VWAP per direct-quote pool, median over distinct pools observed within that window (at most 64 fresh pools). Between direct updates retain the last reference sample for at most 300 seconds; never reset to an older minute average. For non-SOL assets, actual SOL/USDT pool executions with fresh direct bridge anchors immediately update a distinct-pool median; they do not wait behind an older direct-pool sample. Arbitrary expired cross-token estimates stay excluded. Other assets use converted execution VWAP. This is a multi-pool reference index, not raw trade OHLC.',quoteConversion='Prior completed USDC rates; direct observations outrank two-leg estimates; max age 300s. Converted prices outside 0.5–2x the prior base reference are excluded, never clamped.',precisionRule='Both flows >=100 atomic units (one if decimals<2); USDC references require >=0.01 USDC of quote flow and prior-valued base flow when available. Raw tape remains unfiltered.',volumeDefinition='Eligible physical pool execution legs included in priced samples; quote volume converted at prior rates; unanchored executions remain on raw tape; not wallet net swaps',scopeWindowStart=lower,scopeWindowEnd=report['sourceThrough'],nativeQuoteReference=target_quotes[mint]!=USDC)
                dest.execute('INSERT OR REPLACE INTO coverage VALUES(?,?)',(mint,json.dumps(info)))
            for key,value in {'source':source,'targets':list(self.targets),'report':report}.items():dest.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,json.dumps(value)))
        return report

class TokenReferenceReader:
    def __init__(self,directory,continuous_display=False):self.path=Path(directory)/'reference.sqlite3';self.continuous_display=continuous_display
    def catalog(self):
        """One consistent snapshot instead of one HTTP/SQLite read per mint."""
        with closing(sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True)) as c, c:
            c.execute('BEGIN')
            row=c.execute("SELECT value FROM state WHERE key='targets'").fetchone()
            targets=json.loads(row[0]) if row else []
            if not 1<=len(set(targets))==len(targets)<=MAX_REFERENCE_ASSETS:raise ValueError('Invalid reference catalog')
            coverage=dict(c.execute('SELECT mint,value FROM coverage'))
            spans={r[0]:r[1:] for r in c.execute('SELECT mint,min(time),max(time),count(*) FROM bars GROUP BY mint')}
        return dict(tokens=[self.resolution(mint,coverage.get(mint),spans.get(mint,(None,None,0))) for mint in targets])
    def analytics(self,limit=300):
        if type(limit)!=int or not 1<=limit<=300:raise ValueError('Bounded analytics window required')
        with closing(sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True)) as c, c:
            c.row_factory=sqlite3.Row;c.execute('BEGIN')
            row=c.execute("SELECT value FROM state WHERE key='targets'").fetchone()
            targets=json.loads(row[0]) if row else []
            if not 1<=len(set(targets))==len(targets)<=MAX_REFERENCE_ASSETS:raise ValueError('Invalid reference catalog')
            coverage={r['mint']:json.loads(r['value']) for r in c.execute('SELECT mint,value FROM coverage')}
            grouped=collections.defaultdict(list)
            query='''WITH ranked AS (SELECT mint,time,open,close,quote_volume,count,
              row_number() OVER (PARTITION BY mint ORDER BY time DESC) n FROM bars
              WHERE mint IN ('''+','.join('?' for _ in targets)+'''))
              SELECT * FROM ranked WHERE n<=? ORDER BY mint,time'''
            for r in c.execute(query,(*targets,limit)):grouped[r['mint']].append(r)
        tokens=[]
        for mint in targets:
            bars=grouped.get(mint)
            if not bars:continue
            cfg=coverage[mint]
            tokens.append(dict(mint=mint,quoteMint=cfg['quoteMint'],volume=math.fsum(r['quote_volume'] for r in bars),fills=sum(r['count'] for r in bars),
                change=(bars[-1]['close']/bars[0]['open']-1)*100,start=bars[0]['time']*1000,end=bars[-1]['time']*1000,
                stale=time.time()-bars[-1]['time']>180,pricedPoolCount=len(cfg.get('pricedPools',[]))))
        return dict(tokens=tokens,window=limit)
    @staticmethod
    def compact_coverage(coverage):
        out={k:v for k,v in coverage.items() if k not in {'observedPools','pricedPools','unpricedQuotePools'}}
        for src,name in [('observedPools','observedPoolCount'),('pricedPools','pricedPoolCount'),('unpricedQuotePools','unpricedQuotePoolCount')]:out[name]=len(coverage.get(src,[]))
        out['poolSelection']='all_retained_verified_pools_with_supported_execution_decoders'
        return out
    def resolve(self,mint):
        with closing(sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True)) as c, c:
            row=c.execute('SELECT value FROM coverage WHERE mint=?',(mint,)).fetchone()
            if not row:return self.resolution(mint,None,(None,None,0))
            span=c.execute('SELECT min(time),max(time),count(*) FROM bars WHERE mint=?',(mint,)).fetchone()
        return self.resolution(mint,row[0],span)
    @classmethod
    def resolution(cls,mint,encoded,span):
        if encoded is None:return dict(mint=mint,ready=False,state='token_reference_pending',coverage=None)
        coverage=json.loads(encoded);return dict(network='solana',mint=mint,ready=bool(span[2]),state='reference_history_available' if span[2] else 'awaiting_priced_executions',minuteCandles=span[2],first=span[0],last=span[1],coverage=cls.compact_coverage(coverage),quoteMint=coverage['quoteMint'],aggregation=POLICY,live=False)
    def history(self,mint,interval=60,limit=300,before=None,gap_policy='recorded'):
        if not 60<=interval<=WEEK or interval%60 or not 1<=limit<=600 or gap_policy not in {'recorded','carry'}:raise ValueError('Unsupported token history request')
        with closing(sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True)) as c, c:
            c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
            deadline=time.monotonic()+5;c.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
            cfg=c.execute('SELECT value FROM coverage WHERE mint=?',(mint,)).fetchone()
            if not cfg:raise ValueError('Untracked token')
            coverage=json.loads(cfg[0]);end=c.execute('SELECT max(time) FROM bars WHERE mint=?',(mint,)).fetchone()[0]
            bound=bucket_start(before,interval) if before is not None else (bucket_start(end,interval)+interval if end is not None else 0)
            lower=bound-interval*limit
            # Both display policies page real buckets across sparse history.
            # A stale seed must never replace an older execution with hundreds
            # of flat candles or trap the historical cursor in an empty gap.
            origin=345600 if interval==WEEK else 0
            from history_bounds import recorded_window_start
            first=recorded_window_start(c,mint,interval,limit,bound,origin)
            if first is not None:lower=first
            rows=c.execute('SELECT * FROM bars WHERE mint=? AND time>=? AND time<? ORDER BY time',(mint,lower,bound)).fetchall();groups={}
            for r in rows:
                t=bucket_start(r['time'],interval)
                if t+interval>bound:continue
                b=groups.get(t)
                if b is None:groups[t]=dict(time=t,open=r['open'],high=r['high'],low=r['low'],close=r['close'],baseVolume=r['base_volume'],volume=r['quote_volume'],count=r['count'],samples=r['samples'],synthetic=False,sourceTime=t)
                else:b['high']=max(b['high'],r['high']);b['low']=min(b['low'],r['low']);b['close']=r['close'];b['baseVolume']+=r['base_volume'];b['volume']+=r['quote_volume'];b['count']+=r['count'];b['samples']+=r['samples']
            result=[];previous=None
            for t in sorted(groups):
                if gap_policy=='carry' and previous:
                    for carried_time in range(max(previous['time']+interval,t-(limit-1)*interval) if self.continuous_display else previous['time']+interval,t if self.continuous_display else min(t,previous['time']+6*interval),interval):
                        p=previous['close'];result.append(dict(time=carried_time,open=p,high=p,low=p,close=p,baseVolume=0,volume=0,count=0,samples=0,synthetic=True,gapKind='unverified_no_record',sourceTime=previous['time']))
                previous=groups[t];result.append(previous)
            result=result[-limit:]
            earliest=c.execute('SELECT min(time) FROM bars WHERE mint=?',(mint,)).fetchone()[0]
            report=json.loads(c.execute("SELECT value FROM state WHERE key='report'").fetchone()[0])
        coverage=self.compact_coverage(coverage)
        coverage.update(complete=False,start=earliest,end=end,commitment='finalized_prefix_processed_observation_suffix',canonicalFinalityVerified=False,shredJoinVerified=False)
        return dict(candles=result,identity=dict(network='solana',market=mint,baseMint=mint,quoteMint=coverage['quoteMint'],series='reference',aggregation=POLICY),coverage=coverage,sequence=int(time.time()*1000),hasMore=bool(result and earliest<result[0]['time']),nextBefore=result[0]['time'] if result else None,observationAvailable=True,observedThrough=end,engineThrough=report['sourceThrough'],carriedBars=sum(b['synthetic'] for b in result),fillPolicy='carry' if self.continuous_display and gap_policy=='carry' else 'recorded',gapDisclosure='Display-only last-close carry between retained records; no recovered trades or volume. Upstream completeness remains unverified.' if self.continuous_display and gap_policy=='carry' else 'At most five short intervals carry the prior close; longer gaps remain unfilled and upstream completeness is unverified')
    def trades(self,mint,limit=50):
        if not 1<=limit<=100:raise ValueError('Invalid tape size')
        with closing(sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True)) as c, c:
            c.row_factory=sqlite3.Row;rows=c.execute('SELECT * FROM tape WHERE mint=? ORDER BY slot DESC,tx_order DESC,event_order DESC,identity DESC LIMIT ?',(mint,limit)).fetchall()
        return dict(trades=[dict(r) for r in reversed(rows)],identity=dict(network='solana',market=mint,baseMint=mint,series='pool_executions'),commitment='finalized_prefix_processed_observation_suffix',live=False,latencySla=None)
