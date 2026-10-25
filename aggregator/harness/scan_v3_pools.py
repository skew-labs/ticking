"""Full PoolCreated scan for the V3-family factories -> data/rh_v3_pools_scan.json. Public RPC, 10k-block windows."""
import sys,json,time;sys.path.insert(0,'harness');from rpc import *
from concurrent.futures import ThreadPoolExecutor
FAC={'0x1f7d7550b1b028f7571e69a784071f0205fd2efa':'univ3','0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8':'ramses','0xe51960f1b45f1c9fb6d166e6a884f866fc70433b':'sushi','0x1ac9db4a2608ba45d6127b1737949b51bb54b7f3':'up','0xece6ecd61177336ea6fb9b17937ac439d85ee20b':'giga'}
PC='0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118'
head=int(call('rh','eth_blockNumber',[]),16)
W=[(s,min(s+9999,head)) for s in range(0,head+1,10000)]
def one(w):
  for t in range(10):
    try: return call('rh','eth_getLogs',[{'address':list(FAC),'fromBlock':hex(w[0]),'toBlock':hex(w[1]),'topics':[PC]}],retries=1)
    except Exception as e: time.sleep(0.5*(t+1))
  return None
pools=[];miss=[];t=time.time()
with ThreadPoolExecutor(6) as ex:
  for i,(w,r) in enumerate(zip(W,ex.map(one,W))):
    if r is None: miss.append(w); continue
    for l in r:
      d=l['data'][2:]
      pools.append(dict(pool='0x'+d[64+24:128],factory=l['address'].lower(),venue=FAC[l['address'].lower()],token0='0x'+l['topics'][1][-40:],token1='0x'+l['topics'][2][-40:],fee=int(l['topics'][3],16),block=int(l['blockNumber'],16)))
    if i%500==0: print(i,len(W),len(pools),len(miss),round(time.time()-t),flush=True)
json.dump(dict(head=head,missing_windows=miss,pools=pools),open('data/rh_v3_pools_scan.json','w'))
print('done',len(pools),'missing windows',len(miss))
