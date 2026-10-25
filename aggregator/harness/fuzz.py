# Differential fuzz: on-chain QuoterV2 (eth_call pinned) vs Rust exact math, same block.
import sys,json,random,subprocess,time;sys.path.insert(0,'harness');from rpc import *;from rpc import _w
N=int(sys.argv[1]); seed=int(sys.argv[2]) if len(sys.argv)>2 else 1
Q='0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7'
b=hex(int(call('rh','eth_blockNumber',[]),16)-3)
subprocess.run([sys.executable,'harness/fetch_state.py',b],check=True)
S=json.load(open(f'data/state_{int(b,16)}.json'))
rnd=random.Random(seed); cases=[]
pools=[p for p in S['pools'] if int(p['liquidity'])>0 or rnd.random()<0.1]
for i in range(N):
  p=rnd.choice(pools); zfo=rnd.random()<0.5
  tin=p['token0'] if zfo else p['token1']; tout=p['token1'] if zfo else p['token0']
  dec=S['tokens'][tin]['decimals']
  # log-uniform from 1 wei to 10^(dec+9) token units: covers dust, tick crossings, full drain / partial fills
  e=rnd.uniform(0,dec+9); amt=max(1,int(10**e))
  if rnd.random()<0.05: amt=rnd.randint(1,1000)
  cases.append(dict(pool=p['pool'],zfo=zfo,tin=tin,tout=tout,fee=p['fee'],amount=str(amt)))
calls=[(Q,'0xc6a5026a'+_w(int(c['tin'],16))+_w(int(c['tout'],16))+_w(int(c['amount']))+_w(c['fee'])+_w(0)) for c in cases]
from concurrent.futures import ThreadPoolExecutor
def one(cd):
  for t in range(6):
    try: return (True,call('rh','eth_call',[{'to':cd[0],'data':cd[1]},b],retries=1))
    except Exception as e:
      if 'revert' in str(e).lower() or 'gas' in str(e).lower(): return (False,str(e))
      time.sleep(0.5*(t+1))
  return (False,'rpcfail')
t=time.time()
with ThreadPoolExecutor(12) as ex: r=list(ex.map(one,calls))
for c,(ok,v) in zip(cases,r):
  if ok and len(v)>=2+64*4: c['q_out']=str(int(v[2:66],16)); c['q_sqrt']=str(int(v[66:130],16))
  else: c['q_err']=v[:80]
json.dump(dict(block=int(b,16),cases=cases),open('data/fuzz_cases.json','w'))
import collections;print(collections.Counter(c.get('q_err','ok')[:40] for c in cases).most_common(5));print('block',int(b,16),'quoted',N,'in',round(time.time()-t,1),'s; reverted', sum('q_err' in c for c in cases))
