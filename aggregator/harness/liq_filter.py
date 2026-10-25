"""Liquidity filter over full scans: keep zero-hook pools touching a hub (WETH/ETH, USDG, USDC) with in-range hub-side virtual reserve >= threshold.
Writes data/universe_liq.json {v3:[...], v4:[...]}."""
import sys,json,time;sys.path.insert(0,'harness');from rpc import *;from rpc import _w
from concurrent.futures import ThreadPoolExecutor
W='0x0bd7d308f8e1639fab988df18a8011f41eacad73';E0='0x'+'0'*40;G='0x5fc5360d0400a0fd4f2af552add042d716f1d168';C='0x80e0e24718dbfcad49ecaa6f1e6c89a190586ca8'
HUB={W:2*10**17,E0:2*10**17,G:500*10**6,C:1*10**6}   # ~ $500 on hub side (ETH ~2.3k)
SV='0xf3334192d15450cdd385c8b70e03f9a6bd9e673b'
v3=[p for p in json.load(open('data/rh_v3_pools_scan.json'))['pools'] if p['token0'] in HUB or p['token1'] in HUB]
v4=[k for k in json.load(open('data/rh_v4_keys_scan.json'))['keys'] if int(k['hooks'],16)==0 and (k['currency0'] in HUB or k['currency1'] in HUB)]
print('candidates v3',len(v3),'v4',len(v4),flush=True)
B=hex(int(call('rh','eth_blockNumber',[]),16)-5)
def chunk(calls): 
  for t in range(8):
    try: return mc('rh',calls,B)
    except Exception as e: time.sleep(1+t)
  return [(False,'0x')]*len(calls)
def run(calls,n=400):
  cs=[calls[i:i+n] for i in range(0,len(calls),n)]; out=[]
  with ThreadPoolExecutor(6) as ex:
    for i,r in enumerate(ex.map(chunk,cs)):
      out+=r
      if i%100==0: print(i,len(cs),flush=True)
  return out
# v3: slot0 + liquidity
r3=run([c for p in v3 for c in ((p['pool'],'0x3850c7bd'),(p['pool'],'0x1a686502'))])
r4=run([c for k in v4 for c in ((SV,'0xc815641c'+k['id'][2:]),(SV,'0xfa6793d5'+k['id'][2:]))])
def keep(t0,t1,s0,l):
  if not(s0[0] and l[0]) or len(s0[1])<66: return False
  sq=int(s0[1][2:66],16); L=int(l[1][2:66],16)
  if sq==0 or L==0: return False
  ok=False
  if t0 in HUB: ok|= L*(1<<96)//sq >= HUB[t0]
  if t1 in HUB: ok|= L*sq//(1<<96) >= HUB[t1]
  return ok
K3=[p for i,p in enumerate(v3) if keep(p['token0'],p['token1'],r3[2*i],r3[2*i+1])]
K4=[k for i,k in enumerate(v4) if keep(k['currency0'],k['currency1'],r4[2*i],r4[2*i+1])]
json.dump(dict(block=int(B,16),v3=K3,v4=K4),open('data/universe_liq.json','w'))
from collections import Counter
print('kept v3',len(K3),Counter(p['venue'] for p in K3),'v4',len(K4))
