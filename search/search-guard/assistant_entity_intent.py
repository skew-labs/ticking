"""Deterministic name/intent layer over attributed identity catalogues.
Recognized institution identity and verified wallet coverage are separate outputs.
"""
import re,unicodedata
METRICS={
 'attribution':r'거\s*맞|누구\s*거|ownership|belongs\s+to|소유자',
 'institutional_accumulation':r'기관.{0,18}(?:모았|매집|사[는고]|사는|accumulat)|\binstitutional\b.{0,25}(?:accumulat|buying)',
 'institutional_share':r'기관.{0,18}(?:비중|보유|얼마)|\binstitutional\b.{0,25}(?:share|ownership|holding)',
 'inflows':r'유입|inflows?|incoming', 'outflows':r'유출|outflows?|outgoing',
 'transactions':r'거래\s*내역|이체|이동|transactions?|transfers?',
 'holdings':r'뭐\s*(?:들었|들고)|뭐들|잔고|보유량|보유\s*자산|포트폴리오|holdings|balances?|portfolio',
 'holders':r'보유자|누가.{0,16}(?:들고|보유)|holders?|who\s+held|who\s+holds',
 'graph':r'그래프|연결|\bgraph\b|\bconnections?\b', 'chart':r'차트|캔들|charts?|candles?',
 'fees':r'수수료|\bfees?\b', 'wallets':r'지갑|wallets?', 'overview':r'소개|정보|뭐야|overview|explain|what is'}
INSTITUTIONS=[('okx','OKX',['okx','okex','오케이엑스','오케이엑스 거래소']),('blackrock','BlackRock',['blackrock','블랙록','블랙락']),('coinbase','Coinbase',['coinbase','코인베이스']),('binance','Binance',['binance','바이낸스','바낸']),('fidelity','Fidelity',['fidelity','피델리티']),('grayscale','Grayscale',['grayscale','그레이스케일']),('jpmorgan','JPMorgan',['jpmorgan','jp morgan','제이피모건']),('goldman_sachs','Goldman Sachs',['goldman sachs','골드만삭스']),('morgan_stanley','Morgan Stanley',['morgan stanley','모건스탠리']),('state_street','State Street',['state street','스테이트스트리트']),('vanguard','Vanguard',['vanguard','뱅가드']),('franklin_templeton','Franklin Templeton',['franklin templeton','프랭클린템플턴']),('bitwise','Bitwise',['bitwise','비트와이즈']),('ark_invest','ARK Invest',['ark invest','아크인베스트']),('wisdomtree','WisdomTree',['wisdomtree','위즈덤트리']),('invesco','Invesco',['invesco','인베스코']),('schwab','Charles Schwab',['charles schwab','찰스슈왑'])]
KOREAN_TOKENS={'아비트럼':'ARB','솔라나':'SOL','이더리움':'ETH','비트코인':'BTC','유에스디씨':'USDC','에이브':'AAVE'}

def clean(s):return unicodedata.normalize('NFKC',str(s)).casefold().strip()

class Recognizer:
 def __init__(self,markets,entities=(),protocols=()):
  self.markets=markets;self.entities={x.get('entity_id'):x for x in entities};self.by_alias={}
  def add(alias,kind,id,name,**more):
   key=clean(alias)
   if len(key)<2:return
   item={'entityType':kind,'id':id,'name':name,**more}
   if item not in self.by_alias.setdefault(key,[]):self.by_alias[key].append(item)
  for row in markets:
   for alias in (row.get('symbol'),row.get('name')):
    if alias:add(alias,'token',row['id'],row.get('name') or row['symbol'],chain=row.get('network'),symbol=row.get('symbol'))
  for alias,symbol in KOREAN_TOKENS.items():
   for row in markets:
    if row.get('symbol')==symbol:add(alias,'token',row['id'],row.get('name') or symbol,chain=row.get('network'),symbol=symbol)
  for row in protocols:
   if any(x.get('source') in ('registry','docs') for x in row.get('evidence',[])):
    add(row['name'],'protocol',row['id'],row['name'],chain=row.get('chain'))
    if row['name'].lower().startswith('aave'):add('aave','protocol',row['id'],row['name'],chain=row.get('chain'))
  for id,name,aliases in INSTITUTIONS:
   for alias in aliases:add(alias,'institution',id,name,walletCoverage='catalogued' if id in self.entities else 'unavailable')
  for alias in ('비탈릭','vitalik','vitalik buterin'):add(alias,'individual','vitalik_buterin','Vitalik Buterin',walletCoverage='unavailable')
  alternatives=sorted(self.by_alias,key=lambda x:(-len(x),x))
  self.pattern=re.compile(r'(?<![a-z0-9])(?:'+'|'.join(re.escape(x) for x in alternatives)+r')(?![a-z0-9])',re.I)
  self.typo={}
  for alias in alternatives:
   if len(alias)<4 or ' ' in alias:continue
   for i in range(len(alias)):
    for variant in (alias[:i]+alias[i+1:],alias[:i]+'*'+alias[i+1:]):self.typo.setdefault(variant,set()).add(alias)
 def recognize(self,q,context=None):
  text=clean(q);main=text.split('; constraint:')[0].split('; 조건:')[0]
  if '; now ' in main:main=main.partition('; now ')[2]
  if len(q)>400:return {'state':'clarify','intent':'clarify','entityType':'unknown','entities':[],'lookupState':'invalid_request','reason':'query_too_long'}
  if re.search('[a-z]',text) and re.search('[а-я]',text):return {'state':'clarify','intent':'clarify','entityType':'unknown','entities':[],'lookupState':'unverified_spelling','reason':'mixed_script_name'}
  excluded_action=bool(re.search(r"\b(?:do not|don't|never)\s+(?:show|open|read|fetch|query)\b",main))
  named_spans=list(self.pattern.finditer(main));metric_text=main
  for match in reversed(named_spans):
   alias=clean(match.group());before=main[:match.start()];after=main[match.end():]
   if alias in {'token','protocol','institution','for','show','read','only','and','the','this','that','its','it','on','scope','selected','name','graph','flow','holdings','holders','wallets','fee','fees','overview','explain','chart','inflows','outflows','transactions'} and not before.endswith(('“','\"')) and not re.search(r'(?<![A-Za-z])'+re.escape(alias.upper())+r'(?![A-Za-z])',q):continue
   metric_text=metric_text[:match.start()]+' '*len(match.group())+metric_text[match.end():]
  excluded_transactions=bool(re.search(r'거래.{0,5}(?:빼고|제외)|(?:exclude|without|not)\s+(?:trades|transactions)',main))
  metrics=[k for k,p in METRICS.items() if re.search(p,metric_text,re.I) and not (excluded_transactions and k=='transactions')]
  if 'holdings' in metrics and 'wallets' in metrics:metrics.remove('wallets')
  if 'holders' in metrics and 'wallets' in metrics:metrics.remove('wallets')
  if 'wallets' in metrics and any(x not in ('wallets','chart','overview') for x in metrics):metrics.remove('wallets')
  if len(metrics)>1 and 'overview' in metrics:metrics.remove('overview')
  metrics.sort(key=lambda key:re.search(METRICS[key],metric_text,re.I).start())
  previous=(context or {}).get('task') or {};intent=metrics[0] if metrics else previous.get('intent') or 'overview'
  matches=[]
  explicit_type=next((kind for word,kind in [('token ','token'),('토큰 ','token'),('protocol ','protocol'),('프로토콜 ','protocol'),('institution ','institution')] if word in main),None)
  for match in self.pattern.finditer(main):
   before=main[:match.start()];after=main[match.end():]
   function_aliases={'token','protocol','institution','for','show','read','only','and','the','this','that','its','it','on','scope','selected','name','graph','flow','holdings','fee','fees','overview','explain','holders','wallets','chart','inflows','outflows','transactions'}
   alias=clean(match.group())
   if alias in function_aliases and not before.endswith(('“','\"')) and not re.search(r'(?<![A-Za-z])'+re.escape(alias.upper())+r'(?![A-Za-z])',q):continue
   if re.match(r'\s*[“\"]',after) and clean(match.group()) in ('token','protocol','institution'):continue
   if re.search(r'\bon\s+$',before) or re.match(r'\s*(?:체인|에서는|에서)',after):continue
   if re.search(r'\bnot\s+$',before) or re.match(r'\s*(?:말고|아니라)',after):continue
   for item in self.by_alias[clean(match.group())]:
    if explicit_type and item['entityType']!=explicit_type:continue
    if item not in matches:matches.append(item)
  # A unique one-edit spelling match is a candidate, never a guessed institution wallet.
  corrected=False
  if not matches:
   stop={'show','check','please','read','that','this','with','from','only','official','labels','evidence','holders','holdings','wallets','inflows','outflows','chart','graph','overview','transactions','institution','protocol','token','source','selected','could','would','earlier','uppercase','lowercase','spelling'}
   aliases=set()
   for word in re.findall(r'[a-z][a-z0-9_-]{3,}',main):
    if word in stop:continue
    aliases.update(self.typo.get(word,()))
    for i in range(len(word)):
     aliases.update(self.typo.get(word[:i]+'*'+word[i+1:],()))
     aliases.update(a for a in (word[:i]+word[i+1:],) if a in self.by_alias)
    for i in range(len(word)-1):
     swapped=word[:i]+word[i+1]+word[i]+word[i+2:]
     if swapped in self.by_alias:aliases.add(swapped)
   candidates=[x for alias in aliases for x in self.by_alias[alias] if not explicit_type or x['entityType']==explicit_type]
   keys={(x['entityType'],clean(x['name'])) for x in candidates}
   if len(keys)==1:matches=candidates[:1];corrected=True
  explicit_mention=bool(matches)
  if not matches:
   subject=(context or {}).get('subject') or {}
   if subject.get('kind')=='institution':matches=[{'entityType':'institution','id':subject['entity'],'name':next((name for id,name,a in INSTITUTIONS if id==subject['entity']),subject['entity']),'walletCoverage':'catalogued' if subject['entity'] in self.entities else 'unavailable'}]
   elif subject.get('kind')=='wallet' and subject.get('address'):matches=[{'entityType':'wallet','id':str(subject.get('chain') or 'unselected')+':'+subject['address'],'name':subject['address'],'chain':subject.get('chain')}]
   elif subject.get('asset'):matches=[{'entityType':'token','id':r['id'],'name':r.get('name'),'chain':r.get('network'),'symbol':r.get('symbol')} for r in self.markets if r['id']==subject['asset']]
  # Explicit chain words override compatible previous selection, including exclusions.
  chains=[]
  for chain,aliases in {'arbitrum':['arbitrum','아비트럼'],'base':['base','베이스'],'ethereum':['ethereum','이더리움'],'linea':['linea','리네아'],'unichain':['unichain','유니체인'],'solana':['solana','솔라나']}.items():
   for alias in aliases:
    for hit in re.finditer(r'(?<![a-z0-9])'+re.escape(alias)+r'(?![a-z0-9])',main):
     if not (re.search(r'\bon\s+$|(?:chain|network)\s+$',main[:hit.start()]) or re.match(r'\s*(?:체인|에서는|에서)',main[hit.end():])):continue
     if re.match(r'\s*(?:말고|아니라)',main[hit.end():]) or re.search(r'\bnot\s+$',main[:hit.start()]):continue
     if chain not in chains:chains.append(chain)
  # Token names that equal a network are not independently a selected network.
  if matches and all(x['entityType']=='token' for x in matches) and not re.search(r'\bon\s+|체인|에서|에서는|network',main):chains=[]
  chain=chains[0] if len(chains)==1 else (context or {}).get('subject',{}).get('chain') if not chains else None
  if not matches:
   if re.search(r'모든\s?기관|기관.*지갑|institution.*wallet',main):return {'state':'identified','intent':'wallets','entityType':'institution_directory','entities':[],'lookupState':'bounded_directory','chain':chain,'metrics':['wallets']}
   return {'state':'clarify','intent':intent,'entityType':'unknown','entities':[],'lookupState':'unknown_entity','metrics':metrics,'chain':chain}
  found_kinds={x['entityType'] for x in matches}
  wallet_metric=any(x in metrics for x in ('wallets','holdings','inflows','outflows','transactions','graph','attribution'))
  stock_excluded=bool(re.search(r'주가\s*말고|not\s+(?:the\s+)?stock',main))
  if 'institution' in found_kinds and 'token' in found_kinds and (wallet_metric or stock_excluded) and 'holders' not in metrics:matches=[x for x in matches if x['entityType']=='institution']
  kinds={x['entityType'] for x in matches};kind=next(iter(kinds)) if len(kinds)==1 else 'ambiguous'
  if any(x in metrics for x in ('institutional_share','institutional_accumulation')):metrics=[x for x in metrics if x not in ('holders','holdings','wallets')]
  intent=metrics[0] if metrics else previous.get('intent') or 'overview'
  if kind=='institution' and intent=='institutional_share' and not re.search(r'비중|지분|\bshare\b|ownership',main):intent='holdings';metrics=['holdings']
  if kind=='token' and re.search(r'기관|institution',main) and intent in ('holders','holdings','overview'):intent='institutional_share'
  ids=list(dict.fromkeys(x['id'] for x in matches));eligible=[x for x in matches if not chain or x.get('chain') in (None,chain)]
  # Flow / on-chain asks: prefer Markets contract (chain:0x…) over exchange-only ticker (id=SYMBOL)
  onchain=[x for x in eligible if x.get('entityType')=='token' and isinstance(x.get('id'),str) and ':0x' in x['id'].lower()]
  flowish=intent in ('inflows','outflows','holders','institutional_accumulation','transactions','holdings','graph') or bool(re.search(r'(?i)\b(?:accumulat\w*|inflows?|outflows?|fund\s*flow|wallet\s*flow|asset\s*flow|money\s*flow|whale\s*flow|who\s+(?:bought|sold|held|holds)|flow)\b|유입|유출|축적|플로우', q))
  if kind=='token' and len(eligible)>1 and onchain:
    if flowish and len({x['id'] for x in onchain})==1:
      eligible=onchain[:1]; matches=onchain[:1]; ids=[onchain[0]['id']]; kind='token'
    elif not flowish and len({x['id'] for x in onchain})==1 and len(eligible)==2:
      # Symbol + its on-chain twin only — keep clarify with choices below; do not silently pick for chart/overview
      pass
  if kind=='institution':lookup='verified_entity_available' if all(x.get('walletCoverage')=='catalogued' for x in matches) else 'verified_wallets_unavailable';state='identified'
  elif kind=='wallet':lookup='identity_only' if chain else 'chain_unavailable';state='identified' if chain else 'clarify'
  elif kind=='individual':lookup='verified_wallets_unavailable';state='identified'
  elif kind=='ambiguous' or len(eligible)!=1:lookup='ambiguous' if len(eligible)>1 or kind=='ambiguous' else 'chain_unavailable';state='clarify'
  else:lookup='identity_only';state='identified'
  requested_selection={}
  for window in ('24h','7d','30d','90d'):
   if re.search(r'(?<![a-z0-9])'+window+r'(?![a-z0-9])',main):requested_selection['window']=window
  block=re.search(r'\bblock\s+(\d+)\b',main)
  if block:requested_selection['block']=int(block.group(1))
  if excluded_action:state='clarify';intent='clarify';lookup='excluded_action';metrics=[]
  return {'explicitEntityMention':explicit_mention,'selection':requested_selection,'state':state,'intent':intent,'metrics':metrics if excluded_action else metrics or [intent],'entityType':kind,'entities':matches,'entityIds':ids,'selected':eligible[0] if len(eligible)==1 else None,'lookupState':lookup,'chain':chain,'spellingCorrected':corrected,'ownershipInferred':False,'grade':'official' if re.search(r'공식.*만|official.*only|only.*official',main) else 'unselected'}

# Same recognizer is called by the production request handler and the evaluation.
import json,time
from pathlib import Path
from urllib.parse import urlencode
_DATA=json.loads(Path(__file__).with_name('assistant-identity-catalog.json').read_text()) if Path(__file__).with_name('assistant-identity-catalog.json').exists() else None
_RUNTIME=None

def runtime():
 global _RUNTIME
 if _RUNTIME is None:
  if _DATA is None:raise ValueError('Named identity catalog unavailable')
  _RUNTIME=Recognizer(_DATA['markets'],_DATA['entities'],_DATA['protocols'])
 return _RUNTIME

def prepare_query(q,previous=None):
 import investigation_contract as contract
 plan=runtime().recognize(q,contract.CURRENT.get())
 semantic=any(re.search(p,q,re.I) for p in METRICS.values()) or re.search(r'그럼|그러면|그\s?기관|that\s+institution|what\s+about',q,re.I)
 if not plan.get('explicitEntityMention') and not semantic:return None
 if previous and not plan.get('explicitEntityMention') and not re.search(r'이\s?토큰|this\s+(?:token|market)|이\s?지갑|this\s+wallet',q,re.I):
  previous=contract.normalize(previous)
  if semantic:
   contract.prepare(q,previous);plan=runtime().recognize(q,contract.CURRENT.get())
 if plan['lookupState'] in ('excluded_action','unverified_spelling','invalid_request'):return {'state':'clarify','message':'The request needs clarification before a source read: '+plan['lookupState']+'.','intentResolution':plan,'modelCalls':0}
 if re.search(r'어제|\byesterday\b',q,re.I) and plan['entityType']!='unknown':return {'state':'clarify','message':'The same subject and metric are retained. Choose the exact UTC time for the previous day; no current snapshot was substituted.','intentResolution':plan,'modelCalls':0}
 if plan['entityType']=='unknown':return None
 if plan['state']=='clarify':
  choices=[]
  for e in plan.get('entities') or []:
    eid=e.get('id'); et=e.get('entityType'); name=e.get('symbol') or e.get('name') or str(eid)
    if not eid: continue
    if et=='token' and isinstance(eid,str) and ':0x' in eid.lower():
      choices.append({'title':str(name)+' · '+(e.get('chain') or eid.split(':')[0])+' (on-chain Flow)','route':'#/investigate/'+eid})
    elif et=='token':
      from urllib.parse import quote as _q
      choices.append({'title':str(name)+' · exchange market','route':'#/console/workspace/'+_q(str(eid),safe='')})
    elif et=='protocol':
      choices.append({'title':str(name)+' · protocol','route':'#/protocol/'+str(eid)})
    elif et=='institution':
      choices.append({'title':str(name)+' · institution','route':'#/search?q='+_q(str(name))})
  # de-dupe by route
  seen=set(); uniq=[]
  for c in choices:
    if c['route'] in seen: continue
    seen.add(c['route']); uniq.append(c)
  out={'state':'clarify','message':'I recognized the requested subject or metric, but the exact identity or network is ambiguous. Choose the exact token contract, protocol, or institution.','intentResolution':plan,'modelCalls':0}
  if uniq: out['choices']=uniq[:8]
  return out
 previous=contract.CURRENT.get();subject=contract.subject();selection=contract.selection();selected=plan.get('selected')
 if plan['entityType']=='institution' and len(plan.get('entityIds',[]))==1:
  id=plan['entityIds'][0]
  if plan['intent']!='attribution':
   same=subject.get('kind')=='institution' and subject.get('entity')==id
   ctx=contract.normalize({'schema':contract.SCHEMA,'subject':{'kind':'institution','entity':id,**({'chain':plan['chain']} if plan.get('chain') else {})},'selection':selection if same else {},'task':{'intent':plan['intent']}});contract.prepare(q,ctx)
  return institution_result(plan)
 if plan['entityType']=='institution':return institution_result(plan)
 if plan['entityType']=='institution_directory':
  import em_wallet_intent as em
  try:
   data=em.em('entities',type='exchange',limit=10,timeout=3)
   rows=data.get('rows') or []
   return {'state':'message','message':'Indexed named institutions ('+str(len(rows))+' returned): '+', '.join(str(x.get('name') or x.get('entity_id')) for x in rows)+'. This is a bounded indexed directory, not every institution wallet. Choose one institution and a metric.','intentResolution':plan,'entityEvidence':{'schema':data.get('schema'),'rows':[{k:r.get(k) for k in ['entity_id','name','per_grade']} for r in rows],'notes':data.get('notes')},'modelCalls':0}
  except LookupError:return {'state':'message','message':'The institution-directory intent is recognized. Its verified source is temporarily unavailable; no directory was invented.','intentResolution':plan,'modelCalls':0}
 if plan['entityType']=='individual':return {'state':'message','message':plan['entities'][0]['name']+' is a named individual. No verified wallet attribution was returned by this institution catalog. Supply an exact public address or an attributed wallet source.','intentResolution':plan,'modelCalls':0}
 if plan['entityType']=='token' and selected:
  asset=selected['id'];same=subject.get('asset')==asset
  ctx=contract.normalize({'schema':contract.SCHEMA,'subject':{'kind':'token' if ':0x' in asset else 'market','asset':asset,'symbol':selected.get('symbol')},'selection':selection if same else {},'task':{'intent':plan['intent']}});contract.prepare(q,ctx)
  flowish=plan['intent'] in ('inflows','outflows','holders','institutional_accumulation','transactions','holdings','graph') or bool(re.search(r'(?i)\b(?:accumulat\w*|inflows?|outflows?|fund\s*flow|wallet\s*flow|asset\s*flow|money\s*flow|whale\s*flow|who\s+(?:bought|sold|held|holds)|\bflow\b)\b|유입|유출|축적|플로우', q))
  if flowish and ':0x' in asset:
   sym=str(selected.get('symbol') or selected.get('name') or 'Asset')
   return {'state':'ready','title':sym+' Asset Flow','route':'#/investigate/'+asset,'message':sym+' on-chain identity recognized. Muse reads asset-flow for this Markets contract (In/Out/holdings — not price candles). Pick another chain only if you meant a different deployment.','intentResolution':plan,'modelCalls':0,'entity':{'type':'token','id':asset,'symbol':selected.get('symbol'),'chain':selected.get('chain')},'handoff':{'capability':'muse-agent','next':{'capability':'muse-agent','route':'#/investigate/'+asset,'request':{'query':q,'flow':True,'assetFlow':True}},'schema':'ticking.handoff.v1'}}
  if plan['intent'] in ('chart','overview'):
   from urllib.parse import quote
   temporal=bool(contract.selection())
   return {'state':'message' if temporal else 'ready','title':'Open '+str(selected.get('symbol') or selected['name']),'route':'#/console/workspace/'+quote(asset,safe='') if not temporal else None,'message':selected['name']+' exact market identity recognized. '+('The selected period is retained; chart-period application still needs verification.' if temporal else 'This opens the exact market; it does not claim a completed candle analysis.'),'intentResolution':plan,'modelCalls':0}
  if plan['intent'] in ('institutional_share','institutional_accumulation'):
   return {'state':'message','message':selected['name']+' ('+str(selected.get('symbol'))+'): the institutional '+('accumulation trend' if plan['intent']=='institutional_accumulation' else 'holder share')+' intent is recognized. This metric needs verified institution-wallet labels and matching holder balances or changes at the selected period. That joined evidence is not currently proven; total holder count, general trading volume and pool liquidity were not substituted.','intentResolution':plan,'modelCalls':0}
 return None

def institution_result(plan):
 import investigation_contract as contract,em_wallet_intent as em
 sections=[];evidence=[];choices=[]
 for entity in list({x['id']:x for x in plan.get('entities',[])}.values())[:3]:
  name=entity['name'];id=entity['id']
  if entity.get('walletCoverage')!='catalogued':
   try:
    directory=em.em('entities',q=name,limit=3,timeout=3)
    current=[r for r in directory.get('rows') or [] if (r.get('entity_id')==id or clean(r.get('name'))==clean(name)) and (r.get('per_grade') or {}).get('official',0)>0]
   except LookupError:
    sections.append(name+': institution and '+plan['intent']+' intent recognized. Current verified-wallet coverage could not be read; this is unverified availability, not proof that no wallets exist.');evidence.append({'entity':id,'state':'unverified','reason':'entity_directory_read_unavailable'});continue
   if not current:
    sections.append(name+': institution and '+plan['intent']+' intent recognized. The connected entity directory returned no verified-wallet coverage for this name. No wallet ownership, holdings or zero flow was inferred.');evidence.append({'entity':id,'state':'unavailable','reason':'verified_wallet_coverage_unavailable','sourceSchema':directory.get('schema'),'notes':directory.get('notes')});continue
   id=current[0]['entity_id'];plan['lookupState']='verified_entity_available'

  if any(k in contract.selection() for k in ('block','time','from','to','window')):sections.append(name+': the requested period is retained. The institution directory cannot prove historical ownership or period flows; no current holdings were substituted.');continue
  try:
   args={'id':id,'grade':'official','limit':3,**({'chain':plan['chain']} if plan.get('chain') else {})};data=em.em('entity',timeout=3,**args)
   rows=[r for r in data.get('addresses') or [] if r.get('grade')=='official' and r.get('verified') is True and r.get('source')]
   sections.append(name+': '+str(len(rows))+' attributed official wallet rows returned (bounded to 3). Strong and candidate addresses are excluded. Aggregate balances across mixed label grades are withheld; these rows do not establish a complete institutional portfolio.')
   if plan['intent']=='holdings' and rows:
    first=rows[0]
    try:
     balance=em.get('wallet',address=first['address'],chain=first['chain'])
     contract.validate_evidence(balance,address=first['address'],chain=first['chain'])
     sections.append('One sampled official wallet on '+first['chain']+': '+em.wallet_message(balance)+' This is a bounded sample, not the institution total; attribution source date '+str(first.get('as_of') or 'unavailable')+'.')
     evidence.append({'entity':id,'state':'partial','kind':'sampled_official_wallet_balance','address':first['address'],'chain':first['chain'],'labelAsOf':first.get('as_of'),'card':balance,'evidence':contract.evidence(balance)})
    except (LookupError,KeyError,TypeError):sections.append('The sampled official-wallet balance read is unavailable. No institutional portfolio or zero balance was inferred.')
   evidence.append({'entity':id,'state':'returned','filter':args,'addresses':rows,'sources':data.get('sources'),'nextCursor':data.get('nextCursor'),'notes':data.get('notes'),'aggregateMixedGradesWithheld':True})
   for row in rows:
    if em.ADDR.fullmatch(str(row.get('address') or '')) and row.get('chain') in em.chain_catalog.ids():choices.append({'title':name+' official wallet · '+row['chain'],'route':'#/console/map?'+urlencode({'chain':row['chain'],'focus':row['address']})})
  except LookupError:sections.append(name+': identity recognized, but the verified-wallet reader is temporarily unavailable. No holdings or ownership was invented.')
 return {'state':'message','message':'\n\n'.join(sections),'intentResolution':plan,'entityEvidence':evidence,'choices':choices[:6],'resolver':'economic-machine-entities','modelCalls':0}
