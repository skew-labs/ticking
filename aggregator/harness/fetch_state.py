# Load full V3 pool state at a pinned block (all initialized ticks via full-range tickBitmap scan).
import sys,json,time;sys.path.insert(0,'harness');from rpc import *
from rpc import _w
from concurrent.futures import ThreadPoolExecutor
U=json.load(open('data/rh_universe.json'))
UNI='0x1f7d7550b1b028f7571e69a784071f0205fd2efa'
pools=[p['pool'] for p in U['pools'] if p['factory']==UNI]
block=sys.argv[1] if len(sys.argv)>1 else hex(int(call('rh','eth_blockNumber',[]),16)-5)
t0=time.time()
def s32(h): v=int(h,16); return v-(1<<256) if v>>255 else v
calls=[]
for p in pools:
  for sel in ('0x0dfe1681','0xd21220a7','0xddca3f43','0xd0c93a7c','0x3850c7bd','0x1a686502'): calls.append((p,sel))
r=mc('rh',calls,block)
P={}
for i,p in enumerate(pools):
  x=r[i*6:i*6+6]
  if not all(ok for ok,_ in x): continue
  s0=x[4][1]
  P[p]=dict(pool=p,token0='0x'+x[0][1][-40:],token1='0x'+x[1][1][-40:],fee=int(x[2][1],16),spacing=s32(x[3][1]),
            sqrtPriceX96=str(int(s0[2:66],16)),tick=s32(s0[66:130]),liquidity=str(int(x[5][1],16)),ticks=[])
br=mc('rh',sum([[(d['token0'],'0x70a08231'+_w(int(p,16))),(d['token1'],'0x70a08231'+_w(int(p,16)))] for p,d in P.items()],[]),block)
for i,(p,d) in enumerate(P.items()): d['bal0']=str(int(br[2*i][1],16)) if br[2*i][0] else '0'; d['bal1']=str(int(br[2*i+1][1],16)) if br[2*i+1][0] else '0'
# bitmap scan
wc=[];wk=[]
for p,d in P.items():
  sp=d['spacing']; lo=(-887272//sp)>>8; hi=(887272//sp)>>8
  for w in range(lo,hi+1):
    wc.append((p,'0x5339c296'+_w(w&((1<<256)-1)))); wk.append((p,w))
def run(ch): return mc('rh',ch,block)
CH=[wc[i:i+400] for i in range(0,len(wc),400)]
with ThreadPoolExecutor(8) as ex: res=sum(ex.map(run,CH),[])
tc=[];tk=[]
for (p,w),(ok,v) in zip(wk,res):
  assert ok,(p,w)
  bm=int(v,16)
  while bm:
    bit=(bm&-bm).bit_length()-1; bm&=bm-1
    t=((w<<8)+bit)*P[p]['spacing']; tc.append((p,'0xf30dba93'+_w(t&((1<<256)-1)))); tk.append((p,t))
CH=[tc[i:i+400] for i in range(0,len(tc),400)]
with ThreadPoolExecutor(8) as ex: res=sum(ex.map(run,CH),[])
for (p,t),(ok,v) in zip(tk,res):
  assert ok
  ln=s32(v[66:130])  # liquidityNet int128 (abi-encoded sign-extended)
  P[p]['ticks'].append([t,str(ln)])
# tokens
toks=sorted({d['token0'] for d in P.values()}|{d['token1'] for d in P.values()})
dr=mc('rh',[(t,'0x313ce567') for t in toks],block)
T={t:dict(decimals=int(v,16) if ok and len(v)>2 else 18,symbol=U['tokens'].get(t,U['tokens'].get(t.lower(),t[:8]))) for t,(ok,v) in zip(toks,dr)}
for t in T:
  for k,v in U['tokens'].items():
    if k.lower()==t: T[t]['symbol']=v
bn=int(block,16)
json.dump(dict(block=bn,pools=list(P.values()),tokens=T),open(f'data/state_{bn}.json','w'))
open('data/state_latest.txt','w').write(f'data/state_{bn}.json')
print('block',bn,'pools',len(P),'ticks',len(tk),'words',len(wc),'secs',round(time.time()-t0,1))
