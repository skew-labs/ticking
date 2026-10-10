import ctypes,os
from metadata_execution import MetadataExecutionDecoder
from observer_engine import Fill
from observed_chart import ObservedChart
from test_observed_chart import ObservedProjection
from test_solana_stream import POOL,CFG

class NativeExecutionProjection(ObservedProjection):
 def tx(self,u=None):
  result=super().tx(u);message=result.transaction.transaction.transaction.message
  message.header.num_required_signatures=1;message.header.num_readonly_unsigned_accounts=1;message.recent_blockhash=bytes([9])*32
  return result
 def setUp(self):
  super().setUp();self.decoder=MetadataExecutionDecoder(os.environ.get('TICK_EXECUTION_LIBRARY'))
  self.p=ObservedChart(self.path,{POOL:CFG},execution_decoder=self.decoder)
 def test_exact_reference_result_and_no_python_receipt_conversion(self):
  from unittest.mock import patch
  self.accept(self.meta())
  with patch('observed_chart.protobuf_receipt',side_effect=AssertionError('Python conversion used')):self.accept(self.tx())
  self.assertEqual(self.p.history(POOL,60,600)['candles'][0]['count'],1)
  self.assertEqual(self.decoder.calls,1)
 def test_malformed_batch_cannot_partially_publish(self):
  self.decoder.prepare({POOL:CFG});info=self.tx().transaction.transaction;raw=info.SerializeToString()
  indices=(ctypes.c_uint32*2)(0,1);status=(ctypes.c_int32*2)(123,123);fills=(Fill*2)();fills[0].price=999
  result=self.decoder.lib.sl_observer_amm_batch(raw,len(raw),self.decoder.registry,1,indices,2,fills,status)
  self.assertEqual(result,-4);self.assertEqual(list(status),[123,123]);self.assertEqual(fills[0].price,999)
 def test_bad_envelope_rolls_back_cursor_and_candles(self):
  self.accept(self.meta());u=self.tx();u.transaction.transaction.signature=b'bad'
  with self.assertRaises(ValueError):
   with self.p.connect() as c:
    c.execute("INSERT INTO state VALUES('cursor','not-committed')")
    self.p.accept(c,u)
  with self.p.connect() as c:self.assertIsNone(c.execute("SELECT value FROM state WHERE key='cursor'").fetchone())
  self.assertFalse(self.p.history(POOL,60,600)['candles'])
 def test_pending_results_restart_into_native_decoder(self):
  self.accept(self.tx());self.p=ObservedChart(self.path,{POOL:CFG},execution_decoder=self.decoder);self.accept(self.meta())
  self.assertEqual(self.p.history(POOL,60,600)['candles'][0]['count'],1)
 def test_fixed_registry_is_not_partially_replaced(self):
  with self.assertRaises(ValueError):self.decoder.prepare({POOL:{**CFG,'baseDecimals':19}})
  self.accept(self.meta());self.accept(self.tx());self.assertEqual(self.decoder.calls,1)
 def test_integer_flows_match_decimal_reference_bit_for_bit(self):
  import random
  from native_solana import decode
  from solana_stream import protobuf_receipt
  rng=random.Random(49)
  cases=[(4349968786,481545900),(428584634998,6050960861),(2**64-1,1),(1,2**64-1)]
  cases.extend((rng.randrange(1,2**64),rng.randrange(1,2**64)) for _ in range(500))
  for i,(base,quote) in enumerate(cases):
   bd,qd=[(6,9),(0,18),(18,0),(9,6)][i%4];cfg={**CFG,'baseDecimals':bd,'quoteDecimals':qd};self.decoder.prepare({POOL:cfg})
   info=self.tx().transaction.transaction
   for rows,values in [(info.meta.pre_token_balances,(base,0)),(info.meta.post_token_balances,(0,quote))]:
    for j,row in enumerate(rows):row.ui_token_amount.amount=str(values[j]);row.ui_token_amount.decimals=qd if j else bd
   sig,tx=protobuf_receipt(info,100,1710000000,binary_instructions=True)
   expected=decode(tx,cfg,sig,info.index)[0]
   self.assertEqual(self.decoder.decode_many(info,[cfg],100,1710000000),[expected],(i,base,quote,bd,qd))
