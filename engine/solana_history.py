"""Cold Solana execution history, independent of the bounded live market core.

One writer retains CRC-protected provider response envelopes before a FULL
SQLite transaction. Finalized RPC history is a separate provenance tier from
the exact-bank, raw-shred live pipeline. No CEX bars, transfers or NAV prices.
"""
import hashlib,json,math,os,sqlite3,struct,time
from contextlib import contextmanager
from pathlib import Path
from native_solana import PROGRAMS,decode
from server import valid_mint,b58decode
from solana_stream import InputJournal,HEADER,MAGIC
from engine_runtime import EngineError
from chart_intervals import bucket_start,WEEK
from storage_policy import MIN_FREE_BYTES

TOKEN_PROGRAMS=('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb')
INTERVALS=(60,180,300,900,1800,2700,3600,7200,10800,14400,21600,28800,43200,86400,604800)
VERSION='solana-finalized-vault-flow-v1'

def signature(value):
    try:return isinstance(value,str) and 64<=len(value)<=88 and len(b58decode(value))==64
    except (ValueError,TypeError):return False

def mint_account(owner,data):
    """Only initialized SPL/Token-2022 mints; token accounts aren't assets.
    Keep extension types and raw units; do not apply today's stock multiplier
    to historical fills or silently turn raw mint units into underlying shares.
    """
    if owner not in TOKEN_PROGRAMS or len(data)<82 or data[45]!=1:raise ValueError('Not an initialized Solana mint')
    if data[44]>18 or any(struct.unpack_from('<I',data,o)[0] not in (0,1) for o in (0,46)):
        raise ValueError('Malformed mint state')
    extensions=[]
    if owner==TOKEN_PROGRAMS[0] and len(data)!=82:raise ValueError('Unexpected SPL mint size')
    if owner==TOKEN_PROGRAMS[1] and len(data)!=82:
        if len(data)<166 or data[165]!=1 or any(data[82:165]):raise ValueError('Wrong Token-2022 account type')
        offset=166
        while offset<len(data):
            if not any(data[offset:]):break
            if offset+4>len(data):raise ValueError('Torn mint extension')
            kind,size=struct.unpack_from('<HH',data,offset);offset+=4
            if kind==0 or kind in extensions or offset+size>len(data):raise ValueError('Invalid mint extension')
            extensions.append(kind);offset+=size
    return {'decimals':data[44],'supplyRaw':str(struct.unpack_from('<Q',data,36)[0]),
            'tokenProgram':owner,'extensionTypes':extensions,'priceUnits':'raw_mint_units',
            'historicalDisplayConversionApplied':False}

def instructions(tx):
    keys=[k['pubkey'] if isinstance(k,dict) else k for k in tx['transaction']['message']['accountKeys']]
    inner={}
    for group in tx['meta'].get('innerInstructions') or []:
        index=group['index']
        if index in inner:raise ValueError('Duplicate inner instruction group')
        inner[index]=group['instructions']
    result=[]
    outer=tx['transaction']['message']['instructions']
    if any(not isinstance(k,int) or not 0<=k<len(outer) for k in inner):raise ValueError('Invalid inner instruction order')
    for i,item in enumerate(outer):
        result.append((i,-1,item))
        result.extend((i,j,item) for j,item in enumerate(inner.get(i,[])))
    def identity(item):
        program=item.get('programId')
        if program is None:program=keys[item['programIdIndex']]
        accounts=[keys[a] if isinstance(a,int) else a for a in item.get('accounts',[])]
        return program,accounts
    return keys,[(i,j,*identity(item)) for i,j,item in result]

def normalize(tx,cfg,sig,slot,block_time,transaction_index):
    if tx.get('transaction',{}).get('signatures',[None])[0]!=sig:raise ValueError('Execution signature mismatch')
    if tx.get('slot')!=slot or tx.get('blockTime')!=block_time:raise ValueError('Execution block identity mismatch')
    if not isinstance(tx.get('meta'),dict) or 'err' not in tx['meta']:return [],'execution_status_missing'
    if tx['meta']['err'] is not None:return [],'failed'
    keys,ix=instructions(tx)
    # A repeated swap on one pool has ambiguous transaction-level net flow.
    matched=[(i,j) for i,j,p,accounts in ix if p==cfg['program'] and cfg['pool'] in accounts]
    if len(matched)!=1:return [],'unsupported_or_ambiguous'
    # Integer vault balances are the source of truth, never rounded UI amounts.
    for name in ('preTokenBalances','postTokenBalances'):
        for b in tx['meta'].get(name,[]):
            u=b['uiTokenAmount'];amount=u['amount']
            if not isinstance(amount,str) or not amount.isascii() or not amount.isdigit() or not 0<=int(amount)<2**64:
                raise ValueError('Invalid raw token balance')
    rows,reason=decode(tx,cfg,sig,transaction_index)
    for row in rows:
        if not all(math.isfinite(row[k]) and row[k]>0 for k in ('price','volume','baseVolume')):raise ValueError('Invalid decoded trade')
        row['eventOrder']=matched[0][0]*65536+matched[0][1]+1
        row['identity']=sig+':'+cfg['pool']+':'+str(row['eventOrder'])
    return rows,reason

class SolanaHistory:
    def __init__(self,path,max_bytes=32*1024**3,reserve_bytes=MIN_FREE_BYTES):
        self.directory=Path(path);self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'history.sqlite3';self.max_bytes=max_bytes;self.reserve_bytes=reserve_bytes
        with self.connect() as c:c.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
          INSERT OR IGNORE INTO state VALUES('cursor','0');
          INSERT OR IGNORE INTO state VALUES('decoder','solana-finalized-vault-flow-v1');
          CREATE TABLE IF NOT EXISTS frames(seq INTEGER PRIMARY KEY,offset INTEGER,size INTEGER,sha256 TEXT,kind TEXT);
          CREATE TABLE IF NOT EXISTS pools(pool TEXT PRIMARY KEY,base TEXT,quote TEXT,program TEXT,config TEXT,slot INTEGER,digest TEXT);
          CREATE INDEX IF NOT EXISTS base_pool ON pools(base,quote);
          CREATE INDEX IF NOT EXISTS quote_pool ON pools(quote,base);
          CREATE TABLE IF NOT EXISTS mint_claims(mint TEXT,issuer TEXT,name TEXT,symbol TEXT,source TEXT,seq INTEGER,PRIMARY KEY(mint,issuer));
          CREATE TABLE IF NOT EXISTS mints(mint TEXT PRIMARY KEY,config TEXT,slot INTEGER,digest TEXT,seq INTEGER);
          CREATE TABLE IF NOT EXISTS jobs(pool TEXT PRIMARY KEY,before_signature TEXT,pages INTEGER DEFAULT 0,status TEXT DEFAULT 'pending',touched REAL DEFAULT 0,retry_at REAL DEFAULT 0,error TEXT);
          CREATE INDEX IF NOT EXISTS history_breadth_queue ON jobs(status,pages,touched,pool);
          CREATE TABLE IF NOT EXISTS receipts(pool TEXT,signature TEXT,slot INTEGER,status TEXT,reason TEXT,seq INTEGER,PRIMARY KEY(pool,signature));
          CREATE INDEX IF NOT EXISTS pending_receipts ON receipts(status,slot);
          CREATE INDEX IF NOT EXISTS pending_receipt_signature ON receipts(signature) WHERE status='pending';
          CREATE TABLE IF NOT EXISTS blocks(slot INTEGER PRIMARY KEY,hash TEXT,time INTEGER,signatures TEXT,seq INTEGER,full_seq INTEGER);
          CREATE TABLE IF NOT EXISTS fills(identity TEXT PRIMARY KEY,pool TEXT,signature TEXT,time INTEGER,slot INTEGER,tx_order INTEGER,event_order INTEGER,price REAL,base_volume REAL,quote_volume REAL,seq INTEGER);
          CREATE INDEX IF NOT EXISTS fill_time ON fills(pool,time,slot,tx_order,event_order,identity);
          CREATE TABLE IF NOT EXISTS bars(pool TEXT,interval INTEGER,time INTEGER,open REAL,high REAL,low REAL,close REAL,base_volume REAL,quote_volume REAL,count INTEGER,first_key TEXT,last_key TEXT,PRIMARY KEY(pool,interval,time)) WITHOUT ROWID;
          CREATE TABLE IF NOT EXISTS imports(source TEXT PRIMARY KEY,cursor INTEGER);
        ''')
        with self.connect() as c:
            if c.execute("SELECT value FROM state WHERE key='decoder'").fetchone()[0]!=VERSION:raise EngineError('History decoder revision requires explicit reindex')
        self.journal=InputJournal(self.directory/'originals',max_bytes=max_bytes)
        try:
            with self.connect() as c:cursor=int(c.execute("SELECT value FROM state WHERE key='cursor'").fetchone()[0])
            if cursor>self.journal.sequence:raise EngineError('History index is ahead of retained responses')
            offset=0
            with self.connect() as validation:
                for seq,stamp,raw in self.journal.records():
                    if seq>cursor:self._apply(seq,raw,offset)
                    else:
                        r=validation.execute('SELECT offset,size,sha256 FROM frames WHERE seq=?',(seq,)).fetchone()
                        if not r or tuple(r)!=(offset,len(raw),hashlib.sha256(raw).hexdigest()):raise EngineError('Historical original/index mismatch')
                    offset+=HEADER.size+len(raw)
        except BaseException:self.journal.close();raise
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=15)
        try:
            c.row_factory=sqlite3.Row;c.execute('PRAGMA synchronous=FULL');c.execute('PRAGMA busy_timeout=15000')
            with c:yield c
        finally:c.close()
    def guard(self,size=0):
        disk=os.statvfs(self.directory)
        total=sum(p.stat().st_size for p in (self.path,Path(str(self.path)+'-wal'),self.journal.path) if p.exists())
        total+=sum(p.stat().st_size for p in (self.directory/'issuer-responses').glob('*.response'))
        if (self.max_bytes is not None and total+size+16*1024**2>self.max_bytes) or disk.f_bavail*disk.f_frsize-size<self.reserve_bytes:
            raise EngineError('Cold history storage reserve reached; retain originals and pause')
    def append(self,kind,value):
        raw=json.dumps({'kind':kind,'value':value},sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        self.guard(len(raw)*2);offset=os.fstat(self.journal.fd).st_size
        # Reject malformed/conflicting inputs before advancing the durable
        # journal; publication still happens only after the retained original.
        self._apply(self.journal.sequence+1,raw,offset,dry_run=True)
        seq,_=self.journal.append(raw)
        self._apply(seq,raw,offset);return seq
    def original(self,seq):
        import zlib
        with self.connect() as c:r=c.execute('SELECT * FROM frames WHERE seq=?',(seq,)).fetchone()
        if not r:raise EngineError('Historical original is unavailable')
        with self.journal.path.open('rb') as f:
            f.seek(r['offset']);head=f.read(HEADER.size);raw=f.read(r['size'])
        magic,size,found,stamp,crc=HEADER.unpack(head)
        if magic!=MAGIC or size!=r['size'] or found!=seq or zlib.crc32(raw)!=crc or hashlib.sha256(raw).hexdigest()!=r['sha256']:
            raise EngineError('Historical original integrity failure')
        return json.loads(raw)
    def _apply(self,seq,raw,offset,dry_run=False):
        envelope=json.loads(raw);kind,v=envelope['kind'],envelope['value']
        with self.connect() as c:
            cursor=int(c.execute("SELECT value FROM state WHERE key='cursor'").fetchone()[0])
            if seq!=cursor+1:raise EngineError('History response order mismatch')
            if kind=='pool':
                cfg=v['config'];pool=cfg['pool']
                if v.get('commitment')!='finalized' or v['slot']<=0:raise ValueError('Pool account is not finalized')
                if not all(valid_mint(cfg[k]) for k in ('pool','mint','quoteMint','baseVault','quoteVault')) or cfg['program'] not in PROGRAMS or not 0<=cfg['baseDecimals']<=18 or not 0<=cfg['quoteDecimals']<=18:
                    raise ValueError('Unverified Solana pool identity')
                from pump_curve import PUMP,SOL
                native_curve=cfg['program']==PUMP and cfg['quoteMint']==SOL and cfg['quoteVault']==cfg['pool']
                if len(set(cfg[k] for k in ('pool','mint','quoteMint','baseVault','quoteVault')))!=(4 if native_curve else 5):raise ValueError('Conflicting pool identities')
                if not (len(v['digest'])==64 and all(x in '0123456789abcdef' for x in v['digest'])):raise ValueError('Missing account snapshot digest')
                if 'data' in v:
                    import base64
                    from pool_directory import account_config
                    data=base64.b64decode(v['data'],validate=True)
                    if hashlib.sha256(data).hexdigest()!=v['digest'] or account_config(pool,v['owner'],data,v.get('mintAccounts'))!=cfg:raise ValueError('Pool differs from retained finalized account')
                    if v.get('mintAccounts') and any(a['slot']<v['slot'] for a in v['mintAccounts'].values()):raise ValueError('Mint units predate pool snapshot')
                old=c.execute('SELECT config FROM pools WHERE pool=?',(pool,)).fetchone()
                # This is a persistent discovery catalog, not the in-memory
                # subscription set. append() already enforces the disk reserve.
                # A catalog row count must not halt repair of existing markets.
                # Observer subscriptions and recovery queues remain bounded.
                encoded=json.dumps(cfg,sort_keys=True,separators=(',',':'))
                if old and old[0]!=encoded:raise EngineError('Immutable historical pool changed')
                c.execute('INSERT OR IGNORE INTO pools VALUES(?,?,?,?,?,?,?)',(pool,cfg['mint'],cfg['quoteMint'],cfg['program'],encoded,v['slot'],v['digest']))
                c.execute('INSERT OR IGNORE INTO jobs(pool) VALUES(?)',(pool,))
            elif kind=='issuer':
                if not valid_mint(v['mint']) or v['network']!='solana' or not v['source'].startswith('https://'):raise ValueError('Issuer must identify a Solana mint')
                c.execute('INSERT OR REPLACE INTO mint_claims VALUES(?,?,?,?,?,?)',(v['mint'],v['issuer'],v['name'],v['symbol'],v['source'],seq))
            elif kind=='mint':
                import base64
                if v.get('commitment')!='finalized' or not valid_mint(v['mint']) or v['slot']<=0:raise ValueError('Missing finalized mint identity')
                data=base64.b64decode(v['data'],validate=True);cfg=mint_account(v['owner'],data);digest=hashlib.sha256(data).hexdigest()
                old=c.execute('SELECT config,slot FROM mints WHERE mint=?',(v['mint'],)).fetchone()
                if old and (json.loads(old[0])['decimals']!=cfg['decimals'] or json.loads(old[0])['tokenProgram']!=cfg['tokenProgram']):raise EngineError('Mint units changed')
                if not old or old['slot']<=v['slot']:c.execute('INSERT OR REPLACE INTO mints VALUES(?,?,?,?,?)',(v['mint'],json.dumps(cfg),v['slot'],digest,seq))
            elif kind=='history_source':
                # Provider exhaustion is not chain-history exhaustion. Resume
                # from the retained cursor, preserving every original response.
                old=c.execute("SELECT value FROM state WHERE key='history_rpc_source'").fetchone()
                prior=old[0] if old else 'https://solana-rpc.publicnode.com'
                if v.get('previous')!=prior or v.get('current')!='https://api.mainnet-beta.solana.com' or prior==v['current'] or not isinstance(v.get('observed'),(int,float)) or v['observed']<=0:
                    raise ValueError('Invalid retained history source transition')
                c.execute("UPDATE jobs SET status='pending',retry_at=0,error=NULL WHERE status='endpoint_exhausted'")
                c.execute("UPDATE receipts SET status='pending',reason='history_source_retry' WHERE status IN ('deferred','unavailable')")
                c.execute("INSERT INTO state VALUES('history_rpc_source',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(v['current'],))
            elif kind=='page':
                pool=v['pool'];rows=v['rows'];job=c.execute('SELECT * FROM jobs WHERE pool=?',(pool,)).fetchone()
                if not job or job['before_signature']!=v['before']:raise EngineError('Historical pagination cursor conflict')
                if len(rows)>100:raise ValueError('Signature page exceeds admission')
                prior=2**63
                seen=set()
                for r in rows:
                    if not signature(r['signature']) or r['slot']<=0 or r['slot']>prior or r['signature'] in seen or r['signature']==v['before']:
                        raise ValueError('Signature page is not ordered or advancing')
                    prior=r['slot'];seen.add(r['signature'])
                    c.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?)',(pool,r['signature'],r['slot'],'failed' if r['err'] is not None else 'pending','provider_execution_failed' if r['err'] is not None else '',seq))
                c.execute('UPDATE jobs SET before_signature=?,pages=pages+1,status=?,touched=?,retry_at=0,error=NULL WHERE pool=?',(rows[-1]['signature'] if rows else v['before'],'pending' if rows else 'endpoint_exhausted',v['observed'],pool))
            elif kind=='recent_page':
                # Refresh the head independently of the historical before cursor.
                # Original provider response remains in the same recoverable log.
                pool=v['pool'];rows=v['rows']
                if not c.execute('SELECT 1 FROM pools WHERE pool=?',(pool,)).fetchone() or len(rows)>100:raise ValueError('Invalid current-market page')
                prior=2**63;seen=set()
                for r in rows:
                    if not signature(r['signature']) or not 0<r['slot']<=prior or r['signature'] in seen:raise ValueError('Invalid recent execution order')
                    prior=r['slot'];seen.add(r['signature'])
                    c.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?)',(pool,r['signature'],r['slot'],'failed' if r['err'] is not None else 'pending','provider_execution_failed' if r['err'] is not None else '',seq))
                c.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('recent:'+pool,str(v['observed'])))
            elif kind=='block':self._block(c,v,seq)
            elif kind=='receipt':self._receipt(c,v,seq)
            elif kind=='import':c.execute('INSERT INTO imports VALUES(?,?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor',(v['source'],v['cursor']))
            elif kind=='account_scan':
                if v.get('commitment')!='finalized' or v['response']['context']['slot']<=0 or not 1<=len(v['addresses'])<=100 or len(v['response']['value'])!=len(v['addresses']):raise ValueError('Invalid finalized pool account page')
                if not all(valid_mint(a) for a in v['addresses']):raise ValueError('Invalid discovered pool address')
                cursor_key=v.get('cursorKey','pool_hint_scan')
                if cursor_key not in ('pool_hint_scan','pool_hint_scan_extra_v1'):raise ValueError('Unknown discovery cursor')
                c.execute('INSERT INTO imports VALUES(?,?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor',(cursor_key,v['cursor']))
            elif kind=='dex_directory_page':
                from dex_adapters import DLMM
                response=v['response'];page=v['page'];rows=response.get('data')
                old=c.execute("SELECT cursor FROM imports WHERE source='dlmm_official_directory_page'").fetchone();expected=old[0]+1 if old else 1
                if v.get('program')!=DLMM or page!=expected or response.get('current_page')!=page or response.get('page_size')!=100 or not isinstance(rows,list) or len(rows)>100 or not all(valid_mint(r.get('address','')) for r in rows):raise ValueError('Official DEX directory cursor conflict')
                # Exhaustion is a current inventory endpoint, not complete
                # Solana history. A later run may refresh for new deployments.
                cursor=0 if page>=response['pages'] else page
                c.execute("INSERT INTO imports VALUES('dlmm_official_directory_page',?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor",(cursor,))
            else:raise ValueError('Unknown history response type')
            c.execute('INSERT INTO frames VALUES(?,?,?,?,?)',(seq,offset,len(raw),hashlib.sha256(raw).hexdigest(),kind))
            c.execute("UPDATE state SET value=? WHERE key='cursor'",(str(seq),))
            if dry_run:c.rollback()
    def _block(self,c,v,seq):
        if v.get('commitment')!='finalized':raise ValueError('Only finalized historical blocks are accepted')
        block=v['block'];slot=v['slot'];full=block.get('transactions');sigs=block.get('signatures')
        if full is not None:
            if not isinstance(full,list) or len(full)>10000:raise ValueError('Unbounded historical block')
            sigs=[tx['transaction']['signatures'][0] for tx in full]
        if slot<=0 or not valid_mint(block.get('blockhash','')) or not isinstance(sigs,list) or len(sigs)>10000 or any(not signature(s) for s in sigs) or len(set(sigs))!=len(sigs):
            raise ValueError('Finalized block order is unavailable')
        stamp=block.get('blockTime')
        if stamp is not None and (not isinstance(stamp,int) or stamp<1230768000):raise ValueError('Invalid historical block time')
        old=c.execute('SELECT * FROM blocks WHERE slot=?',(slot,)).fetchone();encoded=json.dumps(sigs,separators=(',',':'))
        if old and (old['hash']!=block['blockhash'] or old['signatures']!=encoded or old['time'] is not None and stamp is not None and old['time']!=stamp):raise EngineError('Conflicting finalized historical block')
        c.execute('INSERT INTO blocks VALUES(?,?,?,?,?,?) ON CONFLICT(slot) DO UPDATE SET time=coalesce(blocks.time,excluded.time),full_seq=coalesce(blocks.full_seq,excluded.full_seq)',(slot,block['blockhash'],stamp,encoded,seq,seq if full is not None else None))
        if full is not None:
            for tx in full:self._receipt(c,{'signature':tx['transaction']['signatures'][0],'tx':dict(tx,slot=slot,blockTime=stamp)},seq)
    def _receipt(self,c,v,seq):
        sig,tx=v['signature'],v['tx']
        if not signature(sig) or not tx or not isinstance(tx.get('slot'),int):raise ValueError('Missing executed receipt identity')
        b=c.execute('SELECT * FROM blocks WHERE slot=?',(tx['slot'],)).fetchone()
        if not b:raise ValueError('Finalized block must precede receipt')
        sigs=json.loads(b['signatures'])
        if sig not in sigs or tx['transaction']['signatures'][0]!=sig:raise ValueError('Receipt not in canonical block order')
        if b['time'] is None:
            # Legacy saved block lists often lack a time. A receipt's recorded
            # RPC time is retained as its provenance; it is never guessed.
            stamp=tx.get('blockTime')
        else:stamp=b['time']
        if tx.get('blockTime')!=stamp:raise ValueError('Receipt and block time disagree')
        keys=[k['pubkey'] if isinstance(k,dict) else k for k in tx['transaction']['message']['accountKeys']]
        configs=[]
        for key in set(keys):
            row=c.execute('SELECT config FROM pools WHERE pool=?',(key,)).fetchone()
            if row:configs.append(json.loads(row[0]))
        for cfg in configs:
            rows,reason=normalize(tx,cfg,sig,tx['slot'],stamp,sigs.index(sig))
            c.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?) ON CONFLICT(pool,signature) DO UPDATE SET status=excluded.status,reason=excluded.reason,seq=excluded.seq',(cfg['pool'],sig,tx['slot'],'decoded' if rows else 'failed' if reason=='failed' else 'skipped',reason,seq))
            for row in rows:self._fill(c,cfg,row,sig,seq)
        # A pool referenced only by a notification, not by a swap, is explicit.
        c.execute("UPDATE receipts SET status='skipped',reason='no_supported_pool_execution',seq=? WHERE signature=? AND status='pending'",(seq,sig))
    def _fill(self,c,cfg,row,sig,seq):
        key=(row['slot'],row['order'],row['eventOrder'],row['identity']);key_json=json.dumps(key,separators=(',',':'))
        previous=c.execute('SELECT * FROM fills WHERE identity=?',(row['identity'],)).fetchone()
        values=(row['identity'],cfg['pool'],sig,row['time'],row['slot'],row['order'],row['eventOrder'],row['price'],row['baseVolume'],row['volume'])
        if previous:
            if tuple(previous)[:10]!=values:raise EngineError('Conflicting finalized historical execution')
            return
        c.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?)',(*values,seq))
        for interval in (60,300,3600):
            bucket=row['time']//interval*interval;bar=c.execute('SELECT * FROM bars WHERE pool=? AND interval=? AND time=?',(cfg['pool'],interval,bucket)).fetchone()
            o=cl=hi=lo=row['price'];bv=row['baseVolume'];qv=row['volume'];count=1;first=last=key_json
            if bar:
                if tuple(json.loads(bar['first_key']))<=key:o=bar['open'];first=bar['first_key']
                if tuple(json.loads(bar['last_key']))>=key:cl=bar['close'];last=bar['last_key']
                hi=max(hi,bar['high']);lo=min(lo,bar['low']);bv=math.fsum((bv,bar['base_volume']));qv=math.fsum((qv,bar['quote_volume']));count+=bar['count']
            c.execute('INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(cfg['pool'],interval,bucket,o,hi,lo,cl,bv,qv,count,first,last))
    def history(self,pool,interval=60,limit=300,before=None):
        if type(interval)!=int or not 60<=interval<=WEEK or interval%60 or not valid_mint(pool):raise ValueError('Unsupported Solana history request')
        limit=max(1,min(int(limit),600));bound=int(before) if before is not None else 2**62
        base=max(i for i in (60,300,3600) if interval%i==0);ratio=interval//base
        with self.connect() as c:
            c.execute('BEGIN');cfg=c.execute('SELECT * FROM pools WHERE pool=?',(pool,)).fetchone()
            if not cfg:raise ValueError('Pool identity is not verified')
            edge=c.execute('SELECT time FROM bars WHERE pool=? AND interval=? AND time<? ORDER BY time DESC LIMIT 1 OFFSET ?',(pool,base,bound,(limit+1)*ratio)).fetchone()
            lower=bucket_start(edge[0],interval) if edge else 0
            rows=c.execute('SELECT * FROM bars WHERE pool=? AND interval=? AND time>=? AND time<? ORDER BY time',(pool,base,lower,bound)).fetchall();groups={}
            for row in rows:
                bucket=bucket_start(row['time'],interval)
                if before is not None and bucket+interval>bound:continue
                bar=groups.get(bucket)
                if not bar:groups[bucket]=dict(time=bucket,open=row['open'],high=row['high'],low=row['low'],close=row['close'],baseVolume=row['base_volume'],volume=row['quote_volume'],count=row['count'])
                else:
                    bar.update(high=max(bar['high'],row['high']),low=min(bar['low'],row['low']),close=row['close'],baseVolume=math.fsum((bar['baseVolume'],row['base_volume'])),volume=math.fsum((bar['volume'],row['quote_volume'])),count=bar['count']+row['count'])
            extent=c.execute('SELECT min(time),max(time),count(*) FROM bars WHERE pool=? AND interval=60',(pool,)).fetchone()
            counts={r['status']:r['n'] for r in c.execute('SELECT status,count(*) n FROM receipts WHERE pool=? GROUP BY status',(pool,))}
            job=dict(c.execute('SELECT * FROM jobs WHERE pool=?',(pool,)).fetchone());cursor=int(c.execute("SELECT value FROM state WHERE key='cursor'").fetchone()[0])
        candles=list(groups.values());result=candles[-limit:]
        return {'candles':result,'sequence':cursor,'hasMore':len(candles)>limit,'nextBefore':result[0]['time'] if result else None,
                'identity':{'network':'solana','market':pool,'baseMint':cfg['base'],'quoteMint':cfg['quote'],'series':'trade','priceUnits':'raw_mint_units'},
                'coverage':{'complete':False,'source':'finalized_rpc_receipts','decoder':VERSION,'start':extent[0],'end':extent[1],'minuteCandles':extent[2],'receipts':counts,'backfill':job['status'],'archiveCompleteness':'not_attested','shredJoinVerified':False}}
    def status(self):
        with self.connect() as c:
            return {'network':'solana','verifiedPools':c.execute('SELECT count(*) FROM pools').fetchone()[0],
                    'poolAssets':c.execute('SELECT count(*) FROM (SELECT base FROM pools UNION SELECT quote FROM pools)').fetchone()[0],
                    'officialRwaClaims':c.execute('SELECT count(DISTINCT mint) FROM mint_claims').fetchone()[0],
                    'verifiedRwaMints':c.execute('SELECT count(DISTINCT m.mint) FROM mints m JOIN mint_claims r ON r.mint=m.mint').fetchone()[0],
                    'normalizedFills':c.execute('SELECT count(*) FROM fills').fetchone()[0],
                    'chartPools':c.execute('SELECT count(DISTINCT pool) FROM fills').fetchone()[0],
                    'chartAssets':c.execute('SELECT count(*) FROM (SELECT p.base mint FROM pools p JOIN fills f ON p.pool=f.pool UNION SELECT p.quote FROM pools p JOIN fills f ON p.pool=f.pool)').fetchone()[0],
                    'minuteCandles':c.execute('SELECT count(*) FROM bars WHERE interval=60').fetchone()[0],
                    'jobs':{r[0]:r[1] for r in c.execute('SELECT status,count(*) FROM jobs GROUP BY status')},
                    'receipts':{r[0]:r[1] for r in c.execute('SELECT status,count(*) FROM receipts GROUP BY status')},
                    'originalFrames':self.journal.sequence,'entireSolanaComplete':False,'liveShredAcceptance':False,
                    'limits':{'maxNewStorageBytes':self.max_bytes,'freeDiskReserveBytes':self.reserve_bytes}}
    def close(self):self.journal.close()
