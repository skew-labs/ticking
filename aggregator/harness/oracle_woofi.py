"""WooFi (RH) tryQuery eth_call oracle. Public sPMM curve via on-chain tryQuery(from,to,amount)->amountOut.
Refuse opaque RFQ; this is the public WooRouter/WooPPV2 quoter surface.
"""
import sys;sys.path.insert(0,'harness')
from rpc import call, _w

WOO='0x5520385bfcf07ec87c4c53a7d8d65595dff69fa4'
# tryQuery(address fromToken, address toToken, uint256 fromAmount) -> uint256
SEL='ce824f19'

def quote_best(tin, tout, amt, block):
  tin,tout=tin.lower(),tout.lower(); amt=int(amt)
  data='0x'+SEL+_w(int(tin,16))+_w(int(tout,16))+_w(amt)
  try:
    r=call('rh','eth_call',[{'to':WOO,'data':data},block],retries=2)
    out=int(r[2:66],16)
    return dict(out=out if out>0 else 0, hits=[(WOO,out)])
  except Exception as e:
    return dict(out=0, err=str(e)[:80], hits=[])

if __name__=='__main__':
  import json
  print(json.dumps(quote_best(sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[1]),indent=1))
