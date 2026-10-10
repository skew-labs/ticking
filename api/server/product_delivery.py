"""Bounded shared customer delivery, resumption and explicit prepaid stream leases."""
import asyncio
from collections import Counter, OrderedDict, deque
from contextlib import suppress
import hashlib
import html
import json
import math
import secrets
import time
from urllib.parse import urlsplit
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from . import credits, engine_data, global_data, market_data, products, realtime
from .store import db
from . import chart_watches

connections=Counter()
receipts=OrderedDict()
receipt_bytes=0
initial_reads={}

def record_id(row):
    return str(row.get('identity') or row.get('id') or ':'.join(str(row.get(k,'')) for k in ('slot','tx_order','event_order','pool','signature')))

def integrity(data):
    global receipt_bytes
    raw=realtime.encode(data).encode();digest=hashlib.sha256(raw).hexdigest()
    if digest in receipts:return digest
    slots={}
    for row in data.get('trades',[]):
        if type(row.get('slot')) is int:slots.setdefault(row['slot'],[]).append(row)
    packet={'id':digest,'algorithm':'sha256','payloadSha256':digest,'payloadJson':raw.decode(),
            'source':data.get('source','ticking retained execution store'),'identity':data.get('identity'),
            'commitment':data.get('commitment',data.get('coverage',{}).get('commitment','unspecified')),
            'canonicalFinalityVerified':False,'consensusProof':False,
            'definition':'Checksum of the exact delivered window and observed slot subsets. It is not a validator signature, full-block proof or proof of ownership.',
            'slots':[{'slot':slot,'records':len(rows),'sha256':hashlib.sha256(realtime.encode(sorted(rows,key=record_id)).encode()).hexdigest()} for slot,rows in sorted(slots.items())]}
    receipts[digest]=packet
    receipt_bytes+=len(realtime.encode(packet).encode())
    while len(receipts)>1024 or receipt_bytes>16777216:
        _,old=receipts.popitem(last=False);receipt_bytes-=len(realtime.encode(old).encode())
    return digest

class DeliveryTopic(realtime.Topic):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.epoch=secrets.token_hex(8);self.frames=deque();self.frame_bytes=0
    def publish(self,data):
        signature=hashlib.sha256(realtime.encode(data).encode()).digest()
        if signature==self.signature:return
        if self.candle and self.latest and self.latest['candles']==data['candles'] and all(self.latest.get(k)==data.get(k) for k in ('identity','interval','stale','history','fillPolicy','hasMore','quoteMint')):
            self.latest,self.signature=data,signature;return
        previous=self.latest;self.seq+=1
        if self.candle:event=realtime.delta(previous,data,self.seq,self.seq-1)
        elif previous and previous.get('identity')==data.get('identity'):
            old={record_id(r):r for r in previous['trades']}
            event={'type':'trade_delta','seq':self.seq,'base':self.seq-1,
                   'meta':{k:v for k,v in data.items() if k!='trades'},
                   'ids':[record_id(r) for r in data['trades']],
                   'trades':[r for r in data['trades'] if old.get(record_id(r))!=r]}
        else:event={'type':'snapshot','seq':self.seq,'data':data}
        event.update(cursor=self.epoch+':'+str(self.seq),publishedAt=time.time()*1000,receipt=integrity(data))
        wire=realtime.encode(event);self.latest,self.signature=data,signature
        self.frames.append((self.seq,wire));self.frame_bytes+=len(wire.encode())
        while len(self.frames)>16 or self.frame_bytes>1048576:
            _,removed=self.frames.popleft();self.frame_bytes-=len(removed.encode())
        self.published+=1;self.wire_bytes+=len(wire)
        for q in tuple(self.clients):
            if q.full():q.get_nowait();q.put_nowait(self.snapshot())
            else:q.put_nowait(wire)
    def snapshot(self):
        return realtime.encode({'type':'snapshot','seq':self.seq,'data':self.latest,
                                'cursor':self.epoch+':'+str(self.seq),'receipt':integrity(self.latest),'publishedAt':time.time()*1000})
    def resume(self,cursor):
        try:
            boot,seq=cursor.split(':');seq=int(seq)
            if boot!=self.epoch or seq<1 or seq>self.seq:raise ValueError()
            frames=[wire for n,wire in self.frames if n>seq]
            if seq!=self.seq and (not self.frames or self.frames[0][0]>seq+1):raise ValueError()
            return frames
        except (ValueError,AttributeError):return [self.snapshot()]

def selection(market,mint,pool,aggregation):
    if global_data.chosen(market) and aggregation=='exchange':
        if mint or pool:raise HTTPException(422,'Choose one exchange reference.')
        return {'market':global_data.chosen(market)['id'],'aggregation':'exchange'}
    if market:
        if mint or pool:raise HTTPException(422,'Choose one market or mint.')
        mint=market_data.markets.token(market.removesuffix('-USDC'))['mint']
    if aggregation=='token':
        if pool or not engine_data.address(mint):raise HTTPException(422,'Token feeds need an exact mint.')
        return {'mint':mint,'aggregation':'token'}
    if aggregation!='pool':raise HTTPException(422,'Choose pool, token or exchange aggregation.')
    return {'mint':mint,'pool':pool,'aggregation':'pool'}

def install(app,session_user,api_user,api_key,rate,display_claims,display_history):
    @app.post('/api/credits/trial')
    def trial(request:Request):
        user=session_user(request);rate('trial-user:'+user['id'],3,86400)
        rate('trial-ip:'+request.headers.get('x-real-ip',request.client.host),3,86400)
        return products.claim_trial(user['id'])

    @app.get('/api/chart-markets')
    async def chart_markets():
        snapshot=await market_data.markets.snapshot(compact=True,reviewed_only=True)
        rows=[{'id':r['id'],'symbol':r['symbol'],'mint':r.get('mint'),'network':r.get('network','solana'),
               'priced':isinstance(r.get('price'),(int,float)) and r['price']>0,'stale':r.get('stale',True)} for r in snapshot['tokens']]
        return {'markets':rows,'count':len(rows),'definition':'Reviewed catalogue. A chart request still checks retained history readiness; execution coverage differs by asset.'}

    class ProjectCreate(BaseModel):
        name:str=Field(min_length=1,max_length=60)
        origins:list[str]=Field(min_length=1,max_length=5)
    @app.post('/api/projects')
    def create_project(body:ProjectCreate,request:Request):
        user=session_user(request);limit=products.limits(user['id']);rate('project:'+user['id'],20,3600)
        origins=sorted(set(engine_data.origin(v) for v in body.origins))
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT origins FROM chart_projects WHERE user_id=? AND revoked IS NULL',(user['id'],)).fetchall()
            domains=set(origins)
            for r in old:domains.update(json.loads(r['origins']))
            if len(old)>=5 or len(domains)>limit['domains']:raise HTTPException(409,'Your project or registered domain limit is reached.')
            ident='cp_'+secrets.token_hex(12);publishable='ticking_pk_'+secrets.token_urlsafe(24)
            c.execute('INSERT INTO chart_projects VALUES(?,?,?,?,?,?,NULL)',(ident,user['id'],body.name.strip(),json.dumps(origins),publishable,int(time.time())))
        return next(r for r in products.projects(user['id']) if r['id']==ident)
    @app.delete('/api/projects/{ident}')
    def revoke_project(ident:str,request:Request):
        user=session_user(request)
        with db() as c:
            if c.execute('UPDATE chart_projects SET revoked=? WHERE id=? AND user_id=? AND revoked IS NULL',(int(time.time()),ident,user['id'])).rowcount!=1:raise HTTPException(404,'Project not found.')
        return {'ok':True}

    def grant(project,origin,market,interval='1m',trades=False,console_view=False):
        limit=products.limits(project['user_id'])
        if not limit['managed']:raise HTTPException(403,'Choose Charts or Data to publish a managed embed. Public SDK tools remain free.')
        if trades and not limit['data']:raise HTTPException(403,'The execution tape is included with Data.')
        if not console_view and origin not in json.loads(project['origins']):raise HTTPException(403,'Register this app origin in your chart project.')
        selected=global_data.chosen(market)
        if selected and (not trades or selected['source']=='hyperliquid_public'):
            aggregation='exchange'
        else:
            if engine_data.address(market):
                mint=chart_watches.mint_identity(market)
                chart_watches.authorize(project['user_id'],mint,project['id'])
            else:
                t=market_data.markets.token(market)
                if not market_data.market_review(t):raise HTTPException(404,'Choose a reviewed supported market or registered token mint.')
                mint=t['mint']
            selected=engine_data.resolve(mint=mint,aggregation='token');aggregation='token'
            if not selected.get('ready'):raise HTTPException(409,'Retained chart history is not ready for this asset.')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            key=c.execute('SELECT * FROM api_keys WHERE user_id=? AND name=? AND revoked IS NULL',(project['user_id'],'Managed embed '+project['id'])).fetchone()
            if not key:
                ident='embed_'+secrets.token_hex(12);secret=secrets.token_urlsafe(32)
                c.execute('INSERT INTO api_keys(id,user_id,name,hash,prefix,scopes,created) VALUES(?,?,?,?,?,?,?)',(ident,project['user_id'],'Managed embed '+project['id'],hashlib.sha256(secret.encode()).hexdigest(),'managed',json.dumps(['charts:embed']),int(time.time())))
            else:ident=key['id']
            token='td_'+secrets.token_urlsafe(32);expires=int(time.time())+300;digest=hashlib.sha256(token.encode()).hexdigest()
            if aggregation=='exchange':
                c.execute('INSERT INTO exchange_display_tokens(hash,key_id,origin,asset,market,source,quote,expires,project_id) VALUES(?,?,?,?,?,?,?,?,?)',(digest,ident,origin,selected['id'],selected['market'],selected['source'],selected['quote'],expires,project['id']))
            else:
                c.execute('INSERT INTO display_tokens(hash,key_id,origin,mint,pool,expires,aggregation,quote_mint,project_id) VALUES(?,?,?,?,?,?,?,?,?)',(digest,ident,origin,selected['mint'],'',expires,aggregation,selected['quoteMint'],project['id']))
            c.execute('DELETE FROM display_tokens WHERE expires<?',(int(time.time()),));c.execute('DELETE FROM exchange_display_tokens WHERE expires<?',(int(time.time()),))
        return {'token':token,'expires':expires,'aggregation':aggregation,'market':market,'mint':selected.get('mint'),'quoteMint':selected.get('quoteMint'),'origin':origin,'tradeFeed':bool(trades)}

    class EmbedGrant(BaseModel):
        market:str=Field(default='SOL',max_length=44)
        trades:bool=False
    @app.post('/api/chart-watches/{ident}/display-token')
    async def console_chart_token(ident:str,request:Request):
        user=session_user(request)
        row=next((r for r in chart_watches.list_watches(user['id']) if r['id']==ident),None)
        if not row:raise HTTPException(404,'Token watch not found.')
        chart_watches.authorize(user['id'],row['mint'],row['project_id'])
        with db() as c:project=c.execute('SELECT * FROM chart_projects WHERE id=? AND user_id=? AND revoked IS NULL',(row['project_id'],user['id'])).fetchone()
        # No caller-selected origin or market: owner session, exact bound mint,
        # and the console's allowlisted Origin middleware are all required.
        origin=engine_data.origin(request.headers.get('origin',''))
        rate('console-chart-grant:'+user['id'],120)
        return await realtime.threaded(grant,dict(project),origin,row['mint'],console_view=True)
    @app.post('/api/embeds/{publishable}/token')
    async def embed_grant(publishable:str,body:EmbedGrant,request:Request):
        with db() as c:project=c.execute('SELECT * FROM chart_projects WHERE publishable=? AND revoked IS NULL',(publishable,)).fetchone()
        if not project:raise HTTPException(404,'Chart project not found.')
        origin=engine_data.origin(request.headers.get('origin',''))
        rate('embed-grant:'+project['user_id'],120)
        data=await realtime.threaded(grant,dict(project),origin,body.market,trades=body.trades)
        from fastapi.responses import JSONResponse
        return JSONResponse(data,headers={'Access-Control-Allow-Origin':origin,'Vary':'Origin','Cache-Control':'no-store'})
    @app.options('/api/embeds/{publishable}/token')
    def embed_options(publishable:str,request:Request):
        origin=engine_data.origin(request.headers.get('origin',''))
        with db() as c:project=c.execute('SELECT origins FROM chart_projects WHERE publishable=? AND revoked IS NULL',(publishable,)).fetchone()
        if not project or origin not in json.loads(project['origins']):raise HTTPException(403,'Unregistered app origin.')
        from fastapi import Response
        return Response(status_code=204,headers={'Access-Control-Allow-Origin':origin,'Vary':'Origin','Access-Control-Allow-Methods':'POST','Access-Control-Allow-Headers':'Content-Type','Access-Control-Max-Age':'300'})

    @app.get('/api/v1/trades/history')
    def trade_history(request:Request,market:str|None=None,mint:str|None=None,limit:int=100,before:str|None=None,quote_mint:str|None=None):
        api_user(request,'trades:read')
        selected=global_data.chosen(market)
        if selected and selected['source']=='hyperliquid_public':
            if mint or quote_mint:raise HTTPException(422,'Keep the Hyperliquid instrument quote unchanged.')
            return global_data.trades(selected['id'],limit,before)
        q=selection(market,mint,None,'token')
        data=engine_data.get('/token/trades-history',token=q['mint'],quoteMint=quote_mint,limit=limit,before=before)
        data['source']='ticking retained pool execution legs';data['receipt']=integrity(data)
        return data

    @app.get('/api/v1/receipts/{ident}')
    def receipt(ident:str,request:Request):
        api_user(request,'trades:read')
        if ident not in receipts:raise HTTPException(404,'This bounded delivery receipt is no longer cached. Verify the checksum in your retained frame.')
        return receipts[ident]

    async def open_stream(request,kind,query,user_id,authorize,origin='*',after=None,prepaid_key=None,direct=False,loader_override=None):
        limit=products.limits(user_id);category='data' if direct or kind=='trades' or prepaid_key else 'chart'
        cap=2 if prepaid_key else limit['data_streams'] if category=='data' else limit['chart_streams']
        if cap==0:raise HTTPException(403,'Choose Data for execution streams, or explicitly enable prepaid stream access.')
        bucket=(user_id,category)
        if connections[bucket]>=cap:raise HTTPException(429,'Your workspace concurrent stream limit is reached.',headers={'Retry-After':'15'})
        async def loader():
            if loader_override:return await loader_override()
            if kind=='candles':return await realtime.threaded(engine_data.candles,**query)
            return await realtime.threaded(engine_data.trades,**query)
        key=('customer-'+kind,realtime.encode(query))
        existing=realtime.hub.topics.get(key)
        if existing and existing.latest is not None:initial=existing.latest
        else:
            if key not in initial_reads:
                if len(initial_reads)>=48:raise HTTPException(429,'Initial stream reads are busy.',headers={'Retry-After':'5'})
                initial_reads[key]=asyncio.create_task(loader())
                def clear(done):
                    if initial_reads.get(key) is done:initial_reads.pop(key,None)
                    if not done.cancelled():done.exception()
                initial_reads[key].add_done_callback(clear)
            task=initial_reads[key]
            try:initial=await asyncio.shield(task)
            finally:
                if task.done() and initial_reads.get(key) is task:initial_reads.pop(key,None)
        if not initial.get(kind):raise HTTPException(409,'No retained records for this selection. No prepaid stream charge was made.')
        frame_bound=262144 if prepaid_key else 1048448
        if len(realtime.encode(initial).encode())>frame_bound-2048:raise HTTPException(422,'This window exceeds the stream bound. Choose a smaller limit or read bounded history pages. No prepaid charge was made.')
        session=request.headers.get('Idempotency-Key','')
        first_lease=None
        if prepaid_key:
            first_lease=await realtime.threaded(credits.reserve,user_id,prepaid_key['id'],session+':1','stream',{'kind':kind,'query':query,'session':session,'slice':1})
            if first_lease['status']!='reserved':raise HTTPException(409,'Use a fresh stream session ID.')
        ip=request.headers.get('x-real-ip',request.client.host)
        # Admission is synchronous after the initial read; concurrent callers cannot bypass it.
        if connections[bucket]>=cap:
            if first_lease:await realtime.threaded(credits.release,first_lease['id'])
            raise HTTPException(429,'Your workspace concurrent stream limit is reached.',headers={'Retry-After':'15'})
        if key not in realtime.hub.topics:
            if len(realtime.hub.topics)>=realtime.hub.max_topics:
                if first_lease:await realtime.threaded(credits.release,first_lease['id'])
                raise HTTPException(429,'Market stream capacity reached.',headers={'Retry-After':'15'})
            deps=None
            try:
                from .changes import GLOBAL,FULL,TOKEN,POOL
                deps=GLOBAL if query.get('aggregation')=='exchange' else (FULL if kind=='candles' and query.get('history')=='full' else TOKEN if query.get('aggregation')=='token' else POOL)
                selected=global_data.chosen(query.get('asset') or query.get('market'))
                if query.get('aggregation')=='exchange' and selected and selected['source']=='hyperliquid_public':deps=frozenset(('hyperliquid','config'))
            except ImportError:pass
            topic=DeliveryTopic(realtime.hub,key,loader,kind=='candles',.5,**({'dependencies':deps} if deps else {}))
            realtime.hub.topics[key]=topic
        try:topic,queue=realtime.hub.attach(key,loader,ip,query.get('mint'),kind=='candles')
        except Exception:
            if first_lease:await realtime.threaded(credits.release,first_lease['id'])
            if key in realtime.hub.topics and not realtime.hub.topics[key].clients:realtime.hub.topics.pop(key)
            raise
        if topic.latest is None:topic.publish(initial)
        connections[bucket]+=1
        while not queue.empty():queue.get_nowait()
        frames=topic.resume(after) if after else [topic.snapshot()]
        pending=first_lease;stamp=time.monotonic();sent=0;slice_number=1 if first_lease else 0
        async def finish():
            nonlocal pending,sent,stamp
            if not pending:return
            row=pending;pending=None
            if sent:
                meter={'seconds':min(60,max(0,time.monotonic()-stamp)),'bytes':sent}
                try:await realtime.threaded(credits.complete,row['id'],{'session':session,'slice':slice_number,'meter':meter},meter)
                except Exception:await realtime.threaded(credits.release,row['id']);raise
            else:await realtime.threaded(credits.release,row['id'])
            sent=0
        async def events():
            nonlocal pending,stamp,sent,slice_number
            checked=started=time.monotonic()
            try:
                yield ': connected\n\n'
                while time.monotonic()-started<240:
                    if await request.is_disconnected():break
                    if time.monotonic()-checked>=15:
                        await realtime.threaded(authorize);checked=time.monotonic()
                    if pending and time.monotonic()-stamp>=55:await finish()
                    if frames:wire=frames.pop(0)
                    else:
                        try:wire=await asyncio.wait_for(queue.get(),timeout=5)
                        except asyncio.TimeoutError:yield ': heartbeat\n\n';continue
                    if json.loads(wire).get('type')=='unavailable':
                        await finish();yield 'data: '+wire+'\n\n';break
                    size=len(wire.encode())
                    if size>frame_bound:raise HTTPException(422,'Stream frame exceeds the transmission bound.')
                    if prepaid_key:
                        if pending and sent+size>262144:await finish()
                        if not pending:
                            slice_number+=1
                            pending=await realtime.threaded(credits.reserve,user_id,prepaid_key['id'],session+':'+str(slice_number),'stream',{'topic':list(key),'session':session,'slice':slice_number})
                            if pending['status']!='reserved':raise HTTPException(409,'Start a fresh stream session; an earlier session already used this ID.')
                            stamp=time.monotonic()
                        sent+=size
                    yield 'data: '+wire+'\n\n'
                await finish();yield 'data: {"type":"reconnect"}\n\n'
            except (HTTPException,credits.CreditError) as e:
                with suppress(Exception):await finish()
                yield 'data: '+realtime.encode({'type':'reauthorize','detail':getattr(e,'detail',str(e))})+'\n\n'
            finally:
                with suppress(Exception):await finish()
                connections[bucket]-=1
                if not connections[bucket]:connections.pop(bucket,None)
                realtime.hub.detach(topic,queue,ip,query.get('mint'))
        return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-store, no-transform','X-Accel-Buffering':'no','Access-Control-Allow-Origin':origin,'Vary':'Origin'})

    @app.get('/api/v1/stream/{kind}')
    async def data_stream(kind:str,request:Request,market:str|None=None,mint:str|None=None,pool:str|None=None,
                          aggregation:str='token',interval:str='1m',after:str|None=None,prepaid:bool=False,limit:int|None=None):
        if kind not in {'trades','candles'}:raise HTTPException(404,'Choose candles or trades.')
        if after and len(after)>100:raise HTTPException(422,'Resume cursor too large.')
        if prepaid:
            from .credit_api import enabled
            if not enabled():raise HTTPException(503,'Prepaid streams are temporarily unavailable.')
            key=await realtime.threaded(api_key,request,'trades:read' if kind=='trades' else 'candles:read')
            if products.plan_for(key['user_id'])['plan']=='Data':raise HTTPException(409,'Data includes this stream. Remove prepaid=true; your balance will not be charged.')
            if request.headers.get('X-Ticking-Accept-Charges')!='stream-v1':raise HTTPException(422,'Explicitly accept stream-v1 charges.')
            idem=request.headers.get('Idempotency-Key','')
            if not 8<=len(idem)<=70:raise HTTPException(422,'Use a fresh stream Idempotency-Key of 8–70 characters.')
            rate('prepaid-stream:'+key['user_id'],10)
        else:
            key=await realtime.threaded(api_user,request,'trades:read' if kind=='trades' else 'candles:read')
            if not products.limits(key['user_id'])['data_streams']:raise HTTPException(403,'Data includes direct streams. Free can use explicitly prepaid streams.')
        query=selection(market,mint,pool,aggregation)
        bound=600 if kind=='candles' else 100
        window=limit if limit is not None else (100 if prepaid else 300) if kind=='candles' else 100
        if not 1<=window<=bound:raise HTTPException(422,'Choose a bounded candle/execution window.')
        if kind=='candles':query.update(interval=interval,limit=window,history='full',gap_policy='carry');query['pool_pin']=query.pop('pool',None)
        else:
            if query.get('aggregation')=='exchange' and global_data.chosen(query['market'])['source']!='hyperliquid_public':raise HTTPException(409,'Composite prices have no individual-exchange trade tape.')
            query.update(limit=window)
        def reauthorize():
            api_key(request,'trades:read' if kind=='trades' else 'candles:read')
            if not prepaid and not products.limits(key['user_id'])['data_streams']:raise HTTPException(403,'The Data entitlement expired.')
        return await open_stream(request,kind,query,key['user_id'],reauthorize,after=after,prepaid_key=key if prepaid else None,direct=True)

    @app.get('/api/v1/display/trades/stream')
    async def display_trades(request:Request,after:str|None=None):
        claims=await realtime.threaded(display_claims,request)
        products.require_data(claims['user_id'])
        if claims['aggregation']=='exchange':
            selected=global_data.chosen(claims['asset'])
            if not selected or selected['source']!='hyperliquid_public':raise HTTPException(409,'No execution tape for this composite reference.')
            query={'market':selected['id'],'aggregation':'exchange','limit':100}
            return await open_stream(request,'trades',query,claims['user_id'],lambda:display_claims(request,False),origin=claims['origin'],after=after)
        query={'mint':claims['mint'],'quoteMint':claims['quote_mint'],'aggregation':'token','limit':100} if claims['aggregation']=='token' else {'pool':claims['pool'],'base_mint':claims['mint'],'aggregation':'pool','limit':100}
        async def load():return await realtime.threaded(engine_data.get,'/token/trades-history',token=claims['mint'],quoteMint=claims['quote_mint'],limit=100)
        return await open_stream(request,'trades',query,claims['user_id'],lambda:display_claims(request,False),origin=claims['origin'],after=after,loader_override=load if claims['aggregation']=='token' else None)

    # Keep the documented URL while supplying workspace bounds and resumable windows.
    for route in tuple(app.routes):
        if getattr(route,'path',None)=='/api/v1/display/stream':app.routes.remove(route)
    @app.get('/api/v1/display/stream')
    async def chart_stream(request:Request,interval:str='1m',history:str='full',after:str|None=None):
        claims=await realtime.threaded(display_claims,request)
        query={k:claims.get(k) for k in ('aggregation','mint','pool','quote_mint','asset','market','source','quote')}
        query.update(interval=interval,history=history)
        async def load():return await realtime.threaded(display_history,claims,interval,600,None,'carry',history)
        return await open_stream(request,'candles',query,claims['user_id'],lambda:display_claims(request,False),origin=claims['origin'],after=after,loader_override=load)

    @app.get('/api/v1/display/trades/history')
    def display_trade_history(request:Request,limit:int=100,before:str|None=None):
        claims=display_claims(request);products.require_data(claims['user_id'])
        if claims['aggregation']=='exchange':
            selected=global_data.chosen(claims['asset'])
            if not selected or selected['source']!='hyperliquid_public':raise HTTPException(422,'This instrument has no retained execution history.')
            from fastapi.responses import JSONResponse
            return JSONResponse(global_data.trades(selected['id'],limit,before),headers={'Access-Control-Allow-Origin':claims['origin'],'Vary':'Origin','Cache-Control':'no-store'})
        if claims['aggregation']!='token':raise HTTPException(422,'Paged tape history requires a token grant.')
        data=engine_data.get('/token/trades-history',token=claims['mint'],quoteMint=claims['quote_mint'],limit=limit,before=before)
        from fastapi.responses import JSONResponse
        return JSONResponse(data,headers={'Access-Control-Allow-Origin':claims['origin'],'Vary':'Origin','Cache-Control':'no-store'})

    @app.options('/api/v1/display/trades/{path}')
    def tape_preflight(path:str,request:Request):
        if path not in {'history','stream'}:raise HTTPException(404)
        origin=engine_data.origin(request.headers.get('origin',''))
        from fastapi import Response
        return Response(status_code=204,headers={'Access-Control-Allow-Origin':origin,'Vary':'Origin','Access-Control-Allow-Methods':'GET','Access-Control-Allow-Headers':'Authorization','Access-Control-Max-Age':'300'})

    @app.post('/api/embeds/{publishable}/renew')
    def renew_hosted(publishable:str,request:Request):
        claims=display_claims(request)
        with db() as c:project=c.execute('SELECT * FROM chart_projects WHERE id=? AND publishable=? AND revoked IS NULL',(claims['project_id'],publishable)).fetchone()
        if not project or claims['origin']!='https://api.ticking.dev':raise HTTPException(403,'Use the registered app token endpoint.')
        market=claims['asset'] if claims['aggregation']=='exchange' else next((t['id'] for t in market_data.markets.catalog() if t.get('mint')==claims['mint']),claims['mint'])
        if not market:raise HTTPException(409,'The market identity changed.')
        result=grant(dict(project),json.loads(project['origins'])[0],market,trades=claims['aggregation']=='token' and products.limits(claims['user_id'])['data'])
        if result['aggregation']!=claims['aggregation'] or claims['aggregation']=='token' and result['mint']!=claims['mint']:raise HTTPException(409,'The market identity changed. Resolve the chart again.')
        with db() as c:
            table='exchange_display_tokens' if result['aggregation']=='exchange' else 'display_tokens'
            c.execute('UPDATE '+table+' SET origin=? WHERE hash=?',('https://api.ticking.dev',hashlib.sha256(result['token'].encode()).hexdigest()))
        result['origin']='https://api.ticking.dev';return result

    @app.get('/embed/{publishable}')
    def hosted(publishable:str,request:Request,market:str='SOL',theme:str='light',trades:bool=False):
        with db() as c:project=c.execute('SELECT * FROM chart_projects WHERE publishable=? AND revoked IS NULL',(publishable,)).fetchone()
        if not project:raise HTTPException(404,'Chart project not found.')
        ref=urlsplit(request.headers.get('referer',''));parent=ref.scheme+'://'+ref.netloc
        if parent not in json.loads(project['origins']):raise HTTPException(403,'Embed this chart from a registered app origin.')
        grant_data=grant(dict(project),parent,market,trades=trades)
        # The iframe's requests originate here; its embedding ancestors are bound by CSP.
        with db() as c:
            table='exchange_display_tokens' if grant_data['aggregation']=='exchange' else 'display_tokens'
            c.execute('UPDATE '+table+' SET origin=? WHERE hash=?',('https://api.ticking.dev',hashlib.sha256(grant_data['token'].encode()).hexdigest()))
        grant_data['origin']='https://api.ticking.dev'
        settings={'publishable':publishable,'market':market,'theme':'dark' if theme=='dark' else 'light','trades':trades,'grant':grant_data}
        payload=json.dumps(settings).replace('<','\\u003c')
        body='<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0;font-family:system-ui}tick-chart{height:'+('calc(100vh - 170px)' if trades else '100vh')+'}#tape{height:170px;overflow:auto;font-size:12px}td{padding:5px 12px;text-align:right}table{width:100%}</style></head><body><tick-chart tools></tick-chart><div id="tape"></div><script type="application/json" id="ticking-settings">'+payload+'</script><script type="module" src="https://ticking.dev/sdk/hosted-chart.js"></script></body></html>'
        return HTMLResponse(body,headers={'Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'; script-src https://ticking.dev; style-src 'unsafe-inline' https://ticking.dev; connect-src https://api.ticking.dev; img-src https://ticking.dev data:; frame-ancestors "+' '.join(json.loads(project['origins']))})
