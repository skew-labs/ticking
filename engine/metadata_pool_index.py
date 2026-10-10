"""Reusable C++ address index for executed metadata; no shred dependency.

Only candidate selection is native. Execution validation and persistence keep
their existing decoder contract. Unsupported native envelopes fall back to a
binary-key lookup, never to dropping a result.
"""
import ctypes
from pathlib import Path
from server import b58decode

class MetadataPoolIndex:
    def __init__(self,library=None):
        self.handle=None
        self.lib=ctypes.CDLL(str(library or Path(__file__).parent/'engine/libslotline_metadata.so'))
        self.lib.sl_pool_index_new.argtypes=[ctypes.c_char_p,ctypes.c_uint32];self.lib.sl_pool_index_new.restype=ctypes.c_void_p
        self.lib.sl_pool_index_delete.argtypes=[ctypes.c_void_p]
        self.lib.sl_pool_index_keys.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32,ctypes.POINTER(ctypes.c_uint32),ctypes.POINTER(ctypes.c_uint32)]
        self.lib.sl_pool_index_batch.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32,ctypes.POINTER(ctypes.c_uint32),ctypes.c_uint32,ctypes.POINTER(ctypes.c_uint32),ctypes.POINTER(ctypes.c_uint32)]
        self.handle=None;self.configs=();self.out=(ctypes.c_uint32*256)();self.count=ctypes.c_uint32(256)
        self.lookups=0;self.fallbacks=0;self.batches=0
        self.offsets=(ctypes.c_uint32*1025)();self.batch_counts=(ctypes.c_uint32*1024)();self.batch_out=(ctypes.c_uint32*(1024*256))()
    def prepare(self,configs):
        if len(configs)>262144:raise ValueError('Native metadata pool index bound')
        keys=[b58decode(k) for k in configs]
        if any(len(k)!=32 for k in keys):raise ValueError('Invalid pool address')
        raw=b''.join(keys);handle=self.lib.sl_pool_index_new(raw,len(keys)) if keys else None
        if keys and not handle:raise ValueError('Native pool index construction failed')
        previous=self.handle;self.handle=handle;self.configs=tuple(configs.values())
        if previous:self.lib.sl_pool_index_delete(previous)
    def candidates(self,info):
        if not self.handle:return []
        # Protobuf has already been decoded by the transport. Transfer only
        # fixed-width addresses rather than serializing/parsing it a second time.
        keys=[*info.transaction.message.account_keys,*info.meta.loaded_writable_addresses,*info.meta.loaded_readonly_addresses]
        if any(len(k)!=32 for k in keys):self.fallbacks+=1;return None
        raw=b''.join(keys);self.count.value=256;self.lookups+=1
        status=self.lib.sl_pool_index_keys(self.handle,raw,len(raw),self.out,ctypes.byref(self.count))
        if status!=1 or self.count.value>256:
            self.fallbacks+=1;return None
        return [self.configs[self.out[i]] for i in range(self.count.value)]
    def candidates_many(self,infos):
        if len(infos)>1024:raise ValueError('Native metadata batch bound')
        if not infos:return []
        if not self.handle:return [[] for _ in infos]
        chunks=[];size=0;self.offsets[0]=0
        for i,info in enumerate(infos):
            keys=[*info.transaction.message.account_keys,*info.meta.loaded_writable_addresses,*info.meta.loaded_readonly_addresses]
            if len(keys)>256 or any(len(k)!=32 for k in keys):self.fallbacks+=len(infos);return [None]*len(infos)
            chunks.extend(keys);size+=len(keys)*32;self.offsets[i+1]=size
        raw=b''.join(chunks);self.lookups+=len(infos);self.batches+=1
        status=self.lib.sl_pool_index_batch(self.handle,raw,len(raw),self.offsets,len(infos),self.batch_out,self.batch_counts)
        if status!=1:self.fallbacks+=len(infos);return [None]*len(infos)
        return [[self.configs[self.batch_out[256*i+j]] for j in range(self.batch_counts[i])] for i in range(len(infos))]
    def updates(self,updates):
        selected=[(i,u.transaction.transaction) for i,u in enumerate(updates) if u.WhichOneof('update_oneof')=='transaction' and u.transaction.transaction.HasField('meta') and not u.transaction.transaction.meta.HasField('err')]
        rows=[None]*len(updates)
        for (i,_),candidates in zip(selected,self.candidates_many([info for _,info in selected])):rows[i]=candidates
        return rows
    def close(self):
        if self.handle:self.lib.sl_pool_index_delete(self.handle);self.handle=None
    def __del__(self):self.close()
