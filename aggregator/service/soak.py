"""Soak + correctness gates against the live engine (eth_call only).
Every loop: fast meme quote + best stock quote (HTTP RTT + engine compute_us), lag.
Every PROOF_EVERY s: quote==fill proof: execute the returned legs in Exec (state-override) at the quote's block; must equal grossOut to the wei.
Every FUZZ_EVERY s: 0-wei single-pool fuzz: random pool/direction/amount via /pool_quote vs Exec execution at same block."""
import sys,json,time,random,urllib.request,urllib.error,os;sys.path.insert(0,'harness')
from prove import run as prun, route_ops
from rpc import call
E=os.environ.get('ENGINE','http://127.0.0.1:8782'); DUR=int(sys.argv[1]) if len(sys.argv)>1 else 1500
W='0x0bd7d308f8e1639fab988df18a8011f41eacad73';U='0x5fc5360d0400a0fd4f2af552add042d716f1d168'
pairs=json.load(open('data/rh_dexscreener_pairs.json')); A={}
for p in pairs: A[p['base'].upper()]=p['ba'].lower(); A[p['quote'].upper()]=p['qa'].lower()
C='0x80e0e24718dbfcad49ecaa6f1e6c89a190586ca8'  # canonical-bridge USDC (L2 StandardGateway mapping of L1 USDC)
STOCK=[(W,U,[10**16,10**18,10**19]),(U,W,[10**8,10**10]),(U,A['NVDA'],[10**8,10**10]),(A['NVDA'],U,[10**17,10**18]),(W,A['SPY'],[10**17,10**19]),(A['SPY'],U,[10**17,10**18]),
  (U,A['SPY'],[10**8,10**9]),(U,A['CRCL'],[10**9]),(A['CRCL'],U,[10**18]),(W,A['TSLA'],[10**17]),(A['TSLA'],W,[10**17]),(W,A['AAPL'],[10**17]),(U,A['GME'],[10**8]),(A['GME'],U,[10**18]),
  (C,U,[10**5,10**6]),(U,C,[10**5,10**6]),(C,W,[10**6]),(W,C,[10**14]),(C,A['NVDA'],[10**6]),(A['NVDA'],C,[10**16])]
MEME=[(W,A['WALLET'],[10**17,10**19])]
def get(path):
  t=time.perf_counter(); r=urllib.request.urlopen(E+path,timeout=30); b=r.read(); return json.loads(b),(time.perf_counter()-t)*1e6
def gq(tin,tout,amt,mode): return get(f'/quote?tin={tin}&tout={tout}&amt={amt}&mode={mode}')
R=dict(fast_rtt=[],fast_cmp=[],best_rtt=[],best_cmp=[],lag=[],proof_ok=0,proof_fail=[],fuzz_ok=0,fuzz_fail=[],proof_skip=0,qerr=0)
def prove_quote(q):
  ops=route_ops(q['legs'],{h['pool']:h['venue'] for l in q['legs'] for h in l['hops']})
  if ops is None: R['proof_skip']+=1; return
  p=prun(ops,q['tokenIn'],int(q['amountIn']),q['tokenOut'],hex(q['block']))
  if p.get('err') and ('unsupported block' in p['err'] or 'historical' in p['err']): time.sleep(1.5); p=prun(ops,q['tokenIn'],int(q['amountIn']),q['tokenOut'],hex(q['block']))
  if p.get('err') and ('unsupported block' in p['err'] or 'historical' in p['err']): R['proof_skip']+=1; return
  if p.get('ok') and p['balOut']==int(q['grossOut']): R['proof_ok']+=1; R.setdefault('proof_pairs',{}).setdefault(f"{q['tokenIn'][:8]}>{q['tokenOut'][:8]}",0); R['proof_pairs'][f"{q['tokenIn'][:8]}>{q['tokenOut'][:8]}"]+=1
  else: R['proof_fail'].append(dict(block=q['block'],tin=q['tokenIn'],tout=q['tokenOut'],amt=q['amountIn'],exp=q['grossOut'],got=p.get('balOut'),err=p.get('err'),legs=q['legs']))
def fuzz_one():
  st=json.load(open('data/state_all_latest.txt.json')) if False else None
  q,_=get('/freshness'); pools=list(q.keys())
  random.shuffle(pools)
  for a in pools[:1]:
    for zfo in (True,False):
      amt=random.choice([1,10**random.randint(3,8),10**random.randint(9,15),10**random.randint(16,21)])
      try: pq,_=get(f'/pool_quote?pool={a}&zfo={int(zfo)}&amt={amt}')
      except Exception as e: continue
      if int(pq['used'])!=amt or int(pq['out'])==0: continue  # partial fill / dust: not executable as exact-in
      venue=pq['venue']
      tin_tout=fuzz_tokens(a)
      if not tin_tout: continue
      t0,t1=tin_tout; tin,tout=(t0,t1) if zfo else (t1,t0)
      if tin==U and amt>=10**20: R['fuzz_excl_usdg_cap']=R.get('fuzz_excl_usdg_cap',0)+1; continue  # USDG token reverts transfers >=1e20 raw (0x356680b7)
      ops=route_ops([dict(amountIn=str(amt),hops=[dict(pool=a,zfo=zfo)])],{a:venue})
      if ops is None: continue
      p=prun(ops,tin,amt,tout,hex(pq['block']))
      if p.get('err') and 'unsupported block' in p['err']: time.sleep(1.5); p=prun(ops,tin,amt,tout,hex(pq['block']))
      if p.get('ok') and p['balOut']==int(pq['out']): R['fuzz_ok']+=1
      elif p.get('err') and ('historical' in p['err'] or 'unsupported block' in p['err']): R.setdefault('fuzz_rpc_skip',0); R['fuzz_rpc_skip']+=1
      else: R['fuzz_fail'].append(dict(pool=a,venue=venue,zfo=zfo,amt=amt,exp=pq['out'],got=p.get('balOut'),err=p.get('err'),block=pq['block']))
TOK={}
def fuzz_tokens(a):
  if a in TOK: return TOK[a]
  st=json.load(open(open('data/state_all_latest.txt').read().strip()))
  for p in st['pools']: TOK[p['pool'].lower()]=(p['token0'].lower() if int(p['token0'],16) else W, p['token1'].lower())
  return TOK.get(a)
t_end=time.time()+DUR; last_p=last_f=0; i=0; t_start=time.time()
while time.time()<t_end:
  i+=1
  try:
    for tin,tout,amts in MEME:
      q,rtt=gq(tin,tout,random.choice(amts),'fast'); R['fast_rtt'].append(rtt); R['fast_cmp'].append(q['compute_us']); R['lag'].append(q['lagBlocks'])
    tin,tout,amts=random.choice(STOCK)
    try: q,rtt=gq(tin,tout,random.choice(amts),'best')
    except urllib.error.HTTPError as he:
      k=f'{tin[:8]}>{tout[:8]}'; R.setdefault('pair_err',{}).setdefault(k,{}); m=json.loads(he.read() or b'{}').get('error','http'+str(he.code)); R['pair_err'][k][m]=R['pair_err'][k].get(m,0)+1; continue
    R.setdefault('pairs',{}).setdefault(f'{tin[:8]}>{tout[:8]}',[0,0]); R['pairs'][f'{tin[:8]}>{tout[:8]}'][0]+=1; R['best_rtt'].append(rtt); R['best_cmp'].append(q['compute_us']); R['lag'].append(q['lagBlocks'])
    if time.time()-last_p>float(os.environ.get('PROOF_EVERY','5')): last_p=time.time(); prove_quote(q)
    if time.time()-last_f>float(os.environ.get('FUZZ_EVERY','3')): last_f=time.time(); fuzz_one()
  except Exception as e: R['qerr']+=1; time.sleep(0.2)
  time.sleep(0.02)
  if i%50==0:
    def pc(v,p): s=sorted(v); return s[int((len(s)-1)*p)] if s else None
    print(json.dumps(dict(t=round(time.time()-t_start),n=i,lag_p50=pc(R['lag'],.5),lag_p99=pc(R['lag'],.99),lag_max=max(R['lag'] or [0]),fast_cmp_p50=pc(R['fast_cmp'],.5),fast_cmp_p99=pc(R['fast_cmp'],.99),
      fast_rtt_p99=round(pc(R['fast_rtt'],.99) or 0),best_cmp_p50=pc(R['best_cmp'],.5),best_cmp_p99=pc(R['best_cmp'],.99),proof_ok=R['proof_ok'],proof_fail=len(R['proof_fail']),proof_skip=R['proof_skip'],
      fuzz_ok=R['fuzz_ok'],fuzz_fail=len(R['fuzz_fail']),qerr=R['qerr'])),flush=True)
    json.dump(R,open('data/live/soak.json','w'))
json.dump(R,open('data/live/soak.json','w')); print('done')
