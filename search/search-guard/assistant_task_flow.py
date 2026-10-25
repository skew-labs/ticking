"""Bounded deterministic composition of existing readers under one request identity.
No model calls. A step may be held/unavailable while the other requested steps finish.
"""
import re,time,threading,hashlib,json,contextvars
from concurrent.futures import ThreadPoolExecutor
READ_POOL=ThreadPoolExecutor(max_workers=4,thread_name_prefix="em-source")
from urllib.parse import quote,urlencode
import investigation_contract as contract
import em_wallet_intent as em

PATTERNS={
 'chart':r'\bcharts?|candles?\b|차트|캔들',
 'holders':r'\bholders?|who\s+held|who\s+holds\b|보유자|홀더',
 'graph':r'\bgraph|connections?|connected wallets|related wallets\b|그래프|연결된?\s?지갑',
 'crosschain':r'\bcross.?chain|across chains|other chains|mapping|wallet map\b|wallet flow\b|flow chart\b|멀티체인|다른\s?체인|매핑|지갑\s?맵|지갑\s?플로우|플로우\s?차트',
 'holdings':r'\bholdings|portfolio|positions?|assets?|balances?\b|보유\s?자산|잔고|포지션',
 'transactions':r'\btransactions?|transfers?|trades?|activity|trace\b|거래|이체|활동|추적'}

def intents(q):
 result=[name for name,p in PATTERNS.items() if re.search(p,q,re.I)]
 if re.search(r'거래.{0,5}(?:빼고|제외)|(?:exclude|without|not)\s+(?:trades|transactions)',q,re.I):result=[x for x in result if x!='transactions']
 return result
def requested(q):return len(intents(q))>=2 and bool(contract.subject().get('address') or contract.subject().get('asset'))

def run(q,chart=None,cancel=None,budget=10):
 em._L.ko=bool(re.search('[가-힣]',q))
 cancel=cancel or threading.Event();deadline=time.monotonic()+budget
 ctx=contract.CURRENT.get();s=contract.subject();selection=contract.selection();wanted=intents(q)
 steps=[];actions=[];cards=[];primary=None;sealed=False;step_lock=threading.Lock()
 point=any(k in selection for k in ('block','time','from','to'))
 def alive():return not cancel.is_set() and time.monotonic()<deadline
 def add(name,state,message,*,tool=None,args=None,card=None,derived=None):
  nonlocal primary
  if sealed:return
  step={'id':str(len(steps)+1),'kind':name,'state':state,'requestedContext':ctx,'message':message}
  if tool:step['tool']={'name':tool,'arguments':args or {}}
  if derived:step['derivedSubject']=derived
  if card is not None:
   step['evidence']=contract.evidence(card);step['card']=card;cards.append(card)
   if primary is None:primary=card
  steps.append(step)
 def unavailable(name,why):add(name,'unavailable',why)
 def read(name,path,args,summary,*,wallet=False,derived=None):
  if not alive():add(name,'incomplete','The bounded task stopped before this source read.');return None
  try:
   em._L.ko=bool(re.search('[가-힣]',q))
   if wallet:card=em.get(path,**args)
   else:card=em.em(path,timeout=min(2.5,max(.2,deadline-time.monotonic())),**args)
   if not isinstance(card,dict):raise LookupError('Invalid source response')
   if args.get('market'):contract.validate_evidence(card,market=args['market'])
   elif args.get('chain'):contract.validate_evidence(card,chain=args['chain'],address=args.get('address'))
   if path=='graph' and (card.get('query') or {}).get('window') not in (None,args.get('window')):raise LookupError('The graph source returned a different window; no substituted graph was accepted.')
   if path=='wallet-xc':card={**card,'data':dict(card)}
   card={**card,'kind':card.get('kind') or {'holders-at':'holders-at','wallet-xc':'xc'}.get(path,path)}
   ev=contract.evidence(card);state=ev['state']
   # Source classification/reason outranks a generic diagnostic detail.
   message=em.held_reason(card) if state=='held' else str(card.get('reason') or card.get('heldReason') or card.get('message') or summary) if state in ('unavailable','failed','unverified','incomplete') else str(card.get('message') or summary)
   if state not in ('held','unavailable','failed','unverified','incomplete','preparing'):
    if path=='wallet' and card.get('address'):message=em.wallet_message(card)
    elif path=='graph':message=em.graph_message(card,args['address'])
    elif path=='wallet-xc':
     chains=[x.get('chain') for x in card.get('chains',[]) if isinstance(x,dict) and x.get('chain')]
     message='Returned mapping rows on '+(', '.join(chains) if chains else 'no named chains in this bounded response')+'. This is source coverage, not a claim that other chains have no activity.'
    if state=='partial':message='Partial source result. '+message
   add(name,state,message,tool=path,args=args,card=card,derived=derived);return card
  except (LookupError,OSError,ValueError,TypeError,KeyError) as error:
   add(name,'unavailable',str(error)[:300],tool=path,args=args);return None
 def action(title,asset):
  if not isinstance(asset,str) or not em.chain_catalog.market(asset):return
  if s.get('chain') and asset.split(':')[0]!=s['chain']:return
  params=urlencode(selection);route='#/console/workspace/'+quote(asset,safe='')+('?' +params if params else '')
  if not any(a['route']==route for a in actions):actions.append({'kind':'context_required' if selection else 'open','viewExecution':'unverified' if selection else 'navigation_only','requiresContextApplication':list(selection),'title':title+(' · apply selected period first' if selection else ''),'route':route,'context':{**ctx,'subject':{'kind':'token','asset':asset},'selection':selection}})
 if re.search(r"(?i)\b(?:do not|don't|never)\s+(?:show|open|read|fetch|query)\b",q):
  return {'state':'clarify','message':'A requested action was explicitly excluded. Name the remaining steps to run under this context.','modelCalls':0}
 if s.get('kind')=='token':
  market=s.get('asset');chain=s.get('chain');holders=None
  if 'holders' in wanted or 'graph' in wanted:
   if any(k in selection for k in ('from','to')):unavailable('holders','This reader cannot prove the entire selected holder range. No current snapshot was substituted.')
   else:
    args={'market':market,'limit':10}
    if 'block' in selection:args['block']=selection['block']
    elif 'time' in selection:args['t']=selection['time']
    else:args['snaps']=1
    holders=read('holders','holders-at',args,'Returned exact-market holder evidence; shares require a reconciled same-block supply.')
    if holders and steps[-1]['state'] not in ('held','unavailable'):steps[-1]['message']=em.holders_timeline(holders)
  if 'graph' in wanted:
   if point:unavailable('graph','Holder graph readers cannot prove this selected historical point; no current or all-time graph was substituted.')
   elif not holders or contract.evidence(holders)['state']=='held':unavailable('graph','No usable holder seed was returned; held token balances were not used as graph seeds.')
   else:
    snapshots=holders.get('snaps') or [];rows=(snapshots[-1].get('top') or snapshots[-1].get('rows') or []) if snapshots else holders.get('holders') or []
    seeds=list(dict.fromkeys(x.get('address') for x in rows if isinstance(x,dict) and em.ADDR.fullmatch(str(x.get('address') or ''))))[:2]
    if not seeds:unavailable('graph','No exact holder addresses were returned by the bounded holder read. No graph seed was invented.')
    for address in seeds:read('graph','graph',{'address':address,'chain':chain,'window':selection.get('window','all'),'hops':1,'limit':12},'Returned holder graph; direct transfers do not establish shared ownership.',derived={'kind':'wallet','chain':chain,'address':address,'basis':'returned holder address'})
  if 'crosschain' in wanted:unavailable('crosschain','A token contract is chain-specific. Choose a returned holder wallet for cross-chain address mapping; no same-address token identity was inferred.')
  if 'chart' in wanted:
   action('Open exact token chart',market);add('chart','awaiting_view_context' if selection else 'navigation','The exact token chart action records the requested context. Applying a selected period in the chart view has not yet been verified.' if selection else 'The exact token chart action preserves the token identity; navigation does not assert a completed chart analysis.')
  for name in wanted:
   if name in ('holdings','transactions'):unavailable(name,'Choose a holder wallet for wallet assets or transactions. The token contract was not silently treated as a person’s wallet.')
 elif s.get('kind')=='wallet':
  address=s.get('address');chain=s.get('chain');wallet=None
  jobs=[]
  if 'crosschain' in wanted:
   if point:unavailable('crosschain','Current cross-chain mapping cannot prove the selected historical point. No current mapping was substituted.')
   else:jobs.append(lambda:read('crosschain','wallet-xc',{'address':address,'limit':10},'Returned cross-chain mapping evidence for Flow overlays. Same address and transfer paths do not alone prove beneficial ownership; topology maps are not shown.'))
  if 'holdings' in wanted or 'chart' in wanted:
   if point:unavailable('holdings','Current wallet balance sheets cannot prove this selected historical point.')
   elif not chain:unavailable('holdings','Choose one chain for the wallet balance sheet. Cross-chain discovery does not silently select its busiest chain.')
   else:jobs.append(lambda:read('holdings','wallet',{'address':address,'chain':chain},'Returned decoded wallet positions; a bounded empty result does not prove zero assets.',wallet=True))
  if 'transactions' in wanted:
   if point or selection.get('window') not in (None,'all'):unavailable('transactions','This trace reader cannot prove the selected point or bounded window. No broader transaction history was substituted.')
   elif not chain:unavailable('transactions','Choose the chain for wallet transaction evidence.')
   else:jobs.append(lambda:read('transactions','trace',{'address':address,'chain':chain},'Returned bounded wallet trace evidence.',wallet=True))
  if 'graph' in wanted:
   if point:unavailable('graph','This graph reader cannot prove the selected historical point.')
   elif not chain:unavailable('graph','Choose a graph chain. No network was guessed.')
   else:jobs.append(lambda:read('graph','graph',{'address':address,'chain':chain,'window':selection.get('window','all'),'hops':1,'limit':12},'Returned graph for the requested window; empty rows do not prove zero activity.'))
  futures=[READ_POOL.submit(contextvars.copy_context().run,job) for job in jobs]
  for future in futures:
   try:future.result(timeout=max(.1,deadline-time.monotonic()))
   except TimeoutError:
    future.cancel();add('source','incomplete','A retained source read is still running; no empty result was inferred.')
  wallet=next((x.get('card') for x in steps if x['kind']=='holdings'),None)
  if 'chart' in wanted:
   if wallet and not (wallet.get('held') or (wallet.get('sheet') or {}).get('held')):
    for row in (wallet.get('positions') or [])[:24]:
     if not row.get('held') and row.get('market') and row.get('identityBasis'):action('Chart '+str(row.get('asset') or row['market']),row['market'])
     if len(actions)>=4:break
   add('chart',('awaiting_view_context' if selection else 'navigation') if actions else 'unavailable',str(len(actions))+' exact-source asset chart navigation actions returned. No chart data read or rendered-period verification is claimed; unresolved or held positions are excluded.')
  if 'holders' in wanted:unavailable('holders','Choose one exact token market for holders; this wallet was not silently used as a token contract.')
 else:return None
 sealed=True
 steps.sort(key=lambda x:wanted.index(x['kind']) if x['kind'] in wanted else len(wanted))
 primary=next((x['card'] for x in steps if x['kind']=='holdings' and x.get('card')),None) or next((x['card'] for x in steps if x.get('card')),None)
 if not steps:return None
 message='\n\n'.join(step['kind'].replace('_',' ').title()+': '+step['message'] for step in steps)
 taskid=hashlib.sha256(json.dumps({'context':ctx,'query':q},sort_keys=True).encode()).hexdigest()[:24]
 result={'state':'wallet_card' if primary else 'message','message':message,'modelCalls':0,'resolver':'economic-machine-task-flow','task':{'schema':'ticking.assistant-task.v1','id':taskid,'context':ctx,'state':'partial' if any(x['state'] in ('held','unavailable','incomplete','awaiting_view_context') for x in steps) else 'returned','steps':steps,'actions':actions,'bounds':{'maxHolderGraphSeeds':2,'maxChartActions':4,'budgetSeconds':budget}},'choices':[a for a in actions if a['kind']=='open']}
 if primary:result['card']=primary
 return result
