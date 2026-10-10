"""Bounded customer admission. Shares existing result inputs and cold writer.

An address is a request, not proof of a mint or a pool. An operator must verify retained account evidence before updating a watch
to verified. The private discovery/acquisition worker is outside this export.
"""
import json, re, sqlite3, time
from pathlib import Path
from contextlib import contextmanager
from server import valid_mint

MAX_PAID=64
MAX_PREVIEW=16

class AdmissionFull(ValueError): pass

class WatchRegistry:
    def __init__(self,root,readonly=False):
        self.root=Path(root);self.readonly=readonly;folder=self.root/'data/managed-tokens'
        if not readonly:folder.mkdir(parents=True,exist_ok=True)
        self.path=folder/'watches.sqlite3'
        if readonly:return
        with self.connect() as c:
            c.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS watches(mint TEXT PRIMARY KEY,created INTEGER NOT NULL,
              verified INTEGER,state TEXT NOT NULL DEFAULT 'queued',detail TEXT,checked INTEGER DEFAULT 0,
              retry_at INTEGER DEFAULT 0,pools TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS leases(owner TEXT,mint TEXT REFERENCES watches(mint),
              kind TEXT NOT NULL,expires INTEGER NOT NULL,PRIMARY KEY(owner,mint));
            CREATE INDEX IF NOT EXISTS lease_expiry ON leases(expires,mint);
            ''')
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) if self.readonly else sqlite3.connect(self.path,timeout=5);c.row_factory=sqlite3.Row
        c.execute('PRAGMA synchronous=FULL');c.execute('PRAGMA foreign_keys=ON')
        try:
            with c: yield c
        finally:c.close()
    def request(self,mint,owner,kind,expires):
        stamp=int(time.time())
        if not valid_mint(mint) or not re.fullmatch(r'[a-z0-9:_-]{8,100}',owner or ''):raise ValueError('Invalid watch identity')
        if kind not in {'paid','preview'} or type(expires)!=int or not stamp<expires<=stamp+(600 if kind=='preview' else 366*86400):raise ValueError('Invalid watch lease')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            active={r[0] for r in c.execute('SELECT DISTINCT mint FROM leases WHERE expires>? AND kind=?',(stamp,kind))}
            if mint not in active and len(active)>=(MAX_PAID if kind=='paid' else MAX_PREVIEW):raise AdmissionFull('Shared watch capacity is full. Existing charts keep running.')
            owned={r[0] for r in c.execute('SELECT mint FROM leases WHERE owner=? AND expires>?',(owner,stamp))}
            if mint not in owned and len(owned)>=(20 if kind=='paid' else 2):raise AdmissionFull('Active token watch limit reached.')
            if c.execute('SELECT count(*) FROM watches').fetchone()[0]>=8192 and not c.execute('SELECT 1 FROM watches WHERE mint=?',(mint,)).fetchone():raise AdmissionFull('Watch registry admission is full.')
            c.execute('INSERT OR IGNORE INTO watches(mint,created) VALUES(?,?)',(mint,stamp))
            c.execute('INSERT INTO leases VALUES(?,?,?,?) ON CONFLICT(owner,mint) DO UPDATE SET kind=excluded.kind,expires=excluded.expires',(owner,mint,kind,expires))
        self.publish_scope()
        return self.status(mint)
    def cancel(self,mint,owner):
        with self.connect() as c:c.execute('DELETE FROM leases WHERE owner=? AND mint=?',(owner,mint))
        self.publish_scope()
    def publish_scope(self):
        import fcntl
        with self.path.with_name('scope.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX);self._publish_scope()
    def _publish_scope(self):
        # The existing C++ recorder reloads a bounded exact-mint filter on its
        # current gRPC connection. This file contains no credential or API key.
        from solana_stream import atomic_json
        with self.connect() as c:
            rows=[dict(r) for r in c.execute('SELECT w.mint,w.pools,max(l.expires) expires FROM watches w JOIN leases l ON w.mint=l.mint WHERE w.verified IS NOT NULL AND l.expires>? GROUP BY w.mint ORDER BY w.mint LIMIT 80',(int(time.time()),))]
        import hashlib
        revision=hashlib.sha256(json.dumps([(r['mint'],r['pools']) for r in rows],separators=(',',':')).encode()).hexdigest()
        value={'schema':1,'targets':[{'mint':r['mint'],'expires':r['expires']} for r in rows],'poolRevision':revision};path=self.path.with_name('stream-targets.json')
        try:
            if json.loads(path.read_text())==value:return
        except (OSError,ValueError):pass
        atomic_json(path,value)
    def active(self,verified=False):
        if not self.path.exists():return []
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT w.* FROM watches w WHERE EXISTS(SELECT 1 FROM leases l WHERE l.mint=w.mint AND l.expires>?)'+(' AND verified IS NOT NULL' if verified else '')+' ORDER BY created,mint LIMIT 80',(int(time.time()),))]
    def status(self,mint):
        if not valid_mint(mint):raise ValueError('Invalid mint')
        with self.connect() as c:
            row=c.execute('SELECT * FROM watches WHERE mint=?',(mint,)).fetchone()
            expires=c.execute('SELECT max(expires) FROM leases WHERE mint=? AND expires>?',(mint,int(time.time()))).fetchone()[0]
            preview_expires=c.execute("SELECT max(expires) FROM leases WHERE mint=? AND kind='preview' AND expires>?",(mint,int(time.time()))).fetchone()[0]
        if not row:return {'mint':mint,'state':'not_registered','active':False,'verified':False,'pools':[]}
        return {'mint':mint,'state':row['state'] if expires else 'paused','active':bool(expires),'verified':row['verified'] is not None,'previewExpires':preview_expires,
                'pools':json.loads(row['pools']),'checkedAt':row['checked'],'message':row['detail'],'expires':expires,
                'historyComplete':False,'bondingCurveCoverage':'verified_pump_trade_events',
                'exclusions':['ambiguous repeated curve legs','mint/create plus buy without corroborating pre/post vault balances','unrecognized event or instruction layouts']}
    def update(self,mint,**values):
        if set(values)-{'verified','state','detail','checked','retry_at','pools'}:raise ValueError('Invalid watch update')
        with self.connect() as c:c.execute('UPDATE watches SET '+','.join(k+'=?' for k in values)+' WHERE mint=?',(*values.values(),mint))
        if {'verified','pools'} & values.keys():self.publish_scope()

def targets(root):
    root=Path(root);path=root/'data/ticking-market-targets.json'
    rows=json.loads(path.read_text()) if path.exists() else []
    known={r['mint'] for r in rows}
    rows.extend({'mint':r['mint'],'pools':json.loads(r['pools']),'collection_priority_pools':json.loads(r['pools'])[:10],
                 'history_priority':True,'customer_watch':True} for r in WatchRegistry(root,readonly=True).active(verified=True) if r['mint'] not in known)
    return rows
