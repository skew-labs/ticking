"""Ticking live state engine (RH). Keeps the exact pool state current block-by-block from logs.
- Head poll every POLL_MS; eth_getLogs over all tracked venues for (last, head].
- Exact incremental apply: V3-family Swap (sqrtP, L, tick from event), Mint/Burn (tick liquidityNet + active L),
  V4 Swap / ModifyLiquidity (by PoolId). Unknown events (Algebra/Alandale, etc.) -> pool marked DIRTY and
  published DISABLED (liquidity=0, so the router cannot use stale state) until the next full rebuild.
- Full rebuild (harness/rebuild_all.py) in a background thread: at start, when dirty pools exist (rate-limited),
  on gap > MAX_GAP blocks, or on reorg (parent-hash mismatch). After a rebuild at block B, buffered logs > B are replayed.
- Publishes atomically: data/live/state_<bn>.json + data/live/latest.txt (tmp+rename). qserver hot-reloads.
- Metrics: data/live/engine.prom + data/live/engine.json (lag, per-pool freshness, counters)."""
import sys,os,json,time,threading,subprocess,copy,logging,glob;sys.path.insert(0,'harness')
from rpc import call
from Crypto.Hash import keccak
def topic(s): return '0x'+keccak.new(digest_bits=256,data=s.encode()).hexdigest()
T_SWAP=topic('Swap(address,address,int256,int256,uint160,uint128,int24)')
T_MINT=topic('Mint(address,address,int24,int24,uint128,uint256,uint256)')
T_BURN=topic('Burn(address,int24,int24,uint128,uint256,uint256)')
T_V4SWAP=topic('Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)')
T_V4MOD=topic('ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)')
PM='0x8366a39cc670b4001a1121b8f6a443a643e40951'
POLL_MS=int(os.environ.get('ENGINE_POLL_MS',250)); MAX_GAP=int(os.environ.get('ENGINE_MAX_GAP',3000))
REBUILD_MIN_S=int(os.environ.get('ENGINE_REBUILD_MIN_S',120)); PUBLISH_MIN_MS=int(os.environ.get('ENGINE_PUBLISH_MIN_MS',200))
OUT='data/live'; os.makedirs(OUT,exist_ok=True)
logging.basicConfig(level=logging.INFO,format='%(message)s'); L=logging.getLogger('engine')
def log(**k): k['ts']=round(time.time(),3); k['svc']='engine'; L.info(json.dumps(k))
def s256(h): v=int(h,16); return v-(1<<256) if v>>255 else v
def words(d): d=d[2:]; return [d[i:i+64] for i in range(0,len(d),64)]
class Engine:
  def __init__(s):
    s.lock=threading.Lock(); s.state=None; s.idx={}; s.block=0; s.hashes={}; s.dirty=set(); s.fresh={}
    s.buf=[]; s.rebuilding=False; s.last_rebuild=0; s.c=dict(logs=0,swaps=0,liq=0,unknown=0,rebuilds=0,reorgs=0,gaps=0,publishes=0,errors=0)
    s.head=0; s.last_pub=0; s.pub_block=0
  def load(s,path,replay_from):
    st=json.load(open(path))
    idx={}; 
    for p in st['pools']:
      p['ticks']={int(t):int(n) for t,n in p['ticks']}; idx[p['pool'].lower()]=p
    with s.lock:
      s.state=st; s.idx=idx; s.dirty=set(); base=st['block']
      for lg in [x for x in s.buf if int(x['blockNumber'],16)>base]: s.apply(lg)
      s.block=max(base,s.block if s.block>base else base)
      for k in idx: s.fresh.setdefault(k,base)
    log(msg='loaded',block=base,pools=len(idx))
  def apply(s,lg):
    a=lg['address'].lower(); t=lg['topics']; bn=int(lg['blockNumber'],16); s.c['logs']+=1
    if a==PM:
      pid=t[1].lower(); p=s.idx.get(pid)
      if p is None: return
      w=words(lg['data'])
      if t[0]==T_V4SWAP:
        p['sqrtPriceX96']=str(int(w[2],16)); p['liquidity']=str(int(w[3],16)); p['tick']=s256(w[4]); s.c['swaps']+=1
      elif t[0]==T_V4MOD:
        lo,hi,dl=s256(w[0]),s256(w[1]),s256(w[2]); s._liq(p,lo,hi,dl)
      else: return
      s.fresh[pid]=bn; return
    p=s.idx.get(a)
    if p is None: return
    if p.get('venue')=='alandale': s.dirty.add(a); s.c['unknown']+=1; return
    w=words(lg['data'])
    if t[0]==T_SWAP:
      p['sqrtPriceX96']=str(int(w[2],16)); p['liquidity']=str(int(w[3],16)); p['tick']=s256(w[4]); s.c['swaps']+=1
      if p.get('bal0') is not None: p['bal0']=str(max(0,int(p['bal0'])+s256(w[0]))); p['bal1']=str(max(0,int(p['bal1'])+s256(w[1])))
    elif t[0]==T_MINT:
      s._liq(p,s256(t[2]),s256(t[3]),int(w[1],16))
      if p.get('bal0') is not None: p['bal0']=str(int(p['bal0'])+int(w[2],16)); p['bal1']=str(int(p['bal1'])+int(w[3],16))
    elif t[0]==T_BURN:
      s._liq(p,s256(t[2]),s256(t[3]),-int(w[0],16))
    elif t[0] in (topic('Collect(address,address,int24,int24,uint128,uint128)'),):
      pass
    else: s.dirty.add(a); s.c['unknown']+=1
    s.fresh[a]=bn
  def _liq(s,p,lo,hi,dl):
    tk=p['ticks']; tk[lo]=tk.get(lo,0)+dl; tk[hi]=tk.get(hi,0)-dl
    if tk[lo]==0: del tk[lo]
    if hi in tk and tk[hi]==0: del tk[hi]
    if lo<=p['tick']<hi: p['liquidity']=str(int(p['liquidity'])+dl)
    s.c['liq']+=1
  def publish(s,force=False):
    now=time.time()*1000
    if not force and now-s.last_pub<PUBLISH_MIN_MS: return
    with s.lock:
      if s.state is None or s.block==s.pub_block: return
      st=copy.copy(s.state); pools=[]
      for p in s.state['pools']:
        q=dict(p); q['ticks']=sorted([[k,str(v)] for k,v in p['ticks'].items()])
        if p['pool'].lower() in s.dirty: q['liquidity']='0'  # stale -> disabled
        pools.append(q)
      st['pools']=pools; st['block']=s.block; bn=s.block
    fn=f'{OUT}/state_{bn}.json'; tmp=fn+'.tmp'; json.dump(st,open(tmp,'w')); os.replace(tmp,fn)
    open(f'{OUT}/latest.txt.tmp','w').write(fn); os.replace(f'{OUT}/latest.txt.tmp',f'{OUT}/latest.txt')
    s.last_pub=now; s.pub_block=bn; s.c['publishes']+=1
    for old in sorted(glob.glob(f'{OUT}/state_*.json'),key=os.path.getmtime)[:-5]:
      try: os.remove(old)
      except OSError: pass
  def rebuild(s,reason):
    if s.rebuilding: return
    s.rebuilding=True
    def go():
      try:
        b=hex(s.head or int(call('rh','eth_blockNumber',[]),16)); t0=time.time()
        subprocess.check_call([sys.executable,'harness/rebuild_all.py',b],stdout=subprocess.DEVNULL)
        s.load(open('data/state_all_latest.txt').read().strip(),int(b,16)); s.c['rebuilds']+=1; s.last_rebuild=time.time()
        log(msg='rebuild',reason=reason,block=int(b,16),secs=round(time.time()-t0,1)); s.publish(force=True)
      except Exception as e: s.c['errors']+=1; log(msg='rebuild_failed',err=str(e)[:200])
      finally: s.rebuilding=False
    threading.Thread(target=go,daemon=True).start()
  def addrs(s): return [k for k in s.idx if k.startswith('0x') and len(k)==42]+[PM]
  def step(s):
    head=int(call('rh','eth_blockNumber',[]),16); s.head=head
    if s.state is None: return
    if s.block==0: s.block=s.state['block']
    if head-s.block>MAX_GAP: s.c['gaps']+=1; log(msg='gap',head=head,block=s.block); s.rebuild('gap'); return
    # reorg check on our last processed block
    if s.block in s.hashes:
      h=call('rh','eth_getBlockByNumber',[hex(s.block),False])['hash']
      if h!=s.hashes[s.block]: s.c['reorgs']+=1; log(msg='reorg',block=s.block); s.hashes.clear(); s.rebuild('reorg'); return
    if head<=s.block: return
    frm=s.block+1; to=min(head,frm+499)
    logs=call('rh','eth_getLogs',[{'fromBlock':hex(frm),'toBlock':hex(to),'address':s.addrs()}])
    with s.lock:
      for lg in sorted(logs,key=lambda x:(int(x['blockNumber'],16),int(x['logIndex'],16))):
        if lg.get('removed'): continue
        s.buf.append(lg); s.apply(lg)
      s.buf=[x for x in s.buf if int(x['blockNumber'],16)>to-5000]
      s.block=to
    s.hashes[to]=call('rh','eth_getBlockByNumber',[hex(to),False])['hash']
    for k in [k for k in s.hashes if k<to-64]: del s.hashes[k]
    s.publish()
    if s.dirty and time.time()-s.last_rebuild>REBUILD_MIN_S: s.rebuild('dirty')
  def metrics(s):
    lag=s.head-s.pub_block if s.head and s.pub_block else None
    ages=[s.head-v for v in s.fresh.values()] if s.head else []
    m=dict(head=s.head,state_block=s.block,published_block=s.pub_block,lag_blocks=lag,dirty_pools=len(s.dirty),pools=len(s.idx),rebuilding=s.rebuilding,**s.c,
           pool_last_event_age_blocks_p50=sorted(ages)[len(ages)//2] if ages else None)
    json.dump(dict(m,fresh=s.fresh,dirty=sorted(s.dirty)),open(f'{OUT}/engine.json.tmp','w')); os.replace(f'{OUT}/engine.json.tmp',f'{OUT}/engine.json')
    prom=''.join(f'ticking_engine_{k} {int(v)}\n' for k,v in m.items() if isinstance(v,(int,bool)) and v is not None)
    open(f'{OUT}/engine.prom.tmp','w').write(prom); os.replace(f'{OUT}/engine.prom.tmp',f'{OUT}/engine.prom')
def main():
  e=Engine(); log(msg='start')
  e.head=int(call('rh','eth_blockNumber',[]),16)
  # cold start: full rebuild synchronously
  b=hex(e.head); subprocess.check_call([sys.executable,'harness/rebuild_all.py',b],stdout=subprocess.DEVNULL)
  e.load(open('data/state_all_latest.txt').read().strip(),int(b,16)); e.last_rebuild=time.time(); e.publish(force=True)
  while True:
    t=time.time()
    try: e.step()
    except Exception as ex: e.c['errors']+=1; log(msg='step_error',err=str(ex)[:200])
    e.metrics()
    time.sleep(max(0,POLL_MS/1000-(time.time()-t)))
if __name__=='__main__': main()
