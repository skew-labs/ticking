"""Internal processed-result chart projection, never finalized/shred-joined data.

Reads our existing original journal without another provider connection. Actual
execution metadata and chain block time create bars. Slot-dead/conflicting
block-meta input retracts candidates. Unknown branch binding remains explicit.
"""
import json,sqlite3,time
from functools import lru_cache
from pathlib import Path
from contextlib import contextmanager
from native_solana import decode
from solana_stream import protobuf_receipt,b58encode

class ObservedChart:
 stored_intervals=(60,)
 def __init__(self,path,configs,candidate_selector=None,execution_decoder=None):
  self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.candidate_selector=candidate_selector;self.execution_decoder=execution_decoder;self.configs=configs;self._writer=None
  self.account_encoder=lru_cache(maxsize=16384)(b58encode)
  self.slots={};self.waiting={};self.waiting_count=0;self.metrics={'frames':0,'decoded':0,'failed':0,'unsupported':0,'conflicts':0,'retracted':0,'pendingOverflow':0}
  with self.connect() as c:c.executescript('''PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
   CREATE TABLE IF NOT EXISTS fills(identity TEXT PRIMARY KEY,pool TEXT,time INTEGER,slot INTEGER,tx_order INTEGER,event_order INTEGER,price REAL,base_volume REAL,quote_volume REAL);
   CREATE INDEX IF NOT EXISTS ftime ON fills(pool,time);CREATE INDEX IF NOT EXISTS fslot ON fills(slot);CREATE INDEX IF NOT EXISTS ford ON fills(pool,slot,tx_order,event_order,identity);
   CREATE INDEX IF NOT EXISTS reference_time ON fills(time,slot,tx_order,event_order,identity);
   CREATE TABLE IF NOT EXISTS bars(pool TEXT,interval INTEGER,time INTEGER,open REAL,high REAL,low REAL,close REAL,base_volume REAL,quote_volume REAL,count INTEGER,first_order TEXT,last_order TEXT,PRIMARY KEY(pool,interval,time));
   CREATE TABLE IF NOT EXISTS source(slot INTEGER PRIMARY KEY,time INTEGER,hash TEXT,dead INTEGER);
   CREATE TABLE IF NOT EXISTS pending(signature TEXT PRIMARY KEY,slot INTEGER,info BLOB);
   CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);''')
  with self.connect() as c:
   if c.execute('PRAGMA table_info(pending)').fetchall()[0]['pk']==1 and c.execute('PRAGMA table_info(pending)').fetchall()[1]['pk']==0:
    c.executescript('ALTER TABLE pending RENAME TO pending_old; CREATE TABLE pending(signature TEXT,slot INTEGER,info BLOB,PRIMARY KEY(slot,signature)); INSERT INTO pending SELECT * FROM pending_old; DROP TABLE pending_old;')
 @contextmanager
 def connect(self):
  owned=self._writer is None
  c=sqlite3.connect(self.path,timeout=3) if owned else self._writer;c.row_factory=sqlite3.Row
  try:
   with c:yield c
  finally:
   if owned:c.close()
 @contextmanager
 def writer_session(self):
  """One thread owns the prepared statements/page cache; each batch still commits.

  Keep FULL synchronous WAL commits and SQLite's automatic checkpoints. Avoid
  last-connection checkpoint/teardown and reparsing statements on every tick.
  A failed batch rolls back before this connection is used again.
  """
  if self._writer is not None:raise RuntimeError('Observed writer already owned')
  connection=sqlite3.connect(self.path,timeout=3);connection.row_factory=sqlite3.Row
  connection.execute('PRAGMA synchronous=FULL');self._writer=connection
  try:yield self
  finally:self._writer=None;connection.close()
 @property
 def configs(self):return self._configs
 @configs.setter
 def configs(self,value):
  from server import b58decode
  binary={b58decode(k):cfg for k,cfg in value.items()}
  if any(len(k)!=32 for k in binary):raise ValueError('Invalid observed pool identity')
  if self.candidate_selector:self.candidate_selector.prepare(value)
  if self.execution_decoder:self.execution_decoder.prepare(value)
  self._configs=value;self.binary_configs=binary
 def pool_candidates(self,info):
  if not info.HasField('transaction') or not info.transaction.HasField('message'):return []
  if self.candidate_selector:
   result=self.candidate_selector.candidates(info)
   if result is not None:return result
  keys=dict.fromkeys([*info.transaction.message.account_keys,*info.meta.loaded_writable_addresses,*info.meta.loaded_readonly_addresses])
  return [self.binary_configs[key] for key in keys if key in self.binary_configs]
 def accept(self,c,update,candidates=None):
  from solana_stream import protobuf_modules
  pb,_=protobuf_modules();kind=update.WhichOneof('update_oneof');self.metrics['frames']+=1
  if kind=='slot' and update.slot.status==pb.SLOT_DEAD:
   self.retract(c,int(update.slot.slot));return
  if kind=='block_meta':
   b=update.block_meta;slot=int(b.slot)
   if not b.HasField('block_time') or b.block_time.timestamp<=0:return
   old=c.execute('SELECT * FROM source WHERE slot=?',(slot,)).fetchone()
   if old and old['hash']!=b.blockhash:self.metrics['conflicts']+=1;self.retract(c,slot);return
   if old and old['dead']:return
   c.execute('INSERT OR IGNORE INTO source VALUES(?,?,?,0)',(slot,int(b.block_time.timestamp),b.blockhash))
   stamp=int(b.block_time.timestamp);self.slots[slot]=stamp
   for row in c.execute('SELECT info FROM pending WHERE slot=?',(slot,)).fetchall():
    self.transaction(c,pb.SubscribeUpdateTransactionInfo.FromString(row[0]),slot,stamp)
    if c.execute('SELECT dead FROM source WHERE slot=?',(slot,)).fetchone()[0]:break
   c.execute('DELETE FROM pending WHERE slot=?',(slot,))
   if len(self.slots)>2048:del self.slots[min(self.slots)]
   return
  if kind!='transaction':return
  info=update.transaction.transaction;slot=int(update.transaction.slot)
  if not info.HasField('meta') or info.meta.HasField('err'):self.metrics['failed']+=1;return
  if candidates is None:candidates=self.pool_candidates(info)
  if not candidates:return
  stamp=self.slots.get(slot)
  if stamp is None:
   old=c.execute('SELECT time,dead FROM source WHERE slot=?',(slot,)).fetchone()
   if old and old['dead']:return
   stamp=old['time'] if old else None
  if stamp is None:
   if c.execute('SELECT count(*) FROM pending').fetchone()[0]>=20000:self.metrics['pendingOverflow']+=1;return
   signature=b58encode(bytes(info.signature));encoded=info.SerializeToString();previous=c.execute('SELECT info FROM pending WHERE slot=? AND signature=?',(slot,signature)).fetchone()
   if previous and previous['info']!=encoded:self.metrics['conflicts']+=1;self.retract(c,slot);return
   c.execute('INSERT OR IGNORE INTO pending VALUES(?,?,?)',(signature,slot,encoded));return
  self.transaction(c,info,slot,stamp,candidates)
 def transaction(self,c,info,slot,stamp,candidates=None):
  state=c.execute('SELECT dead FROM source WHERE slot=?',(slot,)).fetchone()
  if state and state['dead']:return
  candidates=candidates if candidates is not None else self.pool_candidates(info)
  if self.execution_decoder:
   decoded=self.execution_decoder.decode_many(info,candidates,slot,stamp)
  else:
   sig,tx=protobuf_receipt(info,slot,stamp,account_encoder=self.account_encoder,binary_instructions=True)
   decoded=[decode(tx,cfg,sig,int(info.index))[0] for cfg in candidates]
  for cfg,rows in zip(candidates,decoded):
   if not rows:self.metrics['unsupported']+=1;continue
   for r in rows:
    previous=c.execute('SELECT * FROM fills WHERE identity=?',(r['id'],)).fetchone()
    if previous:
     expected=(r['id'],cfg['pool'],r['time'],r['slot'],r['order'],0,r['price'],r['baseVolume'],r['volume'])
     if tuple(previous)!=expected:
      self.metrics['conflicts']+=1;self.retract(c,previous['slot']);self.retract(c,slot);return
     continue
    added=c.execute('INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?,?,?,?)',(r['id'],cfg['pool'],r['time'],r['slot'],r['order'],0,r['price'],r['baseVolume'],r['volume'])).rowcount
    if not added:continue
    self.metrics['decoded']+=1;order=f"{r['slot']:020}:{r['order']:010}:000:{r['id']}"
    # Every current reader rolls higher periods up from the canonical minute.
    # Do not rewrite unused 5m/1h materializations for every execution.
    for period in self.stored_intervals:
     t=r['time']//period*period
     c.execute('''INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(pool,interval,time) DO UPDATE SET
       open=CASE WHEN excluded.first_order<first_order THEN excluded.open ELSE open END,
       close=CASE WHEN excluded.last_order>last_order THEN excluded.close ELSE close END,
       high=max(high,excluded.high),low=min(low,excluded.low),base_volume=base_volume+excluded.base_volume,quote_volume=quote_volume+excluded.quote_volume,count=count+1,
       first_order=min(first_order,excluded.first_order),last_order=max(last_order,excluded.last_order)''',(cfg['pool'],period,t,r['price'],r['price'],r['price'],r['price'],r['baseVolume'],r['volume'],1,order,order))
 def retract(self,c,slot):
  self.slots.pop(slot,None);c.execute('DELETE FROM pending WHERE slot=?',(slot,));affected=c.execute('SELECT DISTINCT pool,time FROM fills WHERE slot=?',(slot,)).fetchall()
  self.metrics['retracted']+=c.execute('DELETE FROM fills WHERE slot=?',(slot,)).rowcount
  c.execute("INSERT INTO source VALUES(?,0,'',1) ON CONFLICT(slot) DO UPDATE SET dead=1",(slot,))
  keys={(r['pool'],period,r['time']//period*period) for r in affected for period in (60,300,3600)}
  for pool,period,t in keys:
   c.execute('DELETE FROM bars WHERE pool=? AND interval=? AND time=?',(pool,period,t))
   rows=c.execute('SELECT * FROM fills WHERE pool=? AND time>=? AND time<? ORDER BY slot,tx_order,event_order,identity',(pool,t,t+period)).fetchall()
   if not rows:continue
   order=lambda r:f"{r['slot']:020}:{r['tx_order']:010}:000:{r['identity']}"
   c.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(pool,period,t,rows[0]['price'],max(r['price'] for r in rows),min(r['price'] for r in rows),rows[-1]['price'],sum(r['base_volume'] for r in rows),sum(r['quote_volume'] for r in rows),len(rows),order(rows[0]),order(rows[-1])))
 def history(self,pool,period,limit,before=None):
  bound=before if before is not None else 2**62
  with self.connect() as c:
   buckets=c.execute('SELECT DISTINCT time / ? * ? FROM bars WHERE pool=? AND interval=60 AND time<? ORDER BY 1 DESC LIMIT ?',(period,period,pool,bound,limit+1)).fetchall()
   lower=buckets[-1][0] if buckets else bound;groups={}
   for r in c.execute('SELECT * FROM bars WHERE pool=? AND interval=60 AND time>=? AND time<? ORDER BY time',(pool,lower,bound)):
    t=r['time']//period*period
    if before is not None and t+period>bound:continue
    b=groups.get(t)
    if b is None:groups[t]=dict(time=t,open=r['open'],high=r['high'],low=r['low'],close=r['close'],baseVolume=r['base_volume'],volume=r['quote_volume'],count=r['count'])
    else:b.update(high=max(b['high'],r['high']),low=min(b['low'],r['low']),close=r['close'],baseVolume=b['baseVolume']+r['base_volume'],volume=b['volume']+r['quote_volume'],count=b['count']+r['count'])
  rows=[groups[t] for t in sorted(groups)];result=rows[-limit:]
  return {'candles':result,'identity':{'network':'solana','pool':pool},'sequence':int(time.time()*1000),'coverage':{'complete':False,'commitment':'processed_observation','shredJoinVerified':False},'hasMore':len(rows)>limit,'nextBefore':result[0]['time'] if result else None}

class ObservedChartReader(ObservedChart):
 """Readonly chart adapter; never migrates schema or acquires the writer lock."""
 def __init__(self,path):self.path=Path(path)
 @contextmanager
 def connect(self):
  c=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3);c.row_factory=sqlite3.Row
  try:
   c.execute('PRAGMA query_only=ON');deadline=time.monotonic()+3;c.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
   with c:yield c
  finally:c.close()
