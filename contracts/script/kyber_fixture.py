"""Fetch a Kyber route + calldata whose sender/recipient is the fork-test router address. Read-only HTTP; no txs."""
import json, sys, time, requests
API = 'https://aggregator-api.kyberswap.com/robinhood/api/v1'
HD = {'x-client-id': 'ticking-research'}
import os
ROUTER = os.environ.get('ROUTER_ADDR', '0x00000000000000000000000000000000071C4E15')
WETH, USDG = '0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73', '0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168'
amt = int(sys.argv[1]) if len(sys.argv) > 1 else 10**17
d = requests.get(API + '/routes', params=dict(tokenIn=WETH, tokenOut=USDG, amountIn=str(amt)), headers=HD, timeout=30).json()['data']
b = requests.post(API + '/route/build', json=dict(routeSummary=d['routeSummary'], sender=ROUTER, recipient=ROUTER,
                  slippageTolerance=5000, deadline=int(time.time()) + 3600), headers=HD, timeout=30).json()['data']
json.dump(dict(router=b['routerAddress'], data=b['data'], amountIn=str(amt), quoted=d['routeSummary']['amountOut']),
          open('test/fixtures/kyber.json', 'w'))
print('kyber fixture', b['routerAddress'], 'quoted', d['routeSummary']['amountOut'], 'bytes', (len(b['data']) - 2) // 2)
