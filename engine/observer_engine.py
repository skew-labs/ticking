"""Control/transport binding to our C++ receive-only observer, not Firedancer.

Original wire and bank bindings are durable before the native cache is visible.
External executing nodes generate metadata; this client only observes results.
"""
import ctypes,hashlib,os,struct,subprocess,threading,time
from collections import OrderedDict
from pathlib import Path
from engine_runtime import EngineError
from solana_stream import InputJournal,b58encode,atomic_json
ROOT=Path(__file__).resolve().parent
FRAME=struct.Struct('<4sI');WIRE=struct.Struct('<QIIII64s32s');BIND=struct.Struct('<4sQQHH')
class AmmConfig(ctypes.Structure):
    _fields_=[(name,ctypes.c_uint8*32) for name in ('pool','program','base_vault','quote_vault','base_mint','quote_mint')]+[('base_decimals',ctypes.c_uint32),('quote_decimals',ctypes.c_uint32)]
class Fill(ctypes.Structure):
    _fields_=[('price',ctypes.c_double),('volume',ctypes.c_double),('base',ctypes.c_double),('index',ctypes.c_uint64)]
def library():
    lib=ctypes.CDLL(str(ROOT/'engine/libslotline_observer.so'))
    lib.sl_observer_new.argtypes=[ctypes.c_uint32];lib.sl_observer_new.restype=ctypes.c_void_p
    lib.sl_observer_delete.argtypes=[ctypes.c_void_p]
    for name in ('sl_observer_observe','sl_observer_match'):
        getattr(lib,name).argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.c_char_p,ctypes.c_uint32,ctypes.c_char_p]
    lib.sl_observer_match_any.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.c_char_p,ctypes.c_uint32,ctypes.c_char_p,ctypes.c_uint32]
    lib.sl_observer_count.argtypes=[ctypes.c_void_p];lib.sl_observer_count.restype=ctypes.c_uint64
    lib.sl_observer_retire.argtypes=[ctypes.c_void_p,ctypes.c_uint64]
    lib.sl_observer_result.argtypes=[ctypes.c_char_p,ctypes.c_uint32,ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32),ctypes.POINTER(ctypes.c_uint64)]
    lib.sl_observer_last_error.restype=ctypes.c_char_p
    lib.sl_observer_selected.argtypes=[ctypes.c_char_p,ctypes.c_uint32,ctypes.c_char_p,ctypes.c_uint32]
    lib.sl_observer_candidates.argtypes=[ctypes.c_char_p,ctypes.c_uint32,ctypes.c_char_p,ctypes.c_uint32,ctypes.c_void_p]
    lib.sl_pool_index_new.argtypes=[ctypes.c_char_p,ctypes.c_uint32];lib.sl_pool_index_new.restype=ctypes.c_void_p
    lib.sl_pool_index_reserved_new.argtypes=[ctypes.c_char_p,ctypes.c_uint32,ctypes.c_uint32];lib.sl_pool_index_reserved_new.restype=ctypes.c_void_p
    lib.sl_pool_index_append.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32]
    lib.sl_pool_index_delete.argtypes=[ctypes.c_void_p]
    lib.sl_pool_index_candidates.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32,ctypes.POINTER(ctypes.c_uint32),ctypes.POINTER(ctypes.c_uint32)]
    lib.sl_observer_amm.argtypes=[ctypes.c_char_p,ctypes.c_uint32,ctypes.POINTER(AmmConfig),ctypes.POINTER(Fill)]
    return lib
def result_wire(info):
    lib=library();raw=info.SerializeToString();output=ctypes.create_string_buffer(4096);n=ctypes.c_uint32(4096);index=ctypes.c_uint64()
    status=lib.sl_observer_result(raw,len(raw),output,ctypes.byref(n),ctypes.byref(index))
    if status not in (1,-5):raise EngineError('C++ observer rejected malformed or unsupported result metadata')
    return bytes(output.raw[:n.value]),index.value,status==-5
class NativeObservationIndex:
    def __init__(self,directory,capacity=65536,watched=()):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True);self.lib=library();self.handle=self.lib.sl_observer_new(capacity)
        if not self.handle:raise EngineError('Native observation cache allocation failed')
        self.lock=threading.RLock();self.watched=tuple(watched);self.bindings={};self.floor=0;self.config_cache=OrderedDict();self.pool_keys=();self.pool_handle=None;self.pool_generation=None;self.binding_log=InputJournal(self.directory/'bank-bindings',max_bytes=64*1024**2)
        from shred_bindings import FinalizedShredBindings
        self.finalized_proofs=FinalizedShredBindings(self.directory/'slot-proofs')
        self.candidate_out=(ctypes.c_uint32*256)();self.candidate_count=ctypes.c_uint32(256)
        for seq,stamp,raw in self.binding_log.records():self._binding(raw)
    def _binding(self,raw):
        if raw[:4]==b'SLR1':
            if len(raw)!=12:raise EngineError('Retirement barrier invalid')
            floor=struct.unpack_from('<Q',raw,4)[0]
            if floor<self.floor:raise EngineError('Retirement barrier regressed')
            if self.lib.sl_observer_retire(self.handle,floor)<0:raise EngineError('Native retirement invalid')
            self.floor=floor;self.bindings={key:value for key,value in self.bindings.items() if key[0]>floor};return
        if len(raw)<BIND.size:raise EngineError('Bank binding truncated')
        magic,slot,bank,n,count=BIND.unpack_from(raw)
        if magic!=b'SLK1' or not slot or not bank or not 0<n<=128 or not 0<count<=512 or len(raw)!=BIND.size+n+32*count:raise EngineError('Bank binding invalid')
        blockhash=raw[BIND.size:BIND.size+n].decode('ascii');roots=frozenset(raw[BIND.size+n+32*i:BIND.size+n+32*(i+1)] for i in range(count))
        if bytes(32) in roots:raise EngineError('Bank binding root unavailable')
        key=(slot,bank);old=self.bindings.get(key)
        value=(blockhash,roots,b''.join(sorted(roots)))
        if old and old!=value:raise EngineError('Conflicting authoritative bank binding')
        if key not in self.bindings and len(self.bindings)>=8192:raise EngineError('Bank binding bound reached; verified recovery required')
        self.bindings[key]=value
    def bind_bank(self,slot,bank,blockhash,roots,*,verified_source=False):
        # Explicit trusted integration hook. Ordinary Geyser bank_id alone is
        # not a mapping from a shred Merkle chain to an executed bank.
        if not verified_source:raise EngineError('Authoritative shred-chain to executed-bank binding is required')
        roots=sorted(set(roots));encoded=blockhash.encode('ascii');raw=BIND.pack(b'SLK1',slot,bank,len(encoded),len(roots))+encoded+b''.join(roots)
        with self.lock:
            old=dict(self.bindings)
            self._binding(raw);self.bindings=old
            self.binding_log.append(raw);self._binding(raw)
    @property
    def count(self):return self.lib.sl_observer_count(self.handle)
    def observe(self,row):
        with self.lock:
            branch=row.get('branch',bytes(32));wire=row['wire']
            if row['slot']<=self.floor:return 0
            if len(branch)!=32:raise EngineError('Shred branch identity invalid')
            result=self.lib.sl_observer_observe(self.handle,row['slot'],wire,len(wire),branch)
            if result<0:raise EngineError('Native observation conflict, invalid identity or bounded cache full')
            return result
    def selected(self,info):
        raw=info.SerializeToString()
        if not self.watched:return not info.meta.HasField('err')
        result=self.lib.sl_observer_selected(raw,len(raw),b''.join(self.watched),len(self.watched))
        if result==-4:raise EngineError('Native metadata selection rejected malformed input')
        return result==1
    def prepare_pools(self,configs,generation=None):
        from server import b58decode
        keys=tuple(configs)
        if len(keys)>262144:raise EngineError('Cold market address index bound exceeded')
        raw=b''.join(b58decode(k) for k in keys)
        if len(raw)!=32*len(keys):raise EngineError('Native pool configuration invalid')
        handle=self.lib.sl_pool_index_reserved_new(raw,len(keys),262144) if keys else None
        if keys and not handle:raise EngineError('Native pool index allocation or identity validation failed')
        with self.lock:
            old=self.pool_handle;self.pool_handle=handle;self.pool_keys=list(keys);self.pool_generation=generation
            if old:self.lib.sl_pool_index_delete(old)
    def append_pools(self,configs,generation):
        from server import b58decode
        keys=tuple(configs)
        if not keys:return
        with self.lock:
            if not self.pool_handle:return self.prepare_pools(configs,generation)
            if len(self.pool_keys)+len(keys)>262144:raise EngineError('Cold address index capacity reached')
            raw=b''.join(b58decode(k) for k in keys)
            if len(raw)!=32*len(keys):raise EngineError('Native pool append identity invalid')
            previous=len(self.pool_keys);self.pool_keys.extend(keys)
            if self.lib.sl_pool_index_append(self.pool_handle,raw,len(keys))!=1:
                del self.pool_keys[previous:];raise EngineError('Native pool index append rejected; prior index retained')
            self.pool_generation=generation
    def candidate_pools(self,info,configs=None,generation=None,raw=None):
        # The processor prepares immutable indexes only on registry revisions.
        # Legacy callers are checked here without changing their contract.
        if configs is not None and (generation!=self.pool_generation or (generation is None and tuple(configs)!=tuple(self.pool_keys))):
            self.prepare_pools(configs,generation)
        raw=raw if raw is not None else info.SerializeToString()
        with self.lock:
            if not self.pool_handle:return []
            out=self.candidate_out;count=self.candidate_count;count.value=256
            status=self.lib.sl_pool_index_candidates(self.pool_handle,raw,len(raw),out,ctypes.byref(count))
            candidates=[self.pool_keys[out[i]] for i in range(count.value)] if status==1 else []
        if status==-4:raise EngineError('Native candidate lookup rejected malformed result')
        return candidates
    def matches_block(self,block,info,raw=None):
        with self.lock:
            binding=self.bindings.get((int(block.slot),int(block.bank_id)))
            if not binding:return False
            if binding[0]!=block.blockhash:raise EngineError('Executed bank hash conflicts with verified shred binding')
            raw=raw if raw is not None else info.SerializeToString()
            result=self.lib.sl_observer_match_any(self.handle,block.slot,raw,len(raw),binding[2],len(binding[1]))
            if result in (-1,-4):raise EngineError('Restored wire conflicts with executed metadata')
            return result==1
    def matches_finalized_block(self,block,info,raw=None):
        # Called only by the explicitly finalized transport. A processed block
        # with the same slot/signature never gains this authority implicitly.
        wire,index,failed=result_wire(info)
        if failed:return False
        with self.lock:return self.finalized_proofs.matches(block,info,wire)
    def native_decode(self,info,cfg,slot,block_time,raw=None):
        from server import b58decode
        fields=[('pool','pool'),('program','program'),('base_vault','baseVault'),('quote_vault','quoteVault'),('base_mint','mint'),('quote_mint','quoteMint')]
        fingerprint=tuple(cfg[key] for name,key in fields)+(cfg['baseDecimals'],cfg['quoteDecimals'])
        native=self.config_cache.get(fingerprint)
        if native is None:
            if len(self.config_cache)>=1024:self.config_cache.popitem(last=False)
            native=AmmConfig()
            for name,key in fields:
                value=b58decode(cfg[key])
                if len(value)!=32:raise EngineError('Native decoder configuration identity invalid')
                getattr(native,name)[:]=value
            native.base_decimals=cfg['baseDecimals'];native.quote_decimals=cfg['quoteDecimals'];self.config_cache[fingerprint]=native
        else:self.config_cache.move_to_end(fingerprint)
        raw=raw if raw is not None else info.SerializeToString();fill=Fill()
        result=self.lib.sl_observer_amm(raw,len(raw),ctypes.byref(native),ctypes.byref(fill))
        if result==-4:raise EngineError('Native AMM decoder rejected malformed execution')
        if result!=1:return [],'failed' if result==-5 else 'unsupported_or_ambiguous'
        signature=b58encode(bytes(info.signature))
        return [{'id':signature+':'+cfg['pool'],'time':int(block_time),'slot':int(slot),'order':fill.index,'series':'trade','price':fill.price,'volume':fill.volume,'baseVolume':fill.base}],'decoded'
    def retire(self,slot,*,verified_recovery=False):
        if not verified_recovery:raise EngineError('Complete recovery and durable consumer barrier required before cache retirement')
        with self.lock:
            if slot<self.floor:raise EngineError('Native cache retirement regressed')
            raw=b'SLR1'+struct.pack('<Q',slot);self.binding_log.append(raw);self._binding(raw)
            path=self.directory/'retire.slc';tmp=path.with_suffix('.tmp');fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'wb') as f:f.write(b'SLC1'+struct.pack('<Q',slot));f.flush();os.fsync(f.fileno())
            os.replace(tmp,path);fd=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
    def close(self):
        if self.pool_handle:self.lib.sl_pool_index_delete(self.pool_handle);self.pool_handle=None
        if self.handle:self.lib.sl_observer_delete(self.handle);self.handle=None
        self.binding_log.close()
        self.finalized_proofs.close()
def exact(source,n):
    value=bytearray()
    while len(value)<n:
        part=source.read(n-len(value))
        if not part:raise EngineError('Native observer interrupted; replay retained inputs')
        value.extend(part)
    return bytes(value)
def read_frame(source):
    magic,n=FRAME.unpack(exact(source,FRAME.size))
    if magic not in (b'SLW2',b'SLB1',b'SLB2') or not 0<n<=2*1024**2:raise EngineError('Native observer frame invalid')
    return magic,exact(source,n)
def wire_row(raw):
    if len(raw)<=WIRE.size:raise EngineError('Native wire observation truncated')
    slot,first,entry,within,ordinal,signature,branch=WIRE.unpack_from(raw)
    return {'slot':slot,'first':first,'signature':b58encode(signature),'branch':branch,'wire':raw[WIRE.size:]}
class NativeShredExtractor:
    def __init__(self,directory,watched=()):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True);self.index=NativeObservationIndex(self.directory,watched=watched);self.journal=InputJournal(self.directory/'wire-observations',max_bytes=8*1024**3);self.transactions=0;self.batches=0
        for seq,stamp,raw in self.journal.records():self.index.observe(wire_row(raw));self.transactions+=1
    def retain(self,rows):
        if not rows:return
        with self.index.lock:
            self.journal.append_many([(raw,time.time_ns()) for raw in rows])
            for raw in rows:self.index.observe(wire_row(raw));self.transactions+=1
    def consume(self,source):
        pending=[];slot=None
        while True:
            magic,raw=read_frame(source)
            if magic==b'SLW2':
                row=wire_row(raw)
                if slot is not None and row['slot']!=slot:raise EngineError('Native batch changed slot without barrier')
                slot=row['slot'];pending.append(raw)
                if len(pending)==128:self.retain(pending);pending=[]
            else:
                if magic==b'SLB2':
                    from shred_bindings import unpack
                    proof=unpack(raw);actual_slot=proof['slot']
                else:
                    if len(raw)!=8:raise EngineError('Native batch barrier invalid')
                    actual_slot=struct.unpack('<Q',raw)[0]
                if slot is not None and actual_slot!=slot:raise EngineError('Native batch barrier slot mismatch')
                self.retain(pending);pending=[];slot=None;self.batches+=1
                if magic==b'SLB2':
                    with self.index.lock:self.index.finalized_proofs.accept(raw,authenticated_native=True)
                atomic_json(self.directory/'status.json',{'source':'slotline_cpp_observer','durableObservations':self.journal.sequence,'restoredTransactions':self.transactions,'restoredBatches':self.batches,'joinRecords':self.index.count,'executionKnown':False,'completeSlotCoverage':False})
    def close(self):self.journal.close();self.index.close()
class ShredNode:
    def __init__(self,directory,bind,peer,leaders,watched=()):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True);self.failure=None;self.extractor=NativeShredExtractor(self.directory,watched);self.log=open(self.directory/'receiver.log','ab')
        args=[str(ROOT/'engine/slotline-observer'),'--wire','--udp',bind,'--peer',peer,'--leaders',str(leaders),'--journal',str(self.directory/'original-packets.slp'),'--retire-path',str(self.directory/'retire.slc')]
        for key in watched:args.extend(['--watch-hex',key.hex()])
        self.receiver=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=self.log);self.thread=threading.Thread(target=self.consume,daemon=True,name='slotline-observer-binding');self.thread.start()
    def consume(self):
        try:self.extractor.consume(self.receiver.stdout)
        except Exception as e:
            self.failure=type(e).__name__
            if self.receiver.poll() is None:self.receiver.terminate()
            self.receiver.wait(timeout=5);atomic_json(self.directory/'failure.json',{'failure':self.failure,'action':'retain_originals_and_repair_upstream_range','executionKnown':False})
    def status(self):return {'source':'slotline_cpp_observer','nodeRunning':self.receiver.poll() is None,'restoredTransactions':self.extractor.transactions,'restoredBatches':self.extractor.batches,'durableObservations':self.extractor.journal.sequence,'failure':self.failure,'firedancerLinked':False,'executionKnown':False,'bankBindings':len(self.extractor.index.bindings)}
