"""UniV3 QuoterV2 eth_call oracle — all fee tiers, full amount. Exact on-chain."""
import sys;sys.path.insert(0,'harness')
from rpc import call
from rpc import _w
QV='0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7'
FEES=(100,500,3000,10000)
def quote_best(tin,tout,amt,block):
  best=0; hits=[]
  for fee in FEES:
    d='0xc6a5026a'+_w(int(tin,16))+_w(int(tout,16))+_w(int(amt))+_w(fee)+_w(0)
    try:
      out=int(call('rh','eth_call',[{'to':QV,'data':d},block],retries=1)[2:66],16)
      hits.append((fee,out)); best=max(best,out)
    except Exception: pass
  return dict(out=best,hits=hits)
