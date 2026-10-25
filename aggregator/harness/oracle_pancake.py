"""Pancake Infinity CLQuoter eth_call oracle (RH). Differential vs on-chain CLQuoter.
PoolKey = (currency0, currency1, hooks, poolManager, fee, parameters); tickSpacing = params>>16.
"""
import json,sys;sys.path.insert(0,'harness')
from rpc import call
from eth_abi import encode
from Crypto.Hash import keccak
from concurrent.futures import ThreadPoolExecutor

CLQ='0x6b3E15009681869FCF6AE2F3bBf6e33B2D0C590e'
SEL=bytes.fromhex(keccak.new(digest_bits=256,
  data=b'quoteExactInputSingle(((address,address,address,address,uint24,bytes32),bool,uint128,bytes))').hexdigest()[:8])

def quote_best(tin, tout, amt, block, keys_path='data/rh_pancake_cl_keys.json'):
  tin,tout=tin.lower(),tout.lower(); amt=int(amt)
  keys=json.load(open(keys_path))
  cands=[]
  for k in keys:
    c0,c1=k['currency0'].lower(),k['currency1'].lower()
    if {c0,c1}!={tin,tout}: continue
    zfo = c0==tin
    params=bytes.fromhex(k['parameters'][2:])
    data=encode(['((address,address,address,address,uint24,bytes32),bool,uint128,bytes)'],
                [((k['currency0'],k['currency1'],k['hooks'],k['poolManager'],k['fee'],params),
                  zfo, min(amt,(1<<128)-1), b'')])
    cands.append((k,'0x'+(SEL+data).hex()))
  def one(x):
    k,cd=x
    try:
      r=call('rh','eth_call',[{'to':CLQ,'data':cd},block],retries=2)
      out=int(r[2:66],16); gas=int(r[66:130],16) if len(r)>=130 else None
      return dict(out=out,gas=gas,id=k['id'],fee=k['fee'],ts=k.get('tickSpacing'))
    except Exception as e:
      return dict(err=str(e)[:80],id=k['id'])
  if not cands: return dict(out=0,n=0,best=None)
  with ThreadPoolExecutor(min(8,len(cands))) as ex:
    res=list(ex.map(one,cands))
  ok=[x for x in res if x.get('out',0)>0]
  best=max(ok, key=lambda x:x['out']) if ok else None
  return dict(out=best['out'] if best else 0, n=len(cands), ok=len(ok), best=best, all=res)

if __name__=='__main__':
  print(json.dumps(quote_best(sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[1]),indent=1))
