import requests,json,time
seen={}
for q in ['robinhood WETH','robinhood USDG','robinhood USDC','NVDAX','TSLAX','SPYX','QQQX','AAPLX','HOODX','robinhood meme','CRCLX','AMZNX','GOOGLX','METAX','MSTRX']:
  d=requests.get('https://api.dexscreener.com/latest/dex/search',params={'q':q},timeout=20).json()
  for p in d.get('pairs') or []:
    if p['chainId']!='robinhood':continue
    seen[p['pairAddress']]=dict(dex=p['dexId'],ver=(p.get('labels') or ['?'])[0],pair=p['pairAddress'],base=p['baseToken']['symbol'][:12],ba=p['baseToken']['address'],quote=p['quoteToken']['symbol'],qa=p['quoteToken']['address'],liq=(p.get('liquidity') or {}).get('usd',0),vol=(p.get('volume') or {}).get('h24',0))
  time.sleep(0.3)
L=sorted(seen.values(),key=lambda x:-(x['liq'] or 0))
json.dump(L,open('data/rh_dexscreener_pairs.json','w'),indent=0)
from collections import Counter
print(len(L),Counter((x['dex'],x['ver']) for x in L))
for x in L[:40]: print(x['dex'],x['ver'],x['base'],x['quote'],round(x['liq'] or 0),round(x['vol'] or 0),x['pair'][:12])
