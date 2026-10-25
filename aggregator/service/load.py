"""Concurrent load: BEST workers on :8782 (stock rows) + FAST workers on :8783 (meme/WETH pairs). Reports client RTT + server compute + lag."""
import http.client,json,random,threading,time,sys,glob,os
sys.path.insert(0,'service'); from soak_pairs import STOCK,MEME_TOKENS,W
DUR=int(sys.argv[1]) if len(sys.argv)>1 else 120; NB=int(sys.argv[2]) if len(sys.argv)>2 else 8; NF=int(sys.argv[3]) if len(sys.argv)>3 else 2
R={'best':[], 'fast':[]}; stop=time.time()+DUR
def worker(kind,port):
  c=http.client.HTTPConnection('127.0.0.1',port); rnd=random.Random()
  while time.time()<stop:
    if kind=='best': tin,tout,amts=rnd.choice(STOCK); a=rnd.choice(amts)
    else: t=rnd.choice(MEME_TOKENS); tin,tout=(W,t) if rnd.random()<.5 else (t,W); a=10**rnd.randint(15,19) if tin==W else 10**rnd.randint(18,24)
    t0=time.perf_counter(); c.request('GET',f'/quote?tin={tin}&tout={tout}&amt={a}'); r=c.getresponse(); b=r.read(); rtt=(time.perf_counter()-t0)*1e6
    try: j=json.loads(b)
    except: j={}
    R[kind].append((rtt,j.get('compute_us'),j.get('lagBlocks'),r.status,tin,tout,a))
ts=[threading.Thread(target=worker,args=('best',8782)) for _ in range(NB)]+[threading.Thread(target=worker,args=('fast',8783)) for _ in range(NF)]
[t.start() for t in ts]; [t.join() for t in ts]
def P(v,q): v=sorted(v); return round(v[int((len(v)-1)*q)]) if v else None
out={}
for k,v in R.items():
  ok=[x for x in v if x[3]==200]; lag=[x[2] for x in ok]
  out[k]=dict(n=len(v),ok=len(ok),non200={s:sum(1 for x in v if x[3]==s) for s in set(x[3] for x in v) if s!=200},
    rtt_us={q:P([x[0] for x in ok],q) for q in (.5,.99,.999,1)},compute_us={q:P([x[1] for x in ok],q) for q in (.5,.99,.999,1)},
    lag0=round(sum(1 for l in lag if l==0)/max(1,len(lag)),4),lag_le1=round(sum(1 for l in lag if l<=1)/max(1,len(lag)),4),
    worst=sorted(ok,key=lambda x:-x[1])[:3])
print(json.dumps(out,indent=1))
