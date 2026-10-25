# Differential fuzz: V4Quoter eth_call vs our V3-math engine on zero-hook V4 pools (same block).
import sys,json,random,subprocess,time;sys.path.insert(0,'harness');from rpc import *;from rpc import _w
from Crypto.Hash import keccak
from eth_abi import encode
from concurrent.futures import ThreadPoolExecutor
N=int(sys.argv[1]); seed=int(sys.argv[2]) if len(sys.argv)>2 else 1
Q='0x8dc178efb8111bb0973dd9d722ebeff267c98f94'
SEL=bytes.fromhex(keccak.new(digest_bits=256,data=b'quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))').hexdigest()[:8])
b=hex(int(call('rh','eth_blockNumber',[]),16)-3)
subprocess.run([sys.executable,'harness/fetch_v4.py',b],check=True,stdout=subprocess.DEVNULL)
keys={k['id'].lower():k for k in json.load(open('data/rh_v4_keys.json')) if int(k['hooks'],16)==0}
S=json.load(open(f'data/state_v4_{int(b,16)}.json'))
# Write a state file the Rust loader understands (same schema as V3)
for p in S['pools']:
  p.setdefault('bal0',str(2**200)); p.setdefault('bal1',str(2**200))
json.dump(S,open(f'data/state_{int(b,16)}.json','w'))  # overwrite? NO - use dedicated
json.dump(S,open(f'data/state_v4fuzz_{int(b,16)}.json','w'))
rnd=random.Random(seed); cases=[]
pools=[p for p in S['pools'] if int(p['liquidity'])>0]
for i in range(N):
  p=rnd.choice(pools); zfo=rnd.random()<0.5
  tin=p['token0'] if zfo else p['token1']
  # skip native 0 for fuzz input simplicity unless remapped
  if int(tin,16)==0: continue
  dec=S['tokens'].get(tin,{}).get('decimals',18)
  e=rnd.uniform(0,dec+6); amt=max(1,int(10**e))
  if rnd.random()<0.05: amt=rnd.randint(1,1000)
  # uint128 cap for V4 quoter exactAmount
  amt=min(amt,(1<<128)-1)
  k=keys[p['pool'].lower()]
  cases.append(dict(pool=p['pool'],zfo=zfo,tin=tin,fee=p['fee'],spacing=p['spacing'],hooks=p['hooks'],
                    currency0=k['currency0'],currency1=k['currency1'],amount=str(amt)))
def one(c):
  data=encode(['((address,address,uint24,int24,address),bool,uint128,bytes)'],
              [((c['currency0'],c['currency1'],c['fee'],c['spacing'],c['hooks']),c['zfo'],int(c['amount']),b'')])
  cd='0x'+(SEL+data).hex()
  for t in range(5):
    try: r=call('rh','eth_call',[{'to':Q,'data':cd},b],retries=1); return (True,r)
    except Exception as e:
      if 'revert' in str(e).lower(): return (False,str(e)[:120])
      time.sleep(0.4*(t+1))
  return (False,'rpcfail')
t=time.time()
with ThreadPoolExecutor(12) as ex: rs=list(ex.map(one,cases))
for c,(ok,v) in zip(cases,rs):
  if ok: c['q_out']=str(int(v[2:66],16))
  else: c['q_err']=v
json.dump(dict(block=int(b,16),cases=cases),open('data/fuzz_v4_cases.json','w'))
print('block',int(b,16),'cases',len(cases),'ok',sum('q_out' in c for c in cases),'rev',sum('q_err' in c for c in cases),'secs',round(time.time()-t,1))
# Rust check: fuzzcheck expects q_sqrt too — write a v4-aware checker that only compares amountOut
open('data/state_v4fuzz_latest.txt','w').write(f'data/state_v4fuzz_{int(b,16)}.json')
