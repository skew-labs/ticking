"""Bounded, rate-aware last-good observations for public market pages."""
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from .provider_budget import ProviderBudget


class ProviderCache:
    MAX_KEYS = 4096
    MAX_BYTES = 64 * 1024 * 1024
    # At most 12 GeckoTerminal requests/minute, below its public 30/min limit.
    PACING = {'api.geckoterminal.com': 5, 'api.coingecko.com': 10,
              'api.dexscreener.com': 1, 'api.llama.fi': 1}

    def __init__(self, path):
        self.path = Path(path)
        self.cache = {}
        self.locks = {}
        self.lock_users = {}
        self.host_locks = {}
        self.next_request = {}
        self.pending = {}
        self.gate = asyncio.Semaphore(2)
        self.background = False
        self.gecko_budget=ProviderBudget(self.path.parent/'gecko-budget.json')

    def restore(self):
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict): raise ValueError('Invalid cache')
            self.cache = {k:v for k,v in data.items() if isinstance(v,dict)}
            for k in list(self.cache):
                if k.startswith('_'): continue
                entry = self.cache[k]
                if 'data' in entry and not self.valid(k, entry['data']): del self.cache[k]
            self.prune()
        except (OSError, ValueError, TypeError):
            self.cache = {}

    def prune(self):
        encoded = json.dumps(self.cache,allow_nan=False,separators=(',',':')).encode()
        if len(self.cache)<=self.MAX_KEYS and len(encoded)<=self.MAX_BYTES:return encoded
        # This is replaceable public metadata, never our execution/candle store.
        # Keep provider cooldowns even if all observation entries must be evicted.
        disposable=sorted((k for k in self.cache if not k.startswith('_provider:')),
            key=lambda k:(0 if k.startswith('candles:') else 2 if k=='_chain' else 1,
                          self.cache[k].get('fetched_at',0),k))
        size=len(encoded)
        for k in disposable:
            if len(self.cache)<=self.MAX_KEYS and size<=self.MAX_BYTES:break
            value=self.cache.pop(k)
            size-=len(json.dumps(k).encode())+1+len(json.dumps(value,allow_nan=False,separators=(',',':')).encode())+1
        encoded=json.dumps(self.cache,allow_nan=False,separators=(',',':')).encode()
        if len(self.cache)>self.MAX_KEYS or len(encoded)>self.MAX_BYTES:raise ValueError('Provider cooldown state exceeds cache bound')
        return encoded

    def save(self):
        encoded=self.prune()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_bytes(encoded)
        temporary.replace(self.path)

    @staticmethod
    def valid(key, data):
        if isinstance(data,dict) and ('error' in data or 'errors' in data): return False
        if key == 'prices' or key.startswith('prices:'):
            return isinstance(data,list) and bool(data) and all(isinstance(x,dict) and isinstance(x.get('id'),str)
                and 'current_price' in x and (x['current_price'] is None or type(x['current_price']) in (int,float)) for x in data)
        if key.startswith('pools:'):
            return isinstance(data,list) and all(isinstance(x,dict) and isinstance(x.get('baseToken'),dict)
                and isinstance(x.get('quoteToken',{}),dict) for x in data)
        if key.startswith('gecko-pools:'):
            if not isinstance(data,dict) or not isinstance(data.get('data'),list): return False
            if not isinstance(data.get('included',[]),list) or not all(isinstance(x,dict) for x in data.get('included',[])): return False
            try:
                return all(isinstance(x['attributes'],dict) and isinstance(x['relationships']['base_token']['data']['id'],str)
                    and isinstance(x['relationships']['quote_token']['data']['id'],str) for x in data['data'])
            except (KeyError,TypeError): return False
        if key.startswith(('revenue:','fees:')):
            return isinstance(data,dict) and isinstance(data.get('totalDataChart'),list) and isinstance(data.get('name'),str)
        if key.startswith('candles:'):
            try: return isinstance(data['data']['attributes']['ohlcv_list'],list)
            except (KeyError,TypeError): return False
        return isinstance(data,(dict,list))

    async def fetch(self,key,url,ttl,params=None):
        old = self.cache.get(key,{})
        now = time.time()
        if now < old.get('retry_at',0) or 0 <= now-old.get('fetched_at',0) < ttl: return old
        if self.background:
            if key not in self.pending and len(self.pending)<16:
                task=asyncio.create_task(self._fetch(key,url,ttl,params))
                self.pending[key]=task
                task.add_done_callback(lambda t,k=key:self._finished(k,t))
            return old
        return await self._fetch(key,url,ttl,params)

    def _finished(self,key,task):
        self.pending.pop(key,None)
        if not task.cancelled(): task.exception()  # Retrieve failures; preserve last-good data.

    async def close(self):
        tasks=list(self.pending.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    async def _fetch(self,key,url,ttl,params=None):
        if key not in self.locks and len(self.locks)>=self.MAX_KEYS:
            return {**self.cache.get(key,{}),'error':'Refresh capacity is temporarily full.'}
        lock=self.locks.setdefault(key,asyncio.Lock())
        self.lock_users[key]=self.lock_users.get(key,0)+1
        try:return await self._fetch_locked(lock,key,url,ttl,params)
        finally:
            self.lock_users[key]-=1
            if not self.lock_users[key]:
                self.lock_users.pop(key);self.locks.pop(key)

    async def _fetch_locked(self,lock,key,url,ttl,params=None):
        async with lock:
            old=self.cache.get(key,{})
            now=time.time()
            if now<old.get('retry_at',0) or 0<=now-old.get('fetched_at',0)<ttl: return old
            host=urlsplit(url).hostname
            async with self.host_locks.setdefault(host,asyncio.Lock()):
                provider=self.cache.get('_provider:'+host,{})
                if time.time()<provider.get('retry_at',0):
                    return {**old,'error':'Provider rate limit; keeping the last observation.',
                            'retry_at':provider['retry_at']}
                delay=self.next_request.get(host,0)-time.monotonic()
                if delay>0: await asyncio.sleep(delay)
                if host=='api.geckoterminal.com':
                    if provider.get('retry_at',0):self.gecko_budget.cooldown(provider['retry_at'])
                    delay=self.gecko_budget.reserve()
                    if delay>0:return {**old,'retry_at':time.time()+delay,'error':'Provider pacing; keeping the last observation.'}
                try:
                    async with self.gate, httpx.AsyncClient(timeout=12,follow_redirects=False) as client:
                        r=await client.get(url,params=params,headers={'User-Agent':'ticking-market/2.0'})
                    now=int(time.time())
                    if r.status_code==429:
                        wait=r.headers.get('retry-after','300')
                        wait=max(300,int(wait)) if wait.isdigit() else 300
                        # Do not shorten an upstream-requested cooldown.
                        self.cache['_provider:'+host]={'retry_at':now+wait}
                        if host=='api.geckoterminal.com':self.gecko_budget.cooldown(now+wait)
                        self.cache[key]={**old,'retry_at':now+wait,'error':'Provider rate limit; keeping the last observation.'}
                    else:
                        r.raise_for_status()
                        if len(r.content)>4*1024*1024: raise ValueError('Oversized provider response')
                        data=json.loads(r.content,parse_constant=lambda _:None)
                        if not self.valid(key,data): raise ValueError('Unexpected provider schema')
                        if (key=='prices' or key.startswith('prices:')) and isinstance(old.get('data'),list):
                            # A partial batch must not erase previously observed assets.
                            # Their provider last_updated values remain unchanged.
                            merged={x['id']:x for x in old['data'] if isinstance(x,dict) and isinstance(x.get('id'),str)}
                            merged.update({x['id']:x for x in data})
                            data=list(merged.values())
                        self.cache[key]={'data':data,'fetched_at':now,'url':str(r.url)}
                except (httpx.HTTPError,ValueError,TypeError):
                    self.cache[key]={**old,'retry_at':int(time.time())+180,'error':'Provider unavailable; keeping the last observation.'}
                finally:
                    self.next_request[host]=time.monotonic()+self.PACING.get(host,1)
                try: self.save()
                except OSError:
                    # Read endpoints remain available if the disk cannot checkpoint.
                    self.cache['_storage']={'error':'Market checkpoint could not be written.','checked_at':int(time.time())}
                return self.cache.get(key,{'error':'Observation evicted to preserve the public cache budget.'})
