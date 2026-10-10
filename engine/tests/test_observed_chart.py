import tempfile,unittest
from pathlib import Path
from observed_chart import ObservedChart,ObservedChartReader
from solana_stream import protobuf_modules
from test_solana_stream import block,dead,CFG,POOL

class ObservedProjection(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'chart.db';self.p=ObservedChart(self.path,{POOL:CFG});self.pb,_=protobuf_modules()
 def tearDown(self):self.tmp.cleanup()
 def tx(self,u=None):
  b=(u or block()).block;r=self.pb.SubscribeUpdate();r.transaction.slot=b.slot;r.transaction.transaction.CopyFrom(b.transactions[0]);return r
 def meta(self,hash='block-A'):
  r=self.pb.SubscribeUpdate();r.block_meta.slot=100;r.block_meta.blockhash=hash;r.block_meta.block_time.timestamp=1710000000;return r
 def accept(self,u):
  with self.p.connect() as c:self.p.accept(c,u)
 def test_persistent_writer_commits_cursor_and_fills_before_session_closes(self):
  with self.p.writer_session():
   with self.p.connect() as c:
    self.assertEqual(c.execute('PRAGMA synchronous').fetchone()[0],2)
    self.p.accept(c,self.meta());self.p.accept(c,self.tx());c.execute("INSERT INTO state VALUES('cursor','first')")
   reader=ObservedChartReader(self.path)
   self.assertEqual(reader.history(POOL,60,600)['candles'][0]['count'],1)
   with self.assertRaisesRegex(RuntimeError,'abort'):
    with self.p.connect() as c:
     c.execute("UPDATE state SET value='uncommitted' WHERE key='cursor'");raise RuntimeError('abort')
   with self.p.connect() as c:self.assertEqual(c.execute("SELECT value FROM state WHERE key='cursor'").fetchone()[0],'first')
  self.p=ObservedChart(self.path,{POOL:CFG});self.accept(self.tx())
  self.assertEqual(self.p.history(POOL,60,600)['candles'][0]['count'],1)
 def test_persistent_writer_rejects_nested_ownership_and_releases_on_failure(self):
  import sqlite3
  with self.assertRaisesRegex(RuntimeError,'abort'):
   with self.p.writer_session():
    with self.p.connect() as connection:connection.execute("INSERT INTO state VALUES('committed','yes')")
    with self.assertRaisesRegex(RuntimeError,'already owned'):
     with self.p.writer_session():pass
    raise RuntimeError('abort')
  with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
  with self.p.writer_session():
   with self.p.connect() as c:self.assertEqual(c.execute("SELECT value FROM state WHERE key='committed'").fetchone()[0],'yes')
 def test_hard_exit_recovers_committed_wal_and_discards_uncommitted_batch(self):
  import subprocess,sys
  script='''
import os,sys
from observed_chart import ObservedChart
from solana_stream import protobuf_modules
from test_solana_stream import block,CFG,POOL
p=ObservedChart(sys.argv[1],{POOL:CFG});pb,_=protobuf_modules()
meta=pb.SubscribeUpdate();meta.block_meta.slot=100;meta.block_meta.blockhash='block-A';meta.block_meta.block_time.timestamp=1710000000
tx=pb.SubscribeUpdate();tx.transaction.slot=100;tx.transaction.transaction.CopyFrom(block().block.transactions[0])
with p.writer_session():
 with p.connect() as c:
  p.accept(c,meta);p.accept(c,tx);c.execute("INSERT INTO state VALUES('cursor','durable')")
  if sys.argv[2]=='uncommitted':os._exit(0)
 os._exit(0)
'''
  for committed in (False,True):
   path=Path(self.tmp.name)/('hard-exit-'+str(committed)+'.sqlite3')
   subprocess.run([sys.executable,'-c',script,str(path),'committed' if committed else 'uncommitted'],check=True,timeout=30)
   restored=ObservedChart(path,{POOL:CFG})
   with restored.connect() as c:
    self.assertEqual(c.execute('SELECT count(*) FROM fills').fetchone()[0],int(committed))
    self.assertEqual(c.execute("SELECT value FROM state WHERE key='cursor'").fetchone() is not None,committed)
 def test_execution_before_block_time_survives_restart(self):
  self.accept(self.tx());self.assertFalse(self.p.history(POOL,60,600)['candles'])
  self.p=ObservedChart(self.path,{POOL:CFG});self.accept(self.meta());bar=self.p.history(POOL,60,600)['candles'][0]
  self.assertEqual(bar['count'],1);self.assertAlmostEqual(bar['close'],.1)
 def test_duplicate_failed_and_missing_metadata_do_not_create_volume(self):
  self.accept(self.meta());self.accept(self.tx());self.accept(self.tx());b=block();b.block.transactions[0].meta.err.err=b'failed';self.accept(self.tx(b));self.assertEqual(self.p.history(POOL,60,600)['candles'][0]['count'],1)
 def test_dead_and_conflicting_slot_retract_bar_and_cannot_resurrect(self):
  self.accept(self.meta());self.accept(self.tx());self.accept(dead());self.assertEqual(self.p.history(POOL,60,600)['candles'],[]);self.accept(self.tx());self.assertEqual(self.p.history(POOL,60,600)['candles'],[])
  self.p=ObservedChart(Path(self.tmp.name)/'other.db',{POOL:CFG});self.accept(self.meta());self.accept(self.tx());self.accept(self.meta('block-B'));self.assertFalse(self.p.history(POOL,60,600)['candles'])
 def test_source_contract_never_claims_exact_shred_join_or_finality(self):
  self.accept(self.meta());self.accept(self.tx());d=self.p.history(POOL,60,600)
  self.assertFalse(d['coverage']['shredJoinVerified']);self.assertEqual(d['coverage']['commitment'],'processed_observation')
 def test_conflicting_repeated_signature_quarantines_both_slots(self):
  self.accept(self.meta());self.accept(self.tx());other=self.meta();other.block_meta.slot=101;self.accept(other)
  tx=self.tx();tx.transaction.slot=101;self.accept(tx)
  self.assertFalse(self.p.history(POOL,60,600)['candles']);self.assertEqual(self.p.metrics['conflicts'],1)
 def test_pending_same_signature_is_kept_per_slot(self):
  self.accept(self.tx());tx=self.tx();tx.transaction.slot=101;self.accept(tx)
  with self.p.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM pending').fetchone()[0],2)
 def test_chart_reader_cannot_write_or_initialize_an_archive(self):
  import sqlite3
  self.accept(self.meta());self.accept(self.tx());reader=ObservedChartReader(self.path)
  self.assertEqual(reader.history(POOL,60,600)['candles'][0]['count'],1)
  with reader.connect() as c:
   with self.assertRaises(sqlite3.OperationalError):c.execute('DELETE FROM fills')
 def test_conflict_while_draining_pending_never_crashes_or_resurrects_slot(self):
  self.accept(self.meta());self.accept(self.tx())
  tx=self.tx();tx.transaction.slot=101;self.accept(tx)
  another=self.tx();another.transaction.slot=101;another.transaction.transaction.signature=b'X'*64;another.transaction.transaction.transaction.signatures[0]=b'X'*64;self.accept(another)
  meta=self.meta();meta.block_meta.slot=101;self.accept(meta)
  self.assertFalse(self.p.history(POOL,60,600)['candles'])
  with self.p.connect() as c:
   self.assertEqual(c.execute('SELECT count(*) FROM pending').fetchone()[0],0)
   self.assertEqual(c.execute('SELECT dead FROM source WHERE slot=101').fetchone()[0],1)
