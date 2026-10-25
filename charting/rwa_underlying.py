"""Exact mapped underlying observations. Hot GETs never perform source I/O.

Issuer quotes supply a validated token unit ratio only; their generated time is
never substituted for an exchange observation. No orders, credentials or writes.
"""
import concurrent.futures,copy,json,math,re,threading,time,urllib.request
from datetime import datetime,timezone
import equity_compare
BUILD='rwa-underlying-20261024-01'
SCHEMA='ticking.rwa-underlying.v1'
XAUT0='arbitrum:0x40461291347e1ecbb09499f3371d3f17f10d7159'
GOLD_FEED='0x1F954Dc24a49708C26E0C1777f16750B5C6d5a2c'
GOLD_RPC='https://arb1.arbitrum.io/rpc'
GOLD_SOURCE='https://data.chain.link/feeds/arbitrum/mainnet/xau-usd'
XAUT_SOURCE='https://usdt0.gitbook.io/xaut0/technical-documentation/developer'
XAUT_UNIT_SOURCE='https://usdt0.to/ecosystem/arbitrum'
EVM=re.compile(r'^(arbitrum|robinhood):0x[0-9a-f]{40}$')
SOL=re.compile(r'^solana:[1-9A-HJ-NP-Za-km-z]{32,44}$')

def positive(v):return type(v) in (int,float) and math.isfinite(v) and v>0

def exact_identity(value):
 if not isinstance(value,str) or len(value)>100 or not (EVM.fullmatch(value) or SOL.fullmatch(value)):raise ValueError('Pass one exact mapped chain:contract id.')
 return value

def base(identity):return {'schema':SCHEMA,'readerBuild':BUILD,'id':identity,'network':identity.split(':')[0],'state':'unavailable','multiplier':None,'actualUnderlyingReference':None}

def unavailable(identity,reason):return {**base(identity),'reason':reason}

def mapped_entry(identity,legacy):
 exact_identity(identity)
 if identity==XAUT0:return {'id':identity,'network':'arbitrum','address':identity.split(':')[1],'underlying':'XAU','symbol':'XAUt0'}
 ent=(getattr(legacy,'MAP',{}) or {}).get(identity)
 if not isinstance(ent,dict) or ent.get('id')!=identity or ent.get('network') not in ('robinhood','solana'):return None
 chain,address=identity.split(':',1)
 if ent.get('network')!=chain or ent.get('address')!=address or not re.fullmatch(r'[A-Z0-9.^=-]{1,16}',str(ent.get('underlying') or '')):return None
 return dict(ent)

def source_windows(meta):
 out={}
 for name in ('pre','regular','post'):
  w=(meta.get('currentTradingPeriod') or {}).get(name) or {};start,end=w.get('start'),w.get('end')
  if positive(start) and positive(end) and start<end:out[name]={'start':int(start*1000),'end':int(end*1000)}
 return out

def stock_observations(identity,ent,chart,ratio,now):
 meta=chart.get('meta') or {};underlying=ent['underlying'];multiplier=ratio.get('multiplier')
 if meta.get('symbol')!=underlying or meta.get('currency')!='USD':return unavailable(identity,'underlying_identity_or_currency_mismatch')
 if not positive(multiplier) or not ratio.get('unit_source'):return unavailable(identity,'validated_token_unit_ratio_unavailable')
 times=chart.get('timestamp') or [];closes=((chart.get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
 if not isinstance(times,list) or not isinstance(closes,list) or len(times)>10000 or len(closes)>10000:return unavailable(identity,'source_series_exceeds_bound')
 points=[(int(t*1000),price) for t,price in zip(times,closes) if positive(t) and positive(price) and t*1000<=now+5000]
 periods=source_windows(meta);market=next((n for n,w in periods.items() if w['start']<=now<w['end']),'outside_source_session_windows') if periods else 'unknown'
 actual={'state':'available','currency':'USD','symbol':underlying,'source':'Yahoo Finance chart (unofficial; may be delayed)','sourceUrl':'https://finance.yahoo.com/quote/'+underlying+'/history/','unit':'USD/share','marketState':market,'kind':'exchange_observation','exchange':meta.get('fullExchangeName'),'timezone':meta.get('exchangeTimezoneName'),'officialClosingAuction':False,'sessionWindows':periods,'timestampMeaning':'source minute-bar observation, not issuer generation time'}
 if points:
  at,price=max(points,key=lambda x:x[0]);actual['latestObservation']={'price':price,'asOf':at,'fresh':0<=now-at<=120000,'observationKind':'last_chart_bar_including_extended_hours'}
 rp,rt=meta.get('regularMarketPrice'),meta.get('regularMarketTime')
 if positive(rp) and positive(rt) and rt*1000<=now+5000:
  actual['regularObservation']={'price':rp,'asOf':int(rt*1000),'fresh':0<=now-rt*1000<=120000,'observationKind':'source_regular_market_metadata'}
 if not actual.get('latestObservation') and actual.get('regularObservation'):actual['latestObservation']=copy.deepcopy(actual['regularObservation'])
 if not actual.get('latestObservation'):return unavailable(identity,'independent_underlying_observation_unavailable')
 return {**base(identity),'state':'available','underlying':underlying,'multiplier':multiplier,'unitSource':ratio['unit_source'],'unitReadAt':now,'unitEffectiveAt':ratio.get('unit_effective'),'unitValidUntil':ratio.get('unit_valid_until'),'unitMeaning':'underlying shares per current token unit; not a historical multiplier claim','actualUnderlyingReference':actual,'issuerReference':{'tradingHalt':ratio.get('ref_halted') is True,'exchangeObservationAt':None},'readAt':now}

def rpc_json(payload):
 request=urllib.request.Request(GOLD_RPC,json.dumps(payload).encode(),{'Content-Type':'application/json','User-Agent':'Ticking-readonly-underlying/1'})
 with urllib.request.urlopen(request,timeout=3) as response:raw=response.read(64001)
 if len(raw)>64000:raise ValueError('Oracle response exceeded bound.')
 rows=json.loads(raw)
 if not isinstance(rows,list) or len(rows)!=4:raise ValueError('Oracle batch shape mismatch.')
 values={row.get('id'):row for row in rows if isinstance(row,dict)}
 if set(values)!={1,2,3,4} or any(x.get('error') for x in values.values()):raise ValueError('Oracle batch unavailable.')
 return values

def decode_gold(rows,now):
 if int(rows[1]['result'],16)!=42161:raise ValueError('Oracle RPC returned another chain.')
 decimals=int(rows[2]['result'],16)
 raw=bytes.fromhex(rows[3]['result'][2:]);offset=int.from_bytes(raw[:32],'big')
 if offset!=32 or len(raw)>1024 or len(raw)<64:raise ValueError('Oracle description ABI mismatch.')
 length=int.from_bytes(raw[offset:offset+32],'big');description=raw[offset+32:offset+32+length].decode()
 if description!='XAU / USD' or decimals!=8:raise ValueError('Oracle pair or unit mismatch.')
 data=bytes.fromhex(rows[4]['result'][2:])
 if len(data)!=160:raise ValueError('Oracle round ABI mismatch.')
 round_id,answer,started,updated,answered=[int.from_bytes(data[i:i+32],'big') for i in range(0,160,32)]
 if answer>>255:answer-=1<<256
 if answer<=0 or not updated or updated<started or answered<round_id or updated*1000>now+5000:raise ValueError('Oracle round is invalid.')
 price=answer/(10**decimals)
 if not positive(price):raise ValueError('Oracle value is invalid.')
 return {'price':price,'asOf':updated*1000,'fresh':0<=now-updated*1000<=120000,'roundId':str(round_id),'answeredInRound':str(answered),'startedAt':started*1000,'withinHeartbeat':0<=now-updated*1000<=86400000}

def gold_reference(now):
 calls=[{'jsonrpc':'2.0','id':1,'method':'eth_chainId','params':[]}]+[{'jsonrpc':'2.0','id':i,'method':'eth_call','params':[{'to':GOLD_FEED,'data':selector},'latest']} for i,selector in [(2,'0x313ce567'),(3,'0x7284e416'),(4,'0xfeaf968c')]]
 quote=decode_gold(rpc_json(calls),now)
 return {**base(XAUT0),'state':'available','underlying':'XAU','multiplier':1.0,'unitSource':XAUT_UNIT_SOURCE,'unitReadAt':now,'unitMeaning':'one troy ounce of physical gold per XAUt0 token; not a GLD ETF share','conversion':{'troyOuncesPerToken':1,'gramsPerToken':'31.1034768','source':XAUT_UNIT_SOURCE},'actualUnderlyingReference':{'state':'available','currency':'USD','symbol':'XAU','source':'Chainlink XAU / USD standard oracle','sourceUrl':GOLD_SOURCE,'unit':'USD/troy_ounce','kind':'oracle','marketState':'oracle_reference','latestObservation':quote,'heartbeatSeconds':86400,'feed':GOLD_FEED,'chainId':42161,'timestampMeaning':'oracle round updatedAt, not an exchange trade timestamp'},'readAt':now}

def refresh(identity,legacy):
 now=int(time.time()*1000);ent=mapped_entry(identity,legacy)
 if not ent:return unavailable(identity,'exact_underlying_or_unit_mapping_unavailable')
 if identity==XAUT0:return gold_reference(now)
 chart=legacy.yahoo(ent['underlying'],'1m')
 if not isinstance(chart,dict) or not chart.get('ok'):return unavailable(identity,(chart or {}).get('reason') or 'underlying_source_unavailable')
 ratio=equity_compare.reference(ent,legacy)
 return stock_observations(identity,ent,chart.get('r') or {},ratio,now)

class UnderlyingReader:
 def __init__(self,legacy,clock=time.monotonic,now=lambda:int(time.time()*1000),workers=2):
  self.legacy=legacy;self.clock=clock;self.now=now;self.lock=threading.Lock();self.cache={};self.pending=set();self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=workers,thread_name_prefix='rwa-underlying-read')
 def _fetch(self,identity):
  try:value=refresh(identity,self.legacy)
  except Exception as error:value=unavailable(identity,'underlying_source_error:'+type(error).__name__)
  with self.lock:
   self.cache[identity]={'checked':self.clock(),'access':self.clock(),'value':value}
   self.pending.discard(identity)
 def snapshot(self,identity):
  exact_identity(identity)
  if not mapped_entry(identity,self.legacy):return unavailable(identity,'exact_underlying_or_unit_mapping_unavailable')
  with self.lock:
   hit=self.cache.get(identity);now=self.clock()
   if hit:hit['access']=now
   if (not hit or now-hit['checked']>=20) and identity not in self.pending and len(self.pending)<16:
    if len(self.cache)+sum(key not in self.cache for key in self.pending)>=64 and not hit:
     eligible=[key for key in self.cache if key not in self.pending]
     if eligible:self.cache.pop(min(eligible,key=lambda key:self.cache[key]['access']))
     else:return {**base(identity),'state':'refresh_pending','retryAfterS':2,'reason':'bounded_reader_busy'}
    self.pending.add(identity);self.pool.submit(self._fetch,identity)
   value=copy.deepcopy(hit['value']) if hit else {**base(identity),'state':'refresh_pending','retryAfterS':2}
   value['refreshing']=identity in self.pending
  now=self.now();value['servedAt']=now;actual=value.get('actualUnderlyingReference')
  if actual:
   for key in ('latestObservation','regularObservation'):
    quote=actual.get(key)
    if quote:
     quote['ageS']=round((now-quote['asOf'])/1000,3);quote['fresh']=0<=now-quote['asOf']<=120000
     if actual.get('kind')=='oracle':quote['withinHeartbeat']=0<=now-quote['asOf']<=actual['heartbeatSeconds']*1000
  return value

_readers={};_readers_lock=threading.Lock()
def read_exact(identity,legacy):
 with _readers_lock:
  key=id(legacy)
  if key not in _readers:_readers[key]=UnderlyingReader(legacy)
  reader=_readers[key]
 return reader.snapshot(identity)
