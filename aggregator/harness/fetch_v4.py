# Load zero-hook Uniswap V4 pool state at a pinned block via StateView; refuse non-zero hooks.
import sys,json,time;sys.path.insert(0,'harness');from rpc import *;from rpc import _w
from concurrent.futures import ThreadPoolExecutor
SV='0xf3334192d15450cdd385c8b70e03f9a6bd9e673b'
# selectors
SEL_SLOT0='0xc815641c'; SEL_LIQ='0xfa6793d5'; SEL_BMP='0x1c7ccb4c'; SEL_TICK='0x7c40f1fe'
keys=[k for k in json.load(open('data/rh_v4_keys.json')) if int(k['hooks'],16)==0]
block=sys.argv[1] if len(sys.argv)>1 else hex(int(call('rh','eth_blockNumber',[]),16)-5)
t0=time.time(); s32=lambda h: (lambda v: v-(1<<256) if v>>255 else v)(int(h,16))
# slot0 + liquidity
calls=[]
for k in keys:
  pid=k['id'][2:].rjust(64,'0')
  calls.append((SV,SEL_SLOT0+pid)); calls.append((SV,SEL_LIQ+pid))
r=mc('rh',calls,block)
P={}
for i,k in enumerate(keys):
  s0,lq=r[2*i],r[2*i+1]
  if not (s0[0] and lq[0]): continue
  sqrt=str(int(s0[1][2:66],16)); tick=s32(s0[1][66:130])
  protocolFee=int(s0[1][130:194],16) if len(s0[1])>=194 else 0
  lpFee=int(s0[1][194:258],16) if len(s0[1])>=258 else k['fee']
  # zero-hook static: PoolKey.fee == lpFee; we still record protocolFee (packed uint24)
  P[k['id']]=dict(pool=k['id'],venue='v4',token0=k['currency0'],token1=k['currency1'],fee=lpFee,spacing=k['tickSpacing'],
                  hooks=k['hooks'],protocolFee=protocolFee,sqrtPriceX96=sqrt,tick=tick,liquidity=str(int(lq[1],16)),
                  bal0='0',bal1='0',ticks=[],base=k.get('base'),quote=k.get('quote'))
# balances: ERC20 balanceOf(PoolManager) for each currency (native ETH = getBalance)
PM='0x8366a39cc670b4001a1121b8f6a443a643e40951'
bc=[]; bk=[]
for pid,d in P.items():
  for t,field in ((d['token0'],'bal0'),(d['token1'],'bal1')):
    bk.append((pid,field,t))
    if int(t,16)==0: bc.append(('__eth__',t))  # marker
    else: bc.append((t,'0x70a08231'+_w(int(PM,16))))
# resolve eth separately
eth_bal=int(call('rh','eth_getBalance',[PM,block]),16)
reqs=[(t,d) for t,d in bc if t!='__eth__']
br=mc('rh',reqs,block) if reqs else []
bi=0
for pid,field,t in bk:
  if int(t,16)==0: P[pid][field]=str(eth_bal)  # over-approx: whole PM ETH, not per-pool — V4 flash accounting. Cap is soft.
  else:
    ok,v=br[bi]; bi+=1; P[pid][field]=str(int(v,16) if ok else 0)
# NOTE: V4 singleton means token balances are shared across pools — per-pool bal cap is NOT exact.
# We still use liquidity-based math; bal cap disabled for v4 (set huge).
for d in P.values(): d['bal0']=str(2**200); d['bal1']=str(2**200)
# bitmap scan
wc=[];wk=[]
for pid,d in P.items():
  sp=d['spacing']; lo=(-887272//sp)>>8; hi=(887272//sp)>>8
  for w in range(lo,hi+1):
    wc.append((SV,SEL_BMP+pid[2:].rjust(64,'0')+_w(w&((1<<256)-1)))); wk.append((pid,w))
CH=[wc[i:i+400] for i in range(0,len(wc),400)]
with ThreadPoolExecutor(8) as ex: res=sum(ex.map(lambda ch: mc('rh',ch,block),CH),[])
tc=[];tk=[]
for (pid,w),(ok,v) in zip(wk,res):
  assert ok,(pid,w)
  bm=int(v,16)
  while bm:
    bit=(bm&-bm).bit_length()-1; bm&=bm-1
    t=((w<<8)+bit)*P[pid]['spacing']
    tc.append((SV,SEL_TICK+pid[2:].rjust(64,'0')+_w(t&((1<<256)-1)))); tk.append((pid,t))
CH=[tc[i:i+400] for i in range(0,len(tc),400)]
with ThreadPoolExecutor(8) as ex: res=sum(ex.map(lambda ch: mc('rh',ch,block),CH),[])
for (pid,t),(ok,v) in zip(tk,res):
  assert ok
  # getTickInfo: liquidityGross, liquidityNet, ...
  ln=s32(v[66:130])
  P[pid]['ticks'].append([t,str(ln)])
# tokens meta
toks=sorted({d['token0'] for d in P.values()}|{d['token1'] for d in P.values()})
U=json.load(open('data/rh_universe.json'))
T={}
for t in toks:
  if int(t,16)==0: T[t]=dict(decimals=18,symbol='ETH'); continue
  ok,v=mc('rh',[(t,'0x313ce567')],block)[0]
  T[t]=dict(decimals=int(v,16) if ok and len(v)>2 else 18, symbol=U['tokens'].get(t,U['tokens'].get(t.lower(),t[:8])))
bn=int(block,16)
json.dump(dict(block=bn,pools=list(P.values()),tokens=T),open(f'data/state_v4_{bn}.json','w'))
open('data/state_v4_latest.txt','w').write(f'data/state_v4_{bn}.json')
print('block',bn,'v4_zero_hook',len(P),'ticks',len(tk),'secs',round(time.time()-t0,1))
