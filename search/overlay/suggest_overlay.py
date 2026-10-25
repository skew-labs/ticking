"""Fail-open overlay for GET /api/public/em/search (landing/explorer suggest box).
Adds: ENS forward resolution (name.eth -> wallet), named people/institutions/chains from the search-guard alias
index, and did-you-mean for typos. Any error -> bridge response unchanged. Rollback: remove the nginx location.

2026-10-23: institutions expand ALL catalog wallets to #/investigate (not #/address / recursive #/search);
bridge #/address routes for the same address are rewritten to investigate so holdings/pixel open.
"""
import os,sys,json,re,asyncio
from urllib.parse import quote
REL=os.environ.get('GUARD_RELEASE','/srv/skew/tick-market-ui-20261007/.intelligence-releases/current-search-guard')
sys.path.insert(0,REL)
import httpx
from starlette.applications import Starlette
from starlette.responses import Response,JSONResponse
from starlette.routing import Route
import ens_resolve
BRIDGE=os.environ.get('EM_BRIDGE','http://127.0.0.1:8896')
b=httpx.AsyncClient(base_url=BRIDGE,timeout=10)
G=None
ADDR_RE=re.compile(r'0x[0-9a-f]{40}',re.I)
def guard():
 global G
 if G is None:
  os.chdir(REL);import search_guard as g;g.load();G=g
 return G
PASS=('origin','x-real-ip','user-agent','accept','accept-language','authorization','cookie')
def relay(up,content=None):
 h={k:v for k,v in up.headers.items() if k.lower() in ('cache-control','x-content-type-options','retry-after','access-control-allow-origin','vary')}
 return Response(up.content if content is None else content,up.status_code,headers=h,media_type=up.headers.get('content-type'))

def inv_route(addr,chain=None):
 a=addr.lower(); return '#/investigate/'+a+(('?chain='+chain) if chain else '')

def rewrite_address_routes(results):
 for r in results or []:
  route=r.get('route') or ''
  if '#/address/' in route:
   m=ADDR_RE.search(route) or ADDR_RE.search(str(r.get('id') or ''))
   if m:
    chain=r.get('chain')
    if not chain and 'chain=' in route:
     chain=re.search(r'chain=([a-z0-9-]+)',route)
     chain=chain.group(1) if chain else None
    r['route']=inv_route(m.group(0),chain)
 return results


# Reviewed liquid majors (exchange + Sol) so "BTC" opens workspace, not wrapped EVM first.
MAJOR_MARKETS=(
  ('BTC','Bitcoin','exchange','Global crypto · Binance spot'),
  ('ETH','Ethereum','exchange','Global crypto · Binance spot'),
  ('SOL','Solana','solana','Reviewed Solana market'),
  ('BNB','BNB','exchange','Global crypto · Binance spot'),
  ('XRP','XRP','exchange','Global crypto · Binance spot'),
  ('DOGE','Dogecoin','exchange','Global crypto · Binance spot'),
  ('ADA','Cardano','exchange','Global crypto · Binance spot'),
  ('AVAX','Avalanche','exchange','Global crypto · Binance spot'),
  ('LINK','Chainlink','exchange','Global crypto · Binance spot'),
  ('ARB','Arbitrum','arbitrum','Arbitrum One governance'),
)
def major_market_results(q):
  s=(q or '').strip()
  if not s or len(s)>32: return []
  n=s.casefold()
  out=[]
  for mid,name,chain,sub in MAJOR_MARKETS:
    sym=mid.casefold()
    if n==sym or (len(n)>=2 and sym.startswith(n)) or (len(n)>=3 and n in name.casefold()):
      score=2.5 if n==sym else (2.1 if sym.startswith(n) else 1.6)
      out.append({'type':'market','chain':chain,'id':mid,'title':mid,'subtitle':name+' · '+sub,
        'label':{'text':'Reviewed market','source':'reviewed catalog','verified':True},
        'route':'#/console/workspace/'+mid,'score':score})
  out.sort(key=lambda r:(-r['score'], r['title']))
  return out

def alias_results(q):
 g=guard();out=[];seen=set()
 n=g.repair(g.normalize(q))
 for a,bb,alias,dist in g.IDX.spans(n):
  for it in g.IDX.alias[alias]:
   key=it[:2]
   if key in seen:continue
   seen.add(key)
   if it[0]=='person':
    wallets=(getattr(g,'ENT_WALLETS',{}) or {}).get(it[1]) or []
    if wallets:
     for w in wallets[:12]:
      addr=w['address'].lower(); chain=w.get('chain')
      out.append({'type':'wallet','chain':chain,'id':(chain+':'+addr if chain else addr),'title':w.get('label') or it[2],'subtitle':(it[2]+' · ')+(w.get('label') or 'Attributed wallet')+' · holdings','label':{'text':w.get('label') or it[2],'category':'person','source':w.get('source') or 'entity-catalog','verified':w.get('grade') in ('official','strong')},'addresses':[addr],'route':inv_route(addr,chain),'score':1.2,'entityId':it[1]})
     # entity-level investigate hint (primary address)
     primary=(wallets[0].get('address') or '').lower()
     if primary:
      out.insert(0,{'type':'institution','chain':None,'id':it[1],'title':it[2],'subtitle':'Person · %d mapped wallets · aggregate holdings'%len(wallets),'route':inv_route(primary)+'?entity='+it[1],'score':1.25,'entityId':it[1],'addresses':[w['address'].lower() for w in wallets if w.get('address')]})
    elif it[3]:
     out.append({'type':'wallet','chain':None,'id':it[3],'title':it[2],'subtitle':'Named individual · public ENS-attributed wallet','route':inv_route(it[3]),'score':1.2})
   elif it[0]=='chain':
    out.append({'type':'chain','chain':it[1],'id':it[1],'title':it[2],'subtitle':'Chain overview','route':'#/chains?chain='+it[1],'score':0.9})
   elif it[0]=='entity':
    wallets=(getattr(g,'ENT_WALLETS',{}) or {}).get(it[1]) or []
    meta=(getattr(g,'ENT_META',{}) or {}).get(it[1]) or {}
    etype=meta.get('type') or 'institution'
    if wallets:
     for w in wallets[:12]:
      addr=w['address'].lower(); chain=w.get('chain')
      out.append({'type':'wallet','chain':chain,'id':(chain+':'+addr if chain else addr),'title':w.get('label') or it[2],'subtitle':(it[2]+' · ')+(w.get('label') or 'Verified labeled wallet')+' · holdings','label':{'text':w.get('label') or it[2],'category':etype,'source':w.get('source') or 'entity-catalog','verified':True},'addresses':[addr],'route':inv_route(addr,chain),'score':1.1})
    else:
     out.append({'type':'institution','chain':None,'id':it[1],'title':it[2],'subtitle':etype.title()+' (name indexed; verified wallet pending — only '+str(sum(1 for _ in (getattr(g,'ENT_WALLETS',{}) or {})))+' catalog entities currently carry wallets)','route':'#/search?q='+quote(it[2]+' wallet'),'score':0.8})
 return out,n

async def search(request):
 hdr={k:v for k,v in request.headers.items() if k.lower() in PASS}
 qs=request.url.query;q=request.query_params.get('q','')
 try:
  name=q.strip().lower()
  if ens_resolve.NAME.match(name):
   addr=await asyncio.to_thread(ens_resolve.resolve,name)
   if addr:
    params=dict(request.query_params);params['q']=addr
    up=await b.get('/api/public/em/search',params=params,headers=hdr)
    d=up.json() if up.status_code==200 else {'schema':'search/1','results':[]}
    head={'type':'wallet','chain':'ethereum','id':addr,'title':name,'subtitle':addr[:6]+'…'+addr[-4:]+' · ENS','ens':name,'route':inv_route(addr,'ethereum'),'score':2.0}
    d['results']=[head]+[r for r in d.get('results',[]) if r.get('id')!=addr];d['q']=q;d.setdefault('notes',[]).insert(0,'ENS: '+name+' resolves to '+addr+' (Ethereum mainnet, read now).')
    rewrite_address_routes(d['results'])
    return JSONResponse(d,headers={'Cache-Control':'no-store'})
 except Exception:pass
 up=await b.get('/api/public/em/search'+('?'+qs if qs else ''),headers=hdr)
 try:
  if up.status_code!=200 or not q.strip():return relay(up)
  d=up.json()
  if not isinstance(d,dict) or not isinstance(d.get('results'),list):return relay(up)
  rewrite_address_routes(d['results'])
  extra,norm=await asyncio.to_thread(alias_results,q)
  majors=await asyncio.to_thread(major_market_results,q)
  if not extra and not majors:
   return JSONResponse(d,headers={'Cache-Control':'no-store'}) if d.get('results') else relay(up)
  # Prefer overlay investigate hits: drop bridge rows for the same address
  def addr_of(r):
   m=ADDR_RE.search(str(r.get('id') or '')) or ADDR_RE.search(str(r.get('route') or ''))
   if m: return m.group(0).lower()
   for a in r.get('addresses') or []:
    if isinstance(a,str) and ADDR_RE.fullmatch(a): return a.lower()
   return None
  overlay_addrs={addr_of(r) for r in extra if addr_of(r)}
  kept=[r for r in d['results'] if addr_of(r) not in overlay_addrs]
  # Prefer exact reviewed majors (BTC/ETH/SOL…) over wrapped lookalikes (BTC.b)
  major_ids={r['id'] for r in majors}
  kept=[r for r in kept if r.get('id') not in major_ids and str(r.get('title') or '').upper() not in major_ids]
  ids={r.get('id') for r in kept}|major_ids
  d['results']=majors+[r for r in extra if r['id'] not in ids]+kept
  if norm.casefold()!=q.strip().casefold() and not d.get('didYouMean'):d['didYouMean']=norm
  return JSONResponse(d,headers={'Cache-Control':'no-store'})
 except Exception:return relay(up)
async def health(request):return JSONResponse({'ok':True,'layer':'em-search-overlay-v2-institutions'})
app=Starlette(routes=[Route('/api/public/em/search',search),Route('/healthz',health)])
