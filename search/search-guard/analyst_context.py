"""Read existing attributed research/feed caches only. Never fetch or score headlines."""
import json,math,re,time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path('/srv/skew/tick-market-ui-20261007')
CACHE={}
def load(path):
 try:
  st=path.stat()
  if st.st_size>2_000_000:return {}
  key=str(path);old=CACHE.get(key)
  if not old or old[0]!=st.st_mtime_ns:CACHE[key]=(st.st_mtime_ns,json.loads(path.read_text()))
  value=CACHE[key][1];return value if isinstance(value,dict) else {}
 except (OSError,ValueError):return {}
def safe(url):
 try:
  u=urlsplit(url);return isinstance(url,str) and len(url)<=1600 and u.scheme=='https' and bool(u.hostname) and not u.username and not u.password
 except (TypeError,ValueError):return False
def context(asset,rows,cutoff,*,hours=72,limit=3):
 row=next((r for r in rows if r.get('id')==asset),{});profile=load(ROOT/'server/asset_research.json').get(asset,{})
 if not isinstance(profile,dict):profile={}
 result={'schema':'ticking.asset-context.v1','asset':asset,'asOf':cutoff,'background':None,'headlines':[],'status':'no_recent_attributed_headlines','affectsRanking':False,'limits':'Project background and attributed headlines are context, not evidence of price causality. No sentiment, unlock schedule or causal news impact is inferred.'}
 sources=[{'title':str(s.get('title','Source'))[:160],'url':s['url']} for s in profile.get('sources',[])[:3] if safe(s.get('url'))]
 try:review=datetime.fromisoformat(profile.get('reviewed_at','').replace('Z','+00:00')).timestamp()*1000
 except (ValueError,TypeError):review=0
 if review and review<=cutoff and sources and isinstance(profile.get('summary'),str):result['background']={'summary':profile['summary'][:800],'reviewedAt':profile['reviewed_at'],'sources':sources,'currentVerification':False}
 name=str(row.get('name',''));symbol=str(row.get('symbol',''));terms=[x for x in [name,symbol] if len(x)>=3 and x.lower() not in ['the','cat','data','token','sol']]
 if asset=='SOL':terms=['Solana']
 if not terms:
  from asset_issues import enrich
  return enrich(result)
 pattern=re.compile(r'(?<![A-Za-z0-9])(?:'+'|'.join(re.escape(x) for x in terms)+r')(?![A-Za-z0-9])',re.I)
 feeds=load(ROOT/'.state/news-cache.json').get('feeds',{});seen=set();headlines=[]
 for feed in feeds.values():
  if not isinstance(feed,dict):continue
  checked=feed.get('checked_at',0);checked=checked if isinstance(checked,(int,float)) and math.isfinite(checked) else 0
  for h in feed.get('articles',[]):
   if not isinstance(h,dict):continue
   published=h.get('published_at');title=h.get('title','');url=h.get('url')
   if type(published) is not int or not cutoff/1000-hours*3600<=published<=cutoff/1000 or not isinstance(title,str) or not pattern.search(title) or not safe(url) or url in seen:continue
   # Require the cached source's explicit publisher attribution. No article text or instructions enter the kernel.
   if not isinstance(h.get('source'),str) or not h['source']:continue
   seen.add(url);headlines.append({'title':title[:240],'url':url,'publisher':h['source'][:120],'publishedAt':published*1000,'checkedAt':int(checked*1000),'cacheCurrent':0<=cutoff/1000-checked<=300})
 result['headlines']=sorted(headlines,key=lambda x:x['publishedAt'],reverse=True)[:limit]
 if result['headlines']:result['status']='attributed_headlines' if all(h['cacheCurrent'] for h in result['headlines']) else 'dated_cached_headlines'
 from asset_issues import enrich
 return enrich(result)
