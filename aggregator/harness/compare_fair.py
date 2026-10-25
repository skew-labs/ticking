"""FAIR same-block scoring vs Kyber (eth_call only, no txs).
Per pair: pin block b, rebuild exact local state at b, then for each amount:
  ours = max_k [ Local_net(amt-y_k) (Rust, gas-netted) + DP-optimal split of y_k over external on-chain venues (gas-netted) ]
         + backrun cyclic arb on post-state (Rust, gas-netted)
  proof = Exec eth_call bundle at b (local legs + backrun + fermi/tessera/hooked-v4 legs) -> actual tokenOut balance
  kyber = /routes + /route/build calldata, eth_call at SAME b with state-override funding -> actual output; net of estimateGas*gasPrice
Both sides gas-netted at real RH eth_gasPrice."""
import sys,json,subprocess,time;sys.path.insert(0,'harness')
from rpc import call,_w
from venues import venues,sample,fermi_exec_samples,add_fermi,TESS,WETH
from kyber_sim import route as kroute, build as kbuild, H as KH
from slots import override
from prove import run as prun, route_ops, approve, E, PM
from eth_abi import encode
W=WETH;U='0x5fc5360d0400a0fd4f2af552add042d716f1d168'
pairs=json.load(open('data/rh_dexscreener_pairs.json')); addr={}
for p in pairs: addr[p['base'].upper()]=p['ba'].lower(); addr[p['quote'].upper()]=p['qa'].lower()
STOCK=[('WETH',W,'USDG',U,[0.01,1,10,100],18),('USDG',U,'WETH',W,[100,10000,250000],6),('USDG',U,'NVDA',addr['NVDA'],[100,10000,100000],6),
 ('WETH',W,'SPY',addr['SPY'],[0.1,10,50],18),('USDG',U,'CRCL',addr['CRCL'],[1000,100000],6),('WETH',W,'TSLA',addr['TSLA'],[0.1,5],18),
 ('USDG',U,'SPCX',addr['SPCX'],[1000,100000],6),('WETH',W,'AAPL',addr['AAPL'],[0.1,5],18)]
if len(sys.argv)>1: STOCK=[s for s in STOCK if f'{s[0]}->{s[2]}' in sys.argv[1:]]
RFQ={'bebop','elfomofi','manta-prop','prism-prop','kipseli-prop','pmm-18','pmm-19','pmm-20','tidefi-prop'}
OPAQUE={'metric-propamm'}  # fermi/tessera proven unsigned on-chain -> now routable venues
N=32
KV={'v4h':2,'fermi':0,'tess':0}
def human(x,dec): s=f'{x/10**dec:.12f}'.rstrip('0').rstrip('.'); return s
def local(st,tin,tout,amts_h,nobr,gp):
  cmd=['./core/target/release/quote',st,tin,tout,','.join(amts_h),'32',str(gp),'120000','60000','1']+(['nobr'] if nobr else [])
  return json.loads(subprocess.check_output(cmd,text=True))
def ext_ops(v,tin,tout,x):
  if v['kind']=='fermi': return [approve(tin,v['id']),(0,v['id'],False,0,bytes.fromhex('2203d44a'+_w(int(v['zfo']))+_w(x)+_w(0)+_w(int(E,16))+_w(2**40)))]
  if v['kind']=='tess': return [approve(tin,TESS),(0,TESS,False,0,bytes.fromhex('3ae8b298')+encode(['address','address','int256','uint256','address','bytes'],[tin,tout,x,0,E,b'']))]
  if v['kind']=='v4h':
    k=[k for k in json.load(open('data/rh_v4_keys.json')) if k['id']==v['id']][0]
    if int(k['currency0'],16)==0: return None
    zfo=k['currency0'].lower()==tin
    return [(2,PM,zfo,x,encode(['(address,address,uint24,int24,address)'],[(k['currency0'],k['currency1'],k['fee'],k['tickSpacing'],k['hooks'])]))]
  return None  # pcs/woo: quoter-exact but not wired into Exec
rows=[]
for tin,tia,tout,toa,amts,din in STOCK:
  # discovery: harvest fermi pools Kyber has seen for this pair (public on-chain pools)
  for a in amts:
    try:
      d=kroute(tia,toa,int(round(a*10**din)))
      for rt in d['routeSummary']['route']:
        for x in rt:
          if x['exchange']=='fermi-prop': add_fermi(x['pool'])
    except Exception as e: print('harvest err',e)
  V=venues(tia,toa)
  for a_h in amts:
    t0=time.time(); amt=int(round(a_h*10**din))
    gp=int(call('rh','eth_gasPrice',[]),16)
    bn=int(call('rh','eth_blockNumber',[]),16); b=hex(bn)
    # Kyber fetched FIRST, same moment as the pin (removes our-compute lag from Kyber's side)
    kd=kb=None
    for _t in range(4):
      try: kd=kroute(tia,toa,amt); kb=kbuild(kd); break
      except Exception as e: time.sleep(1.5)
    kyber_lag_ms=round((time.time()-t0)*1000)
    subprocess.check_call([sys.executable,'harness/rebuild_all.py',b],stdout=subprocess.DEVNULL)
    st=open('data/state_all_latest.txt').read().strip(); S=json.load(open(st)); assert S['block']==bn
    ven={p['pool'].lower():p['venue'] for p in S['pools']}
    grid=[amt*k//N for k in range(N+1)]
    # local at amt - y_k
    Lq=local(st,tin,tout,[human(amt-y,din) for y in grid[:-1]],True,gp)
    gpo=int(Lq['gasPerHopOut'])/120000  # tokenOut per gas unit
    Lin=[int(r['amountIn']) for r in Lq['results']]+[0]; Lnet=[int(r['netOut']) for r in Lq['results']]+[0]
    yk=[amt-x for x in Lin]  # exact external budget per k
    # external curves on the y grid (k*amt/N); fermi by execution
    C=sample(V,grid,b)
    for i,v in enumerate(V):
      if v['kind']=='fermi': C[i]=fermi_exec_samples(v,tia,toa,grid,b)
    # DP knapsack over venues on grid steps (exact at grid points, no concavity assumption)
    NEG=-10**60; best=[0]+[NEG]*N; choice=[]
    for i,v in enumerate(V):
      g=int(v['gas']*gpo); nb=[NEG]*(N+1); ch=[0]*(N+1)
      for k in range(N+1):
        for j in range(k+1):
          val=best[k-j]+((C[i][j]-g) if j>0 and C[i][j]>0 else (0 if j==0 else NEG))
          if val>nb[k]: nb[k]=val; ch[k]=j
      best=nb; choice.append(ch)
    tot=[]
    for k in range(N+1):
      if best[k]<=NEG//2: continue
      # local leg must exactly cover amt - grid[k]; Lin[k] is local amountIn for that k (parse rounding) -> require match
      if Lin[k]!=amt-grid[k] and k!=N: continue
      tot.append((Lnet[k]+best[k],k))
    tv,kbest=max(tot)
    alloc={}; k=kbest
    for i in reversed(range(len(V))):
      j=choice[i][k]
      if j>0: alloc[i]=(grid[j],C[i][j])
      k-=j
    # final local with backrun
    lamt=amt-grid[kbest]; L=None; br=0; legs=[]; brl=[]
    if lamt>0:
      L=local(st,tin,tout,[human(lamt,din)],False,gp)['results'][0]; br=int(L['backrunNetOut']); legs=L['legs']; brl=L['backrunLegs']
    lnet=int(L['netOut']) if L else 0
    ext_gross=sum(o for _,o in alloc.values()); ext_net=sum(o-int(V[i]['gas']*gpo) for i,(_,o) in alloc.items())
    ours_net=lnet+br+ext_net
    # proof bundle
    ops=route_ops(legs,ven,brl) if legs else []
    proof=None; unproven=[]
    if ops is not None:
      for i,(x,o) in alloc.items():
        e=ext_ops(V[i],tia,toa,x)
        if e is None: unproven.append(V[i]['kind']); continue
        ops+=e
      p=prun(ops,tia,amt,toa,b); proof=p
    # kyber same block
    try:
      if kd is None: raise Exception('kyber api unavailable')
      tx={'from':KH,'to':kb['routerAddress'],'data':kb['data'],'gas':hex(30_000_000)}; ov=override(tia,KH,kb['routerAddress'],amt)
      krev=None; ksim_latest=None
      try: ksim=int(call('rh','eth_call',[tx,b,ov],retries=2)[2:66],16)
      except Exception as e: ksim=0; krev=str(e)[:160]
      try: ksim_latest=int(call('rh','eth_call',[tx,'latest',ov],retries=2)[2:66],16)
      except Exception as e: ksim_latest=0
      try: kgas=int(call('rh','eth_estimateGas',[tx,b,ov],retries=1),16)
      except Exception: kgas=int(kd['routeSummary'].get('gas',0) or 0)
      kq=int(kd['routeSummary']['amountOut']); kdex=sorted({x['exchange'] for rt in kd['routeSummary']['route'] for x in rt})
    except Exception as e:
      ksim=kq=kgas=0; kdex=['ERR '+str(e)[:60]]; krev='build err'; ksim_latest=None
    knet=ksim-int(kgas*gpo)
    bps=round((ours_net/knet-1)*1e4,2) if knet>0 else (10000.0 if ksim==0 else None)
    # old-style (gross vs quoted, no gas): ours gross approx = net + our gas
    our_gas_out=int(Lq['gasBaseOut'])+ (int(Lq['gasPerHopOut'])*sum(len(l['hops']) for l in legs)) + sum(int(V[i]['gas']*gpo) for i in alloc)
    ours_gross=ours_net+our_gas_out
    old_bps=round((ours_gross/kq-1)*1e4,2) if kq else None
    row=dict(pair=f'{tin}->{tout}',amountIn=a_h,block=bn,gasPrice=gp,ours_net=str(ours_net),ours_gross=str(ours_gross),local_net=str(lnet),local_amt=str(lamt),
      backrun_net=str(br),backrun_legs=len(brl),ext={V[i]['kind']+':'+V[i]['id'][:10]:dict(amt=str(x),out=str(o)) for i,(x,o) in alloc.items()},
      ext_net=str(ext_net),proof_balOut=(proof or {}).get('balOut'),proof_gas=(proof or {}).get('gas'),proof_err=(proof or {}).get('err'),proof_unproven=unproven,
      proof_expected_gross=str(int(L['netOut'])+int(Lq['gasPerHopOut'])*sum(len(l['hops']) for l in legs)+int(Lq['gasBaseOut'])+sum(int(l['out'])-int(l['amountIn']) for l in brl)+ext_gross if L else ext_gross),
      kyber_quoted=str(kq),kyber_sim=str(ksim),kyber_gas=kgas,kyber_revert=krev,kyber_sim_latest=str(ksim_latest),kyber_net=str(knet),kyber_dexes=kdex,
      kyber_rfq=bool(set(kdex)&RFQ),kyber_metric=bool(set(kdex)&OPAQUE),
      fair_bps=bps,kyber_lag_ms=kyber_lag_ms,kyber_api_ok=kd is not None,old_style_bps=old_bps,secs=round(time.time()-t0,1))
    rows.append(row)
    print(('WIN ' if bps is not None and bps>=0 else 'LOSS'),row['pair'],a_h,'fair',bps,'old',old_bps,'ext',list(row['ext']),'br',br,
          'proof',row['proof_balOut'],'exp',row['proof_expected_gross'],'kq',kq,'ksim',ksim,'kgas',kgas,kdex,flush=True)
out='data/compare_fair.json' if len(sys.argv)==1 else 'data/compare_fair_partial.json'
json.dump(rows,open(out,'w'),indent=1)
w=sum(1 for r in rows if r['kyber_api_ok'] and r['fair_bps'] is not None and r['fair_bps']>=0)
print(f'FAIR STOCK {w}/{len(rows)}')
