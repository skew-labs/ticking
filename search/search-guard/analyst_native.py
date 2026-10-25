from economic_machine import chain_catalog
"""Fixed-ABI feature calculation in the installed Economic Machine adapter."""
import ctypes,hashlib,json,math
from market_structure import calculate as structure_features
from strategy_evidence import calculate as evidence_features
from pathlib import Path
FIELDS=['close','ema20','ema50','ema20prev3','rsi','atr','bbmid','bbupper','bblower','adx','pdi','mdi','macd','macdsignal','macdhist','relativeVolume','vwap','support20','resistance20']
class Bar(ctypes.Structure):
 _fields_=[('time',ctypes.c_int64),('open',ctypes.c_double),('high',ctypes.c_double),('low',ctypes.c_double),('close',ctypes.c_double),('volume',ctypes.c_double),('flags',ctypes.c_uint32)]
class Features(ctypes.Structure):
 _fields_=[('abi',ctypes.c_uint32),('bars',ctypes.c_uint32),('first',ctypes.c_int64),('last',ctypes.c_int64),('values',ctypes.c_double*19),('previousHigh',ctypes.c_double),('latestHigh',ctypes.c_double),('previousLow',ctypes.c_double),('latestLow',ctypes.c_double),('highTime',ctypes.c_int64),('lowTime',ctypes.c_int64),('regime',ctypes.c_uint32)]
def load():
 p=Path(__file__).parent/'economic_machine';m=json.loads((p/'native-manifest.json').read_text());binary=p/'libticking_navigation.so'
 if hashlib.sha256(binary.read_bytes()).hexdigest()!=m['sha256'][binary.name]:raise RuntimeError('Analyst native identity mismatch')
 lib=ctypes.CDLL(str(binary));lib.em_ticking_analyst_abi.restype=ctypes.c_uint32
 if lib.em_ticking_analyst_abi()!=1:raise RuntimeError('Unsupported analyst ABI')
 lib.em_ticking_features.argtypes=[ctypes.POINTER(Bar),ctypes.c_size_t,ctypes.c_int64,ctypes.c_int64,ctypes.POINTER(Features)];lib.em_ticking_features.restype=ctypes.c_int
 return lib
LIB=None
def reference_basis(frame):
 identity=frame.get('identity',{})
 exchange=identity.get('network')=='exchange' and identity.get('aggregation')=='multi_venue_asset_reference_v1' and identity.get('historyPolicy')=='median_venue_ohlc_v1' and identity.get('quote')==frame.get('quote')
 evm=identity.get('network') in chain_catalog.evm_ids() and identity.get('aggregation')=='em_pool_reference_v2' and identity.get('historyPolicy')=='em_pool_reference_v2' and identity.get('quoteMint')=='USD' and frame.get('quote')=='USD'
 return not frame.get('stale') and identity.get('series')=='reference' and bool(frame.get('quote')) and (exchange or evm)
def reference_description(frame,period,cutoff):
 # A separate price-reading lane. Never relax the strict strategy/alert input
 # policy, substitute a venue quote, or promote estimated OHLC to execution data.
 if not reference_basis(frame):return None
 basis='em_usd_reference_unattested_v1' if frame.get('identity',{}).get('network') in chain_catalog.evm_ids() else 'median_venue_ohlc_v1'
 if not any(b.get('estimated') and b.get('estimateMethod')==basis for b in frame['candles']):return None
 result=calculate(frame,period,cutoff,reference_description=True)
 if result['bars']<50 or not result['last'] or cutoff-result['last']-period>min(86400000,max(120000,period*1.1)):return None
 caution='USD pool references include stable-peg and as-of conversion assumptions without per-bar conversion attestations. Dollar turnover is excluded from base-volume indicators. No position entries or exact venue extrema are inferred.' if basis=='em_usd_reference_unattested_v1' else 'OHLC combines retained venue observations. These are chart-reference values, not exact venue extrema or executable prices. Recorded volume and position setups remain unavailable for this basis.'
 return {**result,'schema':'ticking.reference-reading.v1','interval':frame['interval'],'quote':frame['quote'],'purpose':'chart_description','readOnly':True,'decisionEligible':False,'basis':basis,'caution':caution}

def calculate(frame,period,cutoff,reference_description=False):
 global LIB
 if reference_description and not reference_basis(frame):raise ValueError('Unsupported reference description basis')
 if LIB is None:LIB=load()
 bars=frame['candles']
 if not 0<=len(bars)<=600:raise ValueError('Use at most 600 retained source bars')
 if not bars:return {'values':dict.fromkeys(FIELDS),'bars':0,'first':0,'last':0,'structure':structure_features((Bar*0)(),0,period,cutoff,full=not reference_description),'chartEvidence':evidence_features((Bar*0)(),0,period,cutoff),'origin':'economic_machine_cpp20','abi':1,'calculationVersion':'sma-seed-wilder-v2'}
 array=(Bar*len(bars))();previous_quote=None
 for i,b in enumerate(bars):
  quote=b.get('quoteCurrency') or frame.get('quote');units=b.get('quoteCurrencies');ambiguous=isinstance(units,list) and (len(units)>1 or len(units)==1 and units[0]!=quote) or b.get('mixedSource') and (not isinstance(units,list) or len(units)!=1)
  method='em_usd_reference_unattested_v1' if frame.get('identity',{}).get('network') in chain_catalog.evm_ids() else 'median_venue_ohlc_v1'
  descriptive_estimate=reference_description and b.get('referenceOnly') is True and b.get('estimateMethod')==method
  invalid=bool(b.get('synthetic') or b.get('estimated') and not descriptive_estimate or b.get('sourceCandleState')=='provisional' or ambiguous or reference_description and quote!=frame['quote'])
  flags=int(invalid)|int(bool(b.get('referenceOnly') or invalid or reference_description))*2|int(previous_quote is not None and quote!=previous_quote)*4
  array[i]=Bar(b['time'],*[b[k] for k in ['open','high','low','close','volume']],flags);previous_quote=quote
 out=Features();status=LIB.em_ticking_features(array,len(bars),period,cutoff,ctypes.byref(out))
 if status!=1 or out.abi!=1:raise ValueError('Invalid native source frame')
 value=lambda x:x if math.isfinite(x) else None
 return {'values':{name:value(out.values[i]) for i,name in enumerate(FIELDS)},'bars':out.bars,'first':out.first,'last':out.last,'structure':structure_features(array,len(bars),period,cutoff,full=not reference_description),'chartEvidence':evidence_features(array,len(bars),period,cutoff),'origin':'economic_machine_cpp20','abi':1,'calculationVersion':'sma-seed-wilder-v2'}
