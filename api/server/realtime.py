"""Bounded shared downstream streams. Originals and source timestamps stay intact."""
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import functools
import hashlib
import json
import os
from pathlib import Path
import time

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from .changes import feed, POOL, TOKEN, FULL, GLOBAL, HYPERLIQUID

WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix='chart-stream')
INTEREST = Path(os.environ.get('TICKING_INTEREST_FILE', './api/.state/market-interest.json'))

def encode(value):
    return json.dumps(value, separators=(',', ':'), allow_nan=False)

def delta(previous, current, seq, base):
    """Changed closed candles are corrections too; never update only the tail."""
    if not previous or previous.get('identity') != current.get('identity'):
        return {'type':'snapshot', 'seq':seq, 'data':current}
    old = {b['time']:b for b in previous['candles']}
    bars = current['candles']
    return {'type':'delta', 'seq':seq, 'base':base,
            'meta':{k:v for k,v in current.items() if k != 'candles'},
            'times':[b['time'] for b in bars],
            'candles':[b for b in bars if old.get(b['time']) != b]}

class Topic:
    def __init__(self, hub, key, loader, candle, period, dependencies=None):
        self.hub, self.key, self.loader = hub, key, loader
        self.candle, self.period = candle, period
        self.clients = set()
        self.latest = None
        self.signature = None
        self.seq = 0
        self.task = None
        self.dependencies = dependencies
        self.changed = asyncio.Event()
        self.loads = 0
        self.published = 0
        self.wire_bytes = 0
        self.load_ms = 0
        self.reconciles = 0
        self.notified = 0

    def publish(self, data):
        signature = hashlib.sha256(encode(data).encode()).digest()
        if signature == self.signature: return
        if self.candle and self.latest and self.latest['candles']==data['candles'] and all(self.latest.get(k)==data.get(k) for k in ('identity','interval','stale','history','fillPolicy','hasMore','quoteMint')):
            self.latest, self.signature=data, signature
            return
        self.seq += 1
        event = delta(self.latest, data, self.seq, self.seq-1) if self.candle else {'type':'snapshot','seq':self.seq,'data':data}
        self.latest, self.signature = data, signature
        event['publishedAt'] = time.time()*1000
        wire = encode(event)
        self.published += 1; self.wire_bytes += len(wire)
        reset = None
        for queue in tuple(self.clients):
            if queue.full():
                queue.get_nowait()
                # Dropped deltas cannot be applied to a stale base. Replace the
                # entire bounded window instead of silently losing corrections.
                if reset is None: reset = encode({'type':'snapshot','seq':self.seq,'data':data,'publishedAt':event['publishedAt']})
                queue.put_nowait(reset)
            else: queue.put_nowait(wire)

    async def run(self):
        try:
            while self.clients:
                start = time.monotonic()
                # Clear before loading. An update during a read must schedule
                # another read, including corrections to older candles.
                self.changed.clear()
                try:
                    data = await self.loader()
                    self.loads += 1; self.load_ms += (time.monotonic()-start)*1000
                    self.publish(data)
                except asyncio.CancelledError: raise
                except Exception:
                    for queue in tuple(self.clients):
                        if not queue.full(): queue.put_nowait(encode({'type':'unavailable','retryMs':5000}))
                    await asyncio.sleep(5)
                if self.dependencies:
                    try:
                        await asyncio.wait_for(self.changed.wait(), timeout=self.hub.reconcile_seconds if feed.connected else self.period)
                        self.notified += 1
                        # Leading-edge delivery after idle; bounded coalescing
                        # under sustained input and many simultaneous markets.
                        # Keep the old maximum read budget per topic. Events
                        # remove idle/phase waits, not the overload protection.
                        minimum = self.period
                        await asyncio.sleep(max(0, minimum-(time.monotonic()-start)))
                    except asyncio.TimeoutError: self.reconciles += 1
                else:
                    await asyncio.sleep(max(.02, self.period-(time.monotonic()-start)))
        finally:
            if self.hub.topics.get(self.key) is self: self.hub.topics.pop(self.key, None)

class StreamHub:
    def __init__(self, max_topics=48, max_clients=128, reconcile_seconds=5):
        self.topics = {}
        self.ips = Counter()
        self.mints = Counter()
        self.max_topics, self.max_clients = max_topics, max_clients
        self.reconcile_seconds = reconcile_seconds
        self.interest_written = 0
        self.finished = Counter()

    def metrics(self):
        totals = Counter(self.finished)
        for t in self.topics.values():
            for k in ('loads','published','wire_bytes','load_ms','reconciles','notified'): totals[k] += getattr(t,k)
        return {**totals,'topics':len(self.topics),'clients':sum(self.ips.values())}

    def write_interest(self, force=False):
        if not force and time.monotonic()-self.interest_written < 5: return
        try:
            INTEREST.parent.mkdir(parents=True, exist_ok=True)
            payload = {'updated':time.time(), 'ttl':45, 'mints':dict(self.mints.most_common(32))}
            from . import global_data
            hot={}
            for topic in self.topics.values():
                if not topic.clients or not topic.candle or len(topic.key)<2:continue
                try:q=json.loads(topic.key[1])
                except (ValueError,TypeError):continue
                if not isinstance(q,dict):continue
                t=global_data.chosen(q.get('asset') or q.get('market'))
                if t and t['source']=='hyperliquid_public':hot[(t['market'],q.get('interval','1m'))]=len(topic.clients)
            payload['hyperliquid']=[{'coin':coin,'interval':interval} for (coin,interval),_ in sorted(hot.items(),key=lambda x:-x[1])[:8]]
            temp = INTEREST.with_suffix('.tmp')
            temp.write_text(encode(payload));os.replace(temp, INTEREST)
            self.interest_written = time.monotonic()
        except OSError: pass  # Priority is optional; never interrupt the feed.

    def attach(self, key, loader, ip, mint=None, candle=True, period=.5, dependencies=None):
        if sum(self.ips.values()) >= self.max_clients or self.ips[ip] >= 8:
            raise HTTPException(429, 'Stream capacity reached; retry shortly.', headers={'Retry-After':'15'})
        if key not in self.topics:
            if len(self.topics) >= self.max_topics:
                raise HTTPException(429, 'Active market capacity reached; retry shortly.', headers={'Retry-After':'15'})
            self.topics[key] = Topic(self, key, loader, candle, period, dependencies)
        topic = self.topics[key]
        queue = asyncio.Queue(maxsize=1)
        topic.clients.add(queue);self.ips[ip] += 1
        if mint: self.mints[mint] += 1
        self.write_interest(True)
        if topic.latest is not None:
            queue.put_nowait(encode({'type':'snapshot','seq':topic.seq,'data':topic.latest,'publishedAt':time.time()*1000}))
        if topic.task is None: topic.task = asyncio.create_task(topic.run())
        return topic, queue

    def detach(self, topic, queue, ip, mint):
        topic.clients.discard(queue)
        self.ips[ip] -= 1
        if not self.ips[ip]: self.ips.pop(ip, None)
        if mint:
            self.mints[mint] -= 1
            if not self.mints[mint]: self.mints.pop(mint, None)
        if not topic.clients:
            for k in ('loads','published','wire_bytes','load_ms','reconciles','notified'): self.finished[k] += getattr(topic,k)
            topic.task.cancel()
            if self.topics.get(topic.key) is topic: self.topics.pop(topic.key, None)
        self.write_interest(True)

    async def close(self):
        tasks=[t.task for t in self.topics.values() if t.task]
        for task in tasks: task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError): await task
        self.topics.clear();self.ips.clear();self.mints.clear();self.write_interest(True)

hub = StreamHub()

async def threaded(fn, *args, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(WORKERS, functools.partial(fn, *args, **kwargs))

def stream(request, key, loader, *, mint=None, candle=True, period=.5, origin='*', authorize=None, initial=None, dependencies=None):
    ip=request.headers.get('x-real-ip', request.client.host)
    topic, queue=hub.attach(key, loader, ip, mint, candle, period, dependencies)
    if initial is not None and topic.latest is None: topic.publish(initial)
    async def events():
        started=time.monotonic();checked=started
        try:
            yield ': connected\n\n'
            while time.monotonic()-started < 240:
                if await request.is_disconnected(): break
                if time.monotonic()-checked >= 15:
                    if authorize:
                        try: await threaded(authorize)
                        except HTTPException:
                            yield 'data: '+encode({'type':'reauthorize'})+'\n\n';break
                    checked=time.monotonic();hub.write_interest()
                try: wire=await asyncio.wait_for(queue.get(), timeout=5)
                except asyncio.TimeoutError:
                    yield ': heartbeat\n\n';continue
                yield 'data: '+wire+'\n\n'
            yield 'data: '+encode({'type':'reconnect'})+'\n\n'
        finally: hub.detach(topic, queue, ip, mint)
    return StreamingResponse(events(), media_type='text/event-stream', headers={
        'Cache-Control':'no-store, no-transform', 'X-Accel-Buffering':'no',
        'Access-Control-Allow-Origin':origin, 'Vary':'Origin'})

def analytics_snapshot(snapshot):
    rows=snapshot['tokens']
    valid=[r for r in rows if r.get('ranking_eligible') and isinstance(r.get('change'), (int,float))]
    return {'tokens':rows, 'generated_at':snapshot['generated_at'], 'listed':len(rows),
            'priced':sum(isinstance(r.get('price'), (int,float)) and r['price']>0 for r in rows),
            'fresh':sum(not r.get('stale',True) for r in rows),
            'solana':sum(r.get('network')!='exchange' for r in rows),
            'global':sum(r.get('network')=='exchange' for r in rows),
            'advancing':sum(r['change']>0 for r in valid), 'declining':sum(r['change']<0 for r in valid),
            'breadthEligible':len(valid), 'scope':snapshot.get('scope'),
            'changeWindow':'24h', 'volumeScope':'Observed Solana pools; overlapping asset volumes are not summed.'}

def install(app, display_claims, display_history):
    from fastapi import Request
    from . import engine_data, global_data, market_data

    @app.get('/api/public/stream/candles')
    async def public_candles(request:Request, market:str|None=None, mint:str|None=None, pool:str|None=None,
                             aggregation:str='token', interval:str='1m', history:str='full'):
        if global_data.chosen(market):
            if mint or pool or aggregation!='exchange': raise HTTPException(422, 'Select one exchange asset.')
            query=dict(market=global_data.chosen(market)['id'], aggregation='exchange')
            mint=next(iter(global_data.ASSETS.get(query['market'],{}).get('solanaMints',[])),None)
        else:
            if market:
                if mint or pool: raise HTTPException(422, 'Select one listed asset.')
                mint=market_data.markets.token(market.removesuffix('-USDC'))['mint']
            token=next((t for t in market_data.markets.catalog() if t['mint']==mint and market_data.market_review(t)), None)
            if not token:
                from .chart_watches import authorize_preview
                await threaded(authorize_preview,mint,aggregation,pool)
            if aggregation not in {'token','pool'} or pool and aggregation!='pool': raise HTTPException(422, 'Invalid chart scope.')
            query=dict(mint=mint, pool_pin=pool, aggregation=aggregation)
        query.update(interval=interval, limit=600, history=history, gap_policy='carry')
        async def load(): return await threaded(engine_data.candles, **query)
        initial=await load()
        deps=GLOBAL if query.get('aggregation')=='exchange' else (FULL if aggregation=='token' and history=='full' else TOKEN if aggregation=='token' else POOL)
        if query.get('aggregation')=='exchange' and global_data.chosen(query['market'])['source']=='hyperliquid_public':deps=HYPERLIQUID
        authorize=None
        if query.get('aggregation')!='exchange' and not token:
            from .chart_watches import authorize_preview
            authorize=lambda:authorize_preview(mint,aggregation,pool)
        return stream(request, ('candles',encode(query)), load, mint=mint, initial=initial, dependencies=deps,authorize=authorize)

    @app.get('/api/public/stream/markets')
    async def public_markets(request:Request):
        async def load(): return await market_data.markets.snapshot(compact=True, reviewed_only=True)
        return stream(request, ('markets',), load, candle=False, period=5, initial=await load())

    @app.get('/api/public/stream/trades')
    async def public_trades(request:Request, mint:str|None=None, pool:str|None=None, aggregation:str='token',market:str|None=None):
        selected=global_data.chosen(market)
        if selected and selected['source']=='hyperliquid_public':
            if mint or pool or aggregation!='exchange':raise HTTPException(422,'Choose one Hyperliquid market.')
            async def load():return await threaded(global_data.trades,selected['id'])
            return stream(request,('hyperliquid-trades',selected['id']),load,candle=False,period=.5,initial=await load(),dependencies=frozenset(('hyperliquid','config')))
        token=next((t for t in market_data.markets.catalog() if t['mint']==mint and market_data.market_review(t)), None)
        if not token: raise HTTPException(404, 'Choose a reviewed market.')
        if aggregation not in {'token','pool'} or pool and aggregation!='pool': raise HTTPException(422, 'Invalid tape scope.')
        if pool: await threaded(engine_data.pinned, pool, mint)
        query=dict(mint=mint, pool=pool, base_mint=mint if pool else None, aggregation=aggregation, limit=100)
        async def load(): return await threaded(engine_data.trades, **query)
        return stream(request, ('trades',encode(query)), load, mint=mint, candle=False, initial=await load(), dependencies=TOKEN if aggregation=='token' else POOL)

    @app.get('/api/public/engine/book')
    async def public_book(market:str):return await threaded(global_data.book,market)

    @app.get('/api/public/stream/book')
    async def public_book_stream(request:Request,market:str):
        async def load():return await threaded(global_data.book,market)
        return stream(request,('hyperliquid-book',market),load,candle=False,period=.5,initial=await load(),dependencies=frozenset(('hyperliquid','config')))

    @app.get('/api/public/analytics')
    async def public_analytics():
        return analytics_snapshot(await market_data.markets.snapshot(compact=True, reviewed_only=True))

    @app.get('/api/v1/display/stream')
    async def display_stream(request:Request, interval:str='1m', history:str='full'):
        claims=await threaded(display_claims, request)
        async def load(): return await threaded(display_history, claims, interval, 600, None, 'carry', history)
        initial=await load()
        # Identity includes pinned grant quote/pool. Never share under a ticker
        # alone or let URL parameters broaden an authenticated grant.
        key=('display',encode({k:claims.get(k) for k in ('aggregation','mint','pool','quote_mint','asset','market','source','quote')}),interval,history)
        return stream(request, key, load, mint=claims.get('mint') or next(iter(global_data.ASSETS.get(claims.get('asset'),{}).get('solanaMints',[])),None), origin=claims['origin'],
                      authorize=lambda:display_claims(request), initial=initial,
                      dependencies=GLOBAL if claims.get('aggregation')=='exchange' else (FULL if claims.get('aggregation')=='token' and history=='full' else TOKEN if claims.get('aggregation')=='token' else POOL))
