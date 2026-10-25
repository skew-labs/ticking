"""Verified fixed-size C++ chart-evidence kernel; no network, models or orders."""
import ctypes,hashlib,json,math
from pathlib import Path
class Item(ctypes.Structure):
 _fields_=[('kind',ctypes.c_uint32),('side',ctypes.c_int32),('status',ctypes.c_uint32),('first',ctypes.c_int64),('last',ctypes.c_int64),('confirmedAt',ctypes.c_int64)]+[(k,ctypes.c_double) for k in ['level','stopAnchor','target','priceA','priceB','oscillatorA','oscillatorB','upper','lower','upperSlope','lowerSlope']]+[('touches',ctypes.c_uint32)]
class Result(ctypes.Structure):
 _fields_=[('abi',ctypes.c_uint32),('count',ctypes.c_uint32),('cutoff',ctypes.c_int64),('items',Item*24)]
LIB=None
KINDS=['','rsi_divergence','triangle','rising_wedge','falling_wedge','channel','failed_breakout','zone_rejection','trendline_reclaim','double_reversal','head_shoulders','macd_cross','macd_divergence','band_reentry','ema_pullback','candle_rejection','triple_reversal','volatility_squeeze']
STATES=['','observed','forming','rejection','breakout','reclaimed']
def calculate(array,count,period,cutoff):
 global LIB
 if not 0<=count<=600:raise ValueError('Invalid evidence frame bound')
 if LIB is None:
  root=Path(__file__).parent/'economic_machine';p=root/'libticking_evidence.so';m=json.loads((root/'evidence-manifest.json').read_text())
  if m['policy']!='chart-evidence-v1' or hashlib.sha256(p.read_bytes()).hexdigest()!=m['sha256']:raise RuntimeError('Evidence kernel identity mismatch')
  LIB=ctypes.CDLL(str(p));LIB.em_evidence_abi.restype=ctypes.c_uint32
  if LIB.em_evidence_abi()!=1:raise RuntimeError('Unsupported evidence ABI')
  LIB.em_ticking_evidence.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int64,ctypes.c_int64,ctypes.POINTER(Result)];LIB.em_ticking_evidence.restype=ctypes.c_int
 out=Result();out.abi=1
 if count and (LIB.em_ticking_evidence(array,count,period,cutoff,ctypes.byref(out))!=1 or out.abi!=1):raise ValueError('Invalid native evidence frame')
 if out.count>24:raise ValueError('Evidence output bound exceeded')
 observations=[]
 for i in range(out.count):
  e=out.items[i];d={name:getattr(e,name) for name,_ in Item._fields_}
  if e.kind not in range(1,len(KINDS)) or e.status not in range(1,len(STATES)) or e.side not in [-1,0,1] or not all(math.isfinite(v) for v in d.values()):raise ValueError('Invalid native evidence result')
  d.update(kind=KINDS[e.kind],status=STATES[e.status]);observations.append(d)
 return {'method':'chart-evidence-v1','cutoff':out.cutoff,'observations':observations}
