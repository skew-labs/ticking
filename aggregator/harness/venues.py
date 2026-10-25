"""External on-chain venues DISJOINT from the local Rust state, each with an exact eth_call quote fn f(x):
 - hooked V4 pools (V4Quoter), Pancake Infinity CL (CLQuoter), WooFi (tryQuery),
 - fermi-prop pools: getAmountOut(bool,uint256) — verified executable unsigned via swapExactIn (Exec proof),
 - tessera: TesseraSwap.tesseraSwapViewAmounts(tin,tout,int256) — verified via tesseraSwapWithAllowances.
metric-propamm EXCLUDED: swap(address,bool,int128,uint128,bytes,bytes) takes opaque bytes; simulateSwapAndRevert
reverts BidGreaterThanAsk without maker-supplied data -> cannot prove unsigned execution.
Curves are sampled on a grid via Multicall3 (one RPC per venue chunk) at the pinned block."""
import sys,json,os;sys.path.insert(0,'harness')
from rpc import call,_w,mc
from eth_abi import encode
from Crypto.Hash import keccak
def sel(s): return keccak.new(digest_bits=256,data=s.encode()).hexdigest()[:8]
VQ='0x8dc178efb8111bb0973dd9d722ebeff267c98f94'; V4SEL=sel('quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))')
CLQ='0x6b3E15009681869FCF6AE2F3bBf6e33B2D0C590e'; CLSEL='9938b8ed'
WOO='0x5520385bfcf07ec87c4c53a7d8d65595dff69fa4'
TESS='0x55555522005BcAE1c2424D474BfD5ed477749E3e'
WETH='0x0bd7d308f8e1639fab988df18a8011f41eacad73'
FERMI_DB='data/fermi_pools.json'
BUSH_ROUTER='0xfdbb603653985df584eb2a4fb33fe66b5d41c88f'
BUSH=[dict(pool='0xc2fef42121fada751ba9f2107c4c8d4c593af435',tokens=['0x0bd7d308f8e1639fab988df18a8011f41eacad73','0x5fc5360d0400a0fd4f2af552add042d716f1d168'])]  # balancer-v3-reclamm (Kyber 'bush-v3-reclamm')
GAS={'bush':200000,'v4h':180000,'pcs':150000,'woo':160000,'fermi':110000,'tess':150000}
def fermi_pools():
  try: return json.load(open(FERMI_DB))
  except: return {}
def add_fermi(addr,block='latest'):
  db=fermi_pools(); a=addr.lower()
  if a in db: return
  t0=call('rh','eth_call',[{'to':a,'data':'0x0dfe1681'},block]); t1=call('rh','eth_call',[{'to':a,'data':'0xd21220a7'},block])
  db[a]=dict(token0='0x'+t0[-40:],token1='0x'+t1[-40:]); json.dump(db,open(FERMI_DB,'w'),indent=1)
def venues(tin,tout):
  tin,tout=tin.lower(),tout.lower(); V=[]
  n=lambda a: WETH if int(a,16)==0 else a.lower()
  for k in json.load(open('data/rh_v4_keys.json')):
    if int(k['hooks'],16)==0: continue  # zero-hook pools are in the local exact state
    if {n(k['currency0']),n(k['currency1'])}!={tin,tout}: continue
    zfo=n(k['currency0'])==tin
    V.append(dict(kind='v4h',id=k['id'],gas=GAS['v4h'],q=lambda x,k=k,zfo=zfo:(VQ,'0x'+V4SEL+encode(['((address,address,uint24,int24,address),bool,uint128,bytes)'],[((k['currency0'],k['currency1'],k['fee'],k['tickSpacing'],k['hooks']),zfo,x,b'')]).hex())))
  for k in json.load(open('data/rh_pancake_cl_keys.json')):
    if {k['currency0'].lower(),k['currency1'].lower()}!={tin,tout}: continue
    zfo=k['currency0'].lower()==tin
    V.append(dict(kind='pcs',id=k['id'],gas=GAS['pcs'],q=lambda x,k=k,zfo=zfo:(CLQ,'0x'+CLSEL+encode(['((address,address,address,address,uint24,bytes32),bool,uint128,bytes)'],[((k['currency0'],k['currency1'],k['hooks'],k['poolManager'],k['fee'],bytes.fromhex(k['parameters'][2:])),zfo,x,b'')]).hex())))
  V.append(dict(kind='woo',id=WOO,gas=GAS['woo'],q=lambda x:(WOO,'0xce824f19'+_w(int(tin,16))+_w(int(tout,16))+_w(x))))
  for a,p in fermi_pools().items():
    if {p['token0'],p['token1']}!={tin,tout}: continue
    zfo=p['token0']==tin
    V.append(dict(kind='fermi',id=a,zfo=zfo,gas=GAS['fermi'],q=lambda x,a=a,zfo=zfo:(a,'0x8290d9b8'+_w(int(zfo))+_w(x))))
  for bp in BUSH:
    if set(bp['tokens'])=={tin,tout}:
      V.append(dict(kind='bush',id=bp['pool'],gas=GAS['bush'],frm='0x0000000000000000000000000000000000000000',q=lambda x,bp=bp:(BUSH_ROUTER,'0xe3b5dff4'+encode(['(address,(address,address,bool)[],uint256,uint256)[]','address','bytes'],[[(tin,[(bp['pool'],tout,False)],x,0)],'0x0000000000000000000000000000000000000000',b'']).hex()),word=4))
  V.append(dict(kind='tess',id=TESS,gas=GAS['tess'],q=lambda x:(TESS,'0x77f65f98'+_w(int(tin,16))+_w(int(tout,16))+_w(x)),word=1))
  return V
def sample(V,grid,block):
  """returns curves[v] = list of outputs at grid amounts (0 if fail)."""
  calls=[];idx=[]
  for i,v in enumerate(V):
    for j,x in enumerate(grid): calls.append(v['q'](int(x))); idx.append((i,j))
  out=[[0]*len(grid) for _ in V]
  from rpc import batch
  rr=batch('rh',[('eth_call',[{'from':'0x0000000000000000000000000000000000000000','to':t,'data':d,'gas':hex(40_000_000)},block]) for t,d in calls],chunk=40)
  res=[(isinstance(r,str),r if isinstance(r,str) else '0x') for r in rr]
  for (i,j),(ok,r) in zip(idx,res):
    if ok and len(r)>=66:
      w=V[i].get('word',0); h=r[2+64*w:2+64*w+64]
      if h: out[i][j]=int(h,16)
  return out

# fermi view (getAmountOut) differs from execution by a few bps -> sample fermi by EXECUTING swapExactIn in Exec (exact).
def fermi_exec_samples(v,tin,tout,grid,block):
  from prove import E,EX,SEL,fund,approve
  from rpc import batch
  reqs=[]
  for x in grid:
    x=int(x)
    ops=[approve(tin,v['id']),(0,v['id'],False,0,bytes.fromhex('2203d44a'+_w(int(v['zfo']))+_w(x)+_w(0)+_w(int(E,16))+_w(2**40)))]
    d=SEL+encode(['(uint8,address,bool,uint256,bytes)[]','address'],[ops,tout]).hex()
    reqs.append(('eth_call',[{'from':'0x00000000000000000000000000000000000beef1','to':E,'data':d,'gas':hex(30_000_000)},block,{E:{'code':EX['runtime']},tin:{'stateDiff':fund(tin,max(x,1))}}]))
  out=[]
  for r in batch('rh',reqs,chunk=20):
    if isinstance(r,str) and len(r)>=194: out.append(int(r[66:130],16))
    else: out.append(0)
  return out
