"""Full PoolManager Initialize scan (all keys, any hooks) -> data/rh_v4_keys_full.json (+ progress). Public RPC, 10k-block windows."""
import sys,json,time;sys.path.insert(0,'harness');from rpc import *
from concurrent.futures import ThreadPoolExecutor
PM='0x8366a39cc670b4001a1121b8f6a443a643e40951'; V4I='0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438'
head=int(call('rh','eth_blockNumber',[]),16); start=9070
W=[(s,min(s+9999,head)) for s in range(start,head+1,10000)]
def one(w):
  for t in range(10):
    try: return call('rh','eth_getLogs',[{'address':PM,'fromBlock':hex(w[0]),'toBlock':hex(w[1]),'topics':[V4I]}],retries=1)
    except Exception as e: time.sleep(0.5*(t+1))
  return None
keys=[];miss=[];t=time.time()
with ThreadPoolExecutor(6) as ex:
  for i,(w,r) in enumerate(zip(W,ex.map(one,W))):
    if r is None: miss.append(w); continue
    for l in r:
      d=l['data'][2:]; wd=lambda j:int(d[64*j:64*j+64],16)
      keys.append(dict(id=l['topics'][1],currency0='0x'+l['topics'][2][-40:],currency1='0x'+l['topics'][3][-40:],fee=wd(0),tickSpacing=wd(1)-(1<<256 if wd(1)>>255 else 0),hooks='0x'+d[64*2+24:64*3],block=int(l['blockNumber'],16)))
    if i%200==0: print(i,len(W),len(keys),len(miss),round(time.time()-t),flush=True)
json.dump(dict(head=head,missing_windows=miss,keys=keys),open('data/rh_v4_keys_scan.json','w'))
print('done',len(keys),'missing windows',len(miss))
