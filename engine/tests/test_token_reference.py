from contextlib import closing
import json,sqlite3,tempfile,unittest
from pathlib import Path
from current_chart_read import CurrentChartReader
from observed_chart import ObservedChart
from token_reference import TokenReferenceBuilder,TokenReferenceReader,USDC,quote_rate

TOKEN='token';SOL='sol'
class TokenReference(unittest.TestCase):
 def test_shared_catalog_matches_individual_reads_and_preserves_pending_identity(self):
  self.pool('a');self.fill('a',61,1,10);self.builder.update()
  rows=self.ref.catalog()['tokens']
  self.assertEqual(rows,[self.ref.resolve(mint) for mint in [TOKEN,SOL]])
  self.assertTrue(rows[0]['ready']);self.assertFalse(rows[1]['ready'])
  self.assertEqual(rows[1]['mint'],SOL)
 def test_shared_analytics_matches_recorded_history_without_merging_quote_units(self):
  self.pool('a');self.pool('b',SOL,'other')
  for t in [61,121,181]:self.fill('a',t,1,t);self.fill('b',t,2,t*2)
  self.builder.update();rows={r['mint']:r for r in self.ref.analytics(2)['tokens']}
  for mint in [TOKEN,SOL]:
   d=self.ref.history(mint,60,2);b=d['candles'];r=rows[mint]
   self.assertEqual(r['volume'],sum(x['volume'] for x in b));self.assertEqual(r['fills'],sum(x['count'] for x in b))
   self.assertEqual(r['change'],(b[-1]['close']/b[0]['open']-1)*100)
   self.assertEqual(r['start'],b[0]['time']*1000);self.assertEqual(r['quoteMint'],d['identity']['quoteMint'])
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name);self.cold=root/'cold';self.cold.mkdir()
  with closing(sqlite3.connect(self.cold/'history.sqlite3')) as c, c:
   c.executescript('''CREATE TABLE pools(pool TEXT PRIMARY KEY,base TEXT,quote TEXT,program TEXT,config TEXT);
   CREATE TABLE fills(identity TEXT PRIMARY KEY,pool TEXT,signature TEXT,time INT,slot INT,tx_order INT,event_order INT,price REAL,base_volume REAL,quote_volume REAL,seq INT);
   CREATE TABLE bars(pool TEXT,interval INT,time INT,open REAL,high REAL,low REAL,close REAL,base_volume REAL,quote_volume REAL,count INT);''')
  self.ob=ObservedChart(root/'observed.sqlite3',{});self.reader=CurrentChartReader(self.cold,self.ob.path)
  self.builder=TokenReferenceBuilder(self.reader,root/'ref',[TOKEN,SOL]);self.ref=TokenReferenceReader(root/'ref');self.seq=0
 def tearDown(self):self.builder.close();self.tmp.cleanup()
 def pool(self,name,base=TOKEN,quote=USDC):
  cfg=dict(baseDecimals=6,quoteDecimals=6)
  with closing(sqlite3.connect(self.cold/'history.sqlite3')) as c, c:c.execute('INSERT INTO pools VALUES(?,?,?,?,?)',(name,base,quote,'program',json.dumps(cfg)))
 def fill(self,pool,t,b,q,observed=False):
  self.seq+=1;ident=str(self.seq)+':'+pool
  if observed:
   with self.ob.connect() as c:
    c.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?)',(ident,pool,t,t+100,0,0,q/b,b,q));c.execute('INSERT OR IGNORE INTO source VALUES(?,?,?,?)',(t+100,t,'hash',0))
  else:
   with closing(sqlite3.connect(self.cold/'history.sqlite3')) as c, c:c.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,pool,str(self.seq),t,t+100,0,0,q/b,b,q,self.seq))
 def test_distinct_pool_median_per_second_then_reference_ohlc(self):
  self.pool('a');self.pool('b');self.fill('a',61,1,100);self.fill('b',61,3,360);self.fill('b',62,2,220)
  self.builder.update();d=self.ref.history(TOKEN,60);b=d['candles'][0]
  self.assertEqual((b['open'],b['high'],b['low'],b['close'],b['count'],b['samples']),(110,110,108,108,3,2))
  self.assertEqual((b['baseVolume'],b['volume']),(6,680));self.assertEqual(d['coverage']['pricedPoolCount'],2)
 def test_dust_stays_on_unfiltered_tape_without_setting_reference_extreme(self):
  self.pool('a');self.fill('a',61,1,100);self.fill('a',62,8e-9,2e-6)
  self.builder.update();b=self.ref.history(TOKEN,60)['candles'][0];self.assertEqual(b['high'],100)
  tape=self.ref.trades(TOKEN)['trades'];self.assertEqual(len(tape),2);self.assertAlmostEqual(tape[-1]['price'],250);self.assertFalse(tape[-1]['reference_eligible'])
 def test_quote_conversion_asof_no_future_or_assumed_peg(self):
  self.pool('anchor',SOL,USDC);self.pool('token',TOKEN,SOL);self.pool('unknown',TOKEN,'usdt')
  self.fill('anchor',10,1,100);self.fill('anchor',70,1,200);self.fill('token',61,2,1);self.fill('unknown',61,2,2)
  self.builder.update();d=self.ref.history(TOKEN,60);self.assertEqual(d['candles'][0]['close'],50)
  self.assertEqual(d['coverage']['unpricedExecutions'],1);self.assertEqual(len(self.ref.trades(TOKEN)['trades']),2)
  self.assertIsNone(quote_rate({SOL:([60],[100])},SOL,361));self.assertIsNone(quote_rate({SOL:([60],[100])},SOL,59))
 def test_quote_side_token_inversion_and_completed_anchor(self):
  self.pool('anchor',SOL,USDC);self.pool('invert',SOL,TOKEN);self.fill('anchor',10,1,100);self.fill('invert',61,1,2)
  self.builder.update();self.assertEqual(self.ref.history(TOKEN,60)['candles'][0]['close'],50)
 def test_two_leg_quote_anchor_uses_only_prior_completed_minutes(self):
  self.pool('anchor',SOL,USDC);self.pool('usd1','usd1',SOL);self.pool('token',TOKEN,'usd1')
  self.fill('anchor',10,1,100);self.fill('usd1',61,100,1);self.fill('token',121,2,10)
  self.builder.update();self.assertEqual(self.ref.history(TOKEN,60)['candles'][0]['close'],5)
 def test_new_older_receipt_rebuilds_and_source_unchanged_is_noop(self):
  self.pool('a');self.fill('a',600,1,100);first=self.builder.update();self.assertEqual(self.builder.update(),first)
  self.fill('a',60,1,80);self.builder.update();self.assertEqual([b['close'] for b in self.ref.history(TOKEN,60)['candles']],[80,100])
 def test_late_finalized_trade_repairs_partially_observed_minute(self):
  self.pool('a');self.fill('a',61,1,100,True)
  with self.ob.connect() as c:c.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('a',60,60,100,100,100,100,1,100,1,'x','x'))
  self.fill('a',900,1,101);self.builder.update()
  self.fill('a',65,2,210);self.builder.update()
  b=self.ref.history(TOKEN,60)['candles'][0]
  self.assertEqual((b['count'],b['baseVolume'],b['volume']),(2,3,310))
  self.assertGreater(b['close'],100)
  self.compare_full_bars('partial-minute-replay')
 def test_source_union_revision_invalidates_old_snapshot(self):
  self.pool('a');self.fill('a',61,1,100);self.builder.update()
  with closing(sqlite3.connect(self.builder.path)) as c, c:
   prior=json.loads(c.execute("SELECT value FROM state WHERE key='report'").fetchone()[0]);prior.pop('sourceUnionPolicy')
   c.execute("UPDATE state SET value=? WHERE key='report'",(json.dumps(prior),))
  report=self.builder.update();self.assertFalse(report['incrementalWindows']);self.assertEqual(report['sourceUnionPolicy'],self.reader.source_union_policy)
 def test_incremental_pool_window_equals_full_replay_of_same_inputs(self):
  self.pool('a');self.pool('b')
  for t in range(61,701,7):self.fill('a',t,1,100+t/1000);self.fill('b',t+1,2,200+t/500)
  self.builder.update()
  for t in range(701,941,7):self.fill('a',t,1,100+t/1000);self.fill('b',t+1,2,200+t/500)
  self.builder.update()
  other=TokenReferenceBuilder(self.reader,Path(self.tmp.name)/'full',[TOKEN,SOL])
  try:
   other.update()
   with closing(sqlite3.connect(self.builder.path)) as a, closing(sqlite3.connect(other.path)) as b:
    self.assertEqual(a.execute('SELECT * FROM bars ORDER BY mint,time').fetchall(),b.execute('SELECT * FROM bars ORDER BY mint,time').fetchall())
  finally:other.close()
 def test_dead_source_rebuild_removes_provisional_price_and_tape(self):
  self.pool('a');self.fill('a',61,1,100,True);self.fill('a',62,1,120,True);self.builder.update()
  with self.ob.connect() as c:self.ob.retract(c,162)
  self.builder.update();self.assertEqual(self.ref.history(TOKEN,60)['candles'][0]['close'],100);self.assertEqual(len(self.ref.trades(TOKEN)['trades']),1)
 def test_pool_inventory_removes_last_retracted_fill_and_keeps_new_pool(self):
  self.pool('a');self.pool('b');self.pool('never-traded');self.fill('a',61,1,100);self.fill('b',62,1,120,True);self.builder.update()
  with self.ob.connect() as c:self.ob.retract(c,162)
  self.pool('new');self.fill('new',63,1,110);self.builder.update()
  with closing(sqlite3.connect(self.builder.path)) as c, c:coverage=json.loads(c.execute('SELECT value FROM coverage WHERE mint=?',(TOKEN,)).fetchone()[0])
  self.assertEqual(coverage['observedPools'],['a','new'])
 def test_tape_limit_retains_exact_latest_identity_order_across_updates(self):
  self.pool('a')
  for t in range(60,610):self.fill('a',t,1,100)
  self.builder.update()
  for t in range(610,640):self.fill('a',t,1,100)
  self.builder.update()
  with closing(sqlite3.connect(self.builder.path)) as c, c:
   rows=c.execute('SELECT time FROM tape WHERE mint=? ORDER BY slot,tx_order,event_order,identity',(TOKEN,)).fetchall()
  self.assertEqual(rows,[(t,) for t in range(140,640)])
 def test_week_monday_rollup_and_carry_keeps_zero_execution_count(self):
  from datetime import datetime,timezone
  stamp=lambda s:int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
  self.pool('a');self.fill('a',stamp('2026-10-05'),1,100);self.fill('a',stamp('2026-10-19'),1,120);self.builder.update()
  d=self.ref.history(TOKEN,604800,3,gap_policy='carry');self.assertEqual(len(d['candles']),3)
  self.assertTrue(d['candles'][1]['synthetic']);self.assertEqual(d['candles'][1]['count'],0);self.assertEqual(d['candles'][1]['volume'],0)
  self.assertEqual(d['candles'][0]['time'],stamp('2026-10-05'))
 def test_native_quote_reference_is_explicit_and_remains_pinned(self):
  self.pool('a',TOKEN,'unknown');self.pool('b',TOKEN,'unknown');self.fill('a',61,1,10);self.fill('b',61,3,60)
  self.builder.update();d=self.ref.history(TOKEN,60);self.assertEqual(d['identity']['quoteMint'],'unknown');self.assertEqual(d['candles'][0]['close'],17.5);self.assertEqual(d['coverage']['pricedPoolCount'],2)
  self.pool('anchor','unknown',USDC);self.fill('anchor',610,1,100);self.fill('a',671,1,20);self.builder.update()
  d=self.ref.history(TOKEN,60);self.assertEqual(d['identity']['quoteMint'],'unknown');self.assertEqual(d['candles'][-1]['close'],20)
 def test_sparse_recorded_history_pages_without_losing_old_prefix(self):
  self.pool('a');self.fill('a',60,1,80);self.fill('a',60*10000,1,100);self.builder.update()
  d=self.ref.history(TOKEN,60,1);self.assertTrue(d['hasMore']);old=self.ref.history(TOKEN,60,1,before=d['nextBefore']);self.assertEqual(old['candles'][0]['close'],80);self.assertFalse(old['hasMore'])
 def test_sparse_display_does_not_flatten_months_or_lose_cursor(self):
  self.pool('a');self.fill('a',60,1,80);self.fill('a',60*10000,1,100);self.builder.update()
  d=self.ref.history(TOKEN,60,600,gap_policy='carry')
  self.assertEqual(sum(not b['synthetic'] for b in d['candles']),2)
  self.assertEqual(d['carriedBars'],5);self.assertEqual(d['fillPolicy'],'recorded')
  old=self.ref.history(TOKEN,60,1,before=60*10000,gap_policy='carry')
  self.assertEqual(old['candles'][0]['time'],60);self.assertFalse(old['hasMore'])
 def test_display_page_bound_and_real_trade_totals_survive_gaps(self):
  self.pool('a')
  for t in range(60,6000,600):self.fill('a',t,2,200)
  self.builder.update();d=self.ref.history(TOKEN,60,9,gap_policy='carry')
  self.assertLessEqual(len(d['candles']),9);self.assertTrue(d['hasMore'])
  self.assertEqual(d['candles'][-1]['time'],5460)
  self.assertTrue(all(b['volume']==0 and b['count']==0 for b in d['candles'] if b['synthetic']))
 def test_usdc_reference_does_not_switch_quote_when_anchors_expire(self):
  self.pool('a');self.pool('b',TOKEN,'unknown');self.fill('a',60,1,100);self.builder.update();self.fill('b',900,1,20);self.builder.update()
  d=self.ref.history(TOKEN,60);self.assertEqual(d['identity']['quoteMint'],USDC);self.assertEqual(d['candles'][-1]['close'],100)
 def test_whole_share_asset_precision_accepts_one_atomic_unit(self):
  self.pool('a');
  with closing(sqlite3.connect(self.cold/'history.sqlite3')) as c, c:c.execute('UPDATE pools SET config=?',(json.dumps(dict(baseDecimals=0,quoteDecimals=6)),))
  self.fill('a',61,1,100);self.builder.update();self.assertEqual(self.ref.history(TOKEN,60)['candles'][0]['close'],100)
 def test_both_original_sources_are_readonly(self):
  with self.reader.connect() as c:
   with self.assertRaises(sqlite3.OperationalError):c.execute('DELETE FROM main.fills')
   with self.assertRaises(sqlite3.OperationalError):c.execute('DELETE FROM observed.fills')
 def test_fresh_direct_anchor_outranks_newer_indirect_estimate(self):
  book={SOL:{'direct':([60],[100]),'indirect':([120],[100000])}}
  self.assertEqual(quote_rate(book,SOL,121),100)
  self.assertEqual(quote_rate(book,SOL,361),100000)
 def test_cross_rate_outlier_remains_in_tape_but_not_reference(self):
  self.pool('anchor',SOL,USDC);self.pool('bad','other',USDC);self.pool('token',SOL,'other')
  self.fill('anchor',10,1,100);self.fill('bad',10,1,1000);self.fill('token',61,1,10);self.fill('anchor',61,1,101)
  self.builder.update();d=self.ref.history(SOL,60);self.assertEqual(d['candles'][-1]['high'],101);self.assertEqual(d['coverage']['conversionOutliers'],1)
  self.assertEqual(len(self.ref.trades(SOL)['trades']),3);self.assertFalse(self.ref.trades(SOL)['trades'][-2]['reference_eligible'])
 def test_tiny_usdc_flow_cannot_bypass_reference_rule_via_fallback(self):
  self.pool('a');self.fill('a',61,1,.001);self.builder.update()
  self.assertFalse(self.ref.resolve(TOKEN)['ready']);self.assertEqual(len(self.ref.trades(TOKEN)['trades']),1)
 def test_direct_quote_price_outranks_other_pools_without_losing_volume(self):
  self.pool('anchor',SOL,USDC);self.pool('other','other',USDC);self.pool('swap',SOL,'other')
  self.fill('anchor',10,1,100);self.fill('other',10,1,10);self.fill('swap',61,1,12);self.fill('anchor',61,1,101);self.fill('swap',62,1,13)
  self.builder.update();b=self.ref.history(SOL,60)['candles'][-1]
  self.assertEqual((b['open'],b['high'],b['low'],b['close']),(101,101,101,101));self.assertEqual((b['count'],b['baseVolume'],b['volume']),(3,3,351));self.assertEqual(len(self.ref.trades(SOL)['trades']),4)
 def test_expired_direct_market_cannot_be_repriced_by_illiquid_cross_rates(self):
  self.pool('direct',SOL,USDC);self.pool('other','other',USDC);self.pool('cross',SOL,'other')
  self.fill('direct',10,1,115);self.fill('other',370,1,100);self.fill('cross',431,1,1)
  self.builder.update();d=self.ref.history(SOL,60,8,gap_policy='carry')
  self.assertEqual([b['close'] for b in d['candles']],[115])
  self.assertEqual(d['coverage']['unanchoredPriceSeconds'],1)
  self.assertEqual(len(self.ref.trades(SOL)['trades']),2)
  self.fill('direct',440,1,116);self.builder.update();d=self.ref.history(SOL,60,8,gap_policy='carry')
  self.assertEqual(d['candles'][-1]['close'],116)
  self.assertTrue(any(b['synthetic'] for b in d['candles']))
  self.assertTrue(all(b['low']>=115 for b in d['candles']))
 def test_direct_quote_known_survives_incremental_suffix_without_anchor(self):
  self.pool('direct',SOL,USDC);self.pool('other','other',USDC);self.pool('cross',SOL,'other')
  self.fill('direct',10,1,115);self.builder.update()
  self.fill('other',1000,1,100);self.fill('cross',1061,1,1);self.builder.update()
  self.fill('other',2000,1,100);self.fill('cross',2061,1,1);self.builder.update()
  d=self.ref.history(SOL,60)
  self.assertEqual([b['close'] for b in d['candles']],[115]);self.assertTrue(d['coverage']['directQuoteObserved'])
 def test_expired_usdc_pool_does_not_suppress_actual_fresh_sol_pool(self):
  from token_reference import SOL_MINT
  self.pool('old');self.pool('sol',SOL_MINT,USDC);self.pool('active',TOKEN,SOL_MINT)
  self.fill('old',10,1,10);self.fill('sol',370,1,100);self.fill('active',431,2,.22)
  self.builder.update();d=self.ref.history(TOKEN,60)
  self.assertEqual(d['candles'][-1]['close'],11)
  self.assertEqual(d['coverage']['bridgePriceSeconds'],1)
  self.assertEqual(d['identity']['quoteMint'],USDC)
 def test_active_bridge_does_not_wait_300_seconds_for_stale_direct_sample(self):
  from token_reference import SOL_MINT
  self.pool('old');self.pool('sol',SOL_MINT,USDC);self.pool('active',TOKEN,SOL_MINT)
  self.fill('old',10,1,10);self.fill('sol',10,1,100)
  self.fill('active',61,2,.22);self.fill('active',69,2,.24)
  self.builder.update();b=self.ref.history(TOKEN,60)['candles'][-1]
  self.assertEqual((b['open'],b['close']),(11,12));self.assertEqual(b['samples'],2)
 def test_bridge_without_fresh_direct_quote_does_not_generate_price(self):
  from token_reference import SOL_MINT
  self.pool('old');self.pool('sol',SOL_MINT,USDC);self.pool('active',TOKEN,SOL_MINT)
  self.fill('old',10,1,10);self.fill('sol',10,1,100);self.fill('active',431,2,.22)
  self.builder.update();self.assertEqual([b['close'] for b in self.ref.history(TOKEN,60)['candles']],[10])
 def test_interleaved_direct_and_bridge_share_one_causal_pool_window(self):
  from token_reference import SOL_MINT
  self.pool('direct');self.pool('sol',SOL_MINT,USDC);self.pool('bridge',TOKEN,SOL_MINT)
  self.fill('sol',10,1,100)
  self.fill('direct',61,1,100);self.fill('bridge',62,1,1.02)
  self.fill('direct',63,1,100);self.fill('bridge',64,1,1.02)
  self.builder.update();b=self.ref.history(TOKEN,60)['candles'][-1]
  self.assertEqual((b['open'],b['high'],b['low'],b['close']),(100,101,100,101))
  self.assertEqual(b['count'],4)
  self.assertEqual(len(self.ref.trades(TOKEN)['trades']),4)
  self.compare_full_bars('unified-window')
 def compare_full_bars(self,name='full-window-check'):
  full=TokenReferenceBuilder(self.reader,Path(self.tmp.name)/name,[TOKEN,SOL])
  try:
   full.update()
   with closing(sqlite3.connect(self.builder.path)) as a, closing(sqlite3.connect(full.path)) as b:
    self.assertEqual(a.execute('SELECT * FROM bars ORDER BY mint,time').fetchall(),b.execute('SELECT * FROM bars ORDER BY mint,time').fetchall())
  finally:full.close()
 def test_late_backfill_rebuilds_disjoint_windows_not_whole_day(self):
  self.pool('a');self.pool('b')
  for t in range(61,30062,30):self.fill('a',t,1,100+t/100000)
  self.builder.update()
  self.fill('b',121,3,306);self.fill('a',30062,1,101)
  report=self.builder.update()
  self.assertEqual(len(report['rebuiltWindows']),2)
  self.assertLess(report['sourceRows'],150)
  self.compare_full_bars()
 def test_late_anchor_correction_reaches_two_leg_conversion_horizon(self):
  self.pool('anchor',SOL,USDC);self.pool('bridge','other',SOL);self.pool('target',TOKEN,'other')
  for t in range(61,6001,30):
   self.fill('anchor',t,1,100+t/100000)
   self.fill('bridge',t+61,10,1)
   self.fill('target',t+122,2,10)
  self.builder.update()
  self.fill('anchor',121,5,505);self.fill('anchor',1201,5,510);self.fill('target',7000,2,10)
  report=self.builder.update();self.assertGreater(len(report['rebuiltWindows']),1)
  self.compare_full_bars()
 def test_first_direct_quote_changes_eligibility_with_full_replay(self):
  self.pool('anchor',SOL,USDC);self.pool('cross',TOKEN,SOL)
  self.fill('anchor',10,1,100);self.fill('cross',61,2,1);self.builder.update()
  self.pool('direct');self.fill('direct',10000,1,50)
  report=self.builder.update();self.assertEqual(report['rebuiltFrom'],0)
  self.assertFalse(report['incrementalWindows']);self.compare_full_bars()
