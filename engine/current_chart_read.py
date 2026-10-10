"""Readonly finalized prefix and explicitly provisional observer suffix.

Source union uses transaction + pool identity, not an entire minute. The
existing observer interpretation wins only its own transaction/pool overlap;
distinct retained finalized transactions repair even partially observed minutes.
Both source ledgers stay unchanged. Provisional fork coverage remains disclosed.
"""
import sqlite3,time
from contextlib import contextmanager
from pathlib import Path
from solana_history_read import ColdHistoryReader
from chart_intervals import bucket_start,bucket_sql,WEEK

class CurrentChartReader(ColdHistoryReader):
 stored_intervals=(60,)
 source_union_policy='observed_transaction_pool_precedence_v2'
 # Count source ranges directly. A correlated count over the UNION view makes
 # SQLite build a global temporary index for each mint, even for a single pool.
 pool_bar_count_sql='''((SELECT count(*) FROM main.bars b WHERE b.pool=p.pool AND b.interval=60
   AND NOT EXISTS(SELECT 1 FROM observed.bars o WHERE o.pool=p.pool AND o.interval=60 AND o.time=b.time))
   +(SELECT count(*) FROM observed.bars o WHERE o.pool=p.pool AND o.interval=60))'''
 def __init__(self,path,observed):super().__init__(path);self.observed=Path(observed)
 @contextmanager
 def connect(self):
  c=sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True,timeout=3);c.row_factory=sqlite3.Row
  try:
   c.execute('ATTACH DATABASE ? AS observed',('file:'+str(self.observed)+'?mode=ro',))
   c.executescript('''CREATE TEMP VIEW bars AS
    SELECT pool,interval,time,open,high,low,close,base_volume,quote_volume,count,'finalized' commitment FROM main.bars b
      WHERE interval=60 AND NOT EXISTS(SELECT 1 FROM observed.bars o WHERE o.pool=b.pool AND o.interval=60 AND o.time=b.time)
    UNION ALL SELECT pool,interval,time,open,high,low,close,base_volume,quote_volume,count,'processed_observation' FROM observed.bars WHERE interval=60;
    CREATE TEMP VIEW fills AS
    SELECT identity,pool,signature,time,slot,tx_order,event_order,price,base_volume,quote_volume,seq,'finalized' commitment FROM main.fills f
      WHERE NOT EXISTS(SELECT 1 FROM observed.fills o WHERE o.identity=f.signature||':'||f.pool)
    UNION ALL SELECT identity,pool,substr(identity,1,instr(identity,':')-1),time,slot,tx_order,event_order,price,base_volume,quote_volume,0,'processed_observation' FROM observed.fills;
   ''')
   c.execute('PRAGMA query_only=ON')
   with c:yield c
  finally:c.close()
 def minute_rows(self,c,pool,lower,bound):
  """Use cached minutes except where BOTH ledgers have executions.

  The bars view is the cheap minute inventory. Rebuild only intersecting
  minutes from the transaction-deduplicated fills view. Each query has a pool
  and a bounded time range, using the existing source indexes.
  """
  rows={r['time']:dict(r) for r in c.execute('SELECT * FROM bars WHERE pool=? AND interval=60 AND time>=? AND time<? ORDER BY time',(pool,lower,bound))}
  overlap=[r[0] for r in c.execute('''SELECT b.time FROM main.bars b JOIN observed.bars o
    ON o.pool=b.pool AND o.interval=60 AND o.time=b.time
    WHERE b.pool=? AND b.interval=60 AND b.time>=? AND b.time<? ORDER BY b.time''',(pool,lower,bound))]
  windows=[]
  for stamp in overlap:
   if windows and windows[-1][1]==stamp:windows[-1][1]=stamp+60
   else:windows.append([stamp,stamp+60])
  for start,end in windows:
   rebuilt={}
   for f in c.execute('SELECT * FROM fills WHERE pool=? AND time>=? AND time<? ORDER BY slot,tx_order,event_order,identity',(pool,start,end)):
    stamp=f['time']//60*60;b=rebuilt.get(stamp)
    if b is None:rebuilt[stamp]=dict(pool=pool,interval=60,time=stamp,open=f['price'],high=f['price'],low=f['price'],close=f['price'],base_volume=f['base_volume'],quote_volume=f['quote_volume'],count=1,commitment=f['commitment'])
    else:
     b.update(high=max(b['high'],f['price']),low=min(b['low'],f['price']),close=f['price'],base_volume=b['base_volume']+f['base_volume'],quote_volume=b['quote_volume']+f['quote_volume'],count=b['count']+1)
     if f['commitment']!='finalized':b['commitment']=f['commitment']
   rows.update(rebuilt)
  return [rows[t] for t in sorted(rows)]
 def resolve(self,*args,**kwargs):
  result=super().resolve(*args,**kwargs)
  with self.connect() as c:
   for p in result['pools']:
    span=c.execute('SELECT max(time) FROM observed.fills WHERE pool=?',(p['pool'],)).fetchone()[0]
    p['observedThrough']=span
  result['observationAvailable']=any(p['observedThrough'] for p in result['pools'])
  return result
 def history(self,pool,interval=60,limit=300,before=None):
  if not 60<=interval<=WEEK or interval%60:raise ValueError('Unsupported history interval')
  limit=max(1,min(int(limit),600));bound=before if before is not None else 2**62
  with self.connect() as c:
   c.execute('BEGIN');cfg=c.execute('SELECT * FROM main.pools WHERE pool=?',(pool,)).fetchone()
   if not cfg:raise ValueError('Unverified pool')
   buckets=c.execute(f'SELECT DISTINCT {bucket_sql(interval)} bucket FROM bars WHERE pool=? AND time<? ORDER BY bucket DESC LIMIT ?',(pool,bound,limit+1)).fetchall()
   lower=buckets[-1][0] if buckets else bound;groups={}
   for r in self.minute_rows(c,pool,lower,bound):
    t=bucket_start(r['time'],interval)
    if before is not None and t+interval>bound:continue
    b=groups.get(t)
    if b is None:groups[t]=dict(time=t,open=r['open'],high=r['high'],low=r['low'],close=r['close'],baseVolume=r['base_volume'],volume=r['quote_volume'],count=r['count'],commitment=r['commitment'])
    else:
     b.update(high=max(b['high'],r['high']),low=min(b['low'],r['low']),close=r['close'],baseVolume=b['baseVolume']+r['base_volume'],volume=b['volume']+r['quote_volume'],count=b['count']+r['count'])
     if r['commitment']!='finalized':b['commitment']=r['commitment']
   span=c.execute('SELECT min(time),max(time),count(*) FROM bars WHERE pool=?',(pool,)).fetchone()
   last=c.execute('SELECT max(time) FROM observed.fills WHERE pool=?',(pool,)).fetchone()[0]
  bars=[groups[t] for t in sorted(groups)];result=bars[-limit:]
  return {'candles':result,'identity':{'network':'solana','market':pool,'baseMint':cfg['base'],'quoteMint':cfg['quote'],'series':'trade'},
    'sequence':int(time.time()*1000),'hasMore':len(bars)>limit,'nextBefore':result[0]['time'] if result else None,
    'observationAvailable':last is not None,'observedThrough':last,
    'coverage':{'complete':False,'commitment':'finalized_prefix_processed_observation_suffix','source':'retained_execution_results','start':span[0],'end':span[1],'minuteCandles':span[2],
      'canonicalFinalityVerified':False,'shredJoinVerified':False,'sourceUnionPolicy':self.source_union_policy,'seamPolicy':'Deduplicate transaction + pool identity, preserving the observer interpretation on overlap. Distinct finalized transactions repair partial minutes. Coverage and the observed suffix remain provisional; original ledgers are retained.'}}
