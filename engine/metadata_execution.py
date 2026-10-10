"""C++ executed-metadata decoder. Python owns durable cursor/DB orchestration.

Verified registry addresses are decoded once on refresh. Each transaction crosses
the ABI once, with bounded candidate indices; only accepted fills become Python
objects. Invalid native envelopes stop the batch before its durable cursor moves.
"""
import ctypes
from pathlib import Path
from observer_engine import AmmConfig,Fill
from server import b58decode
from solana_stream import b58encode

class MetadataExecutionDecoder:
 def __init__(self,library=None):
  self.lib=ctypes.CDLL(str(library or Path(__file__).parent/'engine/libslotline_execution.so'))
  self.lib.sl_observer_amm_batch.argtypes=[ctypes.c_char_p,ctypes.c_uint32,ctypes.POINTER(AmmConfig),ctypes.c_uint32,ctypes.POINTER(ctypes.c_uint32),ctypes.c_uint32,ctypes.POINTER(Fill),ctypes.POINTER(ctypes.c_int32)]
  self.lib.sl_observer_amm_batch.restype=ctypes.c_int
  self.indices=(ctypes.c_uint32*256)();self.fills=(Fill*256)();self.status=(ctypes.c_int32*256)()
  self.registry=();self.offsets={};self.calls=0;self.candidates=0
 def prepare(self,configs):
  if len(configs)>262144:raise ValueError('Native execution registry bound')
  registry=(AmmConfig*len(configs))();offsets={}
  fields=(('pool','pool'),('program','program'),('base_vault','baseVault'),('quote_vault','quoteVault'),('base_mint','mint'),('quote_mint','quoteMint'))
  for i,(pool,cfg) in enumerate(configs.items()):
   if cfg['pool']!=pool:raise ValueError('Native registry pool identity mismatch')
   for field,key in fields:
    raw=b58decode(cfg[key])
    if len(raw)!=32:raise ValueError('Native execution address length')
    getattr(registry[i],field)[:]=raw
   bd,qd=cfg['baseDecimals'],cfg['quoteDecimals']
   if not isinstance(bd,int) or not isinstance(qd,int) or not 0<=bd<=18 or not 0<=qd<=18:raise ValueError('Native execution decimal bound')
   registry[i].base_decimals=bd;registry[i].quote_decimals=qd;offsets[pool]=i
  self.registry=registry;self.offsets=offsets
 def decode_many(self,info,candidates,slot,stamp):
  if not candidates:return []
  if len(candidates)>256:raise ValueError('Native execution candidate bound')
  for i,cfg in enumerate(candidates):self.indices[i]=self.offsets[cfg['pool']]
  raw=info.SerializeToString()
  status=self.lib.sl_observer_amm_batch(raw,len(raw),self.registry,len(self.registry),self.indices,len(candidates),self.fills,self.status)
  if status!=1:raise ValueError('Native executed-result envelope rejected; retained cursor unchanged')
  self.calls+=1;self.candidates+=len(candidates);signature=None;result=[]
  for i,cfg in enumerate(candidates):
   if self.status[i]!=1:result.append([]);continue
   if signature is None:signature=b58encode(bytes(info.signature))
   f=self.fills[i]
   result.append([{'id':signature+':'+cfg['pool'],'time':stamp,'slot':slot,'order':f.index,'series':'trade','price':f.price,'volume':f.volume,'baseVolume':f.base}])
  from pump_curve import PUMP,decode as curve_decode
  curve_indices=[i for i,cfg in enumerate(candidates) if cfg['program']==PUMP]
  if curve_indices:
   from solana_stream import protobuf_receipt
   sig,tx=protobuf_receipt(info,slot,stamp,binary_instructions=True,include_logs=True)
   for i in curve_indices:result[i]=curve_decode(tx,candidates[i],sig,int(info.index))[0]
  return result
