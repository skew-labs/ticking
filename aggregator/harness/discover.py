import sys;sys.path.insert(0,'harness');from rpc import *
chain,factory,start=sys.argv[1],sys.argv[2],int(sys.argv[3])
T='0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118'
b=int(call(chain,'eth_blockNumber',[]),16); out=[]
def get(a,z):
  try: return call(chain,'eth_getLogs',[{'address':factory,'fromBlock':hex(a),'toBlock':hex(z),'topics':[T]}],retries=3)
  except Exception as e:
    if a==z: raise
    m=(a+z)//2; return get(a,m)+get(m+1,z)
step=10_000_000 if chain=='rh' else 2_000_000
for s in range(start,b+1,step): out+=get(s,min(b,s+step-1))
pools=[{'pool':'0x'+l['data'][-40:],'t0':'0x'+l['topics'][1][-40:],'t1':'0x'+l['topics'][2][-40:],'fee':int(l['topics'][3],16)} for l in out]
print(chain,len(pools)); json.dump(pools,open(f'data/{chain}_pools.json','w'))
