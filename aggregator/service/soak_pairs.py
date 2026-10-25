import json
W='0x0bd7d308f8e1639fab988df18a8011f41eacad73';U='0x5fc5360d0400a0fd4f2af552add042d716f1d168'
A={}
for p in json.load(open('data/rh_dexscreener_pairs.json')): A[p['base'].upper()]=p['ba'].lower(); A[p['quote'].upper()]=p['qa'].lower()
STOCK=[(W,U,[10**16,10**18,10**19]),(U,W,[10**8,10**10]),(U,A['NVDA'],[10**8,10**10]),(W,A['SPY'],[10**17,10**19]),(U,A['CRCL'],[10**9]),(W,A['TSLA'],[10**17]),(W,A['AAPL'],[10**17])]
ST={t for r in STOCK for t in r[:2]}
S=json.load(open(open('data/state_all_latest.txt').read().strip()))
MEME_TOKENS=sorted({(p['token1'] if p['token0']==W else p['token0']) for p in S['pools'] if W in (p['token0'],p['token1']) and int(p.get('liquidity',0) or 0)>0} - ST - {U})
