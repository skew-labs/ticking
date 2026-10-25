# For hooked V4 pools we refuse to model: still get EXACT single-hop quotes via V4Quoter eth_call at pinned block.
# Used as additional full-amount candidates (not shared-state-split). No hook logic locally.
import sys,json;sys.path.insert(0,'harness');from rpc import *;from rpc import _w
from Crypto.Hash import keccak
from eth_abi import encode
from concurrent.futures import ThreadPoolExecutor
Q='0x8dc178efb8111bb0973dd9d722ebeff267c98f94'
SEL=bytes.fromhex(keccak.new(digest_bits=256,data=b'quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))').hexdigest()[:8])
keys=[k for k in json.load(open('data/rh_v4_keys.json')) if int(k['hooks'],16)!=0 and (k.get('liq') or 0)>10000]
block=sys.argv[1]; tin=sys.argv[2].lower(); tout=sys.argv[3].lower(); amt=int(sys.argv[4])
WETH='0x0bd7d308f8e1639fab988df18a8011f41eacad73'
def rem(a): return WETH if int(a,16)==0 else a.lower()
cands=[]
for k in keys:
  c0,c1=rem(k['currency0']),rem(k['currency1'])
  if {c0,c1}!={tin,tout}: continue
  zfo = c0==tin
  data=encode(['((address,address,uint24,int24,address),bool,uint128,bytes)'],
              [((k['currency0'],k['currency1'],k['fee'],k['tickSpacing'],k['hooks']), zfo, min(amt,(1<<128)-1), b'')])
  cands.append((k, '0x'+(SEL+data).hex()))
def one(x):
  k,cd=x
  try: r=call('rh','eth_call',[{'to':Q,'data':cd},block],retries=2); return dict(id=k['id'],hooks=k['hooks'],fee=k['fee'],out=str(int(r[2:66],16)),base=k['base'],quote=k['quote'])
  except Exception as e: return dict(id=k['id'],err=str(e)[:80])
with ThreadPoolExecutor(8) as ex: out=list(ex.map(one,cands))
best=max((x for x in out if 'out' in x), key=lambda x:int(x['out']), default=None)
print(json.dumps(dict(candidates=len(cands),best=best,all=out)))
