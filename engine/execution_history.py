"""Recoverable candle index of accepted SLE1 records, never an input authority.

The original journal is retained. SQLite commits a derived index and its exact
WAL/verdict hash together; an interrupted commit is replayed rather than guessed.
One publisher owns writes, readers use bounded snapshot transactions in WAL mode.
"""
import hashlib
import math
import os
import sqlite3
import multiprocessing
import struct
import threading
from collections import OrderedDict
from contextlib import contextmanager,nullcontext
from pathlib import Path
from execution_projection import ExecutionTape, Minute, compensated, INTERVALS

LEVELS = (60, 300, 3600)
ZERO = '0' * 64


class HistoryError(ValueError): pass


def anchor(previous, sequence, payload, accepted):
    return hashlib.sha256(bytes.fromhex(previous) + struct.pack('<Q?', sequence, accepted) + payload).hexdigest()


def order(event):
    # Big endian keeps SQLite BLOB ordering identical to the canonical tuple.
    return struct.pack('>QII', event.slot, event.transaction, event.event) + bytes(event.id)


def unpack_order(value):
    return (*struct.unpack('>QII', value[:16]), value[16:].hex())


class ExecutionHistory:
    def __init__(self, directory, configuration, max_bytes=96*1024**3,
                 min_free_bytes=200*1024**3, read_budget=250000):
        self.path = Path(directory) / 'history.sqlite3'
        self.max_bytes = max_bytes; self.min_free_bytes = min_free_bytes
        self.read_budget = read_budget; self.read_lock = threading.Lock()
        self.configuration = configuration; self.process = self.pipe = None
        self.views = OrderedDict(); self.view_items = 0; self.closed = False
        self.writer = self.reader = None
        try:
            self.writer = sqlite3.connect(self.path, timeout=2, isolation_level=None, check_same_thread=False)
            self.writer.execute('PRAGMA journal_mode=WAL')
            self.writer.execute('PRAGMA synchronous=FULL')
            self.writer.execute('PRAGMA cache_size=-8192')
            self.writer.execute('PRAGMA wal_autocheckpoint=1000')
            self.writer.executescript('''
              CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1),
                format INTEGER NOT NULL, cursor INTEGER NOT NULL, anchor TEXT NOT NULL,
                root INTEGER NOT NULL, configuration TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS fills (market INTEGER NOT NULL, id BLOB NOT NULL,
                fork BLOB NOT NULL, slot INTEGER NOT NULL, time INTEGER NOT NULL,
                ordering BLOB NOT NULL, price REAL NOT NULL, volume REAL NOT NULL,
                base REAL NOT NULL, PRIMARY KEY(market,id));
              CREATE INDEX IF NOT EXISTS fills_fork ON fills(fork,slot);
              CREATE INDEX IF NOT EXISTS fills_bucket ON fills(market,time,ordering);
              CREATE TABLE IF NOT EXISTS candles (market INTEGER NOT NULL, level INTEGER NOT NULL,
                time INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
                close REAL NOT NULL, volume REAL NOT NULL, base REAL NOT NULL,
                count INTEGER NOT NULL, first BLOB NOT NULL, last BLOB NOT NULL,
                oldest INTEGER NOT NULL, volume_error REAL NOT NULL, base_error REAL NOT NULL,
                PRIMARY KEY(market,level,time));
              CREATE TABLE IF NOT EXISTS markets (market INTEGER PRIMARY KEY, revision INTEGER NOT NULL,
                count INTEGER NOT NULL, oldest INTEGER, latest INTEGER);
              CREATE TABLE IF NOT EXISTS corrections (market INTEGER PRIMARY KEY, sequence INTEGER NOT NULL);
            ''')
            import json
            config = json.dumps(configuration, sort_keys=True, separators=(',', ':'))
            self.writer.execute('INSERT OR IGNORE INTO state VALUES(1,1,0,?,0,?)', (ZERO, config))
            version, self.cursor, self.hash, self.root, stored = self.writer.execute('SELECT format,cursor,anchor,root,configuration FROM state').fetchone()
            if version != 1 or stored != config:
                raise HistoryError('History format or engine configuration changed; rebuild a separate index from retained originals')
            if self.cursor < 0 or len(bytes.fromhex(self.hash)) != 32:
                raise HistoryError('Invalid history recovery anchor')
            if self.writer.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise HistoryError('History integrity check failed; retain index and originals')
            self.corrections = dict(self.writer.execute('SELECT market,sequence FROM corrections'))
            self.reader = sqlite3.connect('file:'+str(self.path)+'?mode=ro', uri=True,
                                         timeout=2, isolation_level=None, check_same_thread=False)
            self.reader.execute('PRAGMA query_only=ON'); self.reader.execute('PRAGMA cache_size=-8192')
            for path in (self.path,Path(str(self.path)+'-wal'),Path(str(self.path)+'-shm')):
                if path.exists(): os.chmod(path,0o600)
        except BaseException:
            self.close(); raise

    def _space(self):
        size = sum(p.stat().st_size for p in (self.path, Path(str(self.path)+'-wal')) if p.exists())
        if size >= self.max_bytes: raise HistoryError('Historical index capacity reached; retain originals and archive before resuming')
        disk = os.statvfs(self.path.parent)
        if disk.f_bavail*disk.f_frsize < self.min_free_bytes:
            raise HistoryError('Historical index free-space reserve reached')

    @staticmethod
    def _minute(row):
        # SELECT time,open,...,base_error below has the same stable layout.
        return Minute(*row[:8], unpack_order(row[8]), unpack_order(row[9]), *row[10:])

    def _add_bar(self, event, level):
        key = (event.market, level, event.time//level*level)
        row = self.writer.execute('SELECT time,open,high,low,close,volume,base,count,first,last,oldest,volume_error,base_error FROM candles WHERE market=? AND level=? AND time=?', key).fetchone()
        sequence = order(event)
        if row is None:
            values = (*key, event.price, event.price, event.price, event.price,
                      event.volume, event.base, 1, sequence, sequence, event.time, 0., 0.)
        else:
            volume, ve = compensated(row[5], row[11], event.volume)
            base, be = compensated(row[6], row[12], event.base)
            if not all(math.isfinite(v) for v in (volume, ve, base, be)):
                raise HistoryError('Historical amount exceeds finite range')
            values = (*key, event.price if sequence < row[8] else row[1],
                      max(row[2], event.price), min(row[3], event.price),
                      event.price if sequence > row[9] else row[4], volume, base,
                      row[7]+1, min(sequence,row[8]), max(sequence,row[9]),
                      min(event.time,row[10]), ve, be)
        self.writer.execute('INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)

    def _repair(self, market, level, start):
        # Rebuild only affected minute buckets from retained canonical fills.
        # Coarse levels then use at most 5/60 minutes, not years of executions.
        if level == 60:
            rows = self.writer.execute('SELECT ordering,price,volume,base,time FROM fills WHERE market=? AND time>=? AND time<? ORDER BY ordering', (market,start,start+60))
            bars = (Minute(start,p,p,p,p,v,b,1,unpack_order(o),unpack_order(o),t) for o,p,v,b,t in rows)
        else:
            rows = self.writer.execute('SELECT time,open,high,low,close,volume,base,count,first,last,oldest,volume_error,base_error FROM candles WHERE market=? AND level=60 AND time>=? AND time<? ORDER BY time', (market,start,start+level))
            bars = map(self._minute, rows)
        first = last = None; high = low = opening = closing = None
        volume = base = ve = be = 0.; count = 0; oldest = None
        for bar in bars:
            if first is None or bar.first < first: first = bar.first; opening = bar.open
            if last is None or bar.last > last: last = bar.last; closing = bar.close
            high = max(high,bar.high) if high is not None else bar.high
            low = min(low,bar.low) if low is not None else bar.low
            volume, ve = compensated(volume,ve,bar.volume+bar.volume_error)
            base, be = compensated(base,be,bar.base+bar.base_error)
            count += bar.count; oldest = min(oldest,bar.oldest) if oldest is not None else bar.oldest
        self.writer.execute('DELETE FROM candles WHERE market=? AND level=? AND time=?', (market,level,start))
        if count:
            pack = lambda x: struct.pack('>QII',*x[:3])+bytes.fromhex(x[3])
            self.writer.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (market,level,start,opening,high,low,closing,volume,base,count,pack(first),pack(last),oldest,ve,be))

    def apply_batch(self, records, publication_lock=None, publish=None):
        """records: (WAL sequence, immutable bytes, Event, native verdict)."""
        if not records: return
        if self.process is not None:
            return self._remote_batch(records,publication_lock,publish)
        self._space(); cursor = self.cursor; digest = self.hash; root = self.root
        touched = set(); corrections = dict(self.corrections)
        self.writer.execute('BEGIN IMMEDIATE')
        try:
            for sequence,payload,event,accepted in records:
                if sequence != cursor+1: raise HistoryError('Historical index sequence gap')
                digest = anchor(digest,sequence,payload,accepted); cursor = sequence
                if not accepted: continue
                if event.action == 2:
                    root = max(root,event.slot); continue
                if event.action == 1:
                    affected = self.writer.execute('SELECT DISTINCT market,time/60*60 FROM fills WHERE fork=? AND slot>?', (bytes(event.fork),root)).fetchall()
                    self.writer.execute('DELETE FROM fills WHERE fork=? AND slot>?', (bytes(event.fork),root))
                    for market in set(m for m,_ in affected):
                        corrections[market] = sequence
                        self.writer.execute('INSERT OR REPLACE INTO corrections VALUES(?,?)',(market,sequence))
                    for market,start in affected: self._repair(market,60,start); touched.add(market)
                    for level in LEVELS[1:]:
                        for market,start in set((m,t//level*level) for m,t in affected): self._repair(market,level,start)
                elif event.action == 0:
                    self.writer.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?)',
                        (event.market,bytes(event.id),bytes(event.fork),event.slot,event.time,order(event),event.price,event.volume,event.base))
                    for level in LEVELS: self._add_bar(event,level)
                    touched.add(event.market)
                else: raise HistoryError('Invalid accepted history action')
            for market in touched:
                # Fill extrema/count is indexed by market/time. Only fork repair
                # needs COUNT; ordinary fills update the retained count once.
                previous = self.writer.execute('SELECT count,oldest,latest FROM markets WHERE market=?',(market,)).fetchone()
                adds = [e for _,_,e,a in records if a and not e.action and e.market==market]
                repaired = any(a and e.action==1 for _,_,e,a in records)
                if previous is None or repaired:
                    count,oldest,latest = self.writer.execute('SELECT COUNT(*),MIN(time),MAX(time) FROM fills WHERE market=?',(market,)).fetchone()
                else:
                    count = previous[0]+len(adds); oldest = min([v for v in [previous[1]]+[e.time for e in adds] if v is not None]); latest = max([v for v in [previous[2]]+[e.time for e in adds] if v is not None])
                self.writer.execute('INSERT OR REPLACE INTO markets VALUES(?,?,?,?,?)',(market,cursor,count,oldest,latest))
            self.writer.execute('UPDATE state SET cursor=?,anchor=?,root=? WHERE id=1',(cursor,digest,root))
            # SQL construction runs outside the live publication mutex. Only
            # the durable commit + recent-tape swap form the reader boundary.
            # A reader that starts sooner sees the previous WAL snapshot.
            with publication_lock if publication_lock is not None else nullcontext():
                self.writer.execute('COMMIT')
                self.cursor = cursor; self.hash = digest; self.root = root; self.corrections = corrections
                if publish is not None: publish(records)
        except BaseException:
            if self.writer.in_transaction:self.writer.execute('ROLLBACK')
            raise

    def start_worker(self):
        # The normalized runtime already owns the directory flock. Spawn rather
        # than fork SQLite connections/market threads; no keys enter this IPC.
        self.writer.close(); self.writer = None
        context = multiprocessing.get_context('spawn'); parent,child = context.Pipe()
        self.process = context.Process(target=history_worker,args=(child,str(self.path.parent),self.configuration,self.max_bytes,self.min_free_bytes),name='slotline-history-index',daemon=True)
        self.pipe = parent; self.process.start(); child.close()
        # Cold recovery integrity checks scale with retained history, unlike
        # a live commit. The measured 2.72 GB index takes 37 seconds to check
        # on Canada. Keep admission closed while the bounded startup finishes.
        ready = self._receive(300)
        if ready != ('ready',self.cursor,self.hash):raise HistoryError('Historical worker recovery anchor mismatch')

    def _receive(self, timeout):
        if not self.pipe.poll(timeout):raise HistoryError('Historical worker exceeded its deadline; recover retained originals')
        try:value = self.pipe.recv()
        except (EOFError,OSError):raise HistoryError('Historical worker disconnected; recover retained originals')
        if value[0]=='failed':raise HistoryError('Historical worker failed: '+value[1])
        return value

    def _remote_batch(self, records, publication_lock, publish):
        self.pipe.send(('batch',[(s,p,a) for s,p,e,a in records]))
        if self._receive(5) != ('prepared',):raise HistoryError('Historical worker protocol mismatch')
        # Prepare SQL outside the mutex in a separate interpreter. The parent
        # owns the commit permission so no reader can pin a DB cursor before its
        # corresponding recent tape has been published.
        with publication_lock if publication_lock is not None else nullcontext():
            self.pipe.send(('commit',));reply = self._receive(5)
            if reply[0]!='committed' or reply[1]!=records[-1][0]:raise HistoryError('Historical worker commit cursor mismatch')
            _,self.cursor,self.hash,self.root,self.corrections = reply
            if publish is not None:publish(records)

    @contextmanager
    def snapshot(self, publication_lock, capture=None):
        # Pin SQLite and the recent projection at one publication boundary.
        # Release the publisher before scanning/rolling history; WAL readers do
        # not hold up its next commit. Serialize only our one reader connection.
        with self.read_lock:
            begun = False
            try:
                with publication_lock:
                    self.reader.execute('BEGIN'); begun = True
                    cursor,digest = self.reader.execute('SELECT cursor,anchor FROM state WHERE id=1').fetchone()
                    # Capture the tape while both published snapshots still
                    # refer to exactly this durable journal cursor.
                    value = capture(cursor) if capture else None
                yield (cursor,digest),value
            finally:
                if begun: self.reader.execute('ROLLBACK')

    def page(self, selection, registry, interval, before, limit):
        if interval not in INTERVALS or not isinstance(limit,int) or not 1<=limit<=600 or not isinstance(before,int) or before<0:
            raise ValueError('Unsupported historical interval, cursor or page size')
        markets = {registry[p]:p for p in selection['pools']}
        if not markets: return dict(candles=[],hasMore=False,nextBefore=None,retainedExecutions=0,oldestExecutionTime=None,latestExecutionTime=None)
        width = interval*60; level = max(v for v in LEVELS if width%v==0)
        bound = (limit+1)*(width//level)
        if bound*len(markets)>self.read_budget:
            raise ValueError('Historical read budget exceeded; reduce page size or select one pool')
        versions = [self.reader.execute('SELECT revision,count,oldest,latest FROM markets WHERE market=?',(m,)).fetchone() or (0,0,None,None) for m in markets]
        key = (tuple((m,p,v[0]) for (m,p),v in zip(markets.items(),versions)),tuple(selection.get('invertedPools',())),interval,before,limit)
        cached = self.views.get(key)
        if cached is None:
            captured = {}
            ceiling = ((before+width-1)//width)*width if before else 2**63-1
            for market in markets:
                rows = self.reader.execute('SELECT time,open,high,low,close,volume,base,count,first,last,oldest,volume_error,base_error FROM candles WHERE market=? AND level=? AND time<? ORDER BY time DESC LIMIT ?', (market,level,ceiling,bound))
                captured[market] = list(map(self._minute,rows))
            candles,more = ExecutionTape._rollup(captured,markets,set(selection.get('invertedPools',())),width,limit)
            cached = dict(candles=candles,hasMore=more,nextBefore=candles[0]['time'] if candles else None,
                retainedExecutions=sum(v[1] for v in versions),
                oldestExecutionTime=min((v[2] for v in versions if v[2] is not None),default=None),
                latestExecutionTime=max((v[3] for v in versions if v[3] is not None),default=None))
            weight = len(candles)+len(markets)
            self.views[key] = (cached,weight); self.view_items += weight
            while len(self.views)>128 or self.view_items>40000:
                _,(_,n) = self.views.popitem(last=False); self.view_items-=n
        else:
            cached = cached[0]; self.views.move_to_end(key)
        return {**cached,'candles':[dict(b) for b in cached['candles']]}

    def close(self):
        if self.closed: return
        self.closed = True
        if self.process is not None:
            try:self.pipe.send(('stop',))
            except (OSError,BrokenPipeError):pass
            self.process.join(2)
            if self.process.is_alive():self.process.terminate();self.process.join(2)
            if self.process.is_alive():self.process.kill();self.process.join(2)
            self.pipe.close()
        for connection in (self.reader,self.writer):
            if connection is not None: connection.close()


def history_worker(pipe, directory, configuration, max_bytes, min_free_bytes):
    # Trusted bounded internal messages only; never expose this pickle-based
    # process pipe to an application, network socket or provider.
    from engine_runtime import Event
    history = None
    class CommitPermission:
        def __enter__(self):
            pipe.send(('prepared',))
            if not pipe.poll(5) or pipe.recv()!=('commit',):raise HistoryError('Historical commit permission expired')
        def __exit__(self,*args):return False
    try:
        history = ExecutionHistory(directory,configuration,max_bytes,min_free_bytes)
        pipe.send(('ready',history.cursor,history.hash))
        while True:
            message = pipe.recv()
            if message==('stop',):break
            if message[0]!='batch' or not 1<=len(message[1])<=256:raise HistoryError('Invalid historical worker batch')
            records = [(s,p,Event.from_buffer_copy(p),a) for s,p,a in message[1]]
            history.apply_batch(records,CommitPermission())
            pipe.send(('committed',history.cursor,history.hash,history.root,history.corrections))
    except BaseException as error:
        try:pipe.send(('failed',type(error).__name__+': '+str(error)[:180]))
        except (EOFError,OSError):pass
    finally:
        if history is not None:history.close()
        pipe.close()
