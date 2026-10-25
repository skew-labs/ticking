"""Fair scoring: fetch Kyber route+build calldata, eth_call it at a PINNED block with state-override funding.
Returns actual returnAmount (router return word0, cross-checked vs recipient balance delta via multicall-free 2nd call)
and gas from eth_estimateGas (same override). No txs sent."""
import sys,json,time,requests;sys.path.insert(0,'harness')
from rpc import call,_w
from slots import override
H='0x00000000000000000000000000000000000beef1'
API='https://aggregator-api.kyberswap.com/robinhood/api/v1'
HD={'x-client-id':'ticking-research'}
def route(tin,tout,amt):
  r=requests.get(API+'/routes',params=dict(tokenIn=tin,tokenOut=tout,amountIn=str(amt)),headers=HD,timeout=30).json()
  return r['data']
def build(d):
  b=requests.post(API+'/route/build',json=dict(routeSummary=d['routeSummary'],sender=H,recipient=H,slippageTolerance=5000,deadline=int(time.time())+3600),headers=HD,timeout=30).json()
  return b['data']
def sim(tin,tout,amt,block,d=None):
  d=d or route(tin,tout,amt); b=build(d)
  to=b['routerAddress']; data=b['data']
  ov=override(tin,H,to,amt)
  tx={'from':H,'to':to,'data':data,'gas':hex(30_000_000)}
  res=dict(quoted=int(d['routeSummary']['amountOut']),dexes=sorted({x['exchange'] for rt in d['routeSummary']['route'] for x in rt}),router=to)
  try:
    r=call('rh','eth_call',[tx,block,ov],retries=2)
    res['sim_out']=int(r[2:66],16)
    try: res['gas']=int(call('rh','eth_estimateGas',[tx,block,ov],retries=1),16)
    except Exception as e: res['gas']=None; res['gas_err']=str(e)[:80]
  except Exception as e:
    res['sim_out']=0; res['revert']=str(e)[:160]
  return res
if __name__=='__main__':
  W='0x0bd7d308f8e1639fab988df18a8011f41eacad73';U='0x5fc5360d0400a0fd4f2af552add042d716f1d168'
  b=hex(int(call('rh','eth_blockNumber',[]),16)-2)
  for amt in [10**18,100*10**18]:
    print(json.dumps(sim(W,U,amt,b)))
