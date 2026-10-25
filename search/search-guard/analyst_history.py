"""Read-only exact-token context supplement, never a setup or execution feed."""
import hashlib,json,math,sqlite3
from pathlib import Path
ROOT=Path('/srv/skew/solana-mingle-20261007')
POLICY='registered_pool_daily_context_v1'
SOL='So11111111111111111111111111111111111111112'
USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
POOL='58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2'
def supplement(main,tf,cutoff,root=ROOT):
 # A named approval, never a generic fallback to a same-symbol token/quote.
 i=main.get('identity',{})
 if tf!='1d' or main.get('interval')!='4h' or main.get('pool') or i.get('network')!='solana' or i.get('baseMint')!=SOL or i.get('quoteMint')!=USDC or main.get('quote')!='USDC':return None
 store=root/'data/analysis-context-history';db=store/'context.sqlite3'
 if not db.exists():return None
 with sqlite3.connect('file:'+str(root/'data/solana-history/history.sqlite3')+'?mode=ro',uri=True,timeout=1) as c:
  c.row_factory=sqlite3.Row;record=c.execute('SELECT * FROM pools WHERE pool=?',(POOL,)).fetchone()
 if not record:return None
 config=record['config'];cfg=json.loads(config)
 if {cfg.get('mint'),cfg.get('quoteMint')}!={SOL,USDC}:return None
 config_hash=hashlib.sha256(config.encode()).hexdigest()
 with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True,timeout=1) as c:
  c.row_factory=sqlite3.Row
  rows=c.execute('SELECT * FROM bars WHERE network=? AND mint=? AND quote=? AND pool=? AND period=86400 AND time+86400<=? ORDER BY time DESC LIMIT 180',('solana',SOL,USDC,POOL,cutoff//1000)).fetchall()[::-1]
  receipts={x['sha']:dict(x) for x in c.execute('SELECT * FROM receipts WHERE sha IN ('+','.join('?' for _ in {r['receipt'] for r in rows})+')',tuple({r['receipt'] for r in rows}))} if rows else {}
 if len(rows)<50 or rows[-1]['time']<(cutoff//86400000-2)*86400:return None
 # Only the last uninterrupted run can confirm the current higher frame.
 last=rows[-1]['time'];tail=[]
 for row in reversed(rows):
  if row['time']!=last-len(tail)*86400:break
  tail.append(row)
 if len(tail)<50:return None
 rows=tail[::-1];checked={};candles=[]
 for row in rows:
  sha=row['receipt']
  if sha not in checked:
   rec=receipts.get(sha)
   if not rec:return None
   raw=(store/'originals'/(sha+'.json')).read_bytes()
   if len(raw)>1048576 or hashlib.sha256(raw).hexdigest()!=sha:return None
   payload=json.loads(raw);overlap=json.loads(rec['overlap'])
   if {payload['meta']['base']['address'],payload['meta']['quote']['address']}!={SOL,USDC} or overlap.get('nativePoolConfigSha256')!=config_hash or overlap.get('compared',0)<3 or overlap.get('compared')!=overlap.get('withinTwoPercent') or overlap.get('purpose')!='higher_timeframe_context_only':return None
   if not rec['url'].startswith('https://api.geckoterminal.com/api/v2/networks/solana/pools/'+POOL+'/ohlcv/day?') or 'currency=token&token='+SOL not in rec['url']:return None
   checked[sha]={int(b[0]):b for b in payload['data']['attributes']['ohlcv_list']}
  original=checked[sha].get(row['time'])
  vals=[row[k] for k in ['open','high','low','close','provider_volume']]
  if not original or vals!=original[1:] or not all(math.isfinite(v) for v in vals) or min(vals[:4])<=0 or vals[4]<=0:return None
  candles.append({'time':row['time']*1000,**{k:row[k] for k in ['open','high','low','close']},'volume':0,'quoteCurrency':'USDC','referenceOnly':True})
 provenance={'policy':POLICY,'role':'context','provider':'GeckoTerminal','network':'solana','mint':SOL,'quoteMint':USDC,'quote':'USDC','pool':POOL,'poolConfigSha256':config_hash,'receipts':sorted(checked),'lastReceived':max(r['received'] for r in rows)*1000,'volume':'excluded_unverified_unit','overlap':'at_least_three_native_daily_closes_within_2_percent','conversion':'none'}
 identity={'network':'solana','baseMint':SOL,'quoteMint':USDC,'market':POOL,'pool':POOL,'series':'reference','aggregation':POLICY,'historyPolicy':POLICY}
 return {'mode':'asset_history','market':SOL,'aggregation':'token','quoteMint':USDC,'interval':tf,'quote':'USDC','mint':SOL,'pool':POOL,'identity':identity,'candles':candles,'coverage':{'volumeUnit':'unavailable','scope':'provider pool OHLC, higher timeframe context only'},'contextSource':provenance,'stale':False,'hasMore':False}
def compatible(main,frame,request):
 if frame.get('identity')==main.get('identity') and frame.get('quote')==main.get('quote'):return True
 p=frame.get('contextSource',{});a=main.get('identity',{});b=frame.get('identity',{})
 return bool(request['interval']=='4h' and frame['interval']=='1d' and '1d' in request['contextIntervals'] and p.get('policy')==POLICY and p.get('role')=='context' and main.get('pool') is None and a.get('network')==b.get('network')=='solana' and a.get('baseMint')==b.get('baseMint')==p.get('mint')==SOL and a.get('quoteMint')==b.get('quoteMint')==p.get('quoteMint')==USDC and main.get('quote')==frame.get('quote')==p.get('quote')=='USDC' and p.get('pool')==b.get('pool')==POOL and p.get('conversion')=='none' and p.get('volume')=='excluded_unverified_unit')
