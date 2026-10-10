"""Verified closed-segment restart summaries, never a substitute for originals.

A summary is usable only while its local file identity is unchanged. CRC and
protobuf validation occur when building it; explicit reads always validate the
original again. This is a crash-recovery cache, not a bit-rot or adversarial
filesystem attestation. Daily expiry forces a full scrub before reuse.
"""
import hashlib,json,os,time
from pathlib import Path
from engine_runtime import EngineError
from solana_stream import InputJournal,atomic_json,protobuf_modules

COUNTERS=('retained','executedResults','successful','failed','exactBankIdentities')
MAX_AGE=24*3600
NAME='verified-summary.json'

def identity(path):
    s=path.stat()
    return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

def load(folder):
    """Invalid, expired or modified summaries fall back to original validation."""
    folder=Path(folder);path=folder/NAME
    try:
        if path.stat().st_size>16384:return None
        wrapper=json.loads(path.read_bytes());v=wrapper['value']
        if wrapper['sha256']!=hashlib.sha256(encoded(v)).hexdigest():return None
        if v['schema']!=1 or v['identity']!=identity(folder/'inputs.sli'):return None
        if not 0<=time.time()-v['validatedAt']<=MAX_AGE:return None
        if not all(type(v['state'][k]) is int and v['state'][k]>=0 for k in COUNTERS+('lastSlot','lastFinalizedSlot')):return None
        if v['state']['retained']!=v['frames'] or v['state']['successful']+v['state']['failed']!=v['state']['executedResults']:return None
        return v
    except (OSError,ValueError,KeyError,TypeError):return None

def build(folder):
    """Lock and fully validate one immutable source; publish only after fsync."""
    folder=Path(folder);source=folder/'inputs.sli';before=identity(source)
    pb,_=protobuf_modules();state={k:0 for k in COUNTERS+('lastSlot','lastFinalizedSlot')}
    def observe(raw,stamp):
        u=pb.SubscribeUpdate.FromString(raw);kind=u.WhichOneof('update_oneof')
        if kind not in ('transaction','slot','block_meta'):raise EngineError('Unsupported archival frame')
        v=getattr(u,kind);state['retained']+=1
        state.setdefault('firstObservedSlot',int(v.slot));state['lastSlot']=max(state['lastSlot'],int(v.slot))
        if kind=='slot' and v.status==pb.SLOT_FINALIZED:state['lastFinalizedSlot']=max(state['lastFinalizedSlot'],int(v.slot))
        if kind=='transaction':
            state['exactBankIdentities']+=bool(v.bank_id)
            if v.transaction.HasField('meta'):
                state['executedResults']+=1;state['failed' if v.transaction.meta.HasField('err') else 'successful']+=1
    journal=InputJournal(folder,max_bytes=max(before[2],1),on_record=observe)
    try:
        h=hashlib.sha256()
        with source.open('rb') as f:
            while chunk:=f.read(1024*1024):h.update(chunk)
        if identity(source)!=before:raise EngineError('Original changed during checkpoint validation')
        v=dict(schema=1,identity=before,sourceSHA256=h.hexdigest(),frames=journal.sequence,state=state,validatedAt=time.time())
        atomic_json(folder/NAME,{'value':v,'sha256':hashlib.sha256(encoded(v)).hexdigest()})
        return v
    finally:journal.close()

def merge(state,value):
    for key in COUNTERS:state[key]+=value['state'][key]
    for key in ('lastSlot','lastFinalizedSlot'):state[key]=max(state[key],value['state'][key])
    if 'firstObservedSlot' in value['state']:state.setdefault('firstObservedSlot',value['state']['firstObservedSlot'])
