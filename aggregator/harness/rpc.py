import requests, json, time, os
RPCS={'rh':os.environ.get('RH_QN_HTTP') or 'https://rpc.mainnet.chain.robinhood.com','arb':'https://arb1.arbitrum.io/rpc'}  # endpoint from env (never logged)
S=requests.Session()
def call(chain,method,params,retries=6):
    for i in range(retries):
        try:
            r=S.post(RPCS[chain],json={'jsonrpc':'2.0','id':1,'method':method,'params':params},timeout=30).json()
            if 'error' in r:
                if i<retries-1 and ('rate' in str(r['error']).lower() or r['error'].get('code') in (-32005,429)): time.sleep(1+i); continue
                raise Exception(r['error'])
            return r['result']
        except requests.RequestException as e:
            time.sleep(1+i)
    raise Exception('rpc fail')
def batch(chain,reqs,chunk=50):
    out=[]
    for k in range(0,len(reqs),chunk):
        body=[{'jsonrpc':'2.0','id':k+i,'method':m,'params':p} for i,(m,p) in enumerate(reqs[k:k+chunk])]
        for t in range(8):
            try:
                r=S.post(RPCS[chain],json=body,timeout=60).json()
                if isinstance(r,dict): raise Exception(r)
                r=sorted(r,key=lambda x:x['id'])
                if any('error' in x for x in r) and any('rate' in str(x.get('error','')).lower() or 'limit' in str(x.get('error','')).lower() for x in r):
                    time.sleep(1+t); continue
                out+= [x.get('result') if 'result' in x else {'__err':x.get('error')} for x in r]; break
            except Exception as e:
                time.sleep(1+t)
        else: raise Exception('batch fail')
    return out
def ethcall(chain,to,data,block): return call(chain,'eth_call',[{'to':to,'data':data},block])

MCGAS=int(os.environ.get('MCGAS',0)) or 10**9
MC3='0xcA11bde05977b3631167028862bE2a173976CA11'
def _w(x): return hex(x)[2:].rjust(64,'0')
def mc(chain,calls,block='latest',chunk=400):
    """calls: list of (to, calldata_hex). returns list of (ok, return_hex) via Multicall3.aggregate3 at block."""
    out=[]
    for k in range(0,len(calls),chunk):
        cs=calls[k:k+chunk]; n=len(cs)
        heads=[];tails=b''
        enc=[]
        for to,data in cs:
            d=bytes.fromhex(data[2:])
            e=_w(int(to,16))+_w(1)+_w(96)+_w(len(d))+d.hex().ljust(((len(d)+31)//32)*64,'0')
            enc.append(e)
        offs=[];o=32*n
        for e in enc: offs.append(o); o+=len(e)//2
        body='0x82ad56cb'+_w(32)+_w(n)+''.join(_w(x) for x in offs)+''.join(enc)
        for t in range(8):
            try: r=call(chain,'eth_call',[{'to':MC3,'data':body,'gas':hex(MCGAS)},block]); break
            except Exception as ex:
                last=ex
                if 'not available' in str(ex): raise
                time.sleep(1+t)
        else: raise Exception('mc fail '+str(last)[:300])
        b=bytes.fromhex(r[2:])
        g=lambda i:int.from_bytes(b[i:i+32],'big')
        base=g(0); m=g(base); 
        for i in range(m):
            eo=base+32+g(base+32+32*i)
            ok=g(eo)==1; ro=eo+g(eo+32); ln=g(ro)
            out.append((ok,'0x'+b[ro+32:ro+32+ln].hex()))
    return out
