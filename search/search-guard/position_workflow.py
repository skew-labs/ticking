"""Typed, read-only position planning. Native selectors cannot supply prices."""
import re

def requested(q):
 return bool(re.search(r'\b(?:scalp(?:ing)?|strateg(?:y|ies))\b|스캘핑|스켈핑|전략',q,re.I) or re.search(r'\bposition\b|포지션',q,re.I) and re.search(r'\b(?:plan|planning|setup|prepare|build|create)\b|계획|준비|어떻게',q,re.I))

def explicit(q):
 times=[]
 for n,u in re.findall(r'(?<![\w])([1-9]\d*)\s*(minutes?|mins?|m|hours?|hrs?|h|seconds?|secs?|s|days?|d|w|분|시간|초|일|주)(?:봉)?(?![a-z])',q.lower()):
  unit='m' if u in ('분','m') or u.startswith('min') else 'h' if u in ('시간','h') or u.startswith(('hour','hr')) else 's' if u in ('초','s') or u.startswith(('sec',)) else 'w' if u in ('주','w') else 'd'
  times.append(n+unit)
 sides=[s for s,pat in [('long',r'\blong\b|롱'),('short',r'\bshort\b|숏')] if re.search(pat,q,re.I)]
 styles=[s for s,pat in [('scalping',r'\bscalp(?:ing)?\b|스캘핑|스켈핑|단타'),('intraday',r'\bintraday\b|\bdaytrading\b'),('swing',r'\bswing\b|스윙')] if re.search(pat,q,re.I)]
 return set(times),sides,styles

def validate(p,q,intervals):
 if type(p) is not dict or set(p)!={'asset','interval','side','style'}:raise ValueError('Invalid workflow fields')
 if p['asset'] is not None and (type(p['asset']) is not str or not 1<=len(p['asset'])<=100):raise ValueError('Invalid asset')
 if p['interval'] is not None and p['interval'] not in intervals:raise ValueError('Unsupported interval')
 if p['side'] not in (None,'long','short') or p['style'] not in (None,'scalping','intraday','swing'):raise ValueError('Invalid planning settings')
 times,sides,styles=explicit(q)
 if len(times)>1 or len(sides)>1 or len(styles)>1:raise ValueError('Choose one interval, direction and trading style for this plan.')
 if (times and p['interval'] not in times) or (sides and p['side']!=sides[0]) or (styles and p['style']!=styles[0]):raise ValueError('The full planning request was not preserved.')
 # No implied risk appetite, side or interval may be smuggled in by the model.
 if not sides and p['side'] is not None:raise ValueError('Choose a direction in the planner.')
 if not times and p['interval'] is not None:raise ValueError('Choose an interval in the planner.')
 if re.search(r"\b(?:not|don't|cancel|execute|buy|sell|leverage|risk|stop|target|entry)\b|취소|하지마|주문|손절|익절|레버리지",q,re.I):raise ValueError('Use the planner fields for entry, risk and exits; no trade was sent.')
 return p

def resolve(p,q,chart,rows,aliases,intervals):
 p=validate(p,q,intervals)
 asset=p['asset'] or (chart or {}).get('symbol')
 if not asset:return {'state':'clarify','message':'Which market should I plan? Try “Plan a SOL position on 15min for scalping”.'}
 key=aliases.get(asset.lower(),asset).lower()
 exact=[r for r in rows if r['id'].lower()==key]
 matches=exact or [r for r in rows if key in [str(r.get('symbol','')).lower(),str(r.get('name','')).lower()]]
 if len(matches)!=1:return {'state':'clarify','message':'Choose one exact listed market for this position plan. Include its symbol and interval.'}
 row=matches[0];workflow={'version':1,'kind':'position','asset':row['id'],'symbol':row['symbol'],'interval':p['interval'],'side':p['side'],'style':p['style']}
 return {'state':'chart_workflow','workflow':workflow,'route':'#/terminal/'+row['id'],'title':row['symbol']+' position planner','message':'Reading completed '+row['symbol']+' '+(p['interval'] or 'chart')+' candles for a conditional strategy with entry, invalidation and exit scenarios.'}
