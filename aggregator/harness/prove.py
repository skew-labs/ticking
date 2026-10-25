"""eth_call proof at a pinned block: deploy Exec via state override at E, fund E with tokenIn (storage override),
execute our exact route (+ backrun cycles, + external on-chain venues) and read tokenOut balance. No txs."""
import sys,json;sys.path.insert(0,'harness')
from rpc import call,_w
from slots import find,bal_key
from eth_abi import encode
from Crypto.Hash import keccak
E='0x00000000000000000000000000000000e7ec0001'
EX=json.load(open('sol/Exec.json'))
SEL='0x'+keccak.new(digest_bits=256,data=b'run((uint8,address,bool,uint256,bytes)[],address)').hexdigest()[:8]
def fund(token,amt):
  s=find(token); return {bal_key(E,s['bal'][0],s['bal'][1]):'0x'+_w(amt)}
def approve(token,sp): return (0,token,False,0,bytes.fromhex('095ea7b3'+_w(int(sp,16))+_w((1<<256)-1)))
def run(ops,tin,amt,tout,block):
  ov={E:{'code':EX['runtime']}, tin:{'stateDiff':fund(tin,amt)}}
  d=SEL+encode(['(uint8,address,bool,uint256,bytes)[]','address'],[ops,tout]).hex()
  tx={'from':'0x00000000000000000000000000000000000beef1','to':E,'data':d,'gas':hex(60_000_000)}
  try:
    r=call('rh','eth_call',[tx,block,ov],retries=2)
  except Exception as e: return dict(ok=False,err=str(e)[:200])
  from eth_abi import decode
  outs,bal,gas=decode(['uint256[]','uint256','uint256'],bytes.fromhex(r[2:]))
  return dict(ok=True,outs=list(outs),balOut=bal,gas=gas)
PM='0x8366a39cc670b4001a1121b8f6a443a643e40951'
_K={k['id'].lower():k for k in json.load(open('data/rh_v4_keys.json'))}
def route_ops(legs, venues, backrun=()):
  """legs: quote.rs legs. venues: pool->venue. returns ops or None if a hop isn't V3-callback-provable."""
  ops=[]
  for L in list(legs)+list(backrun):
    for j,h in enumerate(L['hops']):
      if venues.get(h['pool'].lower())=='v4':
        k=_K.get(h['pool'].lower())
        if not k or int(k['currency0'],16)==0: return None  # native-ETH key: needs wrap; not provable here
        kd=encode(['(address,address,uint24,int24,address)'],[(k['currency0'],k['currency1'],k['fee'],k['tickSpacing'],k['hooks'])])
        ops.append((2,PM,h['zfo'],int(L['amountIn']) if j==0 else 0,kd)); continue
      ops.append((1,h['pool'],h['zfo'],int(L['amountIn']) if j==0 else 0,b''))
  return ops
