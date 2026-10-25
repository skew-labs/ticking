"""Find ERC20 balanceOf / allowance mapping slots via eth_call state override (solidity + vyper layouts)."""
import sys,json;sys.path.insert(0,'harness')
from rpc import call,_w
from Crypto.Hash import keccak
def k(b): return '0x'+keccak.new(digest_bits=256,data=b).hexdigest()
def bal_key(holder,slot,vy=False):
  h=bytes.fromhex(_w(int(holder,16))); s=bytes.fromhex(_w(slot))
  return k(s+h) if vy else k(h+s)
def allow_key(o,sp,slot,vy=False):
  return k(bytes.fromhex(_w(int(sp,16)))+bytes.fromhex(bal_key(o,slot,vy)[2:]))
CACHE='data/token_slots.json'
def find(token,block='latest'):
  try: c=json.load(open(CACHE))
  except: c={}
  if c.get(token.lower()): return c[token.lower()]
  h='0x00000000000000000000000000000000000beef1'; sp='0x00000000000000000000000000000000000beef2'
  V='0x'+_w(12345678901234567890)
  res={}
  OZ=0x52c63247e1f47db19d5ce0460030c497f067ca4cebf71ba98eeadabe20bace00  # OZ ERC20Upgradeable v5 ERC-7201 namespace
  if 'bal' not in res and 'oz_tried' not in res:
    try:
      if int(call('rh','eth_call',[{'to':token,'data':'0x70a08231'+_w(int(h,16))},block,{token:{'stateDiff':{bal_key(h,OZ):V}}}],retries=1),16)==12345678901234567890: res['bal']=(OZ,False)
    except Exception: pass
  if 'allow' not in res:
    try:
      if int(call('rh','eth_call',[{'to':token,'data':'0xdd62ed3e'+_w(int(h,16))+_w(int(sp,16))},block,{token:{'stateDiff':{allow_key(h,sp,OZ+1):V}}}],retries=1),16)==12345678901234567890: res['allow']=(OZ+1,False)
    except Exception: pass
  for vy in (False,True):
    if 'bal' in res: break
    for s in range(0,60):
      ov={token:{'stateDiff':{bal_key(h,s,vy):V}}}
      try: r=call('rh','eth_call',[{'to':token,'data':'0x70a08231'+_w(int(h,16))},block,ov],retries=1)
      except Exception: continue
      if int(r,16)==12345678901234567890: res['bal']=(s,vy); break
    if 'bal' in res: break
  # allowance: try slots
  for vy in (False,True):
    if 'allow' in res: break
    for s in range(0,60):
      ov={token:{'stateDiff':{allow_key(h,sp,s,vy):V}}}
      try: r=call('rh','eth_call',[{'to':token,'data':'0xdd62ed3e'+_w(int(h,16))+_w(int(sp,16))},block,ov],retries=1)
      except Exception: continue
      if int(r,16)==12345678901234567890: res['allow']=(s,vy); break
    if 'allow' in res: break
  c[token.lower()]=res; json.dump(c,open(CACHE,'w'),indent=1); return res
def override(token,holder,spender,amount):
  s=find(token); d={}
  if 'bal' in s: d[bal_key(holder,s['bal'][0],s['bal'][1])]='0x'+_w(amount)
  if 'allow' in s: d[allow_key(holder,spender,s['allow'][0],s['allow'][1])]='0x'+_w((1<<256)-1)
  return {token:{'stateDiff':d}}
if __name__=='__main__':
  for t in sys.argv[1:]: print(t,find(t))
