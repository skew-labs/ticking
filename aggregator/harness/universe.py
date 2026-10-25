# Build RH V3 pool universe: Uniswap V3 factory getPool over core tokens x fee tiers, plus DexScreener v3 forks.
import sys,json;sys.path.insert(0,'harness');from rpc import *
UNI='0x1f7d7550b1b028f7571e69a784071f0205fd2efa'
L=json.load(open('data/rh_dexscreener_pairs.json'))
tok={}
for x in L:
  for a,s in ((x['ba'],x['base']),(x['qa'],x['quote'])): tok[a.lower()]=s
# core: tokens appearing in pairs with liq>50k
core=set()
for x in L:
  if (x['liq'] or 0)>50000: core.add(x['ba'].lower()); core.add(x['qa'].lower())
core=[t for t in core if len(t)==42]
pad=lambda a:a[2:].rjust(64,'0')
reqs=[];keys=[]
cl=sorted(core)
for i in range(len(cl)):
  for j in range(i+1,len(cl)):
    for f in (100,500,3000,10000):
      reqs.append(('eth_call',[{'to':UNI,'data':'0x1698ee82'+pad(cl[i])+pad(cl[j])+hex(f)[2:].rjust(64,'0')},'latest']));keys.append((cl[i],cl[j],f))
res=[r if ok else None for ok,r in mc("rh",[(q[1][0]["to"],q[1][0]["data"]) for q in reqs])]
pools=set()
for k,r in zip(keys,res):
  if isinstance(r,str) and int(r,16)!=0: pools.add('0x'+r[-40:])
for x in L:
  if x['ver']=='v3' and len(x['pair'])==42: pools.add(x['pair'].lower())
pools=sorted(pools)
# factory per pool
fr=[r if ok else None for ok,r in mc("rh",[(p,"0xc45a0155") for p in pools])]
out=[{'pool':p,'factory':('0x'+f[-40:]) if isinstance(f,str) else None} for p,f in zip(pools,fr)]
from collections import Counter
print(len(core),'core tokens',len(out),'pools',Counter(o['factory'] for o in out))
json.dump({'tokens':tok,'core':cl,'pools':out},open('data/rh_universe.json','w'),indent=0)
