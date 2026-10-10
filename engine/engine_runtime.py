"""Bounded executed-result engine. JSON ends at this boundary; the core/WAL are binary.
No signing, consensus, full-account indexing, or transaction replay occurs here.
"""
import ctypes as C,fcntl,hashlib,json,os,queue,struct,threading,time,zlib
from collections import OrderedDict
from chart_product import ExecutionTape,select
from execution_history import ExecutionHistory,HistoryError,anchor,ZERO
from pathlib import Path
ROOT=Path(__file__).resolve().parent
class Event(C.Structure):
    _fields_=[('slot',C.c_uint64),('time',C.c_uint64),('transaction',C.c_uint32),('event',C.c_uint32),('market',C.c_uint32),('action',C.c_uint32),('id',C.c_ubyte*32),('fork',C.c_ubyte*32),('price',C.c_double),('volume',C.c_double),('base',C.c_double)]
class Candle(C.Structure):
    _fields_=[('time',C.c_uint64),('open',C.c_double),('high',C.c_double),('low',C.c_double),('close',C.c_double),('volume',C.c_double),('base',C.c_double),('count',C.c_uint64),('revision',C.c_uint64)]
assert C.sizeof(Event)==120 and C.sizeof(Candle)==72
HEADER=struct.Struct('<4sIQI');MAGIC=b'SLE1'
class EngineError(ValueError):pass
def delivery_status(inputs,failure=None,closed=False):
    """A connected socket or an old trade alone is never a live-data promise."""
    geyser=inputs.get('geyser',{});shreds=inputs.get('shreds',{})
    last=geyser.get('lastStreamReceiveNs');age=max(0,(time.time_ns()-last)/1e6) if isinstance(last,int) and last>0 else None
    if closed or failure or geyser.get('failure') or shreds.get('failure'):state='unavailable'
    elif geyser.get('gapRecoveryRequired'):state='recovering'
    elif not geyser.get('validatorStreamConnected') or not shreds.get('nodeRunning'):state='unverified_source'
    elif not geyser.get('shredWireJoinRequired') or not geyser.get('receivedBlocks'):state='unverified_source'
    elif age is None or age>5000:state='stale'
    else:state='live'
    return {'state':state,'live':state=='live','inputAgeMs':round(age,3) if age is not None else None,
            'freshnessLimitMs':5000,'recoveryRequired':bool(geyser.get('gapRecoveryRequired')),
            'executionToSDKLatencyMs':None}
class Core:
    def __init__(self,capacity=65536,markets=100,window=2400):
        self.lib=C.CDLL(str(ROOT/'engine/libslotline.so'));l=self.lib
        l.sl_create.argtypes=[C.c_uint32]*3;l.sl_create.restype=C.c_void_p
        l.sl_destroy.argtypes=[C.c_void_p];l.sl_apply.argtypes=[C.c_void_p,C.POINTER(Event)];l.sl_apply.restype=C.c_int
        l.sl_candle.argtypes=[C.c_void_p,C.c_uint32,C.c_uint64,C.POINTER(Candle)];l.sl_candle.restype=C.c_int
        l.sl_history.argtypes=[C.c_void_p,C.c_uint32,C.c_uint64,C.POINTER(Candle),C.c_uint32];l.sl_history.restype=C.c_uint32
        l.sl_changes.argtypes=[C.c_void_p,C.c_uint32,C.c_uint64,C.POINTER(Candle),C.c_uint32];l.sl_changes.restype=C.c_int
        l.sl_revision.argtypes=[C.c_void_p];l.sl_revision.restype=C.c_uint64;l.sl_active.argtypes=[C.c_void_p];l.sl_active.restype=C.c_uint64
        self.ptr=l.sl_create(capacity,markets,window);self.lock=threading.Lock();self.sequence=0
        if not self.ptr:raise EngineError('Native core allocation failed')
    def apply(self,event,sequence=0):
        with self.lock:
            result=self.lib.sl_apply(self.ptr,C.byref(event));self.sequence=max(self.sequence,sequence);return result
    def history(self,market,before=0,limit=300):
        limit=max(1,min(int(limit),2400));out=(Candle*limit)()
        with self.lock:n=self.lib.sl_history(self.ptr,market,before,out,limit)
        return [dict(time=c.time,open=c.open,high=c.high,low=c.low,close=c.close,volume=c.volume,baseVolume=c.base,count=c.count,revision=c.revision) for c in out[:n]]
    def page(self,market,before=0,limit=300):
        limit=max(1,min(int(limit),2400));out=(Candle*limit)()
        with self.lock:
            n=self.lib.sl_history(self.ptr,market,before,out,limit);sequence=self.lib.sl_revision(self.ptr)
        candles=[dict(time=c.time,open=c.open,high=c.high,low=c.low,close=c.close,volume=c.volume,baseVolume=c.base,count=c.count,revision=c.revision) for c in out[:n]]
        return {'candles':candles,'sequence':sequence,'hasMore':n==limit,'nextBefore':candles[0]['time'] if candles else None}
    def changes(self,market,after):
        out=(Candle*2400)()
        with self.lock:n=self.lib.sl_changes(self.ptr,market,after,out,2400);sequence=self.lib.sl_revision(self.ptr)
        if n<0:return {'gap':True,'candles':[],'deleted':[],'sequence':sequence}
        values=[dict(time=c.time,open=c.open,high=c.high,low=c.low,close=c.close,volume=c.volume,baseVolume=c.base,count=c.count) for c in out[:n] if c.count]
        return {'candles':values,'deleted':[c.time for c in out[:n] if not c.count],'sequence':sequence,'gap':False}
    def candle(self,market,time):
        out=Candle()
        with self.lock:found=self.lib.sl_candle(self.ptr,market,time,C.byref(out))
        return {k:getattr(out,k) for k,_ in Candle._fields_} if found else None
    def close(self):
        if self.ptr:self.lib.sl_destroy(self.ptr);self.ptr=None

def executed_event(market,trade,blockhash):
    if not blockhash or trade.get('series','trade')!='trade':raise EngineError('Executed trade and exact block identity required')
    e=Event();e.slot=int(trade['slot']);e.time=int(trade['time']);e.transaction=int(trade.get('order',0));e.event=int(trade.get('event',0));e.market=market
    if min(e.slot,e.time,e.market)<=0 or min(int(trade.get('order',0)),int(trade.get('event',0)))<0:raise EngineError('Invalid execution order')
    if int(trade.get('order',0))>2**32-1 or int(trade.get('event',0))>2**32-1:raise EngineError('Execution index overflow')
    e.id[:]=hashlib.sha256(str(trade['id']).encode()).digest();e.fork[:]=hashlib.sha256(blockhash.encode()).digest();e.price=float(trade['price']);e.volume=float(trade['volume']);e.base=float(trade['baseVolume']);return e

class EngineRuntime:
    """One persistence worker + fixed market-affinity workers. Admission is bounded.
    Publication occurs only after fdatasync and application. WAL is append-only;
    checkpoints never delete original records. Corrupt/truncated WAL fails closed.
    """
    def __init__(self,directory,workers=4,capacity=16384,max_markets=100,window=2400,queue_size=2048,max_wal_bytes=32*1024**3,min_free_bytes=200*1024**3,max_history_bytes=96*1024**3,history_process=True):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        if min(workers,capacity,max_markets,window,queue_size,max_wal_bytes)<1 or min_free_bytes<0:raise EngineError('Invalid engine resource bounds')
        self.lock_fd=os.open(self.directory/'writer.lock',os.O_RDWR|os.O_CREAT,0o600)
        try:fcntl.flock(self.lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd);self.lock_fd=None;raise EngineError('Another engine owns this execution journal')
        try:self.initialize(workers,capacity,max_markets,window,queue_size,max_wal_bytes,min_free_bytes,max_history_bytes,history_process)
        except BaseException:
            if hasattr(self,'historical'):self.historical.close()
            for core in getattr(self,'cores',()):core.close()
            if hasattr(self,'fd'):os.close(self.fd)
            os.close(self.lock_fd);self.lock_fd=None
            raise
    def initialize(self,workers,capacity,max_markets,window,queue_size,max_wal_bytes,min_free_bytes,max_history_bytes,history_process):
        self.registry_path=self.directory/'markets.json';self.registry=json.loads(self.registry_path.read_text()) if self.registry_path.exists() else {};self.registry_lock=threading.Lock();self.max_markets=max_markets;self.identities_path=self.directory/'identities.json';self.identities=json.loads(self.identities_path.read_text()) if self.identities_path.exists() else {}
        self.selection_cache=OrderedDict();self.selection_items=0
        self.cores=[Core(capacity,max_markets,window) for _ in range(workers)];self.workers=workers;self.wal_path=self.directory/'executions.sle';self.sequence=0;self.durable=0;self.accepted=0;self.duplicates=0;self.rejected=0;self.last_latency={};self.closed=False;self.shutdown_complete=False;self.failure=None
        self.admission_lock=threading.Lock();self.close_lock=threading.Lock();self.publication_lock=threading.Lock()
        self.max_wal_bytes=max_wal_bytes;self.min_free_bytes=min_free_bytes;self.storage_check_at=0.;self.wal_bytes=self.wal_path.stat().st_size if self.wal_path.exists() else 0
        self.tape=ExecutionTape();self.projection_lock=threading.Lock();self.projection_pending={};self.projection_next=1;self.projection_capacity=(workers+1)*queue_size+256
        self.publications=queue.Queue(self.projection_capacity)
        self.historical=ExecutionHistory(self.directory,dict(workers=workers,capacity=capacity,maxMarkets=max_markets,window=window),max_history_bytes,min_free_bytes)
        self.pending=queue.Queue(queue_size);self.channels=[queue.Queue(queue_size) for _ in self.cores];self.recover()
        if history_process:self.historical.start_worker()
        self.fd=os.open(self.wal_path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600);os.fsync(self.fd);self.sync_directory()
        self.publisher=threading.Thread(target=self.publish,daemon=True,name='slotline-history-publication');self.publisher.start()
        self.threads=[threading.Thread(target=self.consume,args=(i,),daemon=True,name='slotline-market-'+str(i)) for i in range(workers)]
        for thread in self.threads:thread.start()
        self.writer=threading.Thread(target=self.persist,daemon=True,name='slotline-journal');self.writer.start()
    def market(self,address,identity=None):
        if not isinstance(address,str) or not 1<=len(address)<=120:raise EngineError('Invalid market identity')
        with self.registry_lock:
            if address not in self.registry and len(self.registry)>=self.max_markets:raise EngineError('Selected market limit reached')
            if identity:
                previous=self.identities.get(address)
                if previous and previous!=identity:raise EngineError('Pinned market identity conflict')
                if not previous:
                    value=dict(self.identities);value[address]=identity;tmp=self.identities_path.with_suffix('.tmp')
                    with tmp.open('w') as f:json.dump(value,f);f.flush();os.fsync(f.fileno())
                    os.replace(tmp,self.identities_path);self.sync_directory();self.identities=value;self.selection_cache.clear();self.selection_items=0
            if address in self.registry:return self.registry[address]
            identifier=max(self.registry.values(),default=0)+1;value=dict(self.registry);value[address]=identifier
            tmp=self.registry_path.with_suffix('.tmp');fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:json.dump(value,f);f.flush();os.fsync(f.fileno())
            os.replace(tmp,self.registry_path);self.sync_directory();self.registry=value;self.selection_cache.clear();self.selection_items=0;return identifier
    def sync_directory(self):
        fd=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    def replay(self,e,sequence=0):
        targets=range(self.workers) if e.action else [e.market%self.workers]
        results=[self.cores[i].apply(e,sequence) for i in targets]
        self.tape.apply(e,not any(r<0 for r in results) and any(r==1 for r in results),sequence)
        self.projection_next=sequence+1
        return results
    def recover(self):
        cursor=self.historical.cursor;expected_hash=self.historical.hash;digest=ZERO;records=[]
        if not self.wal_path.exists():
            if cursor:raise EngineError('Historical index is ahead of the retained execution log')
            return
        with self.wal_path.open('rb') as f:
            while True:
                header=f.read(HEADER.size)
                if not header:break
                if len(header)!=HEADER.size:raise EngineError('Truncated execution log; retain and repair from upstream')
                magic,length,seq,crc=HEADER.unpack(header);payload=f.read(length) if length==120 else b''
                if magic!=MAGIC or length!=120 or len(payload)!=length or zlib.crc32(payload)!=crc or seq!=self.sequence+1:raise EngineError('Execution log integrity failure; no silent truncation')
                e=Event.from_buffer_copy(payload);results=self.replay(e,seq);self.sequence=seq
                accepted=not any(r<0 for r in results) and any(r==1 for r in results)
                digest=anchor(digest,seq,payload,accepted)
                if seq==cursor and digest!=expected_hash:raise EngineError('Historical index does not match retained executions and verdicts')
                if seq>cursor:
                    records.append((seq,payload,e,accepted))
                    if len(records)>=256:self.historical.apply_batch(records);records=[]
        if cursor>self.sequence:raise EngineError('Historical index is ahead of the retained execution log')
        if records:self.historical.apply_batch(records)
        self.durable=self.sequence
    def submit(self,event,timeout=10):
        request={'payload':bytes(event),'done':threading.Event(),'received':time.monotonic_ns(),'results':[],'remaining':self.workers if event.action else 1,'lock':threading.Lock()}
        with self.admission_lock:
            if self.closed or self.failure:raise EngineError('Engine is unavailable')
            try:self.pending.put(request,timeout=min(timeout,1))
            except queue.Full:raise EngineError('Engine queue full; recover from the upstream cursor')
        if not request['done'].wait(timeout):raise EngineError('Execution acknowledgement timed out; replay identical identity to reconcile')
        if request.get('error'):raise EngineError(request['error'])
        return request['results']
    def submit_many(self,events,timeout=10):
        events=list(events)
        if len(events)>1024:raise EngineError('Executed batches are limited to 1024 records')
        deadline=time.monotonic()+timeout;requests=[]
        for event in events:
            req={'payload':bytes(event),'done':threading.Event(),'received':time.monotonic_ns(),'results':[],'remaining':self.workers if event.action else 1,'lock':threading.Lock()}
            with self.admission_lock:
                if self.closed or self.failure:raise EngineError('Partial batch admission; replay identical execution identities after recovery')
                try:self.pending.put(req,timeout=max(0,min(1,deadline-time.monotonic())))
                except queue.Full:raise EngineError('Partial batch admission; replay every identical execution identity to reconcile')
            requests.append(req)
        for req in requests:
            if not req['done'].wait(max(0,deadline-time.monotonic())):raise EngineError('Batch acknowledgement timed out; replay identical identities')
            if req.get('error'):raise EngineError(req['error'])
        return [req['results'] for req in requests]
    def persist(self):
        while True:
            first=self.pending.get()
            if first is None:return
            if self.failure:
                first['error']=self.failure;first['done'].set();continue
            batch=[first]
            while len(batch)<256:
                try:item=self.pending.get_nowait()
                except queue.Empty:break
                if item is None:self.pending.put(None);break
                batch.append(item)
            try:
                size=len(batch)*(HEADER.size+120)
                if self.wal_bytes+size>self.max_wal_bytes:raise EngineError('Execution journal capacity reached')
                now=time.monotonic()
                if now>=self.storage_check_at:
                    disk=os.statvfs(self.directory)
                    if disk.f_bavail*disk.f_frsize-size<self.min_free_bytes:raise EngineError('Execution journal free-space reserve reached')
                    self.storage_check_at=now+1.
                frames=[]
                for req in batch:
                    self.sequence+=1;req['sequence']=self.sequence;frames.append(HEADER.pack(MAGIC,120,self.sequence,zlib.crc32(req['payload']))+req['payload'])
                data=b''.join(frames);offset=0
                while offset<len(data):
                    written=os.write(self.fd,data[offset:])
                    if written<=0:raise OSError('Execution journal write made no progress')
                    offset+=written
                os.fdatasync(self.fd);self.wal_bytes+=len(data);self.durable=self.sequence;persisted=time.monotonic_ns()
                for req in batch:
                    req['persisted']=persisted;e=Event.from_buffer_copy(req['payload']);targets=range(self.workers) if e.action else [e.market%self.workers]
                    for i in targets:self.channels[i].put(req)
            except Exception as error:
                self.failure=(str(error)+'; retain originals and resume after repair' if isinstance(error,EngineError)
                              else 'Execution journal failed; restart and reconcile retained log')
                self.fail_pending()
                for req in batch:req['error']=self.failure;req['done'].set()
    def fail_pending(self):
        with self.projection_lock:
            for req in self.projection_pending.values():req['error']=self.failure;req['done'].set()
            self.projection_pending.clear()
    def consume(self,index):
        while True:
            req=self.channels[index].get()
            if req is None:return
            try:
                if self.failure:raise EngineError(self.failure)
                result=self.cores[index].apply(Event.from_buffer_copy(req['payload']),req['sequence'])
            except Exception:
                self.failure=self.failure or 'Execution worker failed; recover the retained execution journal'
                self.fail_pending()
                with req['lock']:req['error']=self.failure;req['done'].set()
                continue
            with req['lock']:
                req['results'].append(result);req['remaining']-=1
                if not req['remaining']:
                    if any(r<0 for r in req['results']):self.rejected+=1
                    elif any(r==1 for r in req['results']):self.accepted+=1
                    else:self.duplicates+=1
                    try:self.publish_projection(req)
                    except Exception:
                        self.failure='Customer projection failed; recover the retained execution journal';req['error']=self.failure
                        req['done'].set()
    def publish_projection(self,request):
        # Workers still own separate cores. Only the small recent customer
        # projection is published in durable WAL order, including retractions.
        with self.projection_lock:
            if len(self.projection_pending)>=self.projection_capacity:
                self.failure='Customer projection queue full; recover the retained execution log';request['error']=self.failure;request['done'].set();raise EngineError(self.failure)
            self.projection_pending[request['sequence']]=request
            while self.projection_next in self.projection_pending:
                req=self.projection_pending.pop(self.projection_next)
                try:self.publications.put_nowait(req)
                except queue.Full:
                    self.failure='Historical publication backlog full; recover retained executions';req['error']=self.failure;req['done'].set();raise EngineError(self.failure)
                self.projection_next+=1
    def publish(self):
        while True:
            first=self.publications.get()
            if first is None:return
            batch=[first];gather_until=time.monotonic()+.002
            while len(batch)<256:
                try:item=self.publications.get(timeout=max(0,gather_until-time.monotonic()))
                except queue.Empty:break
                if item is None:self.publications.put(None);break
                batch.append(item)
            try:
                if self.failure:raise EngineError(self.failure)
                records=[(req['sequence'],req['payload'],Event.from_buffer_copy(req['payload']),not any(r<0 for r in req['results']) and any(r==1 for r in req['results'])) for req in batch]
                def project(records):
                    for seq,payload,event,accepted in records:self.tape.apply(event,accepted,seq)
                self.historical.apply_batch(records,self.publication_lock,project)
                finished=time.monotonic_ns()
                for req in batch:
                    self.last_latency={'journalMs':(req['persisted']-req['received'])/1e6,'historicalPublicationMs':(finished-req['persisted'])/1e6,'resultToPublicationMs':(finished-req['received'])/1e6}
                    req['done'].set()
            except Exception as error:
                self.failure=(str(error)+'; recover retained originals' if isinstance(error,(EngineError,HistoryError)) else 'Historical publication failed; recover retained originals')
                for req in batch:req['error']=self.failure;req['done'].set()
                self.fail_pending()
    def history(self,address,before=0,limit=300):
        identifier=self.registry.get(address)
        return self.cores[identifier%self.workers].history(identifier,before,limit) if identifier else []
    def page(self,address,before=0,limit=300):
        identifier=self.registry.get(address)
        if not identifier:return {'candles':[],'sequence':0,'hasMore':False,'nextBefore':None}
        value=self.cores[identifier%self.workers].page(identifier,before,limit);value['identity']=self.identities.get(address);return value
    def changes(self,address,after):
        identifier=self.registry.get(address)
        if not identifier:return {'candles':[],'deleted':[],'sequence':0,'gap':False}
        return self.cores[identifier%self.workers].changes(identifier,after)
    def selection(self,pool=None,token=None,quote=None):
        if self.failure:raise EngineError(self.failure)
        try:
            with self.registry_lock:
                key=(pool,token,quote)
                selected=self.selection_cache.get(key)
                if selected is None:
                    selected=select(self.identities,pool,token,quote);weight=len(selected['pools'])
                    self.selection_cache[key]=selected;self.selection_items+=weight
                    while len(self.selection_cache)>256 or self.selection_items>4096:
                        _,removed=self.selection_cache.popitem(last=False);self.selection_items-=len(removed['pools'])
                else:self.selection_cache.move_to_end(key)
                # Selection metadata is immutable inside the registry; returned
                # containers belong to this reader and cannot poison the cache.
                selection={**selected,'identity':dict(selected['identity']),'coverage':dict(selected['coverage']),
                           'pools':list(selected['pools']),'invertedPools':list(selected['invertedPools'])}
                registry=dict(self.registry)
        except ValueError:
            if hasattr(self,'pool_directory') and bool(pool)!=bool(token):self.pool_directory.describe(pool,token,self.registry)
            raise
        return selection,registry
    def resolve(self,pool=None,token=None,quote=None):
        """Identity-only attach: never read history or sort the recent tape."""
        if self.closed or self.failure:raise EngineError('Execution engine requires recovery')
        try:selection,_=self.selection(pool,token,quote)
        except ValueError:
            # Invalid selectors and quote mismatches are not discovery jobs.
            from server import valid_mint
            if bool(pool)==bool(token) or not valid_mint(pool or token) or (quote and not valid_mint(quote)):raise
            if not hasattr(self,'pool_directory'):raise
            pending=self.coverage(pool,token,quote)
            if pending['state']=='registered':raise
            return {**pending,'ready':False,'retryAfterMs':1000,
                    'delivery':delivery_status(self.input_status() if hasattr(self,'input_status') else {},self.failure,self.closed)}
        membership=hashlib.sha256(('|'.join(selection['pools'])+';'+'|'.join(sorted(selection['invertedPools']))).encode()).hexdigest()[:16]
        selection['coverage']['history']='retained_accepted_execution_journal'
        selection.pop('invertedPools',None)
        return {**selection,'ready':True,'source':'slotline_verified_normalized_executions',
                'epoch':self.tape.epoch+':'+membership if token else self.tape.epoch,
                'delivery':delivery_status(self.input_status() if hasattr(self,'input_status') else {},self.failure,self.closed)}
    def product(self,pool=None,token=None,quote=None,interval=1,before=0,limit=300,after=None,epoch=None,includeCandles=True,includeSnapshotTrades=True):
        selection,registry=self.selection(pool,token,quote)
        if includeCandles:
            def capture(cursor):
                if cursor!=self.tape.sequence:raise EngineError('Historical publication requires recovery')
                value=self.tape.snapshot(selection,registry,interval,before,limit,after,epoch,False,includeSnapshotTrades)
                value['historyReset']=bool(value['gap'] or after is None or any(self.historical.corrections.get(registry[p],0)>after for p in selection['pools']))
                return value
            with self.historical.snapshot(self.publication_lock,capture) as ((cursor,digest),value):
                page=self.historical.page(selection,registry,interval,before,limit)
            value.update({k:page[k] for k in ('candles','hasMore','nextBefore')})
            value['coverage'].update(history='retained_accepted_execution_journal',historicalCursor=cursor,
                historicalExecutions=page['retainedExecutions'],oldestExecutionTime=page['oldestExecutionTime'],
                latestExecutionTime=page['latestExecutionTime'],upstreamCompleteness='not_attested',
                historicalWindowTruncated=False)
        else:
            with self.publication_lock:
                value=self.tape.snapshot(selection,registry,interval,before,limit,after,epoch,False,includeSnapshotTrades)
                value['historyReset']=bool(value['gap'] or after is None or any(self.historical.corrections.get(registry[p],0)>after for p in selection['pools']))
        inputs=self.input_status() if hasattr(self,'input_status') else {}
        value['delivery']=delivery_status(inputs,self.failure,self.closed)
        return value
    def coverage(self,pool=None,token=None,quote=None):
        from server import valid_mint
        if quote and not valid_mint(quote):raise ValueError('Invalid Solana quote address')
        if hasattr(self,'pool_directory'):
            value=self.pool_directory.describe(pool,token,self.registry)
            try:
                selection=select(self.identities,pool,token,quote)
                if selection['pools']:value['state']='registered'
            except ValueError:
                if value['state']=='registered':value['state']='awaiting_first_verified_execution'
                if quote:
                    matching=[p for p in value['pools'] if (p['quoteMint']==quote if pool or p['mint']==token else p['mint']==quote)]
                    if not matching and value['pools']:value['state']='quote_unavailable'
                if value['state']=='awaiting_first_verified_execution' and len(self.registry)>=self.max_markets:value['state']='capacity_limited'
            return value
        selection=select(self.identities,pool,token,quote)
        return {'address':pool or token,'state':'registered','pools':selection['pools'],'entireSolanaComplete':False,'automaticNewPools':False}
    def checkpoint(self):
        # A recovery anchor, not a partial in-memory snapshot. Replay validates the
        # entire retained log; never skip records using this checkpoint cursor.
        value={'format':'SLE1','durableSequence':self.durable,'markets':self.registry,'rootSource':'upstream_slot_status','log':self.wal_path.name,'recovery':'validated_binary_log_replay','historicalCursor':self.historical.cursor,'historicalAnchorSHA256':self.historical.hash}
        p=self.directory/'checkpoint.json';tmp=p.with_suffix('.tmp')
        with tmp.open('w') as f:json.dump(value,f);f.flush();os.fsync(f.fileno())
        os.replace(tmp,p);self.sync_directory();return value
    def close(self):
        with self.close_lock:
            if self.shutdown_complete:return
            deadline=time.monotonic()+20
            with self.admission_lock:
                self.closed=True;self.pending.put(None,timeout=max(.001,deadline-time.monotonic()))
            def finish(thread):
                thread.join(max(0,deadline-time.monotonic()))
                if thread.is_alive():raise EngineError('Engine shutdown exceeded its deadline; retain journal ownership until process exits')
            finish(self.writer)
            for channel in self.channels:channel.put(None,timeout=max(.001,deadline-time.monotonic()))
            for thread in self.threads:finish(thread)
            self.publications.put(None,timeout=max(.001,deadline-time.monotonic()));finish(self.publisher)
            try:self.checkpoint()
            finally:
                self.historical.close();os.close(self.fd)
                for core in self.cores:core.close()
                if self.lock_fd is not None:os.close(self.lock_fd);self.lock_fd=None
                self.shutdown_complete=True
