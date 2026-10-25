"""Ticking assistant per-user conversation thread (BFF sidecar, reversible).

Sits in front of the intelligence resolver (127.0.0.1:19444). For each user
(cookie tk_thread, or header X-Ticking-Thread) keeps ONE persistent thread:
every turn, which capability answered (graph assist / AI search / graph loop /
Economic Machine / chart tools) and the canonical investigation context the
resolver returned. Next turn it feeds that context back as `previousContext`
so follow-ups continue the same subject across capabilities. Hot path is an
in-memory dict; sqlite writes run on a background thread. Resolver untouched.
"""
import json,os,queue,re,secrets,sqlite3,threading,time
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse,Response
from starlette.routing import Route

UPSTREAM=os.environ.get('TICKING_RESOLVER','http://127.0.0.1:19444')
DB=os.environ.get('TICKING_THREAD_DB','/srv/skew/ticking-context-layer/state/threads.sqlite3')
SID=re.compile(r'^[A-Za-z0-9_-]{16,64}$')
EXCLUSIVE={'analysis','research','walletResearch'}
client=httpx.AsyncClient(base_url=UPSTREAM,timeout=30)
mem={};lock=threading.Lock();q=queue.Queue()

def db():
    os.makedirs(os.path.dirname(DB),exist_ok=True)
    c=sqlite3.connect(DB,check_same_thread=False);c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS turns(thread TEXT,seq INTEGER,at REAL,query TEXT,capability TEXT,state TEXT,resolver TEXT,context TEXT,summary TEXT,ms REAL,PRIMARY KEY(thread,seq))')
    return c
def writer():
    c=db()
    while True:
        op=q.get()
        try:
            if op[0]=='add':c.execute('INSERT OR REPLACE INTO turns VALUES(?,?,?,?,?,?,?,?,?,?)',op[1])
            else:c.execute('DELETE FROM turns WHERE thread=?',(op[1],))
            c.commit()
        except Exception:pass
threading.Thread(target=writer,daemon=True).start()

def load(tid):
    with lock:
        if tid in mem:return mem[tid]
    c=db();rows=c.execute('SELECT seq,at,query,capability,state,resolver,context,summary,ms FROM turns WHERE thread=? ORDER BY seq DESC LIMIT 20',(tid,)).fetchall();c.close()
    turns=[{'seq':r[0],'at':r[1],'query':r[2],'capability':r[3],'state':r[4],'resolver':r[5],'context':json.loads(r[6]) if r[6] else None,'summary':r[7],'ms':r[8]} for r in reversed(rows)]
    t={'turns':turns,'context':next((x['context'] for x in reversed(turns) if x['context']),None),'seq':turns[-1]['seq'] if turns else 0}
    with lock:mem[tid]=t
    return t

def capability(body,res):
    st,rv=str(res.get('state','')),str(res.get('resolver',''))
    intent=str((((res.get('investigation') or {}).get('context') or {}).get('task') or {}).get('intent') or '')
    if intent=='graph':return 'graph-loop'
    if intent=='chart':return 'chart-tools'
    if 'analysis' in body or st=='analysis':return 'graph-assist'
    if 'research' in body or st=='asset_research':return 'ai-search'
    if st in ('chart_actions','chart_workflow'):return 'chart-tools'
    if st.startswith('graph') or 'graph' in json.dumps(res.get('card') or {})[:4000].lower():return 'graph-loop'
    if 'economic-machine' in rv or 'wallet' in st:return 'economic-machine'
    return st or 'other'

def compact(t):
    return {'turns':t['seq'],'context':t['context'],'recent':[{k:x[k] for k in ('seq','query','capability','state','summary')} for x in t['turns'][-6:]]}
def thread_id(request):
    h=request.headers.get('x-ticking-thread') or request.cookies.get('tk_thread') or ''
    return (h,False) if SID.match(h) else (secrets.token_urlsafe(24),True)
def finish(resp,tid,new):
    resp.headers['X-Ticking-Thread']=tid
    if new:resp.set_cookie('tk_thread',tid,max_age=31536000,httponly=True,secure=True,samesite='none',path='/api/public/intelligence')
    return resp

PASS_HDR=('origin','content-type','x-real-ip','authorization','cookie','user-agent','accept-language')
def up_headers(request):
    hdr={k:v for k,v in request.headers.items() if k.lower() in PASS_HDR}
    if 'x-real-ip' not in hdr and request.client:hdr['x-real-ip']=request.client.host
    return hdr
def relay(up,content=None):
    h={k:v for k,v in up.headers.items() if k.lower() in ('cache-control','x-content-type-options','x-ticking-service-request','retry-after')}
    return Response(up.content if content is None else content,up.status_code,headers=h,media_type=up.headers.get('content-type'))

async def resolve(request):
    raw=await request.body()
    try:return await threaded(request,raw)
    except Exception:
        # Fail-open: any layer error -> original request unchanged to the resolver.
        try:return relay(await client.post('/api/public/intelligence/resolve',content=raw,headers=up_headers(request)))
        except httpx.HTTPError:return JSONResponse({'error':'Assistant is temporarily unavailable.'},503)

async def threaded(request,raw):
    h=request.headers.get('x-ticking-thread') or request.cookies.get('tk_thread') or ''
    if not SID.match(h):
        # No thread: byte-for-byte passthrough; only offer a thread id for opt-in.
        r=relay(await client.post('/api/public/intelligence/resolve',content=raw,headers=up_headers(request)))
        return finish(r,secrets.token_urlsafe(24),True)
    tid=h;body=json.loads(raw)
    t=load(tid);sent=raw
    if isinstance(body,dict) and 'previousContext' not in body and not EXCLUSIVE&set(body) and t['context']:
        cand=json.dumps({**body,'previousContext':t['context']},separators=(',',':')).encode()
        if len(cand)<=2048:sent=cand
    started=time.perf_counter()
    up=await client.post('/api/public/intelligence/resolve',content=sent,headers=up_headers(request))
    ms=round((time.perf_counter()-started)*1000,1)
    try:res=up.json()
    except Exception:return finish(relay(up),tid,False)
    if isinstance(body,dict) and isinstance(res,dict) and isinstance(body.get('query'),str):
        ctx=(res.get('investigation') or {}).get('context')
        with lock:
            t['seq']+=1
            turn={'seq':t['seq'],'at':time.time(),'query':body['query'][:400],'capability':capability(body,res),'state':res.get('state') or ('error' if 'error' in res else None),'resolver':res.get('resolver'),'context':ctx,'summary':str(res.get('message') or res.get('error') or res.get('title') or '')[:300],'ms':ms}
            t['turns']=(t['turns']+[turn])[-20:]
            if ctx:t['context']=ctx
        q.put(('add',(tid,turn['seq'],turn['at'],turn['query'],turn['capability'],turn['state'],turn['resolver'],json.dumps(ctx) if ctx else None,turn['summary'],ms)))
        res['thread']=compact(t)
    return finish(relay(up,json.dumps(res,ensure_ascii=False,separators=(',',':')).encode()),tid,False)

async def thread(request):
    tid,new=thread_id(request)
    if request.method=='DELETE':
        with lock:mem[tid]={'turns':[],'context':None,'seq':0}
        q.put(('clear',tid));return finish(JSONResponse({'cleared':True}),tid,new)
    return finish(JSONResponse(compact(load(tid)),headers={'Cache-Control':'no-store'}),tid,new)

async def health(request):return JSONResponse({'ok':True,'layer':'ticking-thread-v1','upstream':UPSTREAM,'threadsInMemory':len(mem)})

app=Starlette(routes=[Route('/api/public/intelligence/resolve',resolve,methods=['POST']),Route('/api/public/intelligence/thread',thread,methods=['GET','DELETE']),Route('/healthz',health)])
