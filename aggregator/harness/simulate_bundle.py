# Prove quote == fill: route with Rust engine at pinned block B, then eth_call the whole split as ONE SwapRouter02.multicall
# at the same block B, with stateOverride giving a throwaway address the input balance + allowance. No keys, no tx sent.
import sys,json,subprocess,time;sys.path.insert(0,'harness');from rpc import *;from rpc import _w
from Crypto.Hash import keccak
K=lambda b: keccak.new(digest_bits=256,data=b).digest()
R='0xcaf681a66d020601342297493863e78c959e5cb2'
FROM='0x'+'0'*36+'beef'  # throwaway address, never holds real funds
tin,tout,amts=sys.argv[1],sys.argv[2],sys.argv[3]; chunks=sys.argv[4] if len(sys.argv)>4 else '32'
b=hex(int(call('rh','eth_blockNumber',[]),16)-3)
subprocess.run([sys.executable,'harness/fetch_state.py',b],check=True,stdout=subprocess.DEVNULL)
st=f'data/state_{int(b,16)}.json'
gp=int(call('rh','eth_gasPrice',[]),16)
Q=json.loads(subprocess.run(['./core/target/release/quote',st,tin,tout,amts,chunks,str(gp),'120000','60000','5'],capture_output=True,text=True,check=True).stdout)
def slot_for(token,owner,spender=None):
  # find mapping base slot by brute force (solidity layout + OZ v5 namespaced ERC20)
  bases=[0,1,2,3,4,5,51,52,53,101,102]+list(range(6,120))+[int('52c63247e1f47db19d5ce0460030c497f067ca4cebf71ba98eeadabe20bace00',16)]
  val=_w(123456789123456789)
  for base in bases:
    if spender is None: sl='0x'+K(bytes.fromhex(_w(int(owner,16))+_w(base))).hex(); data='0x70a08231'+_w(int(owner,16))
    else: sl='0x'+K(bytes.fromhex(_w(int(spender,16)))+K(bytes.fromhex(_w(int(owner,16))+_w(base)))).hex(); data='0xdd62ed3e'+_w(int(owner,16))+_w(int(spender,16))
    try:
      r=call('rh','eth_call',[{'to':token,'data':data},b,{token:{'stateDiff':{sl:'0x'+val}}}],retries=2)
      if int(r,16)==123456789123456789: return sl
    except Exception: pass
  return None
def enc_path(hops):
  out=bytes.fromhex(hops[0]['tokenIn'][2:])
  for h in hops: out+=h['fee'].to_bytes(3,'big')+bytes.fromhex(h['tokenOut'][2:])
  return out
def enc_exactInput(path,recipient,amountIn,minOut):
  pb=path.hex().ljust(((len(path)+31)//32)*64,'0')
  return '0xb858183f'+_w(32)+_w(128)+_w(int(recipient,16))+_w(amountIn)+_w(minOut)+_w(len(path))+pb
def enc_multicall(calls):
  enc=[bytes.fromhex(c[2:]) for c in calls]; n=len(enc)
  parts=[];offs=[];o=32*n
  for e in enc:
    p=_w(len(e))+e.hex().ljust(((len(e)+31)//32)*64,'0'); parts.append(p); offs.append(o); o+=len(p)//2
  return '0xac9650d8'+_w(32)+_w(n)+''.join(_w(x) for x in offs)+''.join(parts)
token=Q['tokenIn']; bs=slot_for(token,FROM); al=slot_for(token,FROM,R)
rows=[]
for r in Q['results']:
  legs=r['legs']; amt=int(r['amountIn'])
  calls=[enc_exactInput(enc_path(l['hops']),FROM,int(l['amountIn']),0) for l in legs]
  ov={token:{'stateDiff':{bs:'0x'+_w(amt),al:'0x'+_w(2**256-1)}},FROM:{'balance':hex(10**18)}}
  try:
    t=time.time(); res=call('rh','eth_call',[{'from':FROM,'to':R,'data':enc_multicall(calls),'gas':hex(30_000_000)},b,ov],retries=2); dt=time.time()-t
    raw=bytes.fromhex(res[2:]); g=lambda i:int.from_bytes(raw[i:i+32],'big'); base=g(0); n=g(base)
    outs=[]
    for i in range(n):
      eo=base+32+g(base+32+32*i); outs.append(g(eo+32))
    gas=None
    try: gas=int(call('rh','eth_estimateGas',[{'from':FROM,'to':R,'data':enc_multicall(calls)},b,ov],retries=1),16)
    except Exception as e: gas=str(e)[:60]
    gross_engine=sum(outs)  # compare against engine exact per-leg sequence
    rows.append(dict(amountIn=r['amountInHuman'],legs=len(legs),sim_out=str(gross_engine),gas=gas,rtt_ms=round(dt*1000)))
  except Exception as e: rows.append(dict(amountIn=r['amountInHuman'],legs=len(legs),error=str(e)[:200]))
# engine gross (before gas) from a dedicated recompute: netOut + gas penalties
for r,row in zip(Q['results'],rows):
  hops=sum(len(l['hops']) for l in r['legs'])
  eng_gross=int(r['netOut'])+int(Q['gasPerHopOut'])*hops+int(Q['gasBaseOut'])
  row['engine_gross_out']=str(eng_gross)
  if 'sim_out' in row: row['quote_eq_fill']= (int(row['sim_out'])==eng_gross); row['diff_wei']=int(row['sim_out'])-eng_gross
print(json.dumps(dict(block=int(b,16),tokenIn=token,tokenOut=Q['tokenOut'],gasPrice=gp,balSlot=bs,allowSlot=al,rows=rows),indent=1))
