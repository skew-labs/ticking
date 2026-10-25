"""Stock/RWA/USDG/WETH win hunt vs Kyber. Memes reported separately for SPEED only.
Best-of: local split router (UniV3+zero-hook V4+forks) OR V4Quoter OR V3 QuoterV2
         OR Pancake Infinity CLQuoter OR WooFi tryQuery.
No RFQ. Labels RFQ-blocked losses honestly. bush-v3-reclamm = Balancer V3 reCLAMM
(public vault curve exists) but not wired; only appears on already-won / RFQ-mixed rows.
"""
import sys,json,subprocess,time,requests;sys.path.insert(0,'harness')
from rpc import call
from oracle_v4 import quote_best as quote_v4
from oracle_v3 import quote_best as quote_v3
from oracle_pancake import quote_best as quote_pcs
from oracle_woofi import quote_best as quote_woo

A='0x000000000000000000000000000000000000bEEF'
W='0x0bd7d308f8e1639fab988df18a8011f41eacad73';U='0x5fc5360d0400a0fd4f2af552add042d716f1d168'
pairs=json.load(open('data/rh_dexscreener_pairs.json'))
addr={}
for p in pairs: addr[p['base'].upper()]=p['ba'].lower(); addr[p['quote'].upper()]=p['qa'].lower()

STOCK=[
 ('WETH',W,'USDG',U,[0.01,1,10,100],18,6),
 ('USDG',U,'WETH',W,[100,10000,250000],6,18),
 ('USDG',U,'NVDA',addr['NVDA'],[100,10000,100000],6,18),
 ('WETH',W,'SPY',addr['SPY'],[0.1,10,50],18,18),
 ('USDG',U,'CRCL',addr['CRCL'],[1000,100000],6,18),
 ('WETH',W,'TSLA',addr['TSLA'],[0.1,5],18,18),
 ('USDG',U,'SPCX',addr['SPCX'],[1000,100000],6,18),
 ('WETH',W,'AAPL',addr['AAPL'],[0.1,5],18,18),
]
MEME=[('WETH',W,'WALLET',addr['WALLET'],[0.1,10],18,18)]
RFQ={'bebop','elfomofi','manta-prop','prism-prop','kipseli-prop','pmm-18','pmm-19','pmm-20','tidefi-prop'}
OPAQUE={'metric-propamm','fermi-prop','tessera'}

def kyber(ti,to,amt):
  t=time.time()
  r=requests.get('https://aggregator-api.kyberswap.com/robinhood/api/v1/routes',params=dict(tokenIn=ti,tokenOut=to,amountIn=str(amt)),headers={'x-client-id':'ticking-research'},timeout=30).json()
  s=r['data']['routeSummary']
  dexes=sorted({x['exchange'] for rt in s['route'] for x in rt})
  return dict(out=int(s['amountOut']),ms=round((time.time()-t)*1000),dexes=dexes,hops=[x['exchange'] for rt in s['route'] for x in rt])

def lifi(ti,to,amt):
  try:
    t=time.time(); r=requests.get('https://li.quest/v1/quote',params=dict(fromChain=4663,toChain=4663,fromToken=ti,toToken=to,fromAmount=str(amt),fromAddress=A),timeout=30).json()
    return dict(out=int(r['estimate']['toAmount']),ms=round((time.time()-t)*1000))
  except Exception as e: return dict(err=str(e)[:60])

def to_wei(x,dec):
  return int(round(float(x)*10**dec))

# rebuild once
b=hex(int(call('rh','eth_blockNumber',[]),16)-5)
subprocess.check_call([sys.executable,'harness/rebuild_all.py',b])
subprocess.check_call([sys.executable,'harness/fetch_v4.py',b])
st=open('data/state_all_latest.txt').read().strip()
bn=json.load(open(st))['block']
block=hex(bn)

def run_cases(cases, meme=False):
  rows=[]
  for tin,tia,tout,toa,amts,din,dout in cases:
    cmd=['./core/target/release/quote',st,tin,tout,','.join(map(str,amts)),('4' if meme else '32'),'20000000','120000','60000',('200' if meme else '20')]+(['fast'] if meme else [])
    Q=json.loads(subprocess.check_output(cmd,text=True))
    for r,amt_h in zip(Q['results'],amts):
      amt=to_wei(amt_h,din)
      hops=sum(len(l['hops']) for l in r['legs'])
      local_gross=int(r['netOut'])+int(Q['gasPerHopOut'])*hops+int(Q['gasBaseOut'])
      t0=time.time()
      ora4=quote_v4(tia,toa,amt,block)
      ora3=quote_v3(tia,toa,amt,block)
      orap=quote_pcs(tia,toa,amt,block)
      oraw=quote_woo(tia,toa,amt,block)
      ora_ms=round((time.time()-t0)*1000)
      cands=[('local',local_gross),('v4_oracle',ora4['out']),('v3_quoter',ora3['out']),
             ('pancake_cl',orap['out']),('woofi',oraw['out'])]
      src,best=max(cands, key=lambda x:x[1])
      ora_out=max(ora4['out'], ora3['out'], orap['out'], oraw['out'])
      ora={'out':ora_out,'n':ora4.get('n',0)+len(ora3.get('hits',[]))+orap.get('n',0)+len(oraw.get('hits',[])),
           'best':ora4.get('best'),'v3':ora3,'v4':ora4,'pcs':orap,'woo':oraw}
      k=kyber(tia,toa,amt); lf=lifi(tia,toa,amt)
      bps=round((best/k['out']-1)*1e4,2) if k['out'] else None
      lf_bps=round((best/lf['out']-1)*1e4,2) if lf.get('out') else None
      uses_rfq=bool(set(k['dexes'])&RFQ); uses_opaque=bool(set(k['dexes'])&OPAQUE)
      only_pool=bool(k['dexes']) and not uses_rfq and not uses_opaque
      row=dict(pair=f'{tin}->{tout}',amountIn=amt_h,block=bn,local_out=str(local_gross),oracle_out=str(ora['out']),
               pancake_out=str(orap['out']),woofi_out=str(oraw['out']),
               best_out=str(best),best_src=src,legs=len(r['legs']),local_p50_us=r['latency_us']['p50'],local_p99_us=r['latency_us']['p99'],
               oracle_ms=ora_ms,oracle_n=ora['n'],oracle_best=ora.get('best'),
               kyber_out=str(k['out']),kyber_dexes=k['dexes'],kyber_ms=k['ms'],ticking_vs_kyber_bps=bps,
               lifi_out=str(lf.get('out')),ticking_vs_lifi_bps=lf_bps,
               kyber_uses_rfq=uses_rfq,kyber_uses_opaque=uses_opaque,kyber_pool_dex_only=only_pool,
               rfq_blocked=(bps is not None and bps<0 and (uses_rfq or uses_opaque) and not only_pool),
               meme=meme)
      rows.append(row)
      tag='WIN' if bps is not None and bps>=0 else 'LOSS'
      print(tag,row['pair'],amt_h,'bps',bps,'src',src,'local',local_gross,'ora',ora_out,
            'pcs',orap['out'],'woo',oraw['out'],'kyber',k['out'],
            'dex',k['dexes'],'rfq' if uses_rfq else '','opaque' if uses_opaque else '','p50us',r['latency_us']['p50'])
      time.sleep(0.2)
  return rows

print('=== STOCK ===')
stock=run_cases(STOCK, meme=False)
print('=== MEME (speed) ===')
meme=run_cases(MEME, meme=True)
wins=sum(1 for r in stock if (r['ticking_vs_kyber_bps'] or -1)>=0)
losses=[r for r in stock if (r['ticking_vs_kyber_bps'] or 1)<0]
pool_only=[r for r in losses if r['kyber_pool_dex_only']]
rfq_b=[r for r in losses if r['rfq_blocked']]
print(f'STOCK {wins}/{len(stock)} wins; losses {len(losses)}; RFQ/opaque-blocked {len(rfq_b)}; pool-only losses {len(pool_only)}')
for r in pool_only:
  print(' POOL_LOSS',r['pair'],r['amountIn'],'bps',r['ticking_vs_kyber_bps'],'dex',r['kyber_dexes'])
for r in rfq_b:
  print(' RFQ_LOSS',r['pair'],r['amountIn'],'bps',r['ticking_vs_kyber_bps'],'dex',r['kyber_dexes'])
json.dump(dict(stock=stock,meme=meme,summary=dict(stock_wins=wins,stock_n=len(stock),
  pool_only_losses=len(pool_only),rfq_blocked=len(rfq_b))),open('data/compare_stock.json','w'),indent=1)
