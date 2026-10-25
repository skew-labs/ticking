"""Bounded public wallet research; read-only indexed DEX actions and native BTC UTXOs.
No ownership clustering, transaction submission, position sizing or trading authority.
"""
import asyncio,collections,concurrent.futures,hashlib,json,math,re,sqlite3,threading,time,urllib.request,urllib.error,urllib.parse
from pathlib import Path
from economic_machine import chain_catalog
SOURCE=Path('/srv/skew/em-galaxy/live/live.db')
EVM=re.compile(r'^0x[0-9a-f]{40}$');TX=re.compile(r'^[0-9a-f]{64}$');MARKET=type('MarketIdentity',(),{'fullmatch':staticmethod(chain_catalog.market)})()
SERVICES={'dex_pool','dex_router','token_contract','lending','bridge','cex_hot_wallet','cex_deposit','contract_other','other_contract'}
ALIASES={'비트코인':'BTC','bitcoin':'BTC','비티씨':'BTC','솔라나':'SOL','solana':'SOL','이더리움':'ETH','ethereum':'ETH','엔비디아':'NVDA','nvidia':'NVDA','테슬라':'TSLA','tesla':'TSLA','아비트럼':'ARB','arbitrum':'ARB','wrapped bitcoin':'WBTC','wrapped btc':'WBTC'}
def finite(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
def btc_address(a):
 if not isinstance(a,str) or not 14<=len(a)<=90:return False
 if a.startswith(('1','3')):
  chars='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
  if any(c not in chars for c in a):return False
  n=0
  for c in a:n=n*58+chars.index(c)
  b=b'\0'*(len(a)-len(a.lstrip('1')))+(n.to_bytes((n.bit_length()+7)//8,'big') if n else b'')
  return len(b)==25 and b[0] in (0,5) and hashlib.sha256(hashlib.sha256(b[:-4]).digest()).digest()[:4]==b[-4:]
 if a!=a.lower() or not a.startswith('bc1'):return False
 chars='qpzry9x8gf2tvdw0s3jn54khce6mua7l';tail=a[3:]
 if len(tail)<7 or any(c not in chars for c in tail):return False
 data=[chars.index(c) for c in tail];chk=1
 for v in [3,3,0,2,3]+data:
  top=chk>>25;chk=((chk&0x1ffffff)<<5)^v
  for i,g in enumerate([0x3b6a57b2,0x26508e6d,0x1ea119fa,0x3d4233dd,0x2a1462b3]):
   if (top>>i)&1:chk^=g
 version=data[0];program=data[1:-6];bits=len(program)*5;size=bits//8
 if version>16 or bits%8>=5 or size<2 or size>40 or (bits%8 and program[-1]&((1<<(bits%8))-1)):return False
 return chk==(1 if version==0 else 0x2bc830a3) and (version!=0 or size in (20,32))
def requested(q,prior=False):
 return bool(re.search(r'(?i)whales?|who.{0,25}(?:bought|sold|buying|selling)|wallets?.{0,30}(?:bought|sold|buy|sell|activity|evidence)|large(?:st)?\s+(?:trades?|wallets?|holders?)|big(?:gest)?\s+(?:wallets?|traders?)|고래|큰손|대규모.{0,12}(?:지갑|거래)|지갑.{0,16}(?:매수|매도|샀|팔|찾|나열)|(?:누가|어떤 지갑).{0,20}(?:샀|팔|매수|매도)',q) or prior and re.search(r'(?i)^(그중|그 중|그럼|그러면|첫|두.?번|세.?번|마지막|이 지갑|그 지갑|얘|걔|언제|얼마|왜|매핑|지도|차트에|더 |다른|the first|the second|that wallet|this wallet|when|how much|why|what (should|about|next)|show (it|them)|map (it|that)|plot (it|them)|trace (it|that)|follow (it|that))',q.strip()))
def choose(q,context,rows,aliases):
 if not isinstance(context,dict) or set(context)-{'asset','wallet','network','interval'}:raise ValueError('Invalid wallet research context.')
 for k,v in context.items():
  if v is not None and (not isinstance(v,str) or len(v)>100):raise ValueError('Invalid wallet context field.')
 prior=context.get('asset');known={r['id']:r for r in rows};matches=[]
 for word,target in sorted(ALIASES.items(),key=lambda x:-len(x[0])):
  if re.search((r'(?<![a-z0-9])'+re.escape(word)+r'(?![a-z0-9])') if word.isascii() else re.escape(word),q,re.I):
   if target in ('ARB',) and re.search(r'(?i)on arbitrum|아비트럼(?:에서|의| 체인)',q):continue
   matches=[r for r in rows if r.get('id')==target or r.get('symbol','').upper()==target];break
 if not matches:
  for r in rows:
   for name in [r.get('id'),r.get('symbol')]:
    if name and len(name)>1 and re.search(r'(?<![\w.:])'+re.escape(name)+r'(?![\w.:])',q,re.I):matches.append(r);break
 # Exact requested symbols take priority; native BTC is never substituted by WBTC.
 if len(matches)>1:
  chain=chain_catalog.query_chain(q)
  if chain:matches=[r for r in matches if r.get('network')==chain]
  else:
   native=[r for r in matches if r.get('id')==r.get('symbol')]
   if len(native)==1:matches=native
 named_chain=chain_catalog.query_chain(q)
 if named_chain:
  matches=[r for r in matches if r.get('network')==named_chain]
  if not matches:return None,{'state':'clarify','message':'No reviewed asset matches that exact network. Choose its token address; no previous chart was substituted.','choices':[]}
 if not matches and prior in known:matches=[known[prior]]
 if not matches:return None,{'state':'clarify','message':'Which asset should I investigate? Name the coin and network, or open its chart first.','choices':[]}
 if len(matches)>1:
  return None,{'state':'choices','message':'Choose the exact asset and network. Wrapped tokens and native coins have separate wallet evidence.','choices':[{'title':r.get('symbol','')+' · '+r.get('network',''),'route':'#/console/workspace/'+urllib.parse.quote(r['id'],safe='')} for r in matches[:8]]}
 r=matches[0];wallet=context.get('wallet') if prior==r['id'] else None
 evm=re.findall(r'(?<![A-Za-z0-9])0x[0-9a-fA-F]{40}(?![A-Za-z0-9])',q);btc=[a for a in re.findall(r'\b(?:bc1[a-z0-9]{11,87}|[13][1-9A-HJ-NP-Za-km-z]{25,34})\b',q) if btc_address(a)]
 if len(set(evm+btc))>1:raise ValueError('Choose one wallet to inspect; the result list remains available.')
 if evm:wallet=evm[0].lower()
 if btc:wallet=btc[0]
 network='bitcoin' if r['id']=='BTC' else r.get('network')
 if wallet and not (btc_address(wallet) if network=='bitcoin' else bool(EVM.fullmatch(wallet)) if chain_catalog.is_evm(network) else False):raise ValueError('The selected wallet does not match this asset network.')
 return {'row':r,'asset':r['id'],'symbol':r.get('symbol',r['id']),'network':network,'wallet':wallet},None

def base(selection,cutoff):
 return {'schema':'ticking.wallet-research.v1','asset':selection['asset'],'symbol':selection['symbol'],'network':selection['network'],'wallet':selection.get('wallet'),'checkedAt':cutoff,'wallets':[],'events':[],'graph':None,'coverage':[],'status':'unavailable','direction':'unavailable','threshold':None,'scope':{},'source':'','sourceTime':None,'receiptReverified':False,'ownership':'unproven','affectsEligibility':False}
def finish(r):
 r['id']='wallet-'+hashlib.sha256(json.dumps(r,sort_keys=True,allow_nan=False).encode()).hexdigest()[:16]
 assert len(json.dumps(r,allow_nan=False).encode())<45000
 return r

def indexed(selection,cutoff,window=86400,threshold=100000):
 r=base(selection,cutoff);r.update(source='Economic Machine indexed DEX routes',threshold={'value':threshold,'unit':'estimated USD per indexed action'},scope={'from':cutoff-window*1000,'to':cutoff,'maximumRows':1000,'maximumScannedActions':5000})
 r['coverage']=['The latest 5,000 indexed actions on this chain are searched, restricted to the last 24 hours. Only supported above-threshold DEX routes qualify; this is not a whole-market census.','Buy and sell refer to the exact input/output token. Transfers, collateral and bridging are excluded.','Wallet is the indexed transaction sender or decoded account; it is not proven beneficial ownership.','USD amounts are source valuation estimates. Receipts and chain finality are not independently reverified.']
 if not chain_catalog.is_evm(selection['network']) or not MARKET.fullmatch(selection['asset']):
  r.update(status='unsupported',summary='Wallet trade discovery is not connected for this exact market. Its price chart is available, but exchange candles do not reveal customer wallets.');return finish(r)
 if not SOURCE.is_file():r['summary']='The indexed route store is unavailable. No wallet list or directional claim was inferred.';return finish(r)
 started=time.monotonic();c=sqlite3.connect('file:'+str(SOURCE)+'?mode=ro',uri=True,timeout=.5);c.row_factory=sqlite3.Row;c.set_progress_handler(lambda:int(time.monotonic()-started>.65),5000)
 try:
  c.execute('PRAGMA query_only=ON');c.execute('BEGIN');head=c.execute('SELECT * FROM heads WHERE chain=?',(selection['network'],)).fetchone();head=dict(head) if head else {}
  if not head:
   r.update(status='unsupported',summary='The exact chain is recognized, but this wallet-trade index has no source head for it. Other investigation layers may still contain records; no chain was substituted.');return finish(r)
  token=selection['asset'].split(':')[1];params=[selection['network'],int(cutoff/1000)-window,int(cutoff/1000),cutoff/1000,token,token]
  query="SELECT * FROM (SELECT id,chain,block,time,tx_hash,log_index,sub,action,contract,wallet,wallet_category,asset,asset2,amount,amount2,usd,flags,protocol_name,protocol,created FROM actions INDEXED BY actions_chain WHERE chain=? ORDER BY id DESC LIMIT 5000) WHERE time>=? AND time<=? AND created<=? AND action='swap' AND (asset=? OR asset2=?)"
  if selection.get('wallet'):query+=' AND wallet=?';params.append(selection['wallet'])
  raw=[dict(x) for x in c.execute(query+' ORDER BY time DESC LIMIT 1001',params)]
 except sqlite3.Error:
  r['summary']='The bounded indexed read is temporarily unavailable. No wallet ranking was inferred.';return finish(r)
 finally:c.close()
 r['scope'].update(truncated=len(raw)>1000,scannedRows=min(len(raw),1000));raw=raw[:1000]
 fresh=bool(head and not head.get('error') and finite(head.get('polled_at')) and finite(head.get('block_time')) and -30<=cutoff/1000-head['polled_at']<60 and -120<=cutoff/1000-head['block_time']<240)
 r['sourceTime']=int(head['block_time']*1000) if finite(head.get('block_time')) else None
 valid=[];seen=set();excluded=collections.Counter()
 for a in raw:
  if any(not EVM.fullmatch(str(a.get(k) or '')) for k in ['wallet','asset','asset2','contract']) or a.get('asset')==a.get('asset2'):excluded['invalid_or_cycle']+=1;continue
  if not re.fullmatch(r'0x[0-9a-f]{64}',str(a.get('tx_hash') or '')) or not all(isinstance(a.get(k),int) and a[k]>=0 for k in ['id','block','time','log_index']):excluded['invalid_receipt']+=1;continue
  if not all(finite(a.get(k)) and a[k]>0 for k in ['amount','amount2','usd']) or a['usd']<threshold:excluded['below_threshold_or_unknown_value']+=1;continue
  if a.get('wallet_category') in SERVICES or re.search(r'(?i)margin|inferred|unverified',str(a.get('flags') or '')):excluded['service_or_unverified']+=1;continue
  key=(a['chain'],a['tx_hash'],a['log_index'],a.get('sub') or 0)
  if key in seen:continue
  seen.add(key);side='sell' if a['asset']==token else 'buy';amount=a['amount'] if side=='sell' else a['amount2']
  valid.append({'id':'evm-'+str(a['id']),'wallet':a['wallet'],'tx':a['tx_hash'],'time':a['time']*1000,'side':side,'amount':amount,'estimatedUsd':a['usd'],'block':a['block'],'logIndex':a['log_index'],'protocol':str(a.get('protocol_name') or a.get('protocol') or 'Indexed route')[:80],'attribution':'indexed account; beneficial owner unknown'})
 # Do not present a multi-leg round trip in the same transaction as accumulation.
 directions=collections.defaultdict(set)
 for e in valid:directions[(e['wallet'],e['tx'])].add(e['side'])
 mixed={k for k,v in directions.items() if len(v)>1}
 valid=[e for e in valid if (e['wallet'],e['tx']) not in mixed];excluded['mixed_direction_transactions']=len(mixed)
 groups={}
 for e in valid:
  g=groups.setdefault(e['wallet'],{'address':e['wallet'],'category':'large indexed trader','buyAmount':0,'sellAmount':0,'buyUsd':0,'sellUsd':0,'count':0,'lastTime':0})
  g[e['side']+'Amount']+=e['amount'];g[e['side']+'Usd']+=e['estimatedUsd'];g['count']+=1;g['lastTime']=max(g['lastTime'],e['time'])
 wallets=sorted(groups.values(),key=lambda g:-(g['buyUsd']+g['sellUsd']))[:8];allowed={w['address'] for w in wallets}
 r['wallets']=wallets;r['events']=sorted((e for e in valid if e['wallet'] in allowed),key=lambda e:-e['time'])[:24];r['scope'].update(excluded=dict(excluded),qualifyingActions=len(valid),returnedEvents=len(r['events']),walletListTruncated=len(groups)>8,eventsTruncated=len(valid)>24)
 buy=sum(e['estimatedUsd'] for e in valid if e['side']=='buy');sell=sum(e['estimatedUsd'] for e in valid if e['side']=='sell')
 r['totals']={'buyEstimatedUsd':buy,'sellEstimatedUsd':sell,'eligibleActions':len(valid),'wallets':len(groups)}
 r['directionWindowSeconds']=3600
 recent_buy=sum(e['estimatedUsd'] for e in valid if e['side']=='buy' and cutoff-e['time']<=3600000);recent_sell=sum(e['estimatedUsd'] for e in valid if e['side']=='sell' and cutoff-e['time']<=3600000)
 r['status']='available' if fresh else 'stale';latest=max((e['time'] for e in valid),default=0)
 r['direction']='no_recent_evidence' if not valid or cutoff-latest>3600000 or not fresh else 'mixed' if min(recent_buy,recent_sell)>max(recent_buy,recent_sell)*.5 else 'buying' if recent_buy>recent_sell else 'selling'
 r['summary']=(f"{len(wallets)} large indexed {selection['symbol']} trader addresses in the requested window. Inspect individual buys and sells below." if wallets else 'No qualifying large indexed swaps in this returned window. This does not prove that no whales traded.')
 if not fresh:r['summary']+=' The source head is delayed; this evidence cannot corroborate a current entry.'
 return finish(r)

class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**kw):return None
class BitcoinSource:
 def __init__(self):self.cache={};self.sizes={};self.pending={};self.lock=threading.Lock();self.budget=collections.deque();self.cooldown=0
 def get(self,path,ttl=120):
  assert re.fullmatch(r'blocks|block/[0-9a-f]{64}/txs/[0-9]{1,6}|address/[a-zA-Z0-9]{14,90}/txs/chain',path)
  now=time.monotonic()
  with self.lock:
   old=self.cache.get(path)
   if old and now-old[0]<ttl:return old[1]
   if now<self.cooldown:raise RuntimeError('Bitcoin source is cooling down; retry later.')
   while self.budget and now-self.budget[0]>3600:self.budget.popleft()
   if len(self.budget)>=120:raise RuntimeError('The bounded Bitcoin source read budget is reached.')
   job=self.pending.get(path)
   owner=job is None
   if owner:job=threading.Event();self.pending[path]=job;self.budget.append(now)
  if not owner:
   if not job.wait(4.5):raise RuntimeError('Bitcoin shared read is still pending.')
   with self.lock:cached=self.cache.get(path)
   if cached:return cached[1]
   raise RuntimeError('The shared Bitcoin read is unavailable.')
  req=urllib.request.Request('https://blockstream.info/api/'+path,headers={'Accept':'application/json','User-Agent':'Ticking-readonly-research/1'})
  try:
   with urllib.request.build_opener(NoRedirect).open(req,timeout=4) as response:raw=response.read(2500001)
   if len(raw)>2500000:raise RuntimeError('Bitcoin source response exceeded its byte bound.')
   data=json.loads(raw)
  except urllib.error.HTTPError as e:
   if e.code==429:
    with self.lock:self.cooldown=time.monotonic()+min(900,max(60,int(e.headers.get('Retry-After','120')) if str(e.headers.get('Retry-After','')).isdigit() else 120))
   with self.lock:self.pending.pop(path,None);job.set()
   raise RuntimeError('Bitcoin source is temporarily unavailable; no replacement endpoint is used.') from e
  except BaseException:
   with self.lock:self.pending.pop(path,None);job.set()
   raise
  with self.lock:
   self.cache[path]=(now,data);self.sizes[path]=len(raw)
   while len(self.cache)>12 or sum(self.sizes.values())>3500000:
    key=next(iter(self.cache));self.cache.pop(key);self.sizes.pop(key,None)
   self.pending.pop(path,None);job.set()
  return data
BTC=BitcoinSource()
def bitcoin(selection,cutoff):
 r=base(selection,cutoff);r.update(source='Blockstream Esplora · Bitcoin mainnet',threshold={'value':1,'unit':'BTC absolute address change per sampled transaction'},direction='transfer_only')
 r['coverage']=['Confirmed Bitcoin address movements, not exchange buys or sells. Change outputs and self-transfers can be included.','Addresses are not clustered into people. Inputs and outputs connect through transaction nodes, never an inferred sender/recipient pairing.','Block timestamps are not exact trade times. No independently checked merkle proof or finality claim.','Graph bounds: up to 3 transactions, 24 nodes and 36 edges; large transaction input/output sets can be truncated.']
 try:
  if selection.get('wallet'):
   r['threshold']={'value':.00000001,'unit':'BTC absolute address change per sampled transaction'}
   raw=BTC.get('address/'+selection['wallet']+'/txs/chain',180);scope='At most 25 confirmed transactions for the selected address';raw=raw[:25] if isinstance(raw,list) else []
  else:
   blocks=BTC.get('blocks',180)
   if not isinstance(blocks,list):raise RuntimeError('Invalid Bitcoin block list.')
   blocks=[b for b in blocks[:2] if isinstance(b,dict) and TX.fullmatch(str(b.get('id') or ''))]
   requests=[(b,offset) for b in blocks for offset in sorted({0,max(0,((int(b.get('tx_count',25))-1)//25)*25)})]
   with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:pages=list(pool.map(lambda pair:BTC.get('block/'+pair[0]['id']+'/txs/'+str(pair[1]),3600),requests))
   raw=[t for (b,offset),page in zip(requests,pages) if isinstance(page,list) for t in page[:25] if isinstance(t,dict) and t.get('status',{}).get('block_hash')==b['id'] and t.get('status',{}).get('block_height')==b.get('height')];scope='First and last transaction pages (up to 25 each) from the latest two returned confirmed blocks; not a whole-chain whale ranking'
  changes=[];records={};seen=set()
  for t in raw:
   tx=t.get('txid');status=t.get('status') or {};tm=status.get('block_time');vin=t.get('vin');vout=t.get('vout')
   if not TX.fullmatch(str(tx or '')) or not status.get('confirmed') or not isinstance(tm,int) or tm*1000>cutoff or not isinstance(vin,list) or not isinstance(vout,list) or len(vin)+len(vout)>4000 or any(not isinstance(v,dict) for v in vin+vout) or any(v.get('is_coinbase') for v in vin):continue
   if tx in seen:continue
   seen.add(tx)
   fee=t.get('fee');inputs=[v.get('prevout') or {} for v in vin]
   values=[v.get('value') for v in inputs+vout]
   if not isinstance(fee,int) or isinstance(fee,bool) or fee<0 or any(not isinstance(v,int) or isinstance(v,bool) or not 0<=v<=21000000*100000000 for v in values):continue
   if sum(v['value'] for v in inputs)!=sum(v['value'] for v in vout)+fee or sum(v['value'] for v in inputs)>21000000*100000000:continue
   ins=collections.Counter();outs=collections.Counter()
   for seq,target in [(vin,ins),(vout,outs)]:
    for v in seq:
     p=(v.get('prevout') or {}) if target is ins else v;a=p.get('scriptpubkey_address');value=p.get('value')
     if btc_address(a) and isinstance(value,int) and 0<=value<=21000000*100000000:target[a]+=value
   records[tx]={'tx':tx,'time':tm*1000,'block':status.get('block_height'),'inputs':dict(ins),'outputs':dict(outs)}
   for a in ins.keys()|outs.keys():
    delta=outs[a]-ins[a]
    if selection.get('wallet') and a!=selection['wallet']:continue
    if abs(delta)<(1 if selection.get('wallet') else 1*100000000):continue
    changes.append({'id':'btc-'+tx[:20]+'-'+hashlib.sha256(a.encode()).hexdigest()[:8],'wallet':a,'tx':tx,'time':tm*1000,'side':'received' if delta>0 else 'spent','amountSats':abs(delta),'deltaSats':delta,'block':status.get('block_height'),'estimatedUsd':None,'protocol':'Bitcoin UTXO transaction','attribution':'address change; purchase/sale and beneficial ownership unknown'})
  groups={}
  for e in changes:
   g=groups.setdefault(e['wallet'],{'address':e['wallet'],'category':'selected address activity' if selection.get('wallet') else 'large observed address movement','receivedSats':0,'spentSats':0,'count':0,'lastTime':0});g['receivedSats' if e['deltaSats']>0 else 'spentSats']+=e['amountSats'];g['count']+=1;g['lastTime']=max(g['lastTime'],e['time'])
  r['wallets']=sorted(groups.values(),key=lambda g:-(g['receivedSats']+g['spentSats']))[:8];allowed={w['address'] for w in r['wallets']};r['events']=sorted((e for e in changes if e['wallet'] in allowed),key=lambda e:-e['amountSats'])[:24]
  focus=selection.get('wallet') or (r['wallets'][0]['address'] if r['wallets'] else None);edges=[];nodes={focus} if focus else set()
  relevant=[x for x in records.values() if focus in x['inputs'] or focus in x['outputs']][:3]
  for t in relevant:
   node='tx:'+t['tx'];nodes.add(node)
   for side,key in [('input','inputs'),('output','outputs')]:
    for a,value in sorted(t[key].items(),key=lambda p:-p[1])[:10]:
     if len(nodes)>=24 and a not in nodes or len(edges)>=36:continue
     nodes.add(a);edges.append({'id':t['tx']+':'+side+':'+hashlib.sha256(a.encode()).hexdigest()[:8],'from':a if side=='input' else node,'to':node if side=='input' else a,'time':t['time'],'tx':t['tx'],'amountSats':value,'kind':side})
  r.update(status='available',sourceTime=max((x['time'] for x in records.values()),default=None),graph={'id':'bitcoin-'+hashlib.sha256(json.dumps(edges,sort_keys=True).encode()).hexdigest()[:16],'seed':focus,'edges':edges} if focus else None,scope={'description':scope,'sampledTransactions':len(raw),'qualifyingTransactions':len(records),'returnedEvents':len(r['events']),'graphTruncated':any(len(t['inputs'])>10 or len(t['outputs'])>10 for t in relevant) or len(relevant)>=3})
  r['summary']=f"{len(r['wallets'])} Bitcoin addresses with movements in this confirmed sample. These are BTC transfers, not proven purchases or sales."
  if r['sourceTime'] is None or cutoff-r['sourceTime']>3600000:r['status']='stale';r['summary']+=' The returned sample is older than one hour.'
 except (OSError,ValueError,RuntimeError,KeyError,TypeError) as e:r.update(status='unavailable',summary='Bitcoin address evidence is temporarily unavailable. No unrelated chain or wrapped token was substituted.',error=type(e).__name__)
 return finish(r)

def report(selection,cutoff=None,window=86400,threshold=100000):
 cutoff=cutoff or int(time.time()*1000)
 return bitcoin(selection,cutoff) if selection['network']=='bitcoin' else indexed(selection,cutoff,window,threshold)
def resolve(q,context,rows,aliases):
 selection,choice=choose(q,context,rows,aliases)
 if choice:return choice
 if re.search(r'(?i)strategy|plan (?:a |my )?position|what should i do|should i (?:buy|sell)|전략|추천|진입|어떻게.*(?:해야|할까)|뭘.*(?:해야|할까)',q):
  match=re.search(r'(?i)(1|3|5|15|30)\s*(?:m(?:in(?:ute)?)?|분)(?:봉)?|(1|4)\s*(?:h(?:our)?|시간)(?:봉)?|1\s*(?:d(?:ay)?|일)(?:봉)?',q);tf=((match[1]+'m') if match and match[1] else (match[2]+'h') if match and match[2] else '1d' if match else context.get('interval') if context.get('interval') in ('1m','3m','5m','15m','30m','1h','4h','1d') else '15m');a=selection['asset'];s=selection['symbol']
  workflow={'version':1,'kind':'position','asset':a,'symbol':s,'interval':tf,'side':None,'style':'intraday'}
  return {'state':'chart_workflow','workflow':workflow,'analysisWorkflow':{**workflow,'version':2,'query':q},'route':'#/terminal/'+urllib.parse.quote(a,safe=''),'analysisContext':{'asset':a,'interval':tf,'contextIntervals':[],'triggerInterval':None,'style':'intraday','side':None,'costs':{},'analysisId':None,'selectedSetupId':None},'message':'Checking source candles and separate wallet evidence. Transfers alone cannot justify a direction.'}
 r=report(selection)
 if re.search(r'(?i)buy|bought|buyers|매수|샀|sell|sold|sellers|매도|팔',q):
  if r['network']=='bitcoin':r['summary']='Bitcoin transfers do not disclose a purchase or sale. These are confirmed address movements; trade direction and acquisition price are unknown.'
  else:
   side='sell' if re.search(r'(?i)sell|sold|sellers|매도|팔',q) else 'buy';events=[e for e in r['events'] if e['side']==side];addresses={e['wallet'] for e in events};r['events']=sorted(events,key=lambda e:-e['time']);r['wallets']=sorted([w for w in r['wallets'] if w['address'] in addresses],key=lambda w:-max(e['time'] for e in events if e['wallet']==w['address']));r['summary']=f"{len(r['wallets'])} addresses with recorded {side} actions in this returned evidence window. No missing trade was inferred.";r['scope']['displayFilter']=side
  r.pop('id',None);r=finish(r)
 return {'state':'wallet_research','report':r,'message':r['summary'],'resolver':'economic-machine-wallet-research','modelCalls':0}
