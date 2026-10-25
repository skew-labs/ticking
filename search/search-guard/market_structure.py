"""Verified fixed-ABI Economic Machine structure kernel; no provider/model I/O."""
import ctypes,hashlib,json
from pathlib import Path
class Pivot(ctypes.Structure):
 _fields_=[('kind',ctypes.c_uint32),('index',ctypes.c_uint32),('time',ctypes.c_int64),('price',ctypes.c_double),('clusterStart',ctypes.c_int64),('clusterBars',ctypes.c_uint32),('confirmedAt',ctypes.c_int64),('reaction',ctypes.c_double),('confirmationRange',ctypes.c_double)]
class Event(ctypes.Structure):
 _fields_=[('type',ctypes.c_uint32),('direction',ctypes.c_int32),('time',ctypes.c_int64),('confirmedAt',ctypes.c_int64),('pivotTime',ctypes.c_int64),('pivotConfirmedAt',ctypes.c_int64),('level',ctypes.c_double),('close',ctypes.c_double),('buffer',ctypes.c_double),('hasOrigin',ctypes.c_uint32),('origin',Pivot)]
class Layer(ctypes.Structure):
 _fields_=[('left',ctypes.c_uint32),('right',ctypes.c_uint32),('pivotCount',ctypes.c_uint32),('eventCount',ctypes.c_uint32),('bias',ctypes.c_int32),('transition',ctypes.c_int32),('state',ctypes.c_int32),('establishedAt',ctypes.c_int64),('pendingAt',ctypes.c_int64),('hasProtected',ctypes.c_uint32),('hasHigh',ctypes.c_uint32),('hasLow',ctypes.c_uint32),('highRelation',ctypes.c_uint32),('lowRelation',ctypes.c_uint32),('ambiguous',ctypes.c_uint32),('protectedPivot',Pivot),('latestHigh',Pivot),('latestLow',Pivot),('pivots',Pivot*600),('events',Event*600)]
class Result(ctypes.Structure):
 _fields_=[('abi',ctypes.c_uint32),('bars',ctypes.c_uint32),('first',ctypes.c_int64),('last',ctypes.c_int64),('layers',Layer*2)]
LIB=None
def load():
 root=Path(__file__).parent/'economic_machine';manifest=json.loads((root/'structure-manifest.json').read_text());p=root/'libticking_structure.so'
 if manifest['policy']!='market-structure-v1' or hashlib.sha256(p.read_bytes()).hexdigest()!=manifest['sha256']:raise RuntimeError('Structure native identity mismatch')
 lib=ctypes.CDLL(str(p));lib.em_structure_abi.restype=ctypes.c_uint32
 if lib.em_structure_abi()!=1:raise RuntimeError('Unsupported structure ABI')
 lib.em_ticking_structure.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int64,ctypes.c_int64,ctypes.POINTER(Result)];lib.em_ticking_structure.restype=ctypes.c_int
 return lib

def calculate(array,count,period,cutoff,full=False):
 global LIB
 if not 0<=count<=600:raise ValueError('Use 1–600 source candles')
 if LIB is None:LIB=load()
 out=Result();out.abi=1
 if count and (LIB.em_ticking_structure(array,count,period,cutoff,ctypes.byref(out))!=1 or out.abi!=1):raise ValueError('Invalid native structure input')
 def pivot(p):return {'kind':'low' if p.kind==1 else 'high','index':p.index,'time':p.time,'price':p.price,'clusterStart':p.clusterStart,'clusterBars':p.clusterBars,'confirmedAt':p.confirmedAt,'reaction':p.reaction,'confirmationRange':p.confirmationRange}
 def event(e):
  p=pivot(e.origin) if e.hasOrigin else None
  return {'type':{1:'bos',2:'choch',3:'reversal_bos',4:'wick_sweep',5:'failed_choch'}[e.type],'direction':'up' if e.direction>0 else 'down','time':e.time,'confirmedAt':e.confirmedAt,'level':e.level,'pivotTime':e.pivotTime,'pivotConfirmedAt':e.pivotConfirmedAt,'origin':{k:p[k] for k in ['time','price','kind','confirmedAt']} if p else None,'close':e.close,'buffer':e.buffer}
 def layer(l,i):
  if l.pivotCount>600 or l.eventCount>600 or l.state not in range(4):raise ValueError('Invalid native structure bounds')
  d={'left':l.left or (3 if i==0 else 8),'right':l.right or (3 if i==0 else 8),'minimumReaction':.75 if i==0 else 1.5,'ambiguousPivots':l.ambiguous,'latestHigh':pivot(l.latestHigh) if l.hasHigh else None,'latestLow':pivot(l.latestLow) if l.hasLow else None,'highStructure':{0:None,1:'higher_high',2:'lower_high',3:'equal'}[l.highRelation],'lowStructure':{0:None,1:'higher_low',2:'lower_low',3:'equal'}[l.lowRelation],'regime':'rising' if l.bias>0 and l.state==1 else 'falling' if l.bias<0 and l.state==1 else 'undetermined','direction':'up' if (l.transition or l.bias)>0 else 'down' if (l.transition or l.bias)<0 else None,'state':['unconfirmed','active','transition','stale'][l.state],'establishedAt':l.establishedAt or None,'protectedPivot':pivot(l.protectedPivot) if l.hasProtected else None,'transitionDirection':'up' if l.transition>0 else 'down' if l.transition<0 else None,'transitionAt':l.pendingAt or None,'events':[event(l.events[j]) for j in range(0 if full else max(0,l.eventCount-12),l.eventCount)]}
  if full:d['pivots']=[pivot(l.pivots[j]) for j in range(l.pivotCount)]
  return d
 internal,swing=[layer(out.layers[i],i) for i in range(2)];primary='swing' if swing['state'] in ['active','transition'] else 'internal';selected=swing if primary=='swing' else internal
 d={k:selected[k] for k in ['regime','state','transitionDirection','transitionAt','protectedPivot','establishedAt','latestHigh','latestLow','highStructure','lowStructure']}
 d.update(method='market-structure-v1',basis='wick',primary=primary,direction=None if selected['state']=='unconfirmed' else selected['direction'],internal=internal,swing=swing,scope='completed source bars; causal confirmation; CHOCH is a warning, not a confirmed reversal',cutoff=out.last+period if out.bars else None,bars=out.bars)
 if full:d['pivots']=internal['pivots']
 return d
