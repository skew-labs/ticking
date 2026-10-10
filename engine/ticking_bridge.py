"""Private, bounded read bridge from the retained execution store to ticking.

Only loopback, a server-side capability, and readonly SQLite are used. No
provider credentials or mutations cross this boundary. Carried display bars are flagged.
"""
import hmac,json,os,threading,time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from solana_history_read import ColdHistoryReader
from chart_history import history as chart_history
from token_reference import TokenReferenceReader
from response_cache import ResponseCache
from market_limits import MAX_CATALOG_RESPONSE_BYTES
from hyperliquid_public import HyperliquidPublic
from commit_notifications import CommitClock, dependencies
from candle_quality import attach_quality

ROOT=Path(os.environ.get('TICKING_DATA_ROOT',Path(__file__).resolve().parent))
from current_chart_read import CurrentChartReader
reader=CurrentChartReader(ROOT/'data/solana-history',ROOT/'data/observed-chart/history.sqlite3')
token_reader=TokenReferenceReader(ROOT/'data/token-reference')
hyperliquid=HyperliquidPublic(ROOT)
secret=Path(os.environ['TICKING_BRIDGE_SECRET_FILE']).read_text().strip()
slots=threading.BoundedSemaphore(8)
cache=ResponseCache();lock=threading.Lock()
commits=CommitClock(ROOT)
change_slots=threading.BoundedSemaphore(2)
from managed_tokens import WatchRegistry,AdmissionFull
watches=WatchRegistry(ROOT)
managed_reader=TokenReferenceReader(ROOT/'data/managed-reference',continuous_display=True)
catalog_mints={t['mint'] for t in json.loads(Path('./api/server/catalog.json').read_text())}
def reference_reader(mint):return token_reader if mint in catalog_mints or not managed_reader.path.exists() else managed_reader

def query(path,q):
    if path=='/watch/status':
        if set(q)!={'mint'} or len(q['mint'])!=1:raise ValueError('Invalid watch query')
        return watches.status(q['mint'][0])
    if path=="/token/trades-history":
        if not set(q)<={"token","limit","before","quoteMint"} or any(len(v)!=1 for v in q.values()):raise ValueError("Invalid trade history request")
        from product_history import page
        return page(reference_reader(q.get("token",[None])[0]),q.get("token",[None])[0],int(q.get("limit",["100"])[0]),q.get("before",[None])[0],q.get("quoteMint",[None])[0])
    allowed={'/resolve':{'token','pool','quoteMint'},'/candles':{'pool','interval','limit','before','gapPolicy'},'/trades':{'pool','limit'},'/assets':{'after','limit'},'/token/catalog':set(),'/token/analytics':set(),'/token/resolve':{'token'},'/token/candles':{'token','interval','limit','before','gapPolicy','history'},'/token/trades':{'token','limit'}}
    allowed.update({'/exchange/catalog':set(),'/exchange/candles':{'market','interval','limit','before','gapPolicy'}})
    allowed.update({'/reference/catalog':set(),'/reference/candles':{'market','interval','limit','before','gapPolicy'}})
    allowed.update({'/hyperliquid/catalog':set(),'/hyperliquid/candles':{'market','interval','limit','before','gapPolicy'},'/hyperliquid/trades':{'market','limit','before'},'/hyperliquid/book':{'market'}})
    if path not in allowed or not set(q)<=allowed[path] or any(len(v)!=1 for v in q.values()):raise ValueError('Invalid engine request')
    one=lambda k,d=None:q.get(k,[d])[0]
    if path=='/hyperliquid/catalog':return hyperliquid.snapshot()
    if path=='/hyperliquid/candles':return hyperliquid.history(one('market'),int(one('interval','60')),int(one('limit','300')),int(one('before')) if one('before') else None,one('gapPolicy','recorded'))
    if path=='/hyperliquid/trades':return hyperliquid.trades(one('market'),int(one('limit','100')),one('before'))
    if path=='/hyperliquid/book':return hyperliquid.book(one('market'))
    if path=='/reference/catalog':return {'markets':[], 'complete':False}
    if path in {'/reference/candles','/exchange/candles'}:raise ValueError('External history federation is outside this source release')
    if path=='/exchange/catalog':return {'markets':[], 'complete':False}
    if path=='/token/catalog':return token_reader.catalog()
    if path=='/token/analytics':return token_reader.analytics()
    if path.startswith('/token/'):
        from server import valid_mint
        mint=one('token')
        if not valid_mint(mint):raise ValueError('Invalid token mint')
        reference=reference_reader(mint)
        if path=='/token/resolve':
            if not reference.path.exists():return {'mint':mint,'ready':False,'state':'reference_pending','coverage':{}}
            return reference.resolve(mint)
        limit=int(one('limit','300' if path=='/token/candles' else '50'))
        if path=='/token/trades':return reference.trades(mint,limit)
        history=one('history','executions')
        if history not in {'full','executions'}:raise ValueError('Invalid history mode')
        selected=reference  # Public build serves retained observations; external history federation is private.
        return selected.history(mint,int(one('interval','60')),limit,int(one('before')) if one('before') else None,one('gapPolicy','recorded'))
    if path=='/resolve':return reader.resolve(token=one('token'),pool=one('pool'),quote_mint=one('quoteMint'))
    if path=='/assets':
        selected=reader
        return selected.catalog(one('after',''),int(one('limit','100')))
    limit=int(one('limit','300' if path=='/candles' else '50'))
    if not 1<=limit<=(600 if path=='/candles' else 100):raise ValueError('Invalid page size')
    if path=='/candles':
        pool=one('pool');period=int(one('interval','60'));before=int(one('before')) if one('before') else None
        if not 1<=period<=604800:raise ValueError('Invalid interval')
        return chart_history(reader,pool,period,limit,before,one('gapPolicy','recorded'))
    cfg=reader.resolve(pool=one('pool'))
    if not cfg['pools']:raise ValueError('Unknown pool')
    with reader.connect() as c:
        rows=c.execute('SELECT * FROM fills WHERE pool=? ORDER BY slot DESC,tx_order DESC,event_order DESC LIMIT ?',(one('pool'),limit)).fetchall()
    return {'identity':cfg['pools'][0],'trades':[dict(r) for r in reversed(rows)],'commitment':'finalized_prefix_processed_observation_suffix','live':False}

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def process_request(self,request,address):
        if not slots.acquire(False):
            request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n');self.close_request(request);return
        try:super().process_request(request,address)
        except BaseException:slots.release();raise
    def process_request_thread(self,*args):
        try:super().process_request_thread(*args)
        finally:slots.release()

class Handler(BaseHTTPRequestHandler):
    def setup(self):super().setup();self.connection.settimeout(8)
    def log_message(self,*args):pass
    def do_POST(self):
        if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+secret):self.send_error(401);return
        try:
            if self.path not in {'/watch/register','/watch/cancel'}:raise ValueError('Unsupported control path')
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=1024:raise ValueError('Invalid control frame size')
            body=json.loads(self.rfile.read(size))
            if self.path=='/watch/register':
                if set(body)!={'mint','owner','kind','expires'}:raise ValueError('Invalid watch request')
                result=watches.request(**body)
            else:
                if set(body)!={'mint','owner'}:raise ValueError('Invalid watch cancellation')
                watches.cancel(**body);result={'ok':True}
            status=200
        except AdmissionFull as error:result={'detail':str(error)};status=429
        except (ValueError,TypeError):result={'detail':'Invalid watch request'};status=422
        except Exception:result={'detail':'Watch registry temporarily unavailable'};status=503
        payload=json.dumps(result,separators=(',',':')).encode()
        self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(payload)
    def do_GET(self):
        if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+secret):self.send_error(401);return
        try:
            u=urlsplit(self.path)
            if len(self.path)>2048:raise ValueError('Request too long')
            params=parse_qs(u.query)
            if u.path=='/changes':
                if set(params)-{'boot','seq','wait'} or any(len(v)!=1 for v in params.values()):raise ValueError('Invalid change cursor')
                boot=params.get('boot',[''])[0];seq=int(params.get('seq',['-1'])[0]);wait=float(params.get('wait',['15'])[0])
                if len(boot)>64 or not 0<=wait<=15 or seq < -1:raise ValueError('Invalid change cursor')
                if not change_slots.acquire(False):self.send_error(503,'Change listeners full');return
                try:
                    self.connection.settimeout(20)
                    payload=json.dumps(commits.wait(boot,seq,wait),separators=(',',':')).encode()
                    self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(payload)
                finally:change_slots.release()
                return
            # Bind cached results to committed input revisions, not an arbitrary
            # one-second TTL. This also invalidates older pages after backfill.
            generation=commits.fingerprint(dependencies(u.path,params))
            cache_key=(self.path,generation)
            with lock:
                entry=cache.get(cache_key)
                if entry and time.monotonic()-entry[0]<1:payload=entry[1]
                else:payload=None
            if payload is None:
                result=query(u.path,params)
                # Managed SDK mints and explicit pool/venue charts receive the
                # same structural gate as the catalogue's reference readers.
                if u.path.endswith('/candles') and 'chartQuality' not in result.get('coverage',{}):
                    result=attach_quality(result,int(params.get('interval',['60'])[0]),time.time())
                payload=json.dumps(result,allow_nan=False,separators=(',',':')).encode()
                # The shared 2,000-identity catalog has its own bounded budget. Keep the
                # candle/tape bound unchanged and the overall cache byte bound.
                limit=MAX_CATALOG_RESPONSE_BYTES if u.path=='/token/catalog' else 1024**2
                if len(payload)>limit:raise ValueError('Engine response too large')
                with lock:
                    cache.put(cache_key,time.monotonic(),payload)
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(payload)
        except (ValueError,TypeError):self.send_error(422,'Invalid engine query')
        except Exception as error:
            print(json.dumps({'event':'read_failed','path':urlsplit(self.path).path,'error':type(error).__name__,'sqliteCode':getattr(error,'sqlite_errorname',None)}),flush=True)
            self.send_error(503,'Execution store temporarily unavailable')

if __name__=='__main__':
    commits.start()
    try:Server(('127.0.0.1',int(os.environ.get('TICKING_BRIDGE_PORT','19643'))),Handler).serve_forever()
    finally:commits.close()
