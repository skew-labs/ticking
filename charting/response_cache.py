"""Generation-bound public candle cache; bounded memory and coalesced reads."""
from collections import OrderedDict
import copy,json,threading,time

class ResponseCache:
    def __init__(self,max_bytes=8*1024*1024,max_entries=128,max_inflight=32):
        self.lock=threading.Lock();self.items=OrderedDict();self.pending={};self.bytes=0
        self.max_bytes=max_bytes;self.max_entries=max_entries;self.max_inflight=max_inflight
        self.hits=0;self.misses=0;self.coalesced=0
    def read(self,key,fn,ttl=1):
        with self.lock:
            item=self.items.get(key)
            if item and time.monotonic()<item[0]:
                self.hits+=1;self.items.move_to_end(key);return copy.deepcopy(item[1])
            if item:self.bytes-=item[2];del self.items[key]
            job=self.pending.get(key)
            if job:self.coalesced+=1;owner=False
            else:
                if len(self.pending)>=self.max_inflight:return 503,{'detail':'Chart readers are busy; retry shortly.'}
                job={'done':threading.Event()};self.pending[key]=job;owner=True;self.misses+=1
        if not owner:
            if not job['done'].wait(10):return 503,{'detail':'Chart read is still preparing; retry shortly.'}
            if 'error' in job:raise job['error']
            return copy.deepcopy(job['value'])
        try:
            value=fn();size=len(json.dumps(value,default=str,separators=(',',':')).encode())
            with self.lock:
                if value[0]==200 and size<=896*1024:
                    while self.items and (self.bytes+size>self.max_bytes or len(self.items)>=self.max_entries):
                        _,old=self.items.popitem(last=False);self.bytes-=old[2]
                    self.items[key]=(time.monotonic()+ttl,copy.deepcopy(value),size);self.bytes+=size
                job['value']=value
            return value
        except Exception as e:
            job['error']=e;raise
        finally:
            with self.lock:self.pending.pop(key,None);job['done'].set()

def generation(readers,qs):
    import os
    name=str(qs.get('mint') or qs.get('market') or qs.get('pool') or '').split(':')[0]
    selected=[(name,readers[name])] if name in readers else sorted(readers.items())
    result=[]
    for chain,reader in selected:
        for path in [reader.deep_path,reader.hot_path,reader.hot_path+'-wal']:
            try:
                s=os.stat(path);result.append((chain,path,s.st_ino,s.st_size,s.st_mtime_ns))
            except FileNotFoundError:result.append((chain,path,None))
    return tuple(result)
