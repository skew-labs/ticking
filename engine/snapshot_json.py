"""Bounded reader cache for atomically replaced private data manifests."""
import copy,json,threading
from pathlib import Path

class JSONSnapshot:
    def __init__(self,path,max_bytes=4*1024*1024):
        self.path=Path(path);self.max_bytes=max_bytes;self.lock=threading.Lock();self.identity=None;self.value=None
    @staticmethod
    def identity_of(stat):return stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns
    def asset(self,mint):
        with self.lock:
            try:stamp=self.identity_of(self.path.stat())
            except FileNotFoundError:self.identity=None;self.value=None;return None
            if stamp!=self.identity:
                for _ in range(3):
                    with self.path.open('rb') as f:
                        import os
                        first=self.identity_of(os.fstat(f.fileno()))
                        raw=f.read(self.max_bytes+1)
                        last=self.identity_of(os.fstat(f.fileno()))
                    if len(raw)>self.max_bytes:raise ValueError('History manifest exceeds size bound')
                    if first!=last or last!=self.identity_of(self.path.stat()):continue
                    value=json.loads(raw)
                    if not isinstance(value,dict) or not isinstance(value.get('assets'),dict):raise ValueError('Invalid history manifest')
                    self.value=value;self.identity=last;break
                else:raise ValueError('History manifest changed while reading')
            # Callers cannot mutate the shared cache through returned plans.
            return copy.deepcopy(self.value['assets'].get(mint))
