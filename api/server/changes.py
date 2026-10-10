"""One private commit subscription per API process; shared by all chart topics."""
import asyncio
import os
import json
from pathlib import Path
import time
import httpx

POOL = frozenset(('cold', 'hot', 'config'))
TOKEN = frozenset(('token', 'managed', 'config'))
FULL = TOKEN | frozenset(('archive', 'tail', 'gap', 'alpha', 'marks', 'support', 'pool_archive'))
GLOBAL = frozenset(('exchange', 'okx', 'gate', 'edge', 'marks', 'support', 'archive', 'config'))
HYPERLIQUID = frozenset(('hyperliquid','config'))


def dependencies(path, query):
    if path.startswith('/hyperliquid/'): return frozenset(('hyperliquid','config'))
    if path.startswith('/reference/'): return GLOBAL
    if path.startswith('/exchange/'): return frozenset(('exchange', 'archive', 'config'))
    if path.startswith('/token/'):
        return FULL if path == '/token/candles' and query.get('history') == 'full' else TOKEN
    if path == '/assets': return POOL | frozenset(('universe',))
    return POOL


class ChangeFeed:
    def __init__(self):
        self.state = ('', {})
        self.observed_at = 0
        self.connected = False
        self.reconnects = 0
        self.updates = 0
        self.task = None
        self.last_report = 0

    def report(self, hub):
        if time.monotonic()-self.last_report < 10: return
        self.last_report = time.monotonic()
        try:
            from .realtime import INTEREST
            path=Path(os.environ.get('TICKING_STREAM_STATUS',str(INTEREST.with_name('commit-stream-status.json'))))
            data={'at':time.time(),'connected':self.connected,'reconnects':self.reconnects,
                  'changeFrames':self.updates,'lastCommitObservedAt':self.observed_at,**hub.metrics()}
            tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,separators=(',',':')));tmp.replace(path)
        except OSError: pass  # Diagnostics must never delay delivery.

    def fingerprint(self, names):
        # Replace the tuple atomically; loader threads never observe half a vector.
        boot, versions = self.state
        return boot, tuple((k, versions.get(k, 0)) for k in sorted(names))

    def accept(self, data, hub):
        boot, versions = data.get('boot'), data.get('versions')
        if not isinstance(boot, str) or not boot or not isinstance(versions, dict) or len(versions)>32:
            raise ValueError('Invalid private change frame')
        if any(not isinstance(k,str) or type(v) is not int or v<0 for k,v in versions.items()):
            raise ValueError('Invalid private change versions')
        old_boot, old_versions = self.state
        if boot == old_boot and any(versions.get(k, -1)<v for k,v in old_versions.items()):
            raise ValueError('Change version went backwards')
        changed = {k for k in set(versions)|set(old_versions) if versions.get(k)!=old_versions.get(k)}
        if boot != old_boot: changed = set(versions)|set(old_versions)
        self.state = (boot, dict(versions)); self.observed_at = data.get('observedAt', 0)
        self.connected = True; self.updates += 1
        for topic in tuple(hub.topics.values()):
            if topic.dependencies and topic.dependencies & changed:
                topic.changed.set()

    def start(self, hub):
        if self.task is None: self.task = asyncio.create_task(self.run(hub))

    async def close(self):
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
            self.task = None

    async def run(self, hub):
        from .engine_data import SECRET_FILE
        cursor = {}; failures = 0
        async with httpx.AsyncClient(base_url=os.environ.get('TICKING_CHANGE_BRIDGE',os.environ.get('TICKING_ENGINE_BRIDGE','http://127.0.0.1:19643')),
                                     timeout=20, limits=httpx.Limits(max_connections=1)) as client:
            while True:
                try:
                    response = await client.get('/changes', params={**cursor, 'wait':15},
                                                headers={'Authorization':'Bearer '+SECRET_FILE.read_text().strip()})
                    response.raise_for_status(); data = response.json()
                    if type(data.get('seq')) is not int or data['seq']<0: raise ValueError('Invalid change sequence')
                    self.accept(data, hub); cursor = {'boot':data['boot'], 'seq':data['seq']}; failures = 0
                    self.report(hub)
                    # Coalesce a busy writer without a request storm or extra full reads.
                    await asyncio.sleep(.02)
                except asyncio.CancelledError: raise
                except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
                    self.connected = False; self.reconnects += 1
                    self.report(hub)
                    failures += 1
                    await asyncio.sleep(min(5, .25 * 2**min(failures, 4)))


feed = ChangeFeed()
