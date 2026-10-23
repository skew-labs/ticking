"""ticking console, API keys, metering, MCP and non-custodial billing."""
import base64
import asyncio
from contextlib import suppress
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from solders.pubkey import Pubkey
from solders.signature import Signature
from . import products, product_delivery, chart_watches
from .store import db, initialize
from . import privy_auth
from . import market_data, engine_data, realtime
from . import credits, credit_api
from .public_compression import PublicMarketCompression
from .payments import billing_quote, plan_catalog, PLANS, RECIPIENT, MINT, crypto_ready, unsigned_transaction, verify_invoice, PaymentError, FailedTransaction

ORIGINS = set(os.environ.get('TICKING_ORIGINS','https://ticking.dev').split(','))
COOKIE = 'ticking_session'
SCOPES = {'markets:read','candles:read','trades:read','charts:embed','mcp:read'}
TOKENS = [('SOL','Solana',148.32),('JUP','Jupiter',.8642),('BONK','Bonk',.00002148),('WIF','dogwifhat',1.284),('JTO','Jito',2.416),('RAY','Raydium',3.782),('POPCAT','Popcat',.4628),('PYTH','Pyth Network',.1846),('ORCA','Orca',2.738),('RENDER','Render',4.128)]

def now(): return int(time.time())
def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def uid(prefix): return prefix+'_'+secrets.token_hex(12)
def password_hash(password, salt=None):
    salt=salt or secrets.token_hex(16)
    value=hashlib.scrypt(password.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()
    return salt+':'+value

def rate(bucket,limit=60,window=60):
    timestamp=now();key=bucket+':'+str(timestamp//window)
    with db() as c:
        c.execute('DELETE FROM rate_limits WHERE expires<?',(timestamp,))
        c.execute('INSERT INTO rate_limits VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1',(key,timestamp+window))
        count=c.execute('SELECT count FROM rate_limits WHERE bucket=?',(key,)).fetchone()['count']
    if count>limit: raise HTTPException(429,'Too many requests. Please try again shortly.',headers={'Retry-After':str(window)})

@asynccontextmanager
async def lifespan(app):
    initialize()
    market_data.markets.restore()
    tasks=[]
    tasks.append(asyncio.create_task(chart_watches.run()))
    if os.environ.get('TICKING_MARKETS_ENABLED','true')=='true': tasks.append(asyncio.create_task(market_data.markets.run()))
    realtime.feed.start(realtime.hub)
    try:
        yield
    finally:
        await realtime.feed.close()
        await realtime.hub.close()
        for task in tasks:
            task.cancel()
            with suppress(asyncio.CancelledError): await task

app=FastAPI(title='ticking console API',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
app.add_middleware(PublicMarketCompression)
market_data.install(app)

@app.middleware('http')
async def boundaries(request,call_next):
    length=request.headers.get('content-length','0')
    if not length.isdigit() or int(length)>32768:
        return JSONResponse({'detail':'Request too large.'},status_code=413)
    if request.url.path.startswith(('/api/public/engine/','/api/public/stream/')):
        try:rate('chart-ip:'+request.headers.get('x-real-ip',request.client.host),90)
        except HTTPException as e:return JSONResponse({'detail':e.detail},status_code=e.status_code,headers=e.headers)
    is_embed=bool(re.fullmatch(r'/api/embeds/ticking_pk_[A-Za-z0-9_-]{20,50}/(?:token|renew)',request.url.path))
    if request.method in {'POST','PUT','PATCH','DELETE'}:
        if not is_embed and request.headers.get('origin') and request.headers['origin'] not in ORIGINS:
            return JSONResponse({'detail':'This origin is not allowed.'},status_code=403)
        if not is_embed and not request.url.path.startswith('/api/v1') and request.url.path!='/api/mcp':
            if request.headers.get('origin') not in ORIGINS:
                return JSONResponse({'detail':'A same-origin console request is required.'},status_code=403)
        if request.method=='POST' and 'application/json' not in request.headers.get('content-type',''):
            return JSONResponse({'detail':'Use application/json.'},status_code=415)
    result=await call_next(request)
    if result.headers.get('content-type','').startswith('text/event-stream'):
        result.headers['Cache-Control']='no-store, no-transform'
    elif request.method=='GET' and result.status_code==200 and request.url.path=='/api/public/engine/fast-feed':
        result.headers['Cache-Control']='no-store'
    elif request.method=='GET' and result.status_code==200 and request.url.path in {'/api/public/engine/candles','/api/public/engine/trades'}:
        result.headers['Cache-Control']='public, max-age=0, s-maxage=1, must-revalidate'
    elif request.method=='GET' and result.status_code==200 and (request.url.path.startswith('/api/public/engine/') or request.url.path=='/api/public/markets' or request.url.path.startswith('/api/public/markets/')):
        result.headers['Cache-Control']='public, max-age=5, s-maxage=15, stale-while-revalidate=15'
    elif request.url.path.startswith('/api/public/token-icons/') and result.status_code==200: result.headers['Cache-Control']='public, max-age=31536000, immutable'
    elif request.url.path=='/api/news': result.headers['Cache-Control']='public, max-age=15, s-maxage=15, stale-while-revalidate=30'
    elif request.url.path.startswith('/api/news/images/') and result.status_code==200: result.headers['Cache-Control']='public, max-age=86400, immutable'
    else: result.headers['Cache-Control']='no-store'
    result.headers['X-Content-Type-Options']='nosniff'
    result.headers['Referrer-Policy']='no-referrer'
    return result

def session_user(request):
    token=request.cookies.get(COOKIE,'')
    if not token or len(token)>150: raise HTTPException(401,'Sign in to your workspace to continue.')
    with db() as c:
        row=c.execute('SELECT u.id,u.email,u.name,u.created FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.expires>?',(digest(token),now())).fetchone()
    if not row: raise HTTPException(401,'Your session has expired. Please sign in again.')
    user=dict(row)
    with db() as c:
        identities=c.execute('SELECT profile FROM auth_identities WHERE user_id=? ORDER BY created',(user['id'],)).fetchall()
    user['login_accounts']=[a for identity in identities for a in json.loads(identity['profile'])]
    user['password_login']=not user['email'].startswith('privy:')
    if not user['password_login']:
        user['email']=next((a['label'] for a in user['login_accounts'] if a['type']=='google'),None)
    return user

def plan_for(user_id): return products.plan_for(user_id)

def log(user_id,action):
    with db() as c: c.execute('INSERT INTO audit(user_id,action,created) VALUES(?,?,?)',(user_id,action,now()))

def month_start():
    return int(datetime.now(timezone.utc).replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp())

def api_key(request,scope):
    header=request.headers.get('authorization','')
    if not header.startswith('Bearer ') or len(header)>200: raise HTTPException(401,'A ticking API key is required.')
    with db() as c:
        row=c.execute('SELECT * FROM api_keys WHERE hash=? AND revoked IS NULL',(digest(header[7:]),)).fetchone()
    if not row: raise HTTPException(401,'This API key is invalid or revoked.')
    row=dict(row)
    if scope not in json.loads(row['scopes']) and not (scope=='trades:read' and row.get('scope_version')=='legacy-v1' and 'candles:read' in json.loads(row['scopes'])): raise HTTPException(403,'This API key does not have the required scope.')
    return row

def api_user(request,scope,meter=True):
    row=api_key(request,scope);limits=products.limits(row['user_id'])
    if scope in {'candles:read','trades:read','mcp:read'}:products.require_data(row['user_id'])
    rate('workspace:'+row['user_id'],limits['rpm'])
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if limits['requests'] is not None and products.used(c,row['user_id'],month_start())>=limits['requests']:
            raise HTTPException(429,'Your monthly allowance is reached. Use explicit prepaid reads or choose Data.',headers={'Retry-After':'60'})
        if meter:products.meter(c,row['user_id'],row['id'],request.url.path)
        c.execute('UPDATE api_keys SET last_used=? WHERE id=?',(now(),row['id']))
    return row

def display_key(request):
    try:return api_user(request,'charts:embed')
    except HTTPException as e:
        if e.status_code!=403:raise
        return api_user(request,'candles:read')

class Credentials(BaseModel):
    email: str=Field(min_length=5,max_length=180)
    password: str=Field(min_length=12,max_length=128)
    name: str=Field(default='My workspace',min_length=1,max_length=60)

def create_session(response,user_id):
    token=secrets.token_urlsafe(32)
    with db() as c:
        c.execute('DELETE FROM sessions WHERE expires<?',(now(),))
        c.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(token),user_id,now()+7*86400))
    response.set_cookie(COOKIE,token,max_age=7*86400,httponly=True,secure=os.environ.get('TICKING_TEST_HTTP')!='true',samesite='lax',path='/api')

@app.get('/api/health')
def health(): return {'status':'ok','service':'ticking-console','data_mode':'executed_history'}

@app.get('/api/config')
def config():
    return {'brand':'ticking','data_mode':'executed_history','auth':{'privy_app_id':privy_auth.app_id(),'google':bool(privy_auth.app_id()),'wallet':bool(privy_auth.app_id())},'payments':{'crypto':crypto_ready(),'network':'solana:mainnet','token':'USDC','mint':MINT,'recipient':RECIPIENT,'wallets':['Phantom','Solflare','MetaMask']},'plans':plan_catalog(),'api_base':'https://ticking.dev/api/v1','mcp_url':'https://ticking.dev/api/mcp','live_feed':False,'display_tokens':True,'chart_refresh_seconds':30,'markets':[{'id':t['id'],'symbol':t['symbol'],'mint':t.get('mint')} for t in market_data.markets.catalog() if market_data.market_review(t) or engine_data.global_data.chosen(t['id'])],'delivery':{'transport':'SSE','requests_per_minute':120,'resumption':'bounded_cached_frames','execution_history':True,'trial_credit_micro':5000000}}

@app.post('/api/auth/signup')
def signup(body:Credentials,request:Request,response:Response):
    rate('signup:'+request.headers.get('x-real-ip',request.client.host),5,3600)
    email=body.email.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email): raise HTTPException(422,'Enter a valid email address.')
    user_id=uid('ws')
    try:
        with db() as c:
            c.execute('INSERT INTO users VALUES(?,?,?,?,?)',(user_id,email,body.name.strip(),password_hash(body.password),now()))
    except sqlite3.IntegrityError: raise HTTPException(409,'This email is already registered. Sign in instead.')
    create_session(response,user_id);log(user_id,'Workspace created')
    return {'id':user_id,'name':body.name.strip(),'email':email}

@app.post('/api/auth/login')
def login(body:Credentials,request:Request,response:Response):
    email=body.email.strip().lower();rate('login:'+request.headers.get('x-real-ip',request.client.host),20,900);rate('login-email:'+digest(email),12,900)
    with db() as c: row=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
    stored=row['password'] if row else password_hash('not-a-real-account')
    valid=hmac.compare_digest(password_hash(body.password,stored.split(':')[0]),stored)
    if not row or not valid: raise HTTPException(401,'Email or password is incorrect.')
    create_session(response,row['id'])
    return {'id':row['id'],'name':row['name'],'email':row['email']}

class PrivyProof(BaseModel):
    access_token:str=Field(min_length=100,max_length=12000)
    identity_token:str=Field(min_length=100,max_length=16000)
    link_intent:str|None=Field(default=None,max_length=100)

@app.post('/api/auth/privy/link-intent')
def privy_link_intent(request:Request):
    user=session_user(request);rate('link-auth:'+user['id'],10,900)
    token=secrets.token_urlsafe(32)
    with db() as c:
        c.execute('DELETE FROM auth_link_intents WHERE expires<?',(now(),))
        c.execute('INSERT INTO auth_link_intents VALUES(?,?,?,?)',(digest(token),user['id'],digest(request.cookies[COOKIE]),now()+600))
    return {'intent':token,'expires_in':600}

@app.post('/api/auth/privy')
def privy_login(body:PrivyProof,request:Request,response:Response):
    rate('privy-login:'+request.headers.get('x-real-ip',request.client.host),30,900)
    subject,profile=privy_auth.verify(body.access_token,body.identity_token)
    current=None
    try: current=session_user(request)
    except HTTPException as e:
        if e.status_code!=401: raise
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        linked=c.execute('SELECT user_id FROM auth_identities WHERE subject=?',(subject,)).fetchone()
        if body.link_intent:
            intent=c.execute('SELECT * FROM auth_link_intents WHERE hash=? AND expires>?',(digest(body.link_intent),now())).fetchone()
            if not current or not intent or intent['user_id']!=current['id'] or intent['session_hash']!=digest(request.cookies.get(COOKIE,'')):
                raise HTTPException(401,'Account connection expired. Open your workspace and try again.')
            if linked and linked['user_id']!=current['id']:
                raise HTTPException(409,'This login already belongs to another workspace. Sign out to use that workspace.')
            user_id=current['id']
            c.execute('DELETE FROM auth_link_intents WHERE hash=?',(digest(body.link_intent),))
        elif linked:
            user_id=linked['user_id']
            if current and current['id']!=user_id: raise HTTPException(409,'Sign out before switching workspaces.')
        elif current:
            raise HTTPException(409,'Use Connect a login from your workspace account to link this identity.')
        else:
            user_id=uid('ws')
            name=next((p.get('name') for p in profile if p.get('name')), 'My workspace')
            # A namespaced internal identifier avoids merging unverified legacy emails.
            c.execute('INSERT INTO users(id,email,name,password,created) VALUES(?,?,?,?,?)',
                      (user_id,'privy:'+subject,name,'!',now()))
        c.execute('INSERT INTO auth_identities VALUES(?,?,?,?) ON CONFLICT(subject) DO UPDATE SET profile=excluded.profile',
                  (subject,user_id,json.dumps(profile),now()))
    create_session(response,user_id)
    log(user_id,'Login connected' if body.link_intent else 'Signed in with Privy')
    return {'id':user_id,'linked':bool(body.link_intent)}

@app.post('/api/auth/logout')
def logout(request:Request,response:Response):
    with db() as c: c.execute('DELETE FROM sessions WHERE hash=?',(digest(request.cookies.get(COOKIE,'')),))
    response.delete_cookie(COOKIE,path='/api');return {'ok':True}

@app.get('/api/console')
def overview(request:Request):
    user=session_user(request);user_id=user['id'];plan=plan_for(user_id)
    with db() as c:
        keys=[dict(r) for r in c.execute("SELECT id,name,prefix,scopes,created,last_used,revoked FROM api_keys WHERE user_id=? AND prefix!='managed' ORDER BY created DESC",(user_id,))]
        feeds=[dict(r) for r in c.execute('SELECT * FROM feeds WHERE user_id=? ORDER BY created DESC',(user_id,))]
        invoices=[dict(r) for r in c.execute("SELECT id,plan,amount,method,status,created,expires,signature,paid_at,billing_period,term_days FROM invoices WHERE user_id=? AND purpose='plan' ORDER BY created DESC LIMIT 30",(user_id,))]
        usage=[dict(r) for r in c.execute("SELECT strftime('%Y-%m-%d',created,'unixepoch') day,SUM(units) requests FROM usage_events WHERE user_id=? AND created>=? GROUP BY day",(user_id,month_start()))]
        endpoints=[dict(r) for r in c.execute('SELECT endpoint,SUM(units) requests FROM usage_events WHERE user_id=? AND created>=? GROUP BY endpoint',(user_id,month_start()))]
        audit=[dict(r) for r in c.execute('SELECT action,created FROM audit WHERE user_id=? ORDER BY id DESC LIMIT 8',(user_id,))]
    with db() as c:
        extra=[dict(r) for r in c.execute("SELECT strftime('%Y-%m-%d',hour,'unixepoch') day,SUM(units) requests FROM usage_rollups WHERE user_id=? AND hour>=? GROUP BY day",(user_id,month_start()))]
        endpoint_extra=[dict(r) for r in c.execute('SELECT endpoint,SUM(units) requests FROM usage_rollups WHERE user_id=? AND hour>=? GROUP BY endpoint',(user_id,month_start()))]
    def combined(rows,key):
        result={}
        for r in rows:result[r[key]]=result.get(r[key],0)+r['requests']
        return [{key:k,'requests':v} for k,v in sorted(result.items())]
    usage=combined(usage+extra,'day');endpoints=combined(endpoints+endpoint_extra,'endpoint')
    for key in keys: key['scopes']=json.loads(key['scopes'])
    for feed in feeds: feed['markets']=json.loads(feed['markets'])
    return {'user':user,'plan':plan,'limits':products.spec(plan['plan']),'projects':products.projects(user_id),'chart_watches':chart_watches.list_watches(user_id),'keys':keys,'feeds':feeds,'invoices':invoices,'usage':usage,'endpoints':endpoints,'audit':audit,'requests':sum(r['requests'] for r in usage),'data_mode':'executed_history','credits':{'available':credit_api.enabled(),**credits.snapshot(user_id)}}

class KeyCreate(BaseModel):
    name: str=Field(min_length=1,max_length=60)
    scopes: list[str]=Field(min_length=1,max_length=5)

@app.post('/api/keys')
def create_key(body:KeyCreate,request:Request):
    user=session_user(request);rate('create-key:'+user['id'],20,3600)
    if not set(body.scopes)<=SCOPES: raise HTTPException(422,'Choose a supported API scope.')
    key='ticking_sk_'+secrets.token_urlsafe(32);key_id=uid('key');plan=plan_for(user['id'])
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT COUNT(*) n FROM api_keys WHERE user_id=? AND revoked IS NULL AND prefix!='managed'",(user['id'],)).fetchone()['n']>=products.spec(plan['plan'])['keys']: raise HTTPException(409,'Your active API key limit is reached. Revoke an unused key or change plans.')
        c.execute('INSERT INTO api_keys(id,user_id,name,hash,prefix,scopes,created) VALUES(?,?,?,?,?,?,?)',(key_id,user['id'],body.name.strip(),digest(key),key[:18],json.dumps(sorted(set(body.scopes))),now()))
    with db() as c:c.execute("UPDATE api_keys SET scope_version='v2' WHERE id=?",(key_id,))
    log(user['id'],'API key created: '+body.name.strip())
    return {'id':key_id,'key':key,'name':body.name.strip(),'shown_once':True}

@app.delete('/api/keys/{key_id}')
def revoke_key(key_id:str,request:Request):
    user=session_user(request)
    with db() as c:
        result=c.execute('UPDATE api_keys SET revoked=? WHERE id=? AND user_id=? AND revoked IS NULL',(now(),key_id,user['id']))
        if result.rowcount!=1: raise HTTPException(404,'API key not found.')
    log(user['id'],'API key revoked');return {'ok':True}

class FeedCreate(BaseModel):
    name:str=Field(min_length=1,max_length=60)
    markets:list[str]=Field(min_length=1,max_length=10)
    interval:str=Field(default='1m',max_length=8)

def interval_seconds(interval):
    match=re.fullmatch(r'([1-9][0-9]{0,4})(s|m|h|d|w)',interval)
    value=int(match[1])*{'s':1,'m':60,'h':3600,'d':86400,'w':604800}[match[2]] if match else 0
    if not 1<=value<=604800: raise HTTPException(422,'Use an interval between 1s and 1w, such as 3s or 3m.')
    return value

@app.post('/api/feeds')
def create_feed(body:FeedCreate,request:Request):
    user=session_user(request);interval_seconds(body.interval)
    for market in body.markets: engine_data.resolve(market=market)
    markets=sorted(set(body.markets));plan=plan_for(user['id'])
    if len(markets)>products.spec(plan['plan'])['markets']: raise HTTPException(409,'The selected markets exceed your plan allowance.')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT COUNT(*) n FROM feeds WHERE user_id=?',(user['id'],)).fetchone()['n']>=10: raise HTTPException(409,'You can save up to 10 feed configurations.')
        feed_id=uid('feed');c.execute('INSERT INTO feeds VALUES(?,?,?,?,?,?)',(feed_id,user['id'],body.name.strip(),json.dumps(markets),body.interval,now()))
    log(user['id'],'Chart feed configured: '+body.name.strip());return {'id':feed_id,'status':'configured','live':False}

@app.delete('/api/feeds/{feed_id}')
def delete_feed(feed_id:str,request:Request):
    user=session_user(request)
    with db() as c:
        if c.execute('DELETE FROM feeds WHERE id=? AND user_id=?',(feed_id,user['id'])).rowcount!=1: raise HTTPException(404,'Feed not found.')
    return {'ok':True}

def markets_data(aggregation='token'):
    return engine_data.markets_data(aggregation)

def candles_data(market,interval,limit):
    return engine_data.candles(market=market,interval=interval,limit=limit)

@app.get('/api/v1/markets')
def markets(request:Request,aggregation:str='token'): api_user(request,'markets:read');return markets_data(aggregation)

@app.get('/api/v1/candles')
def candles(request:Request,market:str|None=None,mint:str|None=None,pool:str|None=None,interval:str='1m',limit:int=300,before:int|None=None,gap_policy:str='recorded',aggregation:str='pool',history:str='executions'):
    api_user(request,'candles:read')
    return engine_data.candles(market=market,mint=mint,pool=pool,interval=interval,limit=limit,before=before,gap_policy=gap_policy,aggregation=aggregation,history=history)

@app.get('/api/v1/resolve')
def resolve_asset(request:Request,mint:str|None=None,pool:str|None=None,market:str|None=None,quote:str|None=None,aggregation:str='pool'):
    api_user(request,'markets:read');return engine_data.resolve(mint,pool,market,quote,aggregation)

@app.get('/api/v1/assets')
def asset_catalog(request:Request,after:str='',limit:int=100):
    api_user(request,'markets:read');return engine_data.get('/assets',after=after,limit=limit)

@app.get('/api/v1/trades')
def asset_trades(request:Request,mint:str|None=None,pool:str|None=None,market:str|None=None,limit:int=50,aggregation:str='pool'):
    api_user(request,'trades:read');return engine_data.trades(mint,pool,market,limit,aggregation=aggregation)

@app.get('/api/v1/book')
def market_book(request:Request,market:str):
    api_user(request,'trades:read')
    return engine_data.global_data.book(market)

class DisplayCreate(BaseModel):
    origin:str=Field(max_length=200)
    project_id:str|None=Field(default=None,max_length=40)
    mint:str|None=Field(default=None,max_length=44)
    pool:str|None=Field(default=None,max_length=44)
    market:str|None=Field(default=None,max_length=40)
    aggregation:Literal['pool','token','exchange']='pool'

@app.post('/api/v1/display-token')
def display_token(body:DisplayCreate,request:Request):
    key=display_key(request);target=engine_data.origin(body.origin)
    if products.plan_for(key['user_id'])['plan'] in {'Charts','Data'}:
        with db() as c:project=c.execute('SELECT * FROM chart_projects WHERE id=? AND user_id=? AND revoked IS NULL',(body.project_id,key['user_id'])).fetchone()
        if not project or target not in json.loads(project['origins']):raise HTTPException(403,'Register this app origin in your chart project first.')
    if body.aggregation=='exchange':
        if body.mint or body.pool:raise HTTPException(422,'Exchange grants require a market, without a Solana selector.')
        selected=engine_data.global_data.chosen(body.market)
        if not selected:raise HTTPException(404,'Unknown exchange instrument.')
        token='td_'+secrets.token_urlsafe(32);expires=now()+300
        with db() as c:
            c.execute('DELETE FROM exchange_display_tokens WHERE expires<?',(now(),))
            c.execute('INSERT INTO exchange_display_tokens(hash,key_id,origin,asset,market,source,quote,expires) VALUES(?,?,?,?,?,?,?,?)',(digest(token),key['id'],target,selected['id'],selected['market'],selected['source'],selected['quote'],expires))
        with db() as c:c.execute('UPDATE exchange_display_tokens SET project_id=? WHERE hash=?',(body.project_id,digest(token)))
        return {'token':token,'expires':expires,'origin':target,'market':selected['id'],'venue':selected['source'],'aggregation':'exchange','mode':'exchange_history','live':False}
    if body.market:raise HTTPException(422,'A market selector requires exchange aggregation for display grants.')
    if body.aggregation=='token':
        if body.mint and not any(t.get('mint')==body.mint for t in market_data.markets.catalog()):chart_watches.authorize(key['user_id'],body.mint,body.project_id)
        selected=engine_data.resolve(mint=body.mint,pool=body.pool,aggregation='token')
        if not selected['ready']:raise HTTPException(409,'The token reference is not priced yet.')
        selected={'baseMint':selected['mint'],'pool':'','quoteMint':selected['quoteMint']}
    else:selected=engine_data.pinned(body.pool,body.mint) if body.pool else engine_data.chosen(mint=body.mint)
    token='td_'+secrets.token_urlsafe(32);expires=now()+300
    with db() as c:
        c.execute('DELETE FROM display_tokens WHERE expires<?',(now(),))
        c.execute('INSERT INTO display_tokens(hash,key_id,origin,mint,pool,expires,aggregation,quote_mint) VALUES(?,?,?,?,?,?,?,?)',(digest(token),key['id'],target,selected['baseMint'],selected['pool'],expires,body.aggregation,selected.get('quoteMint')))
    with db() as c:c.execute('UPDATE display_tokens SET project_id=? WHERE hash=?',(body.project_id,digest(token)))
    return {'token':token,'expires':expires,'origin':target,'mint':selected['baseMint'],'pool':selected['pool'] or None,'quoteMint':selected.get('quoteMint'),'aggregation':body.aggregation,'mode':'executed_history','live':False}

def display_claims(request,meter=True):
    header=request.headers.get('authorization','')
    if not header.startswith('Bearer td_') or len(header)>100:raise HTTPException(401,'A short-lived display token is required.')
    with db() as c:
        row=c.execute('SELECT d.*,k.user_id,k.scopes,k.revoked FROM display_tokens d JOIN api_keys k ON d.key_id=k.id WHERE d.hash=? AND d.expires>?',(digest(header[7:]),now())).fetchone()
        if not row:row=c.execute("SELECT d.*,k.user_id,k.scopes,k.revoked,'exchange' aggregation FROM exchange_display_tokens d JOIN api_keys k ON d.key_id=k.id WHERE d.hash=? AND d.expires>?",(digest(header[7:]),now())).fetchone()
    if not row or row['revoked'] or not {'candles:read','charts:embed'}&set(json.loads(row['scopes'])):raise HTTPException(401,'The display token expired or its API key was revoked.')
    limit=products.limits(row['user_id'])
    if row['aggregation']=='token' and row['project_id'] and not any(t.get('mint')==row['mint'] for t in engine_data.CATALOG):chart_watches.authorize(row['user_id'],row['mint'],row['project_id'])
    if row['project_id']:
        with db() as c:project=c.execute('SELECT * FROM chart_projects WHERE id=? AND user_id=? AND revoked IS NULL',(row['project_id'],row['user_id'])).fetchone()
        if not project or not limit['managed']:raise HTTPException(403,'The chart project was revoked or its managed plan expired.')
    elif not limit['data']:raise HTTPException(403,'Managed Charts requires a registered project.')
    incoming=request.headers.get('origin')
    if not incoming and request.headers.get('sec-fetch-site')=='same-origin':
        # Same-origin GET fetches omit Origin. Only the configured TLS host can
        # use this path; browser Sec-Fetch-Site is a forbidden script header.
        host=request.headers.get('x-forwarded-host',request.headers.get('host',''))
        candidate='https://'+host
        if candidate==row['origin']:incoming=candidate
    if incoming!=row['origin']:raise HTTPException(403,'This display token belongs to another app origin.')
    if not meter:return dict(row)
    rate('workspace:'+row['user_id'],limit['rpm'])
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if limit['requests'] is not None and products.used(c,row['user_id'],month_start())>=limit['requests']:
            raise HTTPException(429,'Your monthly allowance is reached.',headers={'Retry-After':'60'})
        products.meter(c,row['user_id'],row['key_id'],request.url.path)
    return dict(row)

@app.options('/api/v1/display/{kind}')
def display_preflight(kind:str,request:Request):
    target=engine_data.origin(request.headers.get('origin'))
    if kind not in {'candles','trades','stream'}:raise HTTPException(404)
    return Response(status_code=204,headers={'Access-Control-Allow-Origin':target,'Access-Control-Allow-Methods':'GET','Access-Control-Allow-Headers':'Authorization','Access-Control-Max-Age':'300','Vary':'Origin'})

@app.get('/api/v1/display/candles')
def display_candles(request:Request,response:Response,interval:str='1m',limit:int=300,before:int|None=None,gap_policy:str='recorded',history:str='executions'):
    d=display_claims(request);response.headers['Access-Control-Allow-Origin']=d['origin'];response.headers['Vary']='Origin'
    return display_history(d,interval,limit,before,gap_policy,history)

def display_history(d,interval,limit,before=None,gap_policy='recorded',history='executions'):
    if d.get('aggregation')=='exchange':
        selected=engine_data.global_data.chosen(d['asset'])
        if not selected or (selected['source'],selected['market'],selected['quote'])!=(d['source'],d['market'],d['quote']):raise HTTPException(409,'Exchange instrument identity changed. Request a new grant.')
        return engine_data.global_data.candles(d['asset'],interval,limit,before,gap_policy)
    if d.get('aggregation')=='token':return engine_data.candles(mint=d['mint'],quote=d['quote_mint'],interval=interval,limit=limit,before=before,gap_policy=gap_policy,aggregation='token',history=history)
    # Re-resolve with the granted pool pinned; mint controls quote-side inversion.
    return engine_data.candles(mint=d['mint'],interval=interval,limit=limit,before=before,pool_pin=d['pool'],gap_policy=gap_policy)

@app.get('/api/v1/display/trades')
def display_trades(request:Request,response:Response,limit:int=50):
    d=display_claims(request);products.require_data(d['user_id']);response.headers['Access-Control-Allow-Origin']=d['origin'];response.headers['Vary']='Origin'
    if d.get('aggregation')=='exchange':return engine_data.global_data.trades(d['asset'])
    if d.get('aggregation')=='token':return engine_data.trades(mint=d['mint'],limit=limit,aggregation='token')
    return engine_data.trades(pool=d['pool'],limit=limit,base_mint=d['mint'])

@app.post('/api/console/test-request')
def test_request(request:Request):
    user=session_user(request);rate('test:'+user['id'],20)
    limits=products.limits(user['id'])
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if limits['requests'] is not None and products.used(c,user['id'],month_start())>=limits['requests']:raise HTTPException(429,'Your monthly allowance is reached.')
        products.meter(c,user['id'],None,'/api/console/test-request')
    return markets_data()

@app.post('/api/mcp')
async def mcp(request:Request):
    api_user(request,'mcp:read')
    try: body=await request.json()
    except ValueError: raise HTTPException(400,'Invalid JSON.')
    if not isinstance(body,dict) or body.get('jsonrpc')!='2.0': raise HTTPException(400,'A JSON-RPC 2.0 message is required.')
    method=body.get('method');id=body.get('id');params=body.get('params') or {}
    if not isinstance(params,dict): return {'jsonrpc':'2.0','id':id,'error':{'code':-32602,'message':'Invalid params'}}
    if method=='notifications/initialized': return Response(status_code=202)
    if id is None: return Response(status_code=202)
    result=None;error=None
    if method=='initialize': result={'protocolVersion':params.get('protocolVersion') if params.get('protocolVersion') in {'2025-11-25','2025-06-18','2025-03-26'} else '2025-11-25','capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'ticking','version':'0.1.0'},'instructions':'Read-only retained Solana execution history. Coverage and freshness accompany every chart. No trading or wallet access.'}
    elif method=='ping': result={}
    elif method=='tools/list': result={'tools':[
        {'name':'list_markets','description':'List verified ticking markets and their recorded-history readiness.','inputSchema':{'type':'object','properties':{},'additionalProperties':False},'annotations':{'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False}},
        {'name':'get_candles','description':'Read retained executed OHLCV candles. Use a Solana mint or catalog symbol; coverage is partial.','inputSchema':{'type':'object','properties':{'market':{'type':'string'},'interval':{'type':'string'},'limit':{'type':'integer','minimum':1,'maximum':500}},'required':['market'],'additionalProperties':False},'annotations':{'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False}}]}
    elif method=='tools/call':
        args=params.get('arguments') or {};name=params.get('name')
        try:
            if not isinstance(args,dict): raise ValueError('Invalid tool arguments.')
            if name=='list_markets': data=markets_data()
            elif name=='get_candles': data=candles_data(args.get('market','SOL-USDC'),args.get('interval','1m'),args.get('limit',100))
            else: raise HTTPException(404,'Unknown tool.')
            result={'content':[{'type':'text','text':json.dumps(data)}],'isError':False}
        except (HTTPException,TypeError,ValueError) as e: result={'content':[{'type':'text','text':getattr(e,'detail','Invalid tool arguments.')}],'isError':True}
    else: error={'code':-32601,'message':'Method not found'}
    return {'jsonrpc':'2.0','id':id,**({'error':error} if error else {'result':result})}

class InvoiceCreate(BaseModel):
    billing_period:Literal['monthly','yearly']='monthly'
    plan:str=Field(max_length=20)
    payer:str=Field(min_length=32,max_length=44)
    accepted_preview:bool=False

@app.post('/api/billing/invoices')
def new_invoice(body:InvoiceCreate,request:Request):
    user=session_user(request);rate('invoices:'+user['id'],10,3600)
    if not crypto_ready(): raise HTTPException(503,'Wallet checkout is not configured yet.')
    if body.plan not in PLANS or body.plan=='Explore': raise HTTPException(422,'Choose a paid plan.')
    if not body.accepted_preview: raise HTTPException(422,'Confirm the selected chart/data scope and bounded delivery limits before paying.')
    try:
        if not Pubkey.from_string(body.payer).is_on_curve(): raise ValueError()
    except ValueError: raise HTTPException(422,'Connect a valid Solana wallet.')
    if body.payer==RECIPIENT: raise HTTPException(422,'The recipient wallet cannot pay its own invoice.')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        existing=c.execute("SELECT * FROM invoices WHERE user_id=? AND plan=? AND payer=? AND billing_period=? AND status='pending' AND expires>?",(user['id'],body.plan,body.payer,body.billing_period,now())).fetchone()
        if existing: return invoice_public(dict(existing))
        invoice={'id':uid('inv'),'user_id':user['id'],'plan':body.plan,'amount':billing_quote(body.plan,body.billing_period)['amount'],'method':'USDC · Solana','status':'pending','created':now(),'expires':now()+1800,'payer':body.payer,'reference':str(Pubkey.from_bytes(secrets.token_bytes(32)))}
        invoice.update(recipient=RECIPIENT,mint=MINT,billing_period=body.billing_period,term_days=billing_quote(body.plan,body.billing_period)['term_days'])
        c.execute('INSERT INTO invoices(id,user_id,plan,amount,method,status,created,expires,payer,reference,recipient,mint,billing_period,term_days) VALUES(:id,:user_id,:plan,:amount,:method,:status,:created,:expires,:payer,:reference,:recipient,:mint,:billing_period,:term_days)',invoice)
    with db() as c:c.execute('UPDATE invoices SET policy_version=? WHERE id=?',(products.VERSION,invoice['id']))
    return invoice_public(invoice)

def invoice_public(invoice):
    return {k:invoice.get(k) for k in ['id','plan','amount','method','status','created','expires','payer','reference','signature','paid_at']}|{'recipient':invoice.get('recipient') or RECIPIENT,'mint':invoice.get('mint') or MINT,'network':'solana:mainnet','billing_period':invoice.get('billing_period','monthly'),'term_days':invoice.get('term_days',30),'data_mode':'executed_history'}

def owned_invoice(invoice_id,request):
    user=session_user(request)
    with db() as c: invoice=c.execute("SELECT * FROM invoices WHERE id=? AND user_id=? AND purpose='plan'",(invoice_id,user['id'])).fetchone()
    if not invoice: raise HTTPException(404,'Invoice not found.')
    return dict(invoice)

@app.get('/api/billing/invoices/{invoice_id}')
def get_invoice(invoice_id:str,request:Request): return invoice_public(owned_invoice(invoice_id,request))

@app.post('/api/billing/invoices/{invoice_id}/transaction')
def transaction(invoice_id:str,request:Request):
    invoice=owned_invoice(invoice_id,request);rate('prepare:'+invoice_id,10)
    if not crypto_ready(): raise HTTPException(503,'Wallet checkout is temporarily unavailable.')
    if invoice['status']!='pending' or invoice['expires']<now(): raise HTTPException(409,'This invoice is already paid or its payment window has expired.')
    if invoice['signature']: raise HTTPException(409,'A transaction is already submitted. Verify it instead of paying again.')
    rate('credit-rpc-global',6)
    try: return unsigned_transaction(invoice,rpc_fn=credit_api.payment_rpc)
    except PaymentError as e: raise HTTPException(503,str(e))

class Verify(BaseModel):
    signature:str=Field(min_length=64,max_length=88)

@app.post('/api/billing/invoices/{invoice_id}/verify')
def verify(invoice_id:str,body:Verify,request:Request):
    invoice=owned_invoice(invoice_id,request);rate('verify:'+invoice_id,15)
    if invoice['status']=='paid': return invoice_public(invoice)
    if invoice['status']=='failed': raise HTTPException(409,'This transaction failed on Solana. Create a new invoice to retry.')
    rate('credit-rpc-global',6)
    try: Signature.from_string(body.signature)
    except ValueError: raise HTTPException(422,'Invalid Solana transaction signature.')
    if invoice['signature'] and invoice['signature']!=body.signature: raise HTTPException(409,'Verify the already submitted transaction before retrying payment.')
    try:
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT signature FROM invoices WHERE id=?',(invoice_id,)).fetchone()['signature']
            if current and current!=body.signature: raise HTTPException(409,'Another transaction is already associated with this invoice.')
            c.execute('UPDATE invoices SET signature=? WHERE id=? AND signature IS NULL',(body.signature,invoice_id))
        valid=verify_invoice(invoice,body.signature,rpc_fn=credit_api.payment_rpc,evidence=True)
    except sqlite3.IntegrityError: raise HTTPException(409,'This transaction is already associated with another invoice.')
    except FailedTransaction as e:
        with db() as c: c.execute("UPDATE invoices SET status='failed' WHERE id=? AND status='pending'",(invoice_id,))
        raise HTTPException(422,str(e))
    except PaymentError as e: raise HTTPException(422,str(e))
    if not valid: return {'id':invoice_id,'status':'confirming','signature':body.signature,'message':'Waiting for finalized Solana confirmation. Do not pay again.'}
    if not isinstance(valid,dict) or valid.get('signature')!=body.signature or valid.get('genesis')!=credit_api.payments.GENESIS:raise HTTPException(503,'A complete verified payment receipt is required.')
    from .payments import validate_receipt
    if not validate_receipt(invoice,valid.get('transaction'),valid.get('status')):raise HTTPException(422,'The receipt is not finalized.')
    encoded=json.dumps(valid,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(encoded.encode())>262144:raise HTTPException(503,'The receipt needs reconciliation.')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("UPDATE invoices SET status='paid',paid_at=? WHERE id=? AND status='pending'",(now(),invoice_id)).rowcount:
            c.execute('INSERT INTO plan_receipts VALUES(?,?,?,?,?)',(invoice_id,body.signature,encoded,digest(encoded),now()))
            existing=c.execute('SELECT MAX(expires) expires FROM entitlements WHERE user_id=? AND plan=?',(invoice['user_id'],invoice['plan'])).fetchone()['expires'] or 0
            expires=max(now(),existing)+invoice['term_days']*86400
            c.execute('INSERT INTO entitlements VALUES(?,?,?,?)',(invoice['user_id'],invoice_id,invoice['plan'],expires))
            c.execute('INSERT INTO audit(user_id,action,created) VALUES(?,?,?)',(invoice['user_id'],'Payment finalized: '+invoice['plan']+' · '+invoice['billing_period'],now()))
    return invoice_public(owned_invoice(invoice_id,request))




credit_api.install(app,session_user,api_key,rate)

realtime.install(app, display_claims, display_history)

product_delivery.install(app,session_user,api_user,api_key,rate,display_claims,display_history)

chart_watches.install(app,session_user,api_user,rate)
