"""Commit notifications for the private read bridge, never a second writer.

Linux inotify only wakes the reader: SQLite data_version is the commit fence.
Overflow, replacement and missed notifications reconcile against the databases.
No transaction is held open and no provider or customer secrets enter messages.
"""
import ctypes
import os
from pathlib import Path
import select
import sqlite3
import struct
import threading
import time
import uuid

DATABASES = {
    'hyperliquid': 'hyperliquid-public/history.sqlite3',
    'cold': 'solana-history/history.sqlite3', 'hot': 'observed-chart/history.sqlite3',
    'managed': 'managed-reference/reference.sqlite3',
    'token': 'token-reference/reference.sqlite3', 'archive': 'archive.sqlite3',
    'tail': 'asset-history/tail.sqlite3', 'gap': 'asset-history/gap.sqlite3',
    'alpha': 'alpha-history/history.sqlite3', 'marks': 'pool-state-marks/marks.sqlite3',
    'support': 'price-support/support.sqlite3', 'exchange': 'global-markets/history.sqlite3',
    'okx': 'global-reference/okx.sqlite3', 'gate': 'global-reference/gate.sqlite3',
    'edge': 'global-reference/edge.sqlite3', 'universe': 'asset-universe/universe.sqlite3',
    'pool_archive': 'reviewed-pool-prices/prices.sqlite3',
}
CONFIGS = ('asset-history/plan.json', 'alpha-history/plan.json',
           'global-markets/markets.json', 'global-markets/tickers.json',
           'global-reference/assets.json', 'price-support/major-plan.json')
POOL = frozenset(('cold', 'hot', 'config'))
TOKEN = frozenset(('token', 'managed', 'config'))
FULL = TOKEN | frozenset(('archive', 'tail', 'gap', 'alpha', 'marks', 'support', 'pool_archive'))
GLOBAL = frozenset(('exchange', 'okx', 'gate', 'edge', 'marks', 'support', 'archive', 'config'))


def dependencies(path, query):
    if path.startswith('/hyperliquid/'): return frozenset(('hyperliquid','config'))
    if path.startswith('/reference/'): return GLOBAL
    if path.startswith('/exchange/'): return frozenset(('exchange', 'archive', 'config'))
    if path.startswith('/token/'):
        return FULL if path == '/token/candles' and query.get('history', ['executions'])[0] == 'full' else TOKEN
    if path == '/assets': return POOL | frozenset(('universe',))
    return POOL


class CommitClock:
    def __init__(self, root, databases=None, configs=None, reconcile_seconds=2):
        self.root = Path(root) / 'data'
        self.paths = {k: self.root / p for k, p in (DATABASES if databases is None else databases).items()}
        self.configs = [self.root / p for p in (CONFIGS if configs is None else configs)]
        self.reconcile_seconds = reconcile_seconds
        self.boot = uuid.uuid4().hex
        self.seq = 0
        self.versions = {k: 0 for k in (*self.paths, 'config')}
        self.observed_at = 0
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.thread = None
        self.connections = {}
        self.config_stamp = None
        self.fd = -1
        self.watches = {}
        self.errors = 0
        self.overflows = 0
        self.checks = 0

    def start(self):
        if self.thread: return
        self.thread = threading.Thread(target=self._run, name='commit-notifications', daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.condition:
            return dict(boot=self.boot, seq=self.seq, versions=dict(self.versions),
                        observedAt=self.observed_at, mode='inotify_commit' if self.fd >= 0 else 'reconcile',
                        errors=self.errors, overflows=self.overflows, checks=self.checks)

    def fingerprint(self, names):
        with self.condition:
            return self.boot, tuple((k, self.versions.get(k, 0)) for k in sorted(names))

    def wait(self, boot, seq, timeout=15):
        with self.condition:
            self.condition.wait_for(lambda: self.stopped.is_set() or boot != self.boot or seq != self.seq, timeout)
            return self.snapshot()

    def close(self):
        self.stopped.set()
        with self.condition: self.condition.notify_all()
        if self.thread: self.thread.join(timeout=3)

    def _probe(self, names):
        changed = set()
        for name in names:
            path = self.paths[name]
            old = self.connections.get(name)
            try:
                st = path.stat(); identity = (st.st_dev, st.st_ino)
                if old is None or old[0] != identity:
                    if old: old[1].close()
                    c = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.02, isolation_level=None)
                    c.execute('PRAGMA query_only=ON')
                    version = c.execute('PRAGMA data_version').fetchone()[0]
                    self.connections[name] = (identity, c, version)
                    changed.add(name)
                else:
                    version = old[1].execute('PRAGMA data_version').fetchone()[0]
                    if version != old[2]:
                        self.connections[name] = (identity, old[1], version)
                        changed.add(name)
                self.checks += 1
            except FileNotFoundError:
                if old:
                    old[1].close(); self.connections.pop(name, None); changed.add(name)
            except (OSError, sqlite3.Error):
                self.errors += 1
                # Invalidate once on reader failure; recovery reopens and invalidates again.
                if old:
                    old[1].close(); self.connections.pop(name, None); changed.add(name)
        stamp = []
        for p in self.configs:
            try:
                s = p.stat(); stamp.append((s.st_ino, s.st_size, s.st_mtime_ns))
            except FileNotFoundError: stamp.append(None)
        if stamp != self.config_stamp: self.config_stamp = stamp; changed.add('config')
        if changed:
            with self.condition:
                for name in changed: self.versions[name] += 1
                self.seq += 1; self.observed_at = time.time() * 1000
                self.condition.notify_all()
        return changed

    def _watch(self):
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if self.fd < 0:
                self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
                if self.fd < 0: return
            folders = {p.parent for p in self.paths.values()} | {p.parent for p in self.configs}
            existing = set(self.watches.values())
            for folder in folders - existing:
                if folder.exists():
                    wd = libc.inotify_add_watch(self.fd, os.fsencode(folder), 0x2 | 0x8 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800)
                    if wd >= 0: self.watches[wd] = folder
        except (AttributeError, OSError): self.fd = -1

    def _events(self):
        dirty = set()
        while True:
            try: raw = os.read(self.fd, 65536)
            except BlockingIOError: break
            if not raw: break
            offset = 0
            while offset + 16 <= len(raw):
                wd, mask, _, size = struct.unpack_from('iIII', raw, offset)
                name = os.fsdecode(raw[offset+16:offset+16+size].split(b'\0', 1)[0]); offset += 16 + size
                if mask & 0x4000:
                    self.overflows += 1; dirty.update(self.paths)
                folder = self.watches.get(wd)
                if mask & (0x8000 | 0x400 | 0x800):
                    self.watches.pop(wd, None); dirty.update(self.paths)
                if folder:
                    for key, p in self.paths.items():
                        if p.parent == folder and name in (p.name, p.name+'-wal', p.name+'-journal'): dirty.add(key)
                    if any(p.parent == folder and p.name == name for p in self.configs): dirty.update(self.paths)
        return dirty

    def _run(self):
        pending = set(); retry_until = 0; next_reconcile = 0
        try:
            while not self.stopped.is_set():
                now = time.monotonic()
                if now >= next_reconcile:
                    self._watch(); self._probe(self.paths)
                    next_reconcile = now + self.reconcile_seconds
                ready = self.fd >= 0 and bool(select.select([self.fd], [], [], .02 if pending else .1)[0])
                if self.fd < 0: self.stopped.wait(.1)
                if ready:
                    events = self._events()
                    if events: pending.update(events); retry_until = time.monotonic() + .25
                if pending:
                    # A WAL write can precede commit. Recheck briefly without reading candles.
                    changed = self._probe(pending)
                    pending.difference_update(changed)
                    if time.monotonic() >= retry_until: pending.clear()
        finally:
            for _, c, _ in self.connections.values(): c.close()
            self.connections.clear()
            if self.fd >= 0: os.close(self.fd); self.fd = -1
