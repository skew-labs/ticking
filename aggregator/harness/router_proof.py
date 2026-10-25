"""Read-only proof against the DEPLOYED TickingRouter (0x9745...CBD6, RH mainnet): engine /quote -> tx.data (swap(bytes)),
eth_call from a holder funded via state override (balance + allowance to ROUTER) at the quote's exact block; returned `out`
must equal the quote's grossOut to the wei. No txs, no keys."""
import sys,os,json,time,urllib.request;sys.path.insert(0,'harness')
from rpc import call
from slots import override
E=os.environ.get('ENGINE','http://127.0.0.1:18782'); ROUTER='0x9745ff56a6fc085b3e70a7509cd49ab6b97dcbd6'
H='0x00000000000000000000000000000000000beef1'; R='0x00000000000000000000000000000000000beef3'
src=open('service/soak.py').read(); src=src[src.index("W='0x0bd7"):src.index('def get(path)')]; exec(src)
routes=[(a,b,x) for a,b,xs in STOCK+MEME for x in xs]
out=[];ok=fail=nx=0
for mode in ('best','fast'):
  for tin,tout,amt in routes:
    if tin==U and amt>=10**20: continue
    q=json.load(urllib.request.urlopen(f'{E}/quote?tin={tin}&tout={tout}&amt={amt}&mode={mode}&recipient={R}',timeout=30))
    t=q.get('tx') or {}
    if not t.get('executable'): nx+=1; out.append(dict(mode=mode,tin=tin,tout=tout,amt=str(amt),skip=t.get('reason') or q.get('error'))); print(mode,tin[:8],tout[:8],amt,'not-exec',t.get('reason') or q.get('error'),flush=True); continue
    ov=override(tin,H,ROUTER,amt); blk=hex(q['block'])
    for a in range(3):
      try: r=call('rh','eth_call',[{'from':H,'to':ROUTER,'data':t['data'],'gas':hex(30_000_000)},blk,ov],retries=1); err=None; break
      except Exception as e: r=None; err=str(e)[:200]; time.sleep(1)
    got=int(r,16) if r and len(r)>=66 else None; good=got==int(q['grossOut'])
    try: g=int(call('rh','eth_estimateGas',[{'from':H,'to':ROUTER,'data':t['data']},blk,ov],retries=1),16)
    except Exception: g=None
    ok+=good; fail+=not good
    out.append(dict(mode=mode,tin=tin,tout=tout,amt=str(amt),block=q['block'],grossOut=q['grossOut'],routerOut=str(got),exact=good,legs=len(q.get('legs',[])),progBytes=len(t['program'])//2-1,gasEst=g,engineGas=q.get('gasUnits'),err=err))
    print(mode,tin[:8],tout[:8],amt,'exact' if good else f'MISMATCH got={got} exp={q["grossOut"]} {err}',g,flush=True)
json.dump(dict(router=ROUTER,ok=ok,fail=fail,not_executable=nx,rows=out),open('data/live/router_proof.json','w'),indent=1)
print('ok',ok,'fail',fail,'not_exec',nx)
