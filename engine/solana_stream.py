"""Executed-result inputs. Public logs are hints; Yellowstone blocks contain results.

Neither source signs, votes, runs consensus, or re-executes a transaction. Original
inputs are fsynced before use. Bounds stop admission rather than discarding fills.
"""
import asyncio,collections,fcntl,hashlib,json,os,queue,struct,sys,threading,time,zlib
from pathlib import Path
from urllib.parse import urlsplit
from engine_runtime import EngineError,Event,executed_event
from native_solana import decode

ROOT=Path(__file__).resolve().parent
HEADER=struct.Struct('<4sIQQI');MAGIC=b'SLI1';COMPRESSED_MAGIC=b'SLZ1';MAX_FRAME=8*1024**2
def original_payload(magic,stored,crc):
    if magic==MAGIC:raw=stored
    elif magic==COMPRESSED_MAGIC:
        try:
            decoder=zlib.decompressobj();raw=decoder.decompress(stored,MAX_FRAME+1)
            if len(raw)>MAX_FRAME or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:raise EngineError('Compressed original exceeds its bound or is incomplete')
        except zlib.error as error:raise EngineError('Compressed original integrity failure') from error
    else:raise EngineError('Original input magic is unsupported')
    if not raw or zlib.crc32(raw)!=crc:raise EngineError('Original input log integrity failure')
    return raw
ALPHABET='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
def b58encode(raw):
    value=int.from_bytes(raw,'big');result=''
    while value:value,remainder=divmod(value,58);result=ALPHABET[remainder]+result
    return '1'*(len(raw)-len(raw.lstrip(b'\0')))+result
def atomic_json(path,value):
    path=Path(path);tmp=path.with_suffix('.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

class InputJournal:
    """Single-writer original-input log. A torn or corrupt frame fails closed."""
    def __init__(self,directory,max_bytes=10*1024**3,compress=False,on_record=None):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'inputs.sli';self.sequence=0;self.max_bytes=max_bytes;self.compress=bool(compress)
        self.fd=os.open(self.path,os.O_RDWR|os.O_CREAT|os.O_APPEND,0o600)
        try:
            fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for seq,stamp,raw in self.records():
                self.sequence=seq
                if on_record is not None:on_record(raw,stamp)
            os.fsync(self.fd);directory_fd=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)
        except BaseException:os.close(self.fd);raise
    def records(self):
        sequence=0
        with self.path.open('rb') as source:
            while True:
                head=source.read(HEADER.size)
                if not head:return
                if len(head)!=HEADER.size:raise EngineError('Original input log has a torn header; retain and reconcile')
                magic,size,seq,stamp,crc=HEADER.unpack(head)
                if magic not in (MAGIC,COMPRESSED_MAGIC) or not 0<size<=MAX_FRAME or seq!=sequence+1:raise EngineError('Original input log integrity failure')
                stored=source.read(size)
                if len(stored)!=size:raise EngineError('Original input log integrity failure')
                raw=original_payload(magic,stored,crc)
                sequence=seq;yield seq,stamp,raw
    def append(self,raw,stamp=None):
        stamp=stamp or time.time_ns();return self.append_many([(raw,stamp)])[0]
    def append_many(self,records):
        if not records or len(records)>128:raise EngineError('Original input batch bound exceeded')
        if any(not 0<len(raw)<=MAX_FRAME for raw,stamp in records):raise EngineError('Input frame exceeds admission bound')
        if any(not isinstance(stamp,int) or not 0<stamp<2**64 for raw,stamp in records):raise EngineError('Invalid original input timestamp')
        size=sum(HEADER.size+len(raw) for raw,stamp in records)
        if size>32*1024**2:raise EngineError('Original input batch byte bound exceeded')
        encoded=[]
        for raw,stamp in records:
            packed=zlib.compress(raw,1) if self.compress and len(raw)>=256 else raw
            encoded.append((COMPRESSED_MAGIC,packed,stamp,zlib.crc32(raw)) if len(packed)<len(raw) else (MAGIC,raw,stamp,zlib.crc32(raw)))
        size=sum(HEADER.size+len(raw) for magic,raw,stamp,crc in encoded)
        if self.max_bytes is not None and os.fstat(self.fd).st_size+size>self.max_bytes:raise EngineError('Original input storage bound reached; archive before resuming')
        result=[]
        for offset,(magic,raw,stamp,crc) in enumerate(encoded,1):
            seq=self.sequence+offset;data=HEADER.pack(magic,len(raw),seq,stamp,crc)+raw;written=0
            while written<len(data):
                count=os.write(self.fd,data[written:])
                if count<=0:raise EngineError('Original input write did not progress')
                written+=count
            result.append((seq,stamp))
        os.fdatasync(self.fd);self.sequence+=len(records);return result
    def close(self):os.close(self.fd)


def protobuf_modules():
    # Generated only on the Canada builder from pinned upstream definitions.
    path=str(ROOT/'vendor/yellowstone')
    if path not in sys.path:sys.path.insert(0,path)
    import geyser_pb2,geyser_pb2_grpc
    return geyser_pb2,geyser_pb2_grpc

def protobuf_receipt(info,slot,block_time,*,account_encoder=None,binary_instructions=False,include_logs=False):
    """No RPC re-execution: map executed protobuf fields into decoder values."""
    if not info.HasField('meta') or not info.HasField('transaction'):raise EngineError('Missing executed transaction metadata')
    tx=info.transaction;meta=info.meta
    if not tx.HasField('message') or not tx.signatures or bytes(tx.signatures[0])!=bytes(info.signature):raise EngineError('Execution signature identity mismatch')
    raw_keys=[*tx.message.account_keys,*meta.loaded_writable_addresses,*meta.loaded_readonly_addresses]
    if any(len(key)!=32 for key in raw_keys):raise EngineError('Malformed executed account identity')
    encode=account_encoder or b58encode;keys=[encode(key) for key in raw_keys]
    def instruction(i):
        if i.program_id_index>=len(keys) or any(a>=len(keys) for a in i.accounts):raise EngineError('Execution account index overflow')
        value={'programIdIndex':i.program_id_index,'accounts':list(i.accounts),'data':bytes(i.data) if binary_instructions else b58encode(bytes(i.data))}
        if hasattr(i,'stack_height') and i.HasField('stack_height'):value['stackHeight']=int(i.stack_height)
        return value
    def balance(b):return {'accountIndex':b.account_index,'mint':b.mint,'uiTokenAmount':{'amount':b.ui_token_amount.amount,'decimals':b.ui_token_amount.decimals}}
    signature=b58encode(bytes(info.signature))
    if len(bytes(info.signature))!=64:raise EngineError('Malformed execution signature')
    body={'slot':int(slot),'blockTime':int(block_time),'transaction':{'message':{'accountKeys':keys,'instructions':[instruction(i) for i in tx.message.instructions]}},'meta':{'err':{'executedError':True} if meta.HasField('err') else None,'innerInstructions':[{'index':b.index,'instructions':[instruction(i) for i in b.instructions]} for b in meta.inner_instructions],'preTokenBalances':[balance(b) for b in meta.pre_token_balances],'postTokenBalances':[balance(b) for b in meta.post_token_balances]}}
    if include_logs:body['meta']['logMessages']=list(meta.log_messages)
    return signature,body

class GeyserResultProcessor:
    """Selected executed blocks + exact-bank corrections, backed by original bytes.

    No automatic root reclamation: a filtered stream's highest slot does not
    establish a complete replay range. Saturation remains a visible admission gate.
    """
    def __init__(self,runtime,configs,directory,shred_index=None,finalized_transport=False):
        self.runtime=runtime;self.configs={c['pool']:c for c in configs};self.journal=InputJournal(directory);self.path=Path(directory)/'status.json'
        self.shred_index=shred_index;self.finalized_transport=bool(finalized_transport);self.pending=collections.OrderedDict();self.lock=threading.RLock()
        if self.finalized_transport and (shred_index is None or not hasattr(shred_index,'matches_finalized_block')):raise EngineError('Finalized transport requires whole-slot shred proofs')
        self.config_revision=0;self.recent_blocks=collections.OrderedDict();self.recent_bytes=0;self.capacity_deferred=set()
        if shred_index is not None and hasattr(shred_index,'prepare_pools'):shred_index.prepare_pools(self.configs,self.config_revision)
        self.banks={};self.dead=set();self.state={'source':'yellowstone_executed_blocks','validatorStreamConnected':False,'receivedBlocks':0,'decodedFills':0,'failedTransactions':0,'unsupportedTransactions':0,'corrections':0,'gapRecoveryRequired':False,'rootReclamation':'blocked_until_complete_upstream_recovery_is_verified'}
        for seq,stamp,raw in self.journal.records():self.apply(raw,stamp)
    def status(self):return {**self.state,'durableInputs':self.journal.sequence,'waitingForRestoredBlocks':len(self.pending),'shredWireJoinRequired':self.shred_index is not None,'forkBindingMode':'finalized_whole_slot_poh' if self.finalized_transport else 'authoritative_executed_bank','catalogPools':len(self.configs),'configRevision':self.config_revision,'capacityDeferredMarkets':len(self.capacity_deferred)}
    def update_configs(self,configs):
        """Atomic control-path revision. Never reparse all configs per execution."""
        from native_solana import PROGRAMS
        from server import valid_mint
        incoming={c['pool']:dict(c) for c in configs}
        if len(incoming)>262144:raise EngineError('Cold pool registry bound exceeded')
        with self.lock:
            for pool,cfg in incoming.items():
                if cfg['program'] not in PROGRAMS or not all(valid_mint(cfg.get(k,'')) for k in ('pool','mint','quoteMint','baseVault','quoteVault')):raise EngineError('Invalid execution adapter identity')
                old=self.configs.get(pool)
                if old and any(old.get(k)!=cfg.get(k) for k in ('program','mint','quoteMint','baseVault','quoteVault','baseDecimals','quoteDecimals')):raise EngineError('Discovered pool conflicts with pinned identity')
            added=set(incoming)-set(self.configs)
            if not added:return
            merged={**self.configs,**incoming};revision=self.config_revision+1
            if self.shred_index is not None and hasattr(self.shred_index,'append_pools'):self.shred_index.append_pools({p:incoming[p] for p in added},revision)
            elif self.shred_index is not None and hasattr(self.shred_index,'prepare_pools'):self.shred_index.prepare_pools(merged,revision)
            self.configs=merged;self.config_revision=revision
            # Recover a small discovery-lag window from durable inputs. Older
            # history remains in the journal and requires an explicit replay.
            pb,_=protobuf_modules()
            from server import b58decode
            added_keys={b58decode(pool) for pool in added}
            for key,(raw,stamp) in self.recent_blocks.items():
                if key in self.dead or key in self.pending:continue
                block=pb.SubscribeUpdate.FromString(raw).block
                if self.shred_index is not None and hasattr(self.shred_index,'candidate_pools'):
                    # Cheap address hints on the control path. Publication still
                    # rechecks canonical wire, signatures and exact branch join.
                    relevant=any(any(bytes(key) in added_keys for key in [*info.transaction.message.account_keys,*info.meta.loaded_writable_addresses,*info.meta.loaded_readonly_addresses]) for info in block.transactions)
                else:
                    relevant=any(added.intersection(protobuf_receipt(info,block.slot,block.block_time.timestamp)[1]['transaction']['message']['accountKeys']) for info in block.transactions if not info.meta.HasField('err'))
                if relevant:
                    if len(self.pending)>=128:raise EngineError('New-pool replay window full; replay retained input journal')
                    self.pending[key]=(raw,stamp)
    def apply(self,raw,stamp):
        with self.lock:return self._apply(raw,stamp)
    def _apply(self,raw,stamp):
        pb,_=protobuf_modules();update=pb.SubscribeUpdate.FromString(raw);kind=update.WhichOneof('update_oneof')
        if kind=='block':
            block=update.block;key=(int(block.slot),block.blockhash if self.finalized_transport else int(block.bank_id))
            if not key[1]:raise EngineError('Exact executed bank identity unavailable; retain metadata without publishing candles')
            if not block.blockhash or not block.HasField('block_time') or block.block_time.timestamp<=0:raise EngineError('Exact executed block identity/time unavailable')
            if self.finalized_transport and any(k[0]==key[0] and value!=block.blockhash for k,value in self.banks.items()):raise EngineError('Conflicting finalized block at the same slot')
            old=self.banks.get(key)
            if old and old!=block.blockhash:raise EngineError('Ambiguous bank identity; correction must precede replacement')
            self.banks[key]=block.blockhash
            if len(self.banks)>8192 or len(self.dead)>8192:raise EngineError('Fork tracking bound reached; verify finalized recovery before reclaiming state')
            if key in self.dead:return
            previous=self.recent_blocks.pop(key,None)
            if previous:self.recent_bytes-=len(previous[0])
            self.recent_blocks[key]=(raw,stamp);self.recent_bytes+=len(raw)
            while len(self.recent_blocks)>128 or self.recent_bytes>32*1024**2:
                _,removed=self.recent_blocks.popitem(last=False);self.recent_bytes-=len(removed[0])
            prepared=[]
            if self.shred_index is not None:
                for info in block.transactions:
                    native=hasattr(self.shred_index,'matches_block')
                    if native:
                        encoded=info.SerializeToString();candidates=self.shred_index.candidate_pools(info,generation=self.config_revision,raw=encoded)
                        if not candidates:continue
                        prepared.append((info,candidates,encoded));matched=self.shred_index.matches_finalized_block(block,info,raw=encoded) if self.finalized_transport else self.shred_index.matches_block(block,info,raw=encoded)
                    else:
                        signature,tx=protobuf_receipt(info,block.slot,block.block_time.timestamp)
                        if tx['meta']['err'] is not None:continue
                        if not set(self.configs).intersection(tx['transaction']['message']['accountKeys']):continue
                        matched=self.shred_index.matches(block.slot,info)
                    if not matched:
                        if key not in self.pending and len(self.pending)>=128:raise EngineError('Executed results waiting for restored shreds exceed the bounded join window')
                        self.pending[key]=(raw,stamp);return
            self.pending.pop(key,None)
            started=time.monotonic_ns();events=[]
            native_decode=self.shred_index is not None and hasattr(self.shred_index,'native_decode')
            for info,candidates,encoded in prepared if native_decode else ((info,None,None) for info in block.transactions):
                if self.shred_index is not None and hasattr(self.shred_index,'native_decode'):
                    if info.meta.HasField('err'):self.state['failedTransactions']+=1;continue
                    if not candidates:continue
                    found=False
                    for pool in candidates:
                        cfg=self.configs[pool]
                        rows,reason=self.shred_index.native_decode(info,cfg,block.slot,block.block_time.timestamp,raw=encoded)
                        if not rows:continue
                        found=True
                        if pool not in self.runtime.registry and len(self.runtime.registry)>=self.runtime.max_markets:
                            self.capacity_deferred.add(pool);self.state['capacityDeferredExecutions']=self.state.get('capacityDeferredExecutions',0)+len(rows);continue
                        from native_solana import PROGRAMS
                        identifier=self.runtime.market(pool,{'market':pool,'program':cfg['program'],'baseMint':cfg['mint'],'quoteMint':cfg['quoteMint'],'quoteSymbol':cfg.get('quote',''),'network':'solana','venue':PROGRAMS[cfg['program']],'series':'trade'})
                        events.extend(executed_event(identifier,row,block.blockhash) for row in rows)
                    if not found:self.state['unsupportedTransactions']+=1
                    continue
                signature,tx=protobuf_receipt(info,block.slot,block.block_time.timestamp)
                if tx['meta']['err'] is not None:self.state['failedTransactions']+=1;continue
                keys=set(tx['transaction']['message']['accountKeys']);found=False
                for pool,cfg in self.configs.items():
                    if pool not in keys:continue
                    rows,reason=decode(tx,cfg,signature,int(info.index))
                    if not rows:continue
                    found=True;identifier=self.runtime.market(pool,{'market':pool,'program':cfg['program'],'baseMint':cfg['mint'],'quoteMint':cfg['quoteMint'],'quoteSymbol':cfg.get('quote',''),'network':'solana','venue':'Raydium AMM v4','series':'trade'})
                    events.extend(executed_event(identifier,row,block.blockhash) for row in rows)
                if not found:self.state['unsupportedTransactions']+=1
            decode_ms=(time.monotonic_ns()-started)/1e6
            for offset in range(0,len(events),500):
                results=self.runtime.submit_many(events[offset:offset+500])
                if any(any(r<0 for r in result) for result in results):raise EngineError('Executed result admission paused; retain original input for reconciliation')
                self.state['decodedFills']+=sum(any(r==1 for r in result) for result in results)
            self.state['receivedBlocks']+=1;self.state['lastSlot']=max(self.state.get('lastSlot',0),int(block.slot))
            self.state['latencyLastSample']={'providerAgeMs':max(0,(stamp/1e9-update.created_at.seconds-update.created_at.nanos/1e9)*1000) if update.HasField('created_at') else None,'blockClockAgeMs':max(0,(stamp/1e9-block.block_time.timestamp)*1000),'decodeMs':decode_ms,'receiveToCoreMs':(time.time_ns()-stamp)/1e6,'core':self.runtime.last_latency,'fanoutMs':None,'browserRenderMs':None}
        elif kind=='slot':
            slot=update.slot;key=(int(slot.slot),int(slot.bank_id))
            if self.finalized_transport and slot.status!=pb.SLOT_FINALIZED:raise EngineError('Nonfinalized status on finalized-only transport; reconcile upstream')
            if slot.status==pb.SLOT_DEAD:
                if key[1]==0 and not slot.HasField('bank_id'):raise EngineError('Dead slot has no exact bank identity; pause and reconcile upstream')
                self.dead.add(key)
                self.pending.pop(key,None)
                if len(self.dead)>8192:raise EngineError('Dead-bank tracking bound reached')
                if key in self.banks:
                    e=Event();e.action=1;e.fork[:]=hashlib.sha256(self.banks[key].encode()).digest()
                    if any(r<0 for r in self.runtime.submit(e)):raise EngineError('Fork correction rejected')
                    self.state['corrections']+=1
            elif slot.status==pb.SLOT_FINALIZED:self.state['observedFinalizedSlot']=int(slot.slot)
        else:raise EngineError('Only executed blocks and exact slot status are admitted')
    def accept(self,raw,stamp=None):
        with self.lock:
            stamp=stamp or time.time_ns();self.journal.append(raw,stamp);self.apply(raw,stamp);atomic_json(self.path,self.status())
    def retry_pending(self):
        with self.lock:
            for raw,stamp in list(self.pending.values()):self.apply(raw,stamp)
            atomic_json(self.path,self.status())

