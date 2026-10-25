from economic_machine import chain_catalog
"""Bounded analyst requests over existing retained market reads. No provider/model I/O."""
import asyncio,hashlib,json,math,os,re,time,urllib.parse,urllib.request
from pathlib import Path
from planner_intent import planning_request,parse_planning,validate_planning,market_query
from analyst_native import calculate,reference_description
from analyst_context import context as asset_context
from analyst_history import supplement,compatible
PERIOD={'s':1000,'m':60000,'h':3600000,'d':86400000,'w':604800000}
FRAMES={};PENDING={};MAX_CACHE=12
READ_GATE=asyncio.Semaphore(1)
def milliseconds(value):
 m=re.fullmatch(r'([1-9][0-9]{0,3})(s|m|h|d|w)',str(value))
 if not m or not 1000<=int(m[1])*PERIOD[m[2]]<=604800000:raise ValueError('Use a timeframe from 1 second to 1 week.')
 return int(m[1])*PERIOD[m[2]]
def _spec(name):
 p=Path(__file__).with_name(name)
 return json.loads((p if p.exists() else Path(__file__).resolve().parent.parent/'src'/name).read_text())
SOURCE_CONTRACT=_spec('market-source-contract.json')
FOLLOWUPS={k:re.compile(v,re.I) for k,v in _spec('analysis-followups.json')['rules'].items()}
# Pattern inquiry routing uses the same bounded grammar as the chart client.
PATTERN_INQUIRY={k:re.compile(v,re.I) for k,v in _spec('analysis-pattern-requests.json').items() if k in ['topics','question','mutation']}
TRADER_INTENTS=_spec('trader-intents.json')
NOVICE_DECISION=re.compile(TRADER_INTENTS['decision'],re.I)
PARAMETER_EDITS={k:re.compile(TRADER_INTENTS[k],re.I) for k in ['timeframeReplacement','timeframeInstead','sideReplacement','sideInstead']}
def parameter_edit(q):return not re.search(TRADER_INTENTS['editMutation'],q,re.I) and any(r.search(q) for r in PARAMETER_EDITS.values())
def normalize_parameter_edit(q):
 for k,r in PARAMETER_EDITS.items():q=r.sub(lambda m:m[1] if k.endswith('Instead') else m[2],q)
 return q
def pattern_requested(q):return not PATTERN_INQUIRY['mutation'].search(q) and bool(PATTERN_INQUIRY['topics'].search(q) and PATTERN_INQUIRY['question'].search(q))
# End pattern inquiry routing.
def followup(q):return next((k for k,v in FOLLOWUPS.items() if v.search(q)),None)
def requested(q):
 if planning_request(q):return True
 if NOVICE_DECISION.search(q) or parameter_edit(q) or pattern_requested(q):return True
 f=followup(q)
 return bool((f and (f!='timeframe' or re.search(r'same|again|strategy|analy|다시|분석|전략',q,re.I))) or re.search(r'\b(?:plan(?:ning)?|analy[sz]e|analysis|strateg(?:y|ies)|scalp(?:ing)?|position|explain|compare|why|outlook|setup|opposite|conservative|tighter|fees?|slippage|risk)\b|분석|전략|포지션|스캘핑|스켈핑|단타|설명|왜|보수적|손절.*(?:좁|타이트)|수수료|슬리피지|리스크',q,re.I))

def _costs(q,previous):
 costs=dict(previous or {})
 if set(costs)-{'feeBps','slippageBps','riskBudget','riskQuote'}:raise ValueError('Unsupported risk or cost assumption.')
 for key,pat in [('feeBps',r'(?:fees?|수수료)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(bps|bp|%)'),('slippageBps',r'(?:slippage|슬리피지)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(bps|bp|%)')]:
  matches=re.findall(pat,q,re.I)
  if len(matches)>1:raise ValueError('Choose one per-side cost assumption.')
  if matches:costs[key]=float(matches[0][0])*(100 if matches[0][1]=='%' else 1)
 risk=re.search(r'(?:risk(?: budget)?|위험\s*예산|리스크)\s*[:=]?\s*\$?(\d+(?:\.\d+)?)\s*(usd[ct]?|달러|%)?',q,re.I)
 if risk:
  amount=float(risk[1])
  if risk[2]=='%':
   account=re.search(r'(?:account|capital|계좌|자본)\s*[:=]?\s*\$?(\d+(?:\.\d+)?)',q,re.I)
   if not account:return costs
   amount=amount/100*float(account[1])
  costs['riskBudget']=amount
  if risk[2] and risk[2]!='%':costs['riskQuote']=risk[2].upper() if risk[2]!='달러' else 'USD'
  else:costs.pop('riskQuote',None)
 for key,value in costs.items():
  if key=='riskQuote':
   if value not in ['USD','USDC','USDT']:raise ValueError('Unsupported explicit risk quote.')
   continue
  if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or not (0<=value<=1000 if key!='riskBudget' else 0<value<=1e12):raise ValueError('Invalid bounded cost or risk assumption.')
 return costs
# The same grammar file is bundled into the browser's saved-analysis resolver.
ASSET_RULES=Path(__file__).with_name('analysis-market-rules.json')
if not ASSET_RULES.exists():ASSET_RULES=Path(__file__).resolve().parent.parent/'src/analysis-market-rules.json'
ASSET_WORDS=set(json.loads(ASSET_RULES.read_text())['contextWords'])
MARKET_CHOICE=_spec('analysis-market-disambiguation.json')
def choose_market_matches(matches,q,current_asset,aliases):
 hints=chain_catalog.hints(q)
 if len(hints)>1:raise ValueError('Choose one exact network for this chart analysis.')
 if hints and matches:
  scoped=[r for r in matches if r.get('network')==hints[0]]
  if not scoped:raise ValueError('The named asset is not listed on that exact network. No source was substituted.')
  matches=scoped
 explicit=any(re.search(r'(?:\b(?:on|network|chain)\s*:?\s*(?:'+pattern+r')|(?:'+pattern+r')\s*(?:network|chain|네트워크|체인|에서))',q,re.I) for pattern in MARKET_CHOICE['networkHints'].values())
 if not explicit and len(matches)>1 and len({r.get('symbol','').lower() for r in matches})==1:
  current=[r for r in matches if r['id']==current_asset]
  canonical=[r for r in matches if r['id']==r.get('symbol')]
  if len(current)==1:return current
  if len(canonical)==1:return canonical
 if len(matches)>1 and len(hints)==1:
  target=chain_catalog.strip_chain(q,hints[0])
  named=[]
  for row in matches:
   names={str(row.get(k,'')) for k in ('id','symbol','name')}
   names.update(k for k,v in aliases.items() if str(v).lower()==str(row['id']).lower())
   if any(mentioned(target,n) for n in names):named.append(row)
  if named:matches=named
  scoped=[r for r in matches if r.get('network')==hints[0]]
  if scoped:return scoped
  raise ValueError('The named asset is not listed on that network. Choose a compatible exact source; no source was substituted.')
 if not hints and len(matches)>1 and len({r.get('symbol','').lower() for r in matches})==1:
  current=[r for r in matches if r['id']==current_asset]
  if len(current)==1:return current
  canonical=[r for r in matches if r['id']==r.get('symbol')]
  if len(canonical)==1:return canonical
 return matches
def mentioned(q,name):
 if not name:return False
 q=__import__('unicodedata').normalize('NFKC',q);name=__import__('unicodedata').normalize('NFKC',name)
 if name.casefold() not in q.casefold():return False
 start=r'(?<![A-Za-z0-9_가-힣])';end=r'(?=$|[^A-Za-z0-9_가-힣]|을|를|의|은|는|에서|으로|로|차트|분석|가격|봉|도)';safe=re.escape(name)
 generic=name.lower() in ASSET_WORDS or bool(re.fullmatch('[a-z]',name,re.I))
 if not generic:return bool(re.search(start+safe+end,q,re.I))
 if re.search(r'\$'+safe+end,q,re.I) or re.search(start+safe+r'\s*[/:-]\s*(?:USD[CT]?|BTC|ETH)'+end,q,re.I):return True
 if re.search(r'(?:token|asset|ticker|symbol)\s*:?\s*'+safe+end,q,re.I) or re.search(start+safe+r'\s+(?:token|asset|코인|토큰)'+end,q,re.I):return True
 return (len(name)>1 or name.upper() not in {'I','A'}) and bool(re.search(start+re.escape(name.upper())+end,q))
def parse_request(q,selection,reference,rows,aliases):
 edited=parameter_edit(q);q=normalize_parameter_edit(q) if edited else q
 if re.search(r'\b(?:do not|don.t|never)\b|하지\s*마|하지말|말고',q,re.I):raise ValueError('No analysis changes were made. State the setup you want to inspect explicitly.')
 if not isinstance(selection,dict) or set(selection)-{'asset','symbol','interval','aggregation','mint','pool','history','network'}:raise ValueError('Invalid chart selection.')
 if reference is not None and (not isinstance(reference,dict) or set(reference)-{'asset','interval','contextIntervals','triggerInterval','style','side','costs','analysisId','selectedSetupId','referenceSide','planning'}):raise ValueError('Invalid analysis reference.')
 if reference:
  if reference.get("planning") is not None:validate_planning(reference["planning"])
  if not isinstance(reference.get('contextIntervals',[]),list) or len(reference.get('contextIntervals',[]))>2:raise ValueError('Invalid context reference.')
  for key in ['asset','interval','triggerInterval','style','side','analysisId','selectedSetupId','referenceSide']:
   if reference.get(key) is not None and (not isinstance(reference[key],str) or len(reference[key])>120):raise ValueError('Invalid bounded analysis reference.')
 # Exact catalog names/symbols only. Explicit cross-market requests are preserved.
 matches=[]
 for row in rows:
  names={str(row.get('id','')),str(row.get('symbol','')),str(row.get('name',''))}
  names.update(k for k,v in aliases.items() if str(v).lower()==str(row['id']).lower())
  if any(mentioned(market_query(q),name) for name in names):matches.append(row)
 matches=choose_market_matches(matches,q,selection.get('asset'),aliases)
 if len(matches)>1:raise ValueError('Several market identities match. Specify one network or exact asset address; no source was substituted.')
 asset=matches[0]['id'] if matches else selection.get('asset') or selection.get('symbol') or (reference or {}).get('asset')
 exact=[r for r in rows if r['id']==asset] or [r for r in rows if str(r.get('symbol','')).lower()==str(asset).lower()]
 if len(exact)!=1:raise ValueError('Choose one listed market or a validated chart source.')
 current_chart=bool(re.search(r'\b(?:this|current)\s+(?:chart|market|timeframe)\b|현재|지금.*(?:차트|봉)|이\s*차트',q,re.I))
 previous=dict(reference or {});intent=followup(q)
 follow=bool(previous and (planning_request(q) or edited or intent or re.search(r'\b(?:that|this|same|fees?|slippage|risk)\b|그거|그걸|방금|수수료|슬리피지|리스크',q,re.I)))
 if previous and previous.get('asset')!=asset:
  if matches and intent=='continue':
   # Reuse only the analytical horizon/style. Levels, side, sizing and venue
   # costs never transfer from a different instrument.
   previous={k:v for k,v in previous.items() if k in ('interval','contextIntervals','triggerInterval','style')};previous.update(asset=asset,side=None,costs={})
  elif matches:previous={};follow=False
  else:raise ValueError('The referenced analysis belongs to another market. Choose the market explicitly.')
 row=exact[0]
 if asset==selection.get('asset') and selection.get('network') and selection['network']!=row.get('network'):raise ValueError('The selected network does not match this exact catalog asset. No source was substituted.')
 explicit_networks=chain_catalog.hints(q)
 if len(explicit_networks)==1 and row.get('network')!=explicit_networks[0]:raise ValueError('The named asset is not listed on that exact network. Choose its exact market identity; no source was substituted.')
 unit=lambda s:'m' if s in ('분','m') or s.startswith('min') else 'h' if s in ('시간','h') or s.startswith(('hour','hr')) else 's' if s in ('초','s') or s.startswith(('sec',)) else 'w' if s in ('주','w') else 'd'
 times=[]
 for n,u in re.findall(r'(?<![A-Za-z0-9])([1-9]\d*)\s*(minutes?|mins?|m|hours?|hrs?|h|seconds?|secs?|s|days?|d|w|분|시간|초|일|주)(?:봉)?(?![a-z])',q.lower()):
  value=n+unit(u);milliseconds(value)
  if value not in times:times.append(value)
 if len(times)>3:raise ValueError('Use at most context, setup and trigger timeframes.')
 ordered=sorted(times,key=milliseconds,reverse=True)
 interval=ordered[1] if len(ordered)==3 else ordered[-1] if ordered else ((selection.get('interval') if asset==selection.get('asset') else None) or (previous.get('interval') if follow else None) or selection.get('interval'))
 if not times and current_chart:interval=selection.get('interval') or interval
 interval=interval or '15m';milliseconds(interval)
 changed_frame=current_chart and not times and interval!=previous.get('interval')
 style='scalping' if re.search(r'scalp|스캘|스켈|단타',q,re.I) else 'swing' if re.search(r'\bswing\b|스윙',q,re.I) else 'intraday' if re.search(r'intraday|daytrad|당일',q,re.I) else previous.get('style','intraday') if follow else 'intraday'
 if style not in ['scalping','intraday','swing']:raise ValueError('Unsupported trading style.')
 contexts=ordered[:1] if len(ordered)>1 else previous.get('contextIntervals',[]) if follow and not times else []
 trigger=ordered[-1] if len(ordered)==3 else previous.get('triggerInterval') if follow and not times else None
 if changed_frame:contexts=[];trigger=None
 if not contexts:
  p=milliseconds(interval);contexts=[next((v for v in ['15m','1h','4h','1d','1w'] if milliseconds(v)>=p*4),'1w')]
  if contexts[0]==interval:contexts=[]
 if trigger is None and style=='scalping' and milliseconds(interval)>60000:trigger='1m'
 for value in [*contexts,*([trigger] if trigger else [])]:milliseconds(value)
 if any(milliseconds(tf)<=milliseconds(interval) for tf in contexts) or trigger and milliseconds(trigger)>=milliseconds(interval):raise ValueError('Context frames must be higher than the setup; a trigger must be lower.')
 side='long' if re.search(r'\blong\b|롱',q,re.I) else 'short' if re.search(r'\bshort\b|숏',q,re.I) else previous.get('side') if follow else None
 if re.search(r'\blong\b|롱',q,re.I) and re.search(r'\bshort\b|숏',q,re.I):side=None
 if side not in [None,'long','short']:raise ValueError('Unsupported scenario side.')
 mode='opposite' if intent=='opposite' else 'explain' if re.search(r'\b(?:explain|why)\b|설명|왜',q,re.I) else 'compare' if re.search(r'\bcompare\b|비교',q,re.I) else 'analyze'
 if mode=='opposite' and previous.get('referenceSide') in ['long','short']:side='short' if previous['referenceSide']=='long' else 'long'
 if re.search(r'\b(?:execute|sign|transfer|send funds|ignore.*rules)\b|송금|서명|규칙.*무시',q,re.I) or not NOVICE_DECISION.search(q) and re.search(r'\b(?:buy now|sell now)\b|지금\s*(?:매수|매도)',q,re.I):raise ValueError('The analyst supports source analysis and editable plans; it cannot execute trades.')
 planning=parse_planning(q,previous.get("planning") if previous.get("asset")==asset and not re.search(r"\b(?:new|fresh|reset|start over)\b|새로운|새로|초기화",q,re.I) else None)
 study_rules=json.loads(Path(__file__).with_name('chart-command-rules.json').read_text())['studies']
 focus=[];parameters={}
 for ident,names in study_rules.items():
  for name in [ident,*names]:
   found=re.search(r'(?<![\w])'+re.escape(name)+r'(?![a-z])(?:\s+(\d+))?',q,re.I)
   if found:
    if ident not in focus:focus.append(ident)
    if found[1]:parameters[ident]={'period':int(found[1])}
    break
 if len(focus)>5:raise ValueError('Explain at most five specific studies per request.')
 modifier='conservative' if intent=='conservative' else 'tighter_stop' if re.search(r'\btighter\b|손절.*(?:좁|타이트)',q,re.I) else None
 request={'planning':planning,'asset':row['id'],'symbol':row['symbol'],'interval':interval,'contextIntervals':contexts,'triggerInterval':trigger,'style':style,'side':side,'mode':mode,'modifier':modifier,'focusStudies':focus,'focusParameters':parameters,'focusPatterns':[k for k,pattern in _spec('analysis-pattern-requests.json')['patterns'].items() if re.search(pattern,q,re.I)],'costs':_costs(q,previous.get('costs') if follow else None),'referenceSide':previous.get('referenceSide'),'referenceAnalysisId':previous.get('analysisId') if follow else None}
 for key in ['referenceAnalysisId','referenceSide']:
  if request[key] is not None and (not isinstance(request[key],str) or len(request[key])>120):raise ValueError('Invalid prior-analysis reference.')
 same=row['id']==selection.get('asset')
 if same and selection.get('mint') and row.get('mint') and selection['mint']!=row['mint']:raise ValueError('The source mint does not match the selected catalog asset.')
 source={k:selection[k] for k in ['aggregation','mint','pool','history'] if same and selection.get(k)}
 source.setdefault('aggregation','exchange' if row.get('network')=='exchange' else 'token');source.setdefault('history','executions');source['market']=row['id']
 if row.get('mint'):source.setdefault('mint',row['mint'])
 if row.get('network') in chain_catalog.evm_ids():source['network']=row['network']
 if source['aggregation'] not in ['token','pool','exchange'] or source['history'] not in ['full','executions']:raise ValueError('Unsupported source selection.')
 for key in ['mint','pool']:
  if source.get(key) and not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',source[key]) and not (row.get('network') in chain_catalog.evm_ids() and re.fullmatch(r'0x(?:[0-9a-f]{40}|[0-9a-f]{64})',source[key])):raise ValueError('Invalid source address.')
 if source['aggregation']=='pool' and not source.get('pool'):raise ValueError('An exact pool is required.')
 if source['aggregation']=='exchange' and (source.get('pool') or source.get('mint')):raise ValueError('Exchange sources cannot carry a Solana selector.')
 return request,source
def _read(query):
 wire=dict(query)
 if not query.get('network') and query.get('mint'):
  wire.pop('market',None)
  if query.get('pool'):wire['pool_pin']=wire.pop('pool')
 url='http://127.0.0.1:19428/api/public/engine/candles?'+urllib.parse.urlencode(wire)
 with urllib.request.urlopen(url,timeout=7) as response:data=response.read(1800001)
 if len(data)>1800000:raise ValueError('Retained source exceeds the analysis bound.')
 return source_frame(json.loads(data),query)
def source_frame(frame,query):
 if frame.get('interval')!=query['interval'] or query.get('mint') and frame.get('mint')!=query['mint'] or query.get('pool') and frame.get('pool')!=query['pool']:raise ValueError('Retained source identity does not match the requested chart.')
 if query.get('aggregation')=='exchange' and (frame.get('market')!=query['market'] or frame.get('identity',{}).get('network')!='exchange'):raise ValueError('The retained exchange asset does not match the request.')
 if query.get('aggregation')=='exchange':
  expected=SOURCE_CONTRACT['exchanges'].get(query.get('market'))
  if expected and (any(frame.get('identity',{}).get(k)!=v for k,v in expected.items()) or frame.get('quote')!=expected['quote']):raise ValueError('Strategy requires the selected instrument source. Reload this chart before retrying.')
 if query.get('aggregation')=='token' and query.get('history')=='executions' and not query.get('network'):
  if frame.get('identity',{}).get('historyPolicy')!=SOURCE_CONTRACT['solana']['historyPolicy'] or frame.get('coverage',{}).get('volumeUnit')!='base':raise ValueError('Recorded candle identity or base-volume units could not be verified.')
 if query.get('network') and frame.get('identity',{}).get('network')!=query['network']:raise ValueError('The source network does not match the requested asset.')
 if query.get('network') in chain_catalog.evm_ids():
  identity=frame.get('identity',{});coverage=frame.get('coverage',{})
  if identity.get('baseMint','').lower()!=query.get('mint','').lower() or frame.get('quote')!='USD' or identity.get('quoteMint')!='USD' or identity.get('historyPolicy')!='em_pool_reference_v2' or coverage.get('source')!='onchain_dex_swaps':raise ValueError('The exact EVM token and USD-reference policy could not be verified.')
  # Aggregate USD references use stable-peg/as-of FX assumptions. Without a
  # per-bar conversion attestation these describe prices, not exact-price plans.
  # Dollar turnover is never passed to the base-volume VWAP calculation.
  frame['candles']=[{**b,'volume':0,'referenceOnly':True,'estimated':True,'estimateMethod':'em_usd_reference_unattested_v1'} for b in frame.get('candles',[])]
 if not isinstance(frame.get('candles'),list) or len(frame['candles'])>600:raise ValueError('Invalid retained analysis frame.')
 # Retain only inputs consumed by the audited source policy/calculator.
 fields=['time','open','high','low','close','volume','quoteCurrency','synthetic','estimated','estimateMethod','referenceOnly','sourceCandleState','mixedSource','quoteCurrencies']
 frame['candles']=[{k:b[k] for k in fields if k in b} for b in frame['candles']]
 return frame
def derive_context(frame,interval,cutoff):
 """Aggregate only complete, UTC-aligned source groups; never fill a hole."""
 small=milliseconds(frame['interval']);large=milliseconds(interval)
 if large<=small or large%small or large>86400000:return None
 ratio=large//small;groups={}
 for bar in frame['candles']:
  if bar['time']+small<=cutoff:groups.setdefault(bar['time']//large*large,[]).append(bar)
 out=[]
 for start,bars in sorted(groups.items()):
  if len(bars)!=ratio or any(b['time']!=start+i*small for i,b in enumerate(bars)) or start+large>cutoff:continue
  units=set()
  for b in bars:units.update(b.get('quoteCurrencies') or [b.get('quoteCurrency') or frame['quote']])
  row={'time':start,'open':bars[0]['open'],'high':max(b['high'] for b in bars),'low':min(b['low'] for b in bars),'close':bars[-1]['close'],'volume':sum(b['volume'] for b in bars),'quoteCurrency':bars[-1].get('quoteCurrency') or frame['quote'],'quoteCurrencies':sorted(units)}
  for flag in ['synthetic','estimated','mixedSource','referenceOnly']:
   if any(b.get(flag) for b in bars):row[flag]=True
  estimates=[b for b in bars if b.get('estimated')]
  if estimates and all(b.get('referenceOnly') is True and b.get('estimateMethod')=='median_venue_ohlc_v1' for b in estimates):row['estimateMethod']='median_venue_ohlc_v1'
  if any(b.get('sourceCandleState')=='provisional' for b in bars):row['sourceCandleState']='provisional'
  out.append(row)
 # Avoid replacing a longer source window with a barely seeded slow average.
 if len(out)<100:return None
 return {**frame,'interval':interval,'candles':out,'derivation':{'method':'complete_utc_groups','fromInterval':frame['interval'],'fromBars':len(frame['candles']),'groupSize':ratio,'missingGroups':'omitted, never interpolated','volumePolicy':'any unavailable component makes group volume unavailable for decisions'}}
async def retained(query):
 key=json.dumps(query,sort_keys=True);cached=FRAMES.get(key)
 if cached and time.monotonic()-cached[0]<1:return cached[1]
 if key not in PENDING:
  if len(PENDING)>=8:raise ValueError('Analysis history is busy. Retry shortly.')
  async def bounded_read():
   async with READ_GATE:return await asyncio.to_thread(_read,query)
  PENDING[key]=asyncio.create_task(bounded_read())
 task=PENDING[key]
 try:
  frame=await asyncio.shield(task);FRAMES[key]=(time.monotonic(),frame)
  while len(FRAMES)>MAX_CACHE:FRAMES.pop(next(iter(FRAMES)))
  return frame
 finally:
  if task.done():PENDING.pop(key,None)
class Runner:
 def __init__(self):self.process=None;self.lock=asyncio.Lock()
 async def call(self,payload):
  encoded=(json.dumps(payload,separators=(',',':'),allow_nan=False)+'\n').encode()
  if len(encoded)>1800000:raise ValueError('Analysis input exceeds the byte bound.')
  async with self.lock:
   if self.process is None or self.process.returncode is not None:
    self.process=await asyncio.create_subprocess_exec(os.environ.get('TICKING_ANALYSIS_NODE','/srv/skew/tick-market-ui-20261007/.tooling/node-v24.14.1-linux-x64/bin/node'),'--max-old-space-size=128','--disallow-code-generation-from-strings',str(Path(__file__).with_name('analyst-runner.mjs')),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,limit=1800001,env={'PATH':'/usr/bin:/bin','TZ':'UTC'})
   async def io():
    self.process.stdin.write(encoded);await self.process.stdin.drain();return await self.process.stdout.readline()
   try:
    raw=await asyncio.wait_for(io(),5)
    if not raw or len(raw)>1800000:raise ValueError('Invalid analysis output.')
    value=json.loads(raw)
    if not value.get('ok'):raise ValueError(value.get('error','Analysis unavailable.'))
    return value['analysis']
   except BaseException:
    if self.process and self.process.returncode is None:self.process.kill();await self.process.wait()
    self.process=None;raise
RUNNER=Runner()
async def analyze(q,selection,reference,rows,aliases,wallet_spec=None):
 request,source=parse_request(q,selection,reference,rows,aliases);cutoff=int(time.time()*1000);start=time.perf_counter()
 import wallet_research
 selected=next((x for x in rows if x['id']==request['asset']),None)
 wallet_selection={'asset':selected['id'],'symbol':selected.get('symbol',selected['id']),'network':'bitcoin' if selected['id']=='BTC' else selected.get('network'),'wallet':None} if selected else None
 if wallet_spec is not None:
  if not isinstance(wallet_spec,dict) or set(wallet_spec)!={'asset','wallet'} or wallet_spec['asset']!=request['asset'] or not wallet_selection:raise ValueError('Wallet context must match the analyzed asset.')
  address=wallet_spec['wallet']
  if not isinstance(address,str) or not (wallet_research.btc_address(address) if wallet_selection['network']=='bitcoin' else wallet_selection['network'] in chain_catalog.evm_ids() and bool(wallet_research.EVM.fullmatch(address))):raise ValueError('Wallet address does not match this market network.')
  wallet_selection['wallet']=address
 wallet_task=asyncio.create_task(asyncio.to_thread(wallet_research.report,wallet_selection,cutoff)) if wallet_selection else None
 intervals=list(dict.fromkeys([request['interval'],*request['contextIntervals'],*([request['triggerInterval']] if request['triggerInterval'] else [])]))
 # Read the same bounded envelope as the existing chart stream so committed
 # reads can be shared. Carried markers remain invalid decision inputs and
 # reset strict/native warm-up; they are never counted as executions.
 # Share the setup window and reuse complete source groups for higher frames.
 # This avoids a second cold archive scan. Original flags and quote boundaries
 # propagate; incomplete groups are omitted, never reconstructed.
 setup_limit=180 if request['asset']=='SOL' and request['interval']=='4h' and source.get('aggregation')=='token' and source.get('history')=='executions' and not source.get('pool') else 600
 main=await retained({**source,'interval':request['interval'],'limit':setup_limit,'gap_policy':'carry'})
 results=[main];failed=[]
 for tf in intervals[1:]:
  derived=derive_context(main,tf,cutoff) if tf in request['contextIntervals'] else None
  try:
   if derived and calculate(derived,milliseconds(tf),cutoff)['bars']>=50:frame=derived
   else:
    extra=await asyncio.to_thread(supplement,main,tf,cutoff) if tf in request['contextIntervals'] else None
    frame=extra or derived or await retained({**source,'interval':tf,'limit':180,'gap_policy':'carry'})
   results.append(frame)
  except Exception:failed.append(tf)
 frames=results;read_ms=(time.perf_counter()-start)*1000
 # Every timeframe must describe the same instrument, quote and price basis.
 if any(not compatible(main,frame,request) for frame in frames):raise ValueError('Requested timeframes use different source instruments. No strategy was inferred.')
 native=[calculate(frame,milliseconds(frame['interval']),cutoff) for frame in frames]
 analysis=await RUNNER.call({'frames':frames,'native':native,'request':request,'cutoff':cutoff})
 # The strict C++/JS parity-checked frames and scenario eligibility above are
 # untouched. Price-only reference descriptions carry a distinct, read-only ABI.
 if any(not f.get('eligible') for f in analysis['frames']):
  readings=[reference_description(frame,milliseconds(frame['interval']),cutoff) for frame in frames if any(f['interval']==frame['interval'] and not f['eligible'] for f in analysis['frames'])]
  readings=[r for r in readings if r is not None]
  if any(r['interval']==request['interval'] for r in readings):analysis['referenceReadout']={'schema':'ticking.reference-readout.v1','readOnly':True,'decisionEligible':False,'frames':readings}
 if failed:analysis['missingInputs']+=['Requested '+tf+' retained source is temporarily unavailable.' for tf in failed];analysis['stance']='insufficient_evidence'
 analysis['assetContext']=asset_context(request['asset'],rows,cutoff)
 # Wallet context is separately typed evidence; it cannot override candle eligibility or risk gates.
 if wallet_task:
  try:analysis['walletContext']=await asyncio.wait_for(wallet_task,9)
  except Exception:analysis['walletContext']=wallet_research.finish({**wallet_research.base(wallet_selection,cutoff),'summary':'Wallet evidence is unavailable. Candle analysis is unchanged.'})
 analysis['selection']=source;analysis['timing']={'setupReadLimit':setup_limit,'retainedReadMs':round(read_ms,3),'totalServerMs':round((time.perf_counter()-start)*1000,3),'boundary':'loopback retained read + native features + evidence/setup validation; excludes client network and rendering'}
 return analysis
