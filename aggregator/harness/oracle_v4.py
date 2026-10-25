"""Exact V4Quoter eth_call oracle for every known PoolKey (zero-hook + hooked). Full-amount single hop only."""
import json,sys;sys.path.insert(0,'harness')
from rpc import call
from eth_abi import encode
from Crypto.Hash import keccak
from concurrent.futures import ThreadPoolExecutor

VQ='0x8dc178efb8111bb0973dd9d722ebeff267c98f94'
SEL=bytes.fromhex(keccak.new(digest_bits=256,data=b'quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))').hexdigest()[:8])
WETH='0x0bd7d308f8e1639fab988df18a8011f41eacad73'

def _norm(a):
  a=a.lower()
  return WETH if int(a,16)==0 else a

def quote_best(tin, tout, amt, block, keys_path='data/rh_v4_keys.json'):
  tin,tout=tin.lower(),tout.lower(); amt=int(amt)
  keys=json.load(open(keys_path))
  cands=[]
  for k in keys:
    c0,c1=k['currency0'].lower(),k['currency1'].lower()
    n0,n1=_norm(c0),_norm(c1)
    if {n0,n1}!={tin,tout}: continue
    zfo = n0==tin
    # PoolKey keeps native 0x0 if that's how it was initialized
    data=encode(['((address,address,uint24,int24,address),bool,uint128,bytes)'],
                [((k['currency0'],k['currency1'],k['fee'],k['tickSpacing'],k['hooks']), zfo, min(amt,(1<<128)-1), b'')])
    cands.append((k,'0x'+(SEL+data).hex()))
  def one(x):
    k,cd=x
    try:
      r=call('rh','eth_call',[{'to':VQ,'data':cd},block],retries=2)
      return dict(out=int(r[2:66],16),id=k['id'],hooks=k['hooks'],fee=k['fee'],spacing=k['tickSpacing'],zero_hook=int(k['hooks'],16)==0)
    except Exception as e:
      return dict(err=str(e)[:80],id=k['id'],hooks=k['hooks'])
  if not cands: return dict(out=0,n=0,best=None)
  with ThreadPoolExecutor(min(16,len(cands))) as ex:
    res=list(ex.map(one,cands))
  ok=[x for x in res if 'out' in x and x['out']>0]
  best=max(ok, key=lambda x:x['out']) if ok else None
  return dict(out=best['out'] if best else 0, n=len(cands), ok=len(ok), best=best, all=res)

if __name__=='__main__':
  # cli: oracle_v4.py <blockHex> <tokenIn> <tokenOut> <amount>
  print(json.dumps(quote_best(sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[1]),indent=1))
