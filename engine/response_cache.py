"""Payload-byte bounded LRU. Caller owns locking; values are immutable bytes."""
from collections import OrderedDict

class ResponseCache:
 def __init__(self,max_bytes=16*1024**2,max_entries=512):
  self.rows=OrderedDict();self.bytes=0;self.max_bytes=max_bytes;self.max_entries=max_entries
 def get(self,key):
  row=self.rows.get(key)
  if row:self.rows.move_to_end(key)
  return row
 def put(self,key,stamp,payload):
  old=self.rows.pop(key,None)
  if old:self.bytes-=len(old[1])
  if len(payload)>self.max_bytes:return
  self.rows[key]=(stamp,payload);self.bytes+=len(payload)
  while len(self.rows)>self.max_entries or self.bytes>self.max_bytes:
   _,old=self.rows.popitem(last=False);self.bytes-=len(old[1])
