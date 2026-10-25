import json,urllib.request,urllib.error
S='ticking.investigation-context.v1';asset='base:0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf';page={'schema':S,'subject':{'kind':'token','asset':asset},'selection':{'window':'7d'}};prior={'schema':S,'subject':{'kind':'institution','entity':'blackrock'},'selection':{},'task':{'intent':'holdings'}}
cases=[('institution-pronoun',{'query':'그 기관 보유량','context':page,'previousContext':prior}),('generic-followup',{'query':'그럼 보유량','context':page,'previousContext':prior}),('explicit-page-token',{'query':'이 토큰 보유자','context':page,'previousContext':prior})];out=[]
for name,body in cases:
 req=urllib.request.Request('http://127.0.0.1:19464/api/public/intelligence/resolve',json.dumps(body).encode(),{'Content-Type':'application/json','Origin':'https://ticking.dev'},method='POST')
 try:
  with urllib.request.urlopen(req,timeout=22) as r:status=r.status;value=json.load(r)
 except urllib.error.HTTPError as e:status=e.code;value=json.load(e)
 out.append({'case':name,'status':status,'response':value})
print(json.dumps(out))
