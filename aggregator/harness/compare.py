# Same-moment comparison after UniV3+V4+forks: Ticking vs KyberSwap + LI.FI on RH (keyless).
import sys,json,subprocess,time,requests;sys.path.insert(0,'harness');from rpc import *
A='0x000000000000000000000000000000000000bEEF'
CASES=[('WETH','USDG',[0.01,1,10,100]),('USDG','WETH',[100,10000,250000]),('USDG','NVDA',[100,10000,100000]),('WETH','SPY',[0.1,10,50]),
       ('USDG','CRCL',[1000,100000]),('WETH','TSLA',[0.1,5]),('WETH','WALLET',[0.1,10]),('USDG','SPCX',[1000,100000]),('WETH','AAPL',[0.1,5])]
def kyber(ti,to,amt):
  try:
    t=time.time();r=requests.get('https://aggregator-api.kyberswap.com/robinhood/api/v1/routes',params=dict(tokenIn=ti,tokenOut=to,amountIn=str(amt)),headers={'x-client-id':'ticking-research'},timeout=30).json()
    s=r['data']['routeSummary']; return dict(out=int(s['amountOut']),ms=round((time.time()-t)*1000),dexes=sorted({x['exchange'] for rt in s['route'] for x in rt}))
  except Exception as e: return dict(err=str(e)[:80])
def lifi(ti,to,amt):
  try:
    t=time.time();r=requests.get('https://li.quest/v1/quote',params=dict(fromChain=4663,toChain=4663,fromToken=ti,toToken=to,fromAmount=str(amt),fromAddress=A),timeout=30).json()
    return dict(out=int(r['estimate']['toAmount']),ms=round((time.time()-t)*1000),tool=r.get('tool'))
  except Exception as e: return dict(err=str(e)[:80])
b=hex(int(call('rh','eth_blockNumber',[]),16)-5)
subprocess.check_call([sys.executable,'harness/rebuild_all.py',b])
st=open('data/state_all_latest.txt').read().strip()
rows=[]
for tin,tout,amts in CASES:
  Q=json.loads(subprocess.run(['./core/target/release/quote',st,tin,tout,','.join(map(str,amts)),'32','20000000','120000','60000','20'],capture_output=True,text=True,check=True).stdout)
  for r in Q['results']:
    hops=sum(len(l['hops']) for l in r['legs']); gross=int(r['netOut'])+int(Q['gasPerHopOut'])*hops+int(Q['gasBaseOut'])
    k=kyber(Q['tokenIn'],Q['tokenOut'],r['amountIn']); l=lifi(Q['tokenIn'],Q['tokenOut'],r['amountIn'])
    d=lambda x: round((gross/x['out']-1)*1e4,2) if 'out' in x and x['out']>0 else None
    venues=sorted({h.get('pool','')[:10] for leg in r['legs'] for h in leg['hops']})
    rows.append(dict(pair=f'{tin}->{tout}',amountIn=r['amountInHuman'],block=Q['block'],ticking_out=str(gross),legs=len(r['legs']),hops=hops,
      split_gain_bps_vs_best_single=round(r['splitGainBps'],2),ticking_p50_us=r['latency_us']['p50'],ticking_p99_us=r['latency_us']['p99'],
      kyber_out=str(k.get('out')),kyber_ms=k.get('ms'),kyber_dexes=k.get('dexes'),ticking_vs_kyber_bps=d(k),
      lifi_out=str(l.get('out')),lifi_ms=l.get('ms'),lifi_tool=l.get('tool'),ticking_vs_lifi_bps=d(l),errs=[k.get('err'),l.get('err')]))
    print(rows[-1]['pair'],rows[-1]['amountIn'],'legs',rows[-1]['legs'],'vsKyber',rows[-1]['ticking_vs_kyber_bps'],'vsLiFi',rows[-1]['ticking_vs_lifi_bps'],'p50us',rows[-1]['ticking_p50_us'],k.get('dexes'))
    time.sleep(0.25)
json.dump(rows,open('data/compare_v2.json','w'),indent=1)
wins=sum(1 for r in rows if (r['ticking_vs_kyber_bps'] or -1)>0)
print('vs Kyber wins',wins,'/',len(rows),'losses',sum(1 for r in rows if (r['ticking_vs_kyber_bps'] or 1)<0))
