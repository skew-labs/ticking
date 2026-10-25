#!/usr/bin/env python3
"""Rebuild UniV3 + V4(zero-hook) + Ramses/Sushi/Up/Giga merged state at a pinned block."""
import sys,json,subprocess,os
sys.path.insert(0,'harness')
from rpc import *; from rpc import _w
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
b=sys.argv[1] if len(sys.argv)>1 else hex(int(call('rh','eth_blockNumber',[]),16)-5)
subprocess.check_call([sys.executable,'harness/fetch_state.py',b])
subprocess.check_call([sys.executable,'harness/fetch_v4.py',b])
bn=int(b,16)
v3=json.load(open(f'data/state_{bn}.json')); v4=json.load(open(f'data/state_v4_{bn}.json'))
WETH='0x0bd7d308f8e1639fab988df18a8011f41eacad73'
for p in v4['pools']:
  if int(p['token0'],16)==0: p['token0']=WETH
  if int(p['token1'],16)==0: p['token1']=WETH
  p['venue']='v4'
for p in v3['pools']:
  p['venue']='univ3'; p.setdefault('protocolFee',0)
other=[]
for p in json.load(open('data/rh_dexscreener_pairs.json')):
  if p['dex'] in ('ramses','sushiswap','up','giga') and p['ver']=='v3': other.append(p['pair'].lower())
for r in json.load(open('data/kyber_edge_audit.json'))['rows']:
  for h in r['hops']:
    if h['ex'] in ('ramses-v3','sushiswap-v3','up-v3','giga-v3') and isinstance(h['pool'],str) and len(h['pool'])==42:
      other.append(h['pool'].lower())
other += [p.lower() for p in json.load(open('data/rh_other_v3_pools.json')).get('sushi',[])]
other=sorted(set(other))
KNOWN={'0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8':'ramses','0xe51960f1b45f1c9fb6d166e6a884f866fc70433b':'sushi','0x1ac9db4a2608ba45d6127b1737949b51bb54b7f3':'up','0xece6ecd61177336ea6fb9b17937ac439d85ee20b':'giga'}
meta=mc('rh',sum([[(p,'0xc45a0155'),(p,'0x3850c7bd'),(p,'0x1a686502'),(p,'0x0dfe1681'),(p,'0xd21220a7'),(p,'0xddca3f43'),(p,'0xd0c93a7c')] for p in other],[]),b)
pools_o=[]
for i,p in enumerate(other):
  fac,s0,liq,t0,t1,fee,sp = meta[i*7:(i+1)*7]
  if not all(x[0] for x in (fac,s0,liq,t0,t1,fee,sp)): continue
  f='0x'+fac[1][-40:]
  if f not in KNOWN or int(liq[1],16)==0: continue
  pools_o.append(dict(pool=p,venue=KNOWN[f],factory=f,token0='0x'+t0[1][-40:],token1='0x'+t1[1][-40:],
    fee=int(fee[1],16),protocolFee=0,spacing=(lambda v:v-(1<<256) if v>>255 else v)(int(sp[1],16)),
    sqrtPriceX96=str(int(s0[1][2:66],16)),tick=(lambda v:v-(1<<256) if v>>255 else v)(int(s0[1][66:130],16)),
    liquidity=str(int(liq[1],16)),ticks=[],bal0='0',bal1='0'))
s32=lambda h:(lambda v:v-(1<<256) if v>>255 else v)(int(h,16))
if pools_o:
  br=mc('rh',sum([[(d['token0'],'0x70a08231'+_w(int(d['pool'],16))),(d['token1'],'0x70a08231'+_w(int(d['pool'],16)))] for d in pools_o],[]),b)
  for i,d in enumerate(pools_o):
    d['bal0']=str(int(br[2*i][1],16) if br[2*i][0] else 0); d['bal1']=str(int(br[2*i+1][1],16) if br[2*i+1][0] else 0)
  wc=[];wk=[]
  for d in pools_o:
    sp=d['spacing']
    if sp<=0: continue
    lo=(-887272//sp)>>8; hi=(887272//sp)>>8
    for w in range(lo,hi+1):
      wc.append((d['pool'],'0x5339c296'+_w(w&((1<<256)-1)))); wk.append((d['pool'],w))
  with ThreadPoolExecutor(8) as ex: res=sum(ex.map(lambda ch: mc('rh',ch,b), [wc[i:i+400] for i in range(0,len(wc),400)]),[])
  by={d['pool']:d for d in pools_o}; tc=[];tk=[]
  for (p,w),(ok,v) in zip(wk,res):
    assert ok; bm=int(v,16)
    while bm:
      bit=(bm&-bm).bit_length()-1; bm&=bm-1
      t=((w<<8)+bit)*by[p]['spacing']; tc.append((p,'0xf30dba93'+_w(t&((1<<256)-1)))); tk.append((p,t))
  with ThreadPoolExecutor(8) as ex: res=sum(ex.map(lambda ch: mc('rh',ch,b), [tc[i:i+400] for i in range(0,len(tc),400)]),[])
  for (p,t),(ok,v) in zip(tk,res):
    assert ok; by[p]['ticks'].append([t,str(s32(v[66:130]))])

# Alandale Algebra CL pools
try:
  from Crypto.Hash import keccak as _K
  _core=[t.lower() for t in json.load(open('data/rh_universe.json'))['core']]
  _FAC='0x16494a80e08bcb9285d87b67149d7b01774d82f8'
  _SEL='0x'+_K.new(digest_bits=256,data=b'poolByPair(address,address)').hexdigest()[:8]
  _reqs=[(_FAC,_SEL+_w(int(_core[i],16))+_w(int(_core[j],16))) for i in range(len(_core)) for j in range(i+1,len(_core))]
  _res=mc('rh',_reqs,b)
  _apools=sorted({'0x'+r[-40:] for ok,r in _res if ok and int(r,16)!=0})
  _s32=lambda h:(lambda v:v-(1<<256) if v>>255 else v)(int(h,16))
  for pool in _apools:
    try:
      gs=ethcall('rh',pool,'0xe76c01e4',b)
      sqrt=str(int(gs[2:66],16)); tick=_s32(gs[66:130])
      fee=int(ethcall('rh',pool,'0xddca3f43',b),16)
      sp=int(ethcall('rh',pool,'0xd0c93a7c',b),16); sp=sp if sp<(1<<31) else sp-(1<<32)
      if sp<=0: continue
      liq=str(int(ethcall('rh',pool,'0x1a686502',b),16))
      if int(liq)==0: continue
      t0='0x'+ethcall('rh',pool,'0x0dfe1681',b)[-40:]; t1='0x'+ethcall('rh',pool,'0xd21220a7',b)[-40:]
      lo,hi=(-887272)>>8,(887272)>>8
      wc=[(pool,'0xc677e3e0'+_w(w&((1<<256)-1))) for w in range(lo,hi+1)]; wk=list(range(lo,hi+1))
      with ThreadPoolExecutor(8) as ex: rr=sum(ex.map(lambda ch: mc('rh',ch,b),[wc[i:i+400] for i in range(0,len(wc),400)]),[])
      tc=[];tk=[]
      for w,(ok,v) in zip(wk,rr):
        if not ok: continue
        bm=int(v,16)
        while bm:
          bit=(bm&-bm).bit_length()-1; bm&=bm-1; t=(w<<8)+bit
          if t%sp: continue
          tc.append((pool,'0xf30dba93'+_w(t&((1<<256)-1)))); tk.append(t)
      with ThreadPoolExecutor(8) as ex: rr=sum(ex.map(lambda ch: mc('rh',ch,b),[tc[i:i+400] for i in range(0,len(tc),400)]),[])
      ticks=[[t,str(_s32(v[66:130]))] for t,(ok,v) in zip(tk,rr) if ok]
      bal0=str(int(ethcall('rh',t0,'0x70a08231'+_w(int(pool,16)),b),16))
      bal1=str(int(ethcall('rh',t1,'0x70a08231'+_w(int(pool,16)),b),16))
      pools_o.append(dict(pool=pool,venue='alandale',token0=t0,token1=t1,fee=fee,protocolFee=0,spacing=sp,
        sqrtPriceX96=sqrt,tick=tick,liquidity=liq,ticks=ticks,bal0=bal0,bal1=bal1))
    except Exception as e: print('alandale skip',pool,e)
except Exception as e: print('alandale block failed',e)

toks={**v3['tokens'],**v4['tokens']}
for d in pools_o:
  for t in (d['token0'],d['token1']):
    if t not in toks:
      ok,v=mc('rh',[(t,'0x313ce567')],b)[0]
      toks[t]=dict(decimals=int(v,16) if ok and len(v)>2 else 18,symbol=t[:8])
merged=dict(block=bn,pools=v3['pools']+v4['pools']+pools_o,tokens=toks)
json.dump(merged,open(f'data/state_all_{bn}.json','w'))
open('data/state_all_latest.txt','w').write(f'data/state_all_{bn}.json')
print('merged',bn,len(merged['pools']),dict(Counter(p['venue'] for p in merged['pools'])))

import glob
for pat in ([] if os.environ.get('PRUNE_STATES')!='1' else None) or []: pass
for pat in [] if os.environ.get('PRUNE_STATES')!='1' else ['data/state_[0-9]*.json','data/state_v4_[0-9]*.json','data/state_all_[0-9]*.json']:
  fs=sorted(glob.glob(pat),key=os.path.getmtime)
  for f in fs[:-3]:
    if str(bn) not in f: os.remove(f)
