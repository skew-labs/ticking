"""User constraints, not trading authority. Shared grammar with the chart client."""
import json,re,math,unicodedata
from pathlib import Path
RULES=json.loads(Path(__file__).with_name('planner-intents.json').read_text())
def test(p,q):return re.search(p,q,re.I)
def planning_request(q):return bool(test(RULES['intent'],q) and (not test(RULES['education'],q) or test(r'set|plan|prepare|설정|만들|계획|짜|띄워',q)))
def validate_planning(p):
 fields=list(RULES['fields']);extras=['targetReturnPct','riskPct']
 if not isinstance(p,dict) or p.get('version')!=1 or set(p)-set(['version',*fields,*extras,'highLeverage','returnBasis','quote','unresolved']):raise ValueError('Invalid planning constraints.')
 for k in fields+extras:
  v=p.get(k)
  if v is not None and (isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<v<=(1000 if k=='leverage' else 100 if k in extras else 1e12) or k=='leverage' and v<1):raise ValueError('Invalid '+k+' planning value.')
 if not isinstance(p.get('highLeverage'),bool) or p.get('returnBasis') not in ['unspecified','capital','margin','price'] or p.get('quote') not in [None,'USD','USDC','USDT'] or not isinstance(p.get('unresolved'),list) or len(p['unresolved'])>8 or any(not isinstance(s,str) or len(s)>120 for s in p['unresolved']):raise ValueError('Invalid planning context.')
 return dict(p,unresolved=list(p['unresolved']))
def parse_planning(q,previous=None):
 p=validate_planning(previous) if previous else dict(version=1,highLeverage=False,returnBasis='unspecified',quote=None,unresolved=[]);p['unresolved']=[];hit=False;q=unicodedata.normalize('NFKC',q)
 number=lambda s:float(s.replace(',',''))
 if test(r'(?:quantity|qty|entry|stop|target|leverage|margin|notional|진입가?|손절가?|익절가?|수량|레버리지|자본|예산)\s*[:=@]?\s*\$?-\s*\d',q):p['unresolved'].append('Negative planning values are unsupported; enter positive values.');hit=True
 for k,pattern in RULES['fields'].items():
  matches=[m for m in re.finditer(pattern,q,re.I) if not re.match(r'\s*(?:%|퍼센트|프로)',q[m.end():])];nums=list(dict.fromkeys(number(m[1]) for m in matches))
  for m in matches:
   if re.match(r'(?:[kmb]\b|e[+-]?\d)',q[m.end():],re.I):p['unresolved'].append('Write the full numeric '+k+' value; shorthand was not applied.');hit=True
  if len(nums)>1:p['unresolved'].append('Choose one '+k+' value.');p.pop(k,None);hit=True
  elif nums:p[k]=nums[0];hit=True
 lev=[number(m[1]) for m in re.finditer(RULES['multiplier'],q,re.I)]
 if len(lev)==1:
  if p.get('leverage') is not None and p['leverage']!=lev[0] and test(r'leverage|레버리지',q):p['unresolved'].append('Conflicting leverage values.')
  p['leverage']=lev[0];hit=True
 if test(RULES['high'],q):p['highLeverage']=True;hit=True
 bare=test(r'(?:\$\s*'+RULES['numeric']+'|'+RULES['numeric']+r'\s*(?:달러|USD[CT]?\b))',q)
 if bare and not any(test(pattern,q) for pattern in RULES['fields'].values()) and not test(RULES['education'],q):p['capital']=number(bare[1] or bare[2]);hit=True
 pct=list(re.finditer(RULES['percent'],q,re.I))
 for m in pct:
  around=q[max(0,m.start()-24):m.end()+20];risk=test(RULES['risk'],around);ret=test(RULES['return'],around)
  if test(r'stop|손절',around):p['unresolved'].append('Specify a stop price or a maximum loss amount; stop % was not assumed.');hit=True
  elif risk and not ret:p['riskPct']=number(m[1]);hit=True
  elif ret and not risk or len(pct)==1 and not risk:p['targetReturnPct']=number(m[1]);hit=True
  else:p['unresolved'].append('Specify whether '+m[1]+'% is return or loss risk.');hit=True
 if len(pct)>2:p['unresolved'].append('Use at most a return percentage and a risk percentage.')
 if p.get('targetReturnPct') is not None:
  if test(r'(?:price|가격)\s*(?:move|change|변동|상승|하락)?|가격\s*기준',q):p['returnBasis']='price'
  elif test(r'(?:margin|roe|증거금)\s*(?:return|수익|기준)|수익.*증거금',q):p['returnBasis']='margin'
  elif test(r'(?:account|capital|계좌|자본)\s*(?:return|수익|기준)|수익.*(?:계좌|자본)',q):p['returnBasis']='capital'
 quotes=list(dict.fromkeys(s.upper() for s in re.findall(r'\bUSDC\b|\bUSDT\b|\bUSD\b',q,re.I)))
 if len(quotes)==1:p['quote']=quotes[0]
 if len(quotes)>1:p['unresolved'].append('Choose one budget currency.')
 return validate_planning(p) if hit or previous else None

def market_query(q):return re.sub(r'\b(?:USDC|USDT|USD)\b',' ',q,flags=re.I) if planning_request(q) and not test(RULES['explicitQuoteMarket'],q) else q
