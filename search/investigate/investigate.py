"""em-investigate: what happened to a token inside a chart range. Staging/dev service on 127.0.0.1:8898; the wallet bridge proxies
/api/public/em/investigate, /api/public/em/cluster and /api/public/em/cluster/first-entry to it.

Everything joins on raw fills and raw transfers (tx, pool, block, trader), never on candles:
  fills      em-charts trade tape (deep, last days) + hot fills; older ranges: the map's dex_trades archive, extracted once per token
             (data/fills/<chain>/<token>.parquet) and valued from the quote side (USD anchors, ETH/BTC anchor minute prices)
  transfers  the token's full ERC-20 transfer history (em-data/transfers/<chain>/<token>.parquet, pull_token_transfers.sh) for balances
             at any time, LP adds/removes (pool transfers outside swaps), exchange deposits/withdrawals and transfers between linked wallets
  links      the label engine (glserve neighbors / feature facts), Safe owners on-chain, and same-block buys from the fills
Cluster model: typed links with evidence and counter-evidence; wallets merge only on a strong link or on two independent weak links,
never on a shared funder alone. Wording: co-occurrence is reported, causation never claimed.
usage: investigate.py [--port 8898]
"""
import hashlib
import fcntl
import queue
import shutil
import tempfile
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict, OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import duckdb

EMC = os.environ.get("EM_CHARTS_ROOT", "/srv/skew/em-charts")
EM = os.environ.get("EM_DATA_ROOT", "/srv/skew/em-data")
SAFE_CANARY = os.environ.get("EM_API_SAFE_CANARY", "0") == "1"


class CanaryBlocked(Exception):
    """A canary cannot start data work, even through an internal helper."""


def require_data_work(operation):
    if SAFE_CANARY:
        raise CanaryBlocked(operation)


def canary_blocked(operation):
    return 503,{"error":"safe_canary","available":False,"complete":False,"operation":operation,
                "detail":"This isolated canary does not execute data scans, RPC calls or background builds."}


def runtime_policy():
    return {"safeCanary":SAFE_CANARY,"dataScans":not SAFE_CANARY,"externalCalls":not SAFE_CANARY,
            "backgroundBuilds":not SAFE_CANARY,"canaryReadRoutes":["/health","/protocols","/protocol"] if SAFE_CANARY else None}


def health(_qs):
    return 200,{"ok":True,"service":"em-investigate","runtimePolicy":runtime_policy()}

GLSERVE = {"robinhood": (8710, 8712), "arbitrum": (8711, 8713)}
RPC = {"robinhood": "https://rpc.mainnet.chain.robinhood.com", "arbitrum": "https://arb1.arbitrum.io/rpc"}
ZERO = "0x0000000000000000000000000000000000000000"
BURN = {ZERO, "0x000000000000000000000000000000000000dead"}
ADDR = re.compile(r"^0x[0-9a-f]{40}$")
VENUE_CATS = {"dex_pool", "dex_router", "token_contract", "bridge", "lending", "nft_contract"}
EXCH_CATS = {"cex_hot_wallet", "cex_deposit"}
FUNDER_MAX_FANOUT = 60
UA = {"User-Agent": "Mozilla/5.0 em-investigate", "Content-Type": "application/json"}
LOCK = threading.Lock()
CLUSTERS = {}            # cluster id -> {market, members, context, _expires}
# Links outlive the 10-minute investigation response cache, but do not accumulate forever.
CLUSTER_TTL_S = 3600
CLUSTER_CAP = 4000
RANGE_CHAINS = frozenset(GLSERVE)  # these readers have the required tape, balances and link backend


def registry_chains():
    path = os.environ.get("EM_CHAIN_REGISTRY", "/srv/skew/em-ticking-bridge/registry/chains.json")
    try:
        with open(path) as f:
            rows = json.load(f)["chains"]
        return {r["id"]: r.get("name") or r["id"] for r in rows
                if isinstance(r, dict) and re.fullmatch(r"[a-z][a-z0-9]{1,15}", r.get("id", ""))}
    except (OSError, ValueError, KeyError, TypeError):
        return {"arbitrum": "Arbitrum One", "robinhood": "Robinhood Chain"}


REGISTRY_CHAINS = registry_chains()


def range_market(mid):
    m = re.fullmatch(r"([a-z][a-z0-9]{1,15}):(0x[0-9a-f]{40})", mid)
    if not m or m[1] not in REGISTRY_CHAINS:
        return None, None, (400, {"detail": "Use a registered chain and a valid token address."})
    return m[1], m[2], None


def range_unavailable(schema, mid, chain):
    return 200, {"schema": schema, "market": mid, "chain": chain, "available": False, "complete": False,
                 "code": "range_history_not_served", "retryable": False,
                 "reason": "Historical range analysis for %s is not available yet." % REGISTRY_CHAINS[chain]}


def cluster_id(mid, t0, t1, members):
    # Keep the public k+10-hex format while scoping links to their exact investigation.
    key = json.dumps([mid, int(t0), int(t1), sorted(members)], separators=(",", ":"))
    return "k" + hashlib.sha256(key.encode()).hexdigest()[:10]


def remember_cluster(cid, context):
    now = time.monotonic()
    with LOCK:
        for key in [key for key, value in CLUSTERS.items() if value["_expires"] <= now]:
            CLUSTERS.pop(key, None)
        previous = CLUSTERS.get(cid)
        if previous and any(previous.get(k) != context.get(k) for k in ("market", "from", "to", "members")):
            raise ValueError("Cluster identity collision")
        CLUSTERS[cid] = {**context, "_expires": now + CLUSTER_TTL_S}
        if len(CLUSTERS) > CLUSTER_CAP:
            for key in sorted(CLUSTERS, key=lambda key: CLUSTERS[key]["_expires"])[:len(CLUSTERS) - CLUSTER_CAP]:
                CLUSTERS.pop(key, None)


def get_cluster(cid):
    with LOCK:
        context = CLUSTERS.get(cid)
        if context is not None and context["_expires"] <= time.monotonic():
            CLUSTERS.pop(cid, None)
            return None
        return context
# stall protection: every heavy request runs under a time budget (its DuckDB / SQLite queries are interrupted when it runs out),
# at most HEAVY_SLOTS run at once (a short queue, then 429), identical requests share one run, and results are cached
BUDGET_S = float(os.environ.get("EM_INV_BUDGET_S", "20"))
HEAVY_SLOTS = threading.BoundedSemaphore(int(os.environ.get("EM_INV_HEAVY", "2")))
QUEUE_WAIT_S = 3
MAX_FILLS = int(os.environ.get("EM_INV_MAX_FILLS", "150000"))
HEAVY_ROUTES = ("/investigate", "/cluster", "/cluster/first-entry")
CACHE_TTL = {200: 600, "too_heavy": 300}
_req = threading.local()
_cache, _inflight, _clock = {}, {}, threading.Lock()


class TooHeavy(Exception):
    """The request cannot finish inside the budget; extra carries the numbers for the client (fills, suggested range)."""
    def __init__(self, detail, **extra):
        super().__init__(detail)
        self.detail, self.extra = detail, extra


def _ctx():
    return getattr(_req, "ctx", None)


def checkpoint():
    """Raise TooHeavy once the request's budget is spent (called between Python-side steps: HTTP lookups, loops)."""
    r = _ctx()
    if r is not None and (r["expired"] or time.time() > r["deadline"]):
        raise TooHeavy("budget")


def remaining(cap):
    r = _ctx()
    return max(0.2, min(cap, r["deadline"] - time.time())) if r is not None else cap


def track(conn):
    """Register a DuckDB or SQLite connection with the running request, so the watchdog can interrupt it."""
    r = _ctx()
    if r is not None:
        if r["expired"]:
            conn.close()
            raise TooHeavy("budget")
        r["conns"].append(conn)
    return conn


def sq(path):
    require_data_work("sqlite_read")
    return track(sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False))
class LookupCache:
    """Bounded monotonic cache; failed lookups expire quickly and same-key callers share one request."""
    def __init__(self,capacity=10000,positive_ttl=60,negative_ttl=5,max_pending=128):
        self.capacity,self.positive_ttl,self.negative_ttl,self.max_pending=capacity,positive_ttl,negative_ttl,max_pending
        self.data,self.pending=OrderedDict(),{}
        self.lock=threading.Lock()
        self.now=time.monotonic

    def get(self,key,load):
        deadline=time.monotonic()+remaining(15)
        while True:
            checkpoint()
            with self.lock:
                hit=self.data.get(key)
                if hit and hit[0]>self.now():
                    self.data.move_to_end(key)
                    return hit[1]
                self.data.pop(key,None)
                event=self.pending.get(key)
                owner=event is None
                if owner:
                    if len(self.pending)>=self.max_pending:
                        raise TooHeavy("lookup_queue_full")
                    event=self.pending[key]=threading.Event()
            if owner:
                break
            if time.monotonic()>=deadline:
                raise TooHeavy("lookup_wait_budget")
            event.wait(min(.05,max(0,deadline-time.monotonic())))
        try:
            value,positive=load()
            checkpoint()
            with self.lock:
                self.data[key]=(self.now()+(self.positive_ttl if positive else self.negative_ttl),value)
                self.data.move_to_end(key)
                while len(self.data)>self.capacity:
                    self.data.popitem(last=False)
            return value
        finally:
            with self.lock:
                self.pending.pop(key,None)
                event.set()

_label,_neigh,_facts=LookupCache(),LookupCache(),LookupCache()


def con():
    require_data_work("duckdb_read")
    c = duckdb.connect()
    c.execute("SET threads=2")
    c.execute("SET memory_limit='1500MB'")
    return track(c)


# ------------------------------------------------------------------------------------------------ chain helpers
_bm = {}
_bm_versions = {}


def block_minutes(chain):
    path=f"{EMC}/data/block_minutes_{chain}.parquet"
    stat=os.stat(path)
    version=(stat.st_mtime_ns,stat.st_size,stat.st_ino)
    if chain not in _bm or _bm_versions.get(chain)!=version:
        rows=con().execute(f"SELECT minute, first_block FROM read_parquet('{path}') WHERE minute>0 ORDER BY first_block").fetchall()
        _bm[chain]=([row[0] for row in rows],[row[1] for row in rows])
        _bm_versions[chain]=version
    return _bm[chain]


def block_at(chain, t):
    import bisect
    mins, blocks = block_minutes(chain)
    i = bisect.bisect_right(mins, t) - 1
    return blocks[max(0, i)]


def time_at(chain, bn):
    import bisect
    mins, blocks = block_minutes(chain)
    i = bisect.bisect_right(blocks, bn) - 1
    return mins[max(0, i)]


def rpc(chain, method, params):
    require_data_work("rpc")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    try:
        checkpoint()
        return json.load(urllib.request.urlopen(urllib.request.Request(RPC[chain], data=body, headers=UA), timeout=remaining(15))).get("result")
    except Exception:
        return None


def gl(chain, path, params):
    require_data_work("label_reader")
    for port in GLSERVE[chain]:
        try:
            checkpoint()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}?{urllib.parse.urlencode(params)}", timeout=remaining(4)) as r:
                d = json.load(r)
                if not d.get("error"):
                    return d
        except Exception:
            continue
    return None


def label(chain,a):
    def load():
        d=gl(chain,"/labels",{"addr":a})
        return {k:(d or {}).get(k) for k in ("category","entity","confidence")},bool(d)
    return _label.get((chain,a),load)


def neighbors(chain,a):
    def load():
        d=gl(chain,"/tools/neighbors",{"addr":a,"dir":"both","limit":64})
        return d or {},d is not None
    return _neigh.get((chain,a),load)


def facts(chain,a):
    def load():
        d=gl(chain,"/tools/feature_pack",{"addr":a})
        return (d or {}).get("facts") or {},d is not None
    return _facts.get((chain,a),load)


def meta(chain, token):
    c = con()
    r = c.execute(f"SELECT symbol, decimals FROM read_parquet('{EM}/econ/{chain}/token_meta.parquet') WHERE lower(token) = ?", [token]).fetchone()
    return (r[0], int(r[1])) if r else (None, 18)


def exchange_set(chain):
    p = f"{EMC}/data/{chain}/analytics/rt/exchange_set.parquet"
    return p if os.path.exists(p) else None


# ------------------------------------------------------------------------------------------------ fills
# Shared by the range service and invscreen. File locks coordinate both processes.
FILL_BUILD_BUDGET_S = max(1.0, min(300.0, float(os.environ.get("EM_FILL_BUILD_BUDGET_S", "90"))))
FILL_MIN_FREE_BYTES = max(0, int(os.environ.get("EM_FILL_MIN_FREE_BYTES", str(60 << 30))))
FILL_JOB_CAP = 4
FILL_FAILURE_COOLDOWN_S = 60


def _fill_path(chain, token):
    if chain not in RANGE_CHAINS or not ADDR.fullmatch(token):
        raise ValueError("Invalid fill extraction subject")
    return f"{EM}/fills/{chain}/{token}.parquet"


def _try_file_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


class FillJobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs, self.failures = {}, {}
        self.queue = queue.Queue(maxsize=FILL_JOB_CAP)
        self.worker = None

    def submit(self, chain, token):
        path = _fill_path(chain, token)
        if SAFE_CANARY:
            return {"state":"paused","reason":"safe_canary"}
        if os.path.exists(path):
            return {"state": "ready", "path": path}
        key, now = (chain, token), time.monotonic()
        with self.lock:
            if key in self.jobs:
                return self.jobs[key]
            failed = self.failures.get(key)
            if failed and failed[0] > now:
                return {"state": "failed", "reason": failed[1]}
            if len(self.jobs) >= FILL_JOB_CAP:
                return {"state": "busy", "reason": "fill_queue_full"}
            if shutil.disk_usage(EM).free < FILL_MIN_FREE_BYTES:
                return {"state": "paused", "reason": "disk_guard"}
            directory = f"{EM}/fills/.jobs"
            os.makedirs(directory, exist_ok=True)
            claim = _try_file_lock(f"{directory}/{chain}-{token}.lock")
            if claim is None:
                return {"state": "running", "reason": "another_process"}
            slot = None
            try:
                # Admission locks cap active + queued jobs across every importing service.
                for i in range(FILL_JOB_CAP):
                    slot = _try_file_lock(f"{directory}/slot-{i}.lock")
                    if slot is not None:
                        break
                if slot is None:
                    os.close(claim)
                    return {"state": "busy", "reason": "fill_queue_full"}
                job = {"state": "queued", "key": key, "event": threading.Event(), "claim": claim,
                       "slot": slot, "accepted": now, "path": path}
                self.jobs[key] = job
                self.queue.put_nowait(job)
                if self.worker is None:
                    self.worker = threading.Thread(target=self._work, name="fill-extraction", daemon=True)
                    self.worker.start()
                return job
            except Exception:
                self.jobs.pop(key, None)
                if slot is not None:
                    os.close(slot)
                os.close(claim)
                raise

    def _work(self):
        while True:
            job = self.queue.get()
            if job is None:  # deterministic shutdown for isolated tests
                self.queue.task_done()
                return
            try:
                if time.monotonic() - job["accepted"] > FILL_BUILD_BUDGET_S * FILL_JOB_CAP:
                    raise TooHeavy("fill_queue_expired")
                code, body = guarded(lambda _: self._run(job), {}, budget_s=FILL_BUILD_BUDGET_S)
                if code != 200:
                    raise TooHeavy("fill_build_budget" if body.get("error") == "too_heavy" else "fill_build_failed")
                job["state"] = "ready"
            except Exception as exc:
                job["state"], job["reason"] = "failed", (exc.detail if isinstance(exc, TooHeavy) else "fill_build_failed")
            finally:
                os.close(job["slot"])
                os.close(job["claim"])
                with self.lock:
                    self.jobs.pop(job["key"], None)
                    if job["state"] == "failed":
                        now = time.monotonic()
                        self.failures = {k: v for k, v in self.failures.items() if v[0] > now}
                        self.failures[job["key"]] = (now + FILL_FAILURE_COOLDOWN_S, job["reason"])
                        while len(self.failures) > 256:
                            self.failures.pop(next(iter(self.failures)))
                job["event"].set()
                self.queue.task_done()

    def _run(self, job):
        fd = None
        try:
            while fd is None:
                checkpoint()
                fd = _try_file_lock(f"{EM}/fills/.jobs/run.lock")
                if fd is None:
                    time.sleep(min(0.1, remaining(0.1)))
            if shutil.disk_usage(EM).free < FILL_MIN_FREE_BYTES:
                raise TooHeavy("disk_guard")
            job["state"] = "running"
            if not os.path.exists(job["path"]):
                _build_deep_fills(*job["key"])
            return 200, {"path": job["path"]}
        finally:
            if fd is not None:
                os.close(fd)


_fill_jobs = FillJobs()


def queue_deep_fills(chain, token):
    job = _fill_jobs.submit(chain, token)
    return {k: job[k] for k in ("state", "reason") if k in job}


def deep_fills_path(chain, token):
    """Use an existing extract or wait within the caller budget for a bounded shared job."""
    path = _fill_path(chain, token)
    job = _fill_jobs.submit(chain, token)
    if job["state"] in ("paused", "busy", "failed"):
        raise TooHeavy("Historical fills are not available yet (%s); retry later." % job.get("reason", job["state"]))
    # An internal caller without a request context must also have a finite wait.
    deadline = time.monotonic() + remaining(FILL_BUILD_BUDGET_S + 1)
    while not os.path.exists(path):
        checkpoint()
        if job["state"] == "failed":
            raise TooHeavy("Historical fills are not available yet; retry later.")
        if time.monotonic() >= deadline:
            raise TooHeavy("budget")
        event = job.get("event")
        if event:
            event.wait(min(0.1, max(0, deadline - time.monotonic())))
        else:  # another process owns the token; poll the atomic publication, never start another scan
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    checkpoint()
    return path


def _build_deep_fills(chain, token):
    require_data_work("deep_fill_build")
    path = _fill_path(chain, token)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    sym, dec = meta(chain, token)
    # A failed/timed-out COPY cannot leave a public file or another job's temporary output.
    with tempfile.TemporaryDirectory(prefix=".fill-", dir=directory) as scratch:
        tmp = scratch + "/fills.parquet"
        c = con()
        try:
            c.execute("SET threads=1")
            c.execute("SET memory_limit='512MB'")
            c.execute("SET max_temp_directory_size='1GB'")
            c.execute("SET temp_directory=?", [scratch + "/spill"])
            checkpoint()
            c.execute(f"ATTACH '{EMC}/data/{chain}/candles.duckdb' AS cd (READ_ONLY)")
            c.execute(f"CREATE TEMP TABLE bm AS SELECT minute, first_block FROM read_parquet('{EMC}/data/block_minutes_{chain}.parquet') WHERE minute > 0 ORDER BY first_block")
            c.execute(f"""CREATE TEMP TABLE tm AS SELECT lower(token) AS token, any_value(decimals) AS decimals FROM read_parquet('{EM}/econ/{chain}/token_meta.parquet') GROUP BY 1""")
            c.execute(f"""CREATE TEMP TABLE raw AS
              SELECT block_number AS bn, '0x' || lower(hex(tx_hash)) AS tx, tx_index, log_index, '0x' || lower(hex(pool)) AS pool, '0x' || lower(hex(trader)) AS trader,
                     CASE WHEN hex(token_out) = upper('{token[2:]}') THEN 'B' ELSE 'A' END AS side,
                     CASE WHEN hex(token_out) = upper('{token[2:]}') THEN amount_out::DOUBLE ELSE amount_in::DOUBLE END / 10 ^ {dec} AS amount,
                     '0x' || lower(hex(CASE WHEN hex(token_out) = upper('{token[2:]}') THEN token_in ELSE token_out END)) AS quote,
                     CASE WHEN hex(token_out) = upper('{token[2:]}') THEN amount_in::DOUBLE ELSE amount_out::DOUBLE END AS quote_raw
              FROM read_parquet('{EM}/map/{chain}/dex_trades/*/*.parquet')
              WHERE hex(token_in) = upper('{token[2:]}') OR hex(token_out) = upper('{token[2:]}')""")
            c.execute(f"""COPY (
              WITH f AS (SELECT r.*, b.minute AS time FROM raw r ASOF JOIN bm b ON r.bn >= b.first_block),
              q AS (SELECT f.*, a.kind, coalesce(tm.decimals, 18) AS qdec FROM f LEFT JOIN cd.anchors a ON a.token = f.quote LEFT JOIN tm ON tm.token = f.quote)
              SELECT q.bn, q.time, q.tx, q.tx_index, q.log_index, q.pool, q.trader, q.side, q.amount,
                     CASE WHEN q.kind = 'usd' THEN q.quote_raw / 10 ^ q.qdec ELSE q.quote_raw / 10 ^ q.qdec * p.px END AS usd
              FROM q ASOF LEFT JOIN cd.anchor_px p ON p.kind = q.kind AND q.time >= p.minute ORDER BY bn, log_index) TO '{tmp}' (FORMAT parquet)""")
            checkpoint()
            c.close()
            checkpoint()
            os.replace(tmp, path)
        finally:
            c.close()
    return path


def fills(chain, token, t0, t1):
    """Fills in [t0, t1]: tape + hot when the range is inside the tape window, else the per-token deep extract."""
    c = con()
    deep = f"{EMC}/data/{chain}/candles.duckdb"
    c.execute(f"ATTACH '{deep}' AS cd (READ_ONLY)")
    tape_lo = c.execute("SELECT min(time) FROM cd.tape").fetchone()[0] or 1e18
    rows, source = [], None
    anchor = c.execute("SELECT symbol FROM cd.anchors WHERE token = ?", [token]).fetchone()
    if anchor:   # WETH, stablecoins, ETH, BTC: the quote side of most markets, not a market of their own here
        raise TooHeavy(f"{anchor[0]} is a base asset that most markets trade against, so a range investigation would scan almost every "
                       "trade on the chain. Investigate a token or stock market instead (pick it on the chart), or a pool.", anchor=anchor[0])

    def guard(n):
        if n > MAX_FILLS:
            raise TooHeavy(f"{n:,} fills in this range; investigations read at most {MAX_FILLS:,}. Narrow the range.",
                           fills=n, maxFills=MAX_FILLS, suggestedMaxSeconds=max(60, int((t1 - t0) * MAX_FILLS / n)))
    if t0 >= tape_lo:
        guard(c.execute("SELECT count(*) FROM cd.tape WHERE token = ? AND time BETWEEN ? AND ?", [token, t0, t1]).fetchone()[0])
        rows = c.execute("""SELECT bn, time, '0x' || lower(hex(tx_hash)), tx_index, log_index, pool, '0x' || lower(hex(trader)), side, base_volume, usd
                            FROM cd.tape WHERE token = ? AND time BETWEEN ? AND ? ORDER BY bn, log_index""", [token, t0, t1]).fetchall()
        cut = c.execute("SELECT max(time) FROM cd.tape").fetchone()[0] or 0
        hot = f"{EMC}/hot/{chain}/history.sqlite3"
        if os.path.exists(hot) and t1 > cut:
            h = sq(hot)
            rows += h.execute("""SELECT f.slot, f.time, m.tx, f.tx_order, f.event_order, f.pool, m.trader, m.side, f.base_volume, m.usd
                                 FROM fill_meta m JOIN fills f ON f.identity = m.identity WHERE m.token = ? AND f.time > ? AND f.time <= ?""",
                              (token, max(cut, t0), t1)).fetchall()
            guard(len(rows))
        source = "em-charts trade tape (deep + hot)"
    else:
        p = deep_fills_path(chain, token)
        guard(c.execute(f"SELECT count(*) FROM '{p}' WHERE time BETWEEN ? AND ?", [t0, t1]).fetchone()[0])
        rows = c.execute(f"""SELECT bn, time, tx, tx_index, log_index, pool, trader, side, amount, usd FROM '{p}'
                             WHERE time BETWEEN ? AND ? ORDER BY bn, log_index""", [t0, t1]).fetchall()
        source = "map dex_trades archive (raw swaps), valued from the quote side"
    keys = ("bn", "time", "tx", "txIndex", "log", "pool", "trader", "side", "amount", "usd")
    return [dict(zip(keys, r)) for r in rows], source


def tape_con(chain):
    require_data_work("tape_attach")
    c = con()
    c.execute(f"ATTACH '{EMC}/data/{chain}/candles.duckdb' AS cd (READ_ONLY)")
    return c, c.execute("SELECT min(time) FROM cd.tape").fetchone()[0] or 1e18


def token_pools(chain, token):
    """Pools that trade the token (em-charts pool table). A first receipt from one of them is a swap whose transaction sender was
    someone else (a router, relayer or smart account), not a transfer from another holder."""
    try:
        c, _ = tape_con(chain)
        return {r[0] for r in c.execute("SELECT pool FROM cd.pools WHERE token = ? OR quote = ?", [token, token]).fetchall()}
    except duckdb.Error:
        return set()


def range_prices(chain, token, t0, t1, F):
    """(first, last, basis) at the range ends: the chart's own candle closes (em-charts token bars, finest period). A single
    fill is a bad price: a dust trade or a routed leg valued from the other side gave $ARROW "-100%" (7.5e-10 against a chart
    at 1e-4). Without candles: the median price of the first / last five fills of at least $1."""
    try:
        c, _ = tape_con(chain)
        per = c.execute("SELECT min(period) FROM cd.token_bars WHERE token = ?", [token]).fetchone()[0]
        if per:
            q = "SELECT close FROM cd.token_bars WHERE token = ? AND period = ? AND time {} ? ORDER BY time {} LIMIT 1"
            p0 = (c.execute(q.format("<=", "DESC"), [token, per, t0 * 1000]).fetchone()
                  or c.execute(q.format(">=", "ASC"), [token, per, t0 * 1000]).fetchone())
            p1 = c.execute(q.format("<=", "DESC"), [token, per, t1 * 1000]).fetchone()
            if p0 and p1 and p0[0] and p1[0]:
                return p0[0], p1[0], f"chart candle closes ({per // 60}-minute bars) at the range ends"
    except duckdb.Error:
        pass
    pr = [(f["usd"] or 0) / f["amount"] for f in F if (f["amount"] or 0) > 0 and (f["usd"] or 0) >= 1]
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else 0
    return med(pr[:5]), med(pr[-5:]), "median of the first / last five fills of at least $1 (no candles for this market)"


def in_tape_only(c, lo, token):
    """True when the token first traded inside the tape window, so the deep extract adds nothing."""
    t = c.execute("SELECT min(time) FROM cd.tape WHERE token = ?", [token]).fetchone()[0]
    return t is not None and t > lo + 3600


def launch_fills(chain, token):
    """(first block of trading, fills in the token's first 1000 blocks), from the tape or the deep extract, whichever holds the launch."""
    c, lo = tape_con(chain)
    if in_tape_only(c, lo, token):
        bn = c.execute("SELECT min(bn) FROM cd.tape WHERE token = ?", [token]).fetchone()[0]
        rows = c.execute("""SELECT bn, '0x' || lower(hex(trader)), side FROM cd.tape WHERE token = ? AND bn <= ? ORDER BY bn, log_index""",
                         [token, bn + 1000]).fetchall()
    else:
        p = deep_fills_path(chain, token)
        bn = c.execute(f"SELECT min(bn) FROM '{p}'").fetchone()[0]
        rows = c.execute(f"SELECT bn, trader, side FROM '{p}' WHERE bn <= ? ORDER BY bn, log_index", [(bn or 0) + 1000]).fetchall()
    return bn, [dict(zip(("bn", "trader", "side"), r)) for r in rows]


def first_buys(chain, token, members):
    """Each member's earliest buy across the deep extract (before the tape window), the tape and the hot store."""
    c, lo = tape_con(chain)
    keys = ("bn", "time", "tx", "pool", "trader", "usd", "amount")
    one = "QUALIFY row_number() OVER (PARTITION BY trader ORDER BY bn, log_index) = 1"
    hx = ",".join(f"'{m[2:]}'" for m in members)
    first = {r[4]: r for r in c.execute(f"""SELECT bn, time, '0x' || lower(hex(tx_hash)) AS tx, pool, '0x' || lower(hex(trader)) AS trader, usd,
                                                   base_volume, log_index FROM cd.tape
                                            WHERE token = ? AND side = 'B' AND lower(hex(trader)) IN ({hx}) {one}""", [token]).fetchall()}
    if not in_tape_only(c, lo, token):
        p = deep_fills_path(chain, token)
        ml = ",".join(f"'{m}'" for m in members)
        first.update({r[4]: r for r in c.execute(f"""SELECT bn, time, tx, pool, trader, usd, amount, log_index FROM '{p}'
                                                     WHERE side = 'B' AND time < ? AND trader IN ({ml}) {one}""", [lo]).fetchall()})
    miss = [m for m in members if m not in first]
    hot = f"{EMC}/hot/{chain}/history.sqlite3"
    if miss and os.path.exists(hot):
        cut = c.execute("SELECT max(time) FROM cd.tape").fetchone()[0] or 0
        h = sq(hot)
        for r in h.execute(f"""SELECT f.slot, f.time, m.tx, f.pool, m.trader, m.usd, f.base_volume FROM fill_meta m JOIN fills f ON f.identity = m.identity
                               WHERE m.token = ? AND m.side = 'B' AND f.time > ? AND m.trader IN ({','.join('?' * len(miss))})
                               ORDER BY f.slot, f.event_order""", [token, cut, *miss]):
            first.setdefault(r[4], r)
    return {a: dict(zip(keys, r[:7])) for a, r in first.items()}


# ------------------------------------------------------------------------------------------------ transfers / balances
def transfers_path(chain, token):
    p = f"{EM}/transfers/{chain}/{token}.parquet"
    return p if os.path.exists(p) else None


def balances_at(chain, token, blocks, dec):
    """{block: ({holder: amount}, supply)} from the token's full transfer history (mints from 0x0 included, burns excluded)."""
    p = transfers_path(chain, token)
    if not p:
        return balances_from_analytics(chain, token, blocks)
    c = con()
    out = {}
    for b in blocks:
        rows = c.execute(f"""SELECT h, sum(d) / 10 ^ {dec} FROM (SELECT ta AS h, amount_raw AS d FROM '{p}' WHERE bn <= {int(b)}
                              UNION ALL SELECT fa, -amount_raw FROM '{p}' WHERE bn <= {int(b)}) GROUP BY h HAVING sum(d) > 0""").fetchall()
        bal = {h: a for h, a in rows if h not in BURN}
        out[b] = (bal, sum(bal.values()))
    return out


def analytics_coverage(chain):
    """Contiguous block ranges of published deltas, with overlaps assigned once."""
    import glob
    directory = f"{EMC}/data/{chain}/analytics"
    try:
        with open(f"{directory}/static.json") as stream:
            state = json.load(stream)
        snapshot = int(state["snapshot_block"])
        tail = max(snapshot, int(state.get("tail_end_block") or snapshot))
    except (OSError, ValueError, KeyError, TypeError):
        return {"snapshotBlock": None, "availableThrough": None, "segments": [], "reason": "analytics_snapshot_unavailable"}
    if not os.path.isfile(f"{directory}/holders_snap.parquet"):
        return {"snapshotBlock": snapshot, "availableThrough": None, "segments": [], "reason": "analytics_snapshot_unavailable"}
    end, segments = snapshot, []
    gap = f"{directory}/xfer_gap.parquet"
    if tail > snapshot:
        if not os.path.isfile(gap):
            return {"snapshotBlock": snapshot, "availableThrough": end, "segments": [], "reason": "analytics_delta_gap"}
        segments.append((gap, snapshot + 1, tail))
        end = tail
    spans=[]
    for path in glob.glob(f"{directory}/rt/xfer_*.parquet"):
        match=re.fullmatch(r"xfer_(\d+)-(\d+)\.parquet", os.path.basename(path))
        if match:
            spans.append((int(match[1]),int(match[2]),path))
    for first,last,path in sorted(spans):
        if last <= end:
            continue
        if first > end + 1:
            return {"snapshotBlock": snapshot, "availableThrough": end, "segments": segments, "reason": "analytics_delta_gap"}
        segments.append((path,end+1,last))
        end=last
    return {"snapshotBlock": snapshot, "availableThrough": end, "segments": segments, "reason": None}


def balances_from_analytics(chain, token, blocks):
    """Balances only at blocks covered by a snapshot plus contiguous published deltas."""
    coverage=analytics_coverage(chain)
    snapshot,head=coverage["snapshotBlock"],coverage["availableThrough"]
    if not blocks or snapshot is None or head is None or min(blocks)<snapshot or max(blocks)>head:
        return None
    directory=f"{EMC}/data/{chain}/analytics"
    c=con()
    c.execute("CREATE TEMP TABLE coverage_ranges(path VARCHAR, first_block BIGINT, last_block BIGINT)")
    if coverage["segments"]:
        c.executemany("INSERT INTO coverage_ranges VALUES (?,?,?)",coverage["segments"])
    out={}
    for block in blocks:
        queries=[f"SELECT holder, amount AS v FROM '{directory}/holders_snap.parquet' WHERE token = ?"]
        params=[token]
        paths=[path for path,first,last in coverage["segments"] if first<=block]
        if paths:
            files=",".join("'"+path.replace("'","''")+"'" for path in paths)
            queries.append(f"SELECT d.holder,d.delta AS v FROM read_parquet([{files}],filename=true) d JOIN coverage_ranges r ON d.filename=r.path WHERE d.token=? AND d.bn BETWEEN r.first_block AND r.last_block AND d.bn<=?")
            params.extend((token,int(block)))
        rows=c.execute("SELECT holder, sum(v) FROM ("+" UNION ALL ".join(queries)+") GROUP BY holder HAVING sum(v)>1e-12",params).fetchall()
        balances={holder:value for holder,value in rows if holder not in BURN}
        out[block]=(balances,sum(balances.values()))
    return out


def transfers_in(chain, token, b0, b1, dec):
    p = transfers_path(chain, token)
    if not p:
        return []
    rows = con().execute(f"SELECT bn, tx_index, log_index, fa, ta, amount_raw / 10 ^ {dec} FROM '{p}' WHERE bn BETWEEN ? AND ? ORDER BY bn, log_index",
                         [b0, b1]).fetchall()
    return [dict(zip(("bn", "txIndex", "log", "from", "to", "amount"), r)) for r in rows]


def first_receipts(chain, token, members, dec):
    """Each member's first incoming transfer of the token (an airdrop, a send from another wallet, or the buy's own transfer); None
    without the token's transfer history."""
    p = transfers_path(chain, token)
    if not p:
        return None
    ml = ",".join(f"'{m}'" for m in members)
    rows = con().execute(f"""SELECT ta, bn, tx_index, fa, amount_raw / 10 ^ {dec} FROM '{p}' WHERE ta IN ({ml})
                             QUALIFY row_number() OVER (PARTITION BY ta ORDER BY bn, log_index) = 1""").fetchall()
    return {r[0]: {"time": time_at(chain, r[1]), "block": r[1], "txIndex": r[2], "from": r[3], "amount": r[4]} for r in rows}


def tx_info(chain, bn, txi):
    """(hash, sender, called contract) of the transaction at (block, index)."""
    r = rpc(chain, "eth_getTransactionByBlockNumberAndIndex", [hex(int(bn)), hex(int(txi))]) or {}
    return r.get("hash"), (r.get("from") or "").lower() or None, (r.get("to") or "").lower() or None


def tx_hash(chain, bn, txi):
    return tx_info(chain, bn, txi)[0]


# ------------------------------------------------------------------------------------------------ cluster evidence model
def knum(v):
    """Label-engine compact numbers: '4.46k' -> 4460.0."""
    if v in (None, ""):
        return None
    v = str(v)
    m = {"k": 1e3, "m": 1e6, "b": 1e9}.get(v[-1].lower())
    try:
        return float(v[:-1]) * m if m else float(v)
    except ValueError:
        return None


def profile(chain, a):
    f = facts(chain, a)
    dg = (f.get("dg") or "/").split("/")   # graph-loop 'dg' = out-degree / in-degree
    return {"contractsCreated": knum(f.get("mk")), "outDegree": knum(dg[0]), "inDegree": knum(dg[1]) if len(dg) > 1 else None,
            "labelHint": f.get("hint"), "activeDays": f.get("seen")}


def safe_owners(chain, a):
    r = rpc(chain, "eth_call", [{"to": a, "data": "0xa0e67e2b"}, "latest"])   # getOwners()
    if not r or len(r) < 130:
        return []
    b = bytes.fromhex(r[2:])
    try:
        off = int.from_bytes(b[:32], "big")
        n = int.from_bytes(b[off:off + 32], "big")
        return ["0x" + b[off + 32 + 32 * i + 12: off + 64 + 32 * i].hex() for i in range(min(n, 20))]
    except Exception:
        return []


def build_clusters(chain, wallets, wstats, first_block_buyers, same_block, same_block_sell=None, token_sender=None, funding_ok=None):
    """wallets: candidate addresses (no venues / exchanges). Returns (clusters, links, funder groups).
    funding_ok(funder, wallet) -> bool, optional: when given, a native transfer counts as funding only if it says yes (callers pass
    "this funder's first transfer to the wallet came before the wallet's first buy"); without it every native sender counts, as before."""
    W = set(wallets)
    pair = defaultdict(lambda: {"types": set(), "evidence": [], "counter": [], "strong": 0, "weak": 0})

    def pairs(ms):
        """Every pair of a small group (the merge rule is per pair); a star from the first member for large groups."""
        ms = sorted(ms)
        return [(x, y) for i, x in enumerate(ms) for y in ms[i + 1:]] if len(ms) <= 12 else [(ms[0], y) for y in ms[1:]]

    def add(a, b, typ, text, strong):
        if a == b:
            return
        k = tuple(sorted((a, b)))
        p = pair[k]
        if typ not in p["types"]:
            p["types"].add(typ)
            p["evidence"].append(text)
            p["strong" if strong else "weak"] += 1
    funders, deposit = defaultdict(set), defaultdict(set)
    for a in wallets:
        nb = neighbors(chain, a)
        outs = {n["ref"].lower(): n for n in nb.get("neighbors") or [] if n.get("dir") == "out" and n.get("ref")}
        ins = {n["ref"].lower(): n for n in nb.get("neighbors") or [] if n.get("dir") == "in" and n.get("ref")}
        for y in W & set(outs) & set(ins):
            add(a, y, "transfers_both_ways", f"transfers both ways ({outs[y].get('n')} out, {ins[y].get('n')} in, {outs[y].get('edge')})", True)
        for y, n in outs.items():
            if n.get("category") == "cex_deposit":
                deposit[y].add(a)
        for y, n in ins.items():
            if n.get("edge") == "native" and (funding_ok is None or funding_ok(y, a)):
                funders[y].add(a)
        f = facts(chain, a)
        by = (f.get("by") or "").lower()
        if by in W:
            add(a, by, "operator_contract", f"{by[:10]}… created contract {a[:10]}…", True)
        cat = label(chain, a).get("category")
        if cat == "multisig_or_safe":
            for o in safe_owners(chain, a):
                o = o.lower()
                if o in W:
                    add(a, o, "safe_owner", f"{o[:10]}… is an owner of Safe {a[:10]}…", True)
    for dep, ms in deposit.items():
        ms = sorted(ms)
        for x, y in pairs(ms):
            add(x, y, "shared_deposit", f"both sent to exchange deposit address {dep}", True)
    fgroups = []
    for fu, ms in funders.items():
        if len(ms) < 2:
            continue
        lab = label(chain, fu)
        fan = facts(chain, fu).get("dg", "")
        fanout = int(knum(fan.split("/")[0]) or 0) if "/" in fan else (neighbors(chain, fu).get("out_degree") or 0)
        bad = []
        if lab.get("category") in EXCH_CATS:
            bad.append("funder is an exchange wallet")
        if lab.get("category") in ("bridge", "dex_router", "dex_pool"):
            bad.append(f"funder is a {lab.get('category').replace('_', ' ')}")
        if fanout > FUNDER_MAX_FANOUT:
            bad.append(f"funder sent to {fanout} addresses (high fan-out)")
        fgroups.append({"funder": fu, "category": lab.get("category"), "fanout": fanout, "wallets": sorted(ms), "usable": not bad, "counter": bad})
        if not bad:
            ms = sorted(ms)
            for x, y in pairs(ms):
                add(x, y, "shared_funder", f"both received ETH from {fu} (funded {fanout} addresses)", False)
    for blk, ms in same_block.items():
        ms = sorted(set(ms) & W)
        for x, y in pairs(ms):
            add(x, y, "same_block_buy", f"both bought in block {blk}", False)
    for blk, ms in (same_block_sell or {}).items():
        ms = sorted(set(ms) & W)
        for x, y in pairs(ms):
            add(x, y, "same_block_sell", f"both sold in block {blk}", False)
    by_sender = defaultdict(list)
    for w, snd in (token_sender or {}).items():
        if w in W:
            by_sender[snd].append(w)
    for snd, ms in by_sender.items():
        cat = label(chain, snd).get("category")
        if len(ms) < 2 or cat in EXCH_CATS or cat in VENUE_CATS:
            continue
        ms = sorted(ms)
        for x, y in pairs(ms):
            add(x, y, "shared_token_sender", f"both first received the token from {snd}", False)
    # counter-evidence per linked pair
    for (a, b), p in pair.items():
        sa, sb = wstats.get(a, {}), wstats.get(b, {})
        na, nb_ = sa.get("netUsd", 0), sb.get("netUsd", 0)
        if na and nb_ and (na > 0) != (nb_ > 0) and min(abs(na), abs(nb_)) > 0.05 * max(abs(na), abs(nb_)):
            p["counter"].append(f"opposite positions in the range ({'+' if na > 0 else ''}{round(na)} vs {'+' if nb_ > 0 else ''}{round(nb_)} USD net)")
        fa_, fb_ = sa.get("firstTime"), sb.get("firstTime")
        la, lb = sa.get("lastTime"), sb.get("lastTime")
        if fa_ and fb_ and la and lb and (la < fb_ or lb < fa_):
            p["counter"].append("activity windows do not overlap")
    # merge: strong link, or >= 2 independent weak links; a shared funder alone never merges
    parent = {w: w for w in W}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    FUND, COOC = {"shared_funder", "shared_token_sender"}, {"same_block_buy", "same_block_sell"}
    ok = lambda p: p["strong"] >= 1 or (p["types"] & FUND and p["types"] & COOC)   # never on a funder alone, never on co-occurrence alone
    for (a, b), p in pair.items():
        if ok(p):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
    groups = defaultdict(list)
    for w in W:
        groups[find(w)].append(w)
    clusters = []
    for ms in groups.values():
        if len(ms) < 2:
            continue
        S = set(ms)
        links = [{"a": a, "b": b, "types": sorted(p["types"]), "evidence": p["evidence"], "counter": p["counter"],
                  "merged": bool(ok(p))} for (a, b), p in pair.items() if a in S and b in S]
        nstrong = sum(1 for l in links if l["merged"] and any(t in ("transfers_both_ways", "shared_deposit", "safe_owner", "operator_contract") for t in l["types"]))
        ncounter = sum(1 for l in links if l["counter"])
        strength = "strong" if nstrong and not ncounter and nstrong >= len(ms) - 1 else "weak" if not nstrong else "mixed"
        reasons = []
        kinds = defaultdict(int)
        for l in links:
            for t in l["types"]:
                kinds[t] += 1
        reasons.append("links: " + ", ".join(f"{k.replace('_', ' ')} ×{v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])))
        if ncounter:
            reasons.append(f"{ncounter} link{'s' if ncounter > 1 else ''} with counter-evidence")
        if strength == "weak":
            reasons.append("merged only on a funding signal plus a timing signal (never on a shared funder or co-occurrence alone)")
        flags = []
        fbb = [m for m in ms if m in first_block_buyers]
        if len(fbb) >= 2:
            flags.append({"kind": "first_block_buyers", "text": f"{len(fbb)} members bought in the first block of trading", "wallets": fbb})
        signals = {t for l in links for t in l["types"]} & {"same_block_buy", "shared_funder", "shared_token_sender", "same_block_sell"}
        if nstrong and len(signals) >= 2:
            flags.append({"kind": "possible_coordinated_buying", "text": "possible coordinated buying: " + ", ".join(sorted(t.replace("_", " ") for t in signals))
                          + " plus a strong link", "signals": sorted(signals)})
        cid = "k" + hashlib.sha1(",".join(sorted(ms)).encode()).hexdigest()[:10]
        clusters.append({"id": cid, "members": sorted(ms), "size": len(ms), "strength": strength, "reasons": reasons, "links": links, "flags": flags})
    return clusters, pair, fgroups


# ------------------------------------------------------------------------------------------------ investigate
def investigate(qs):
    mid = (qs.get("market") or "").lower()
    chain, token, error = range_market(mid)
    if error:
        return error
    try:
        t0, t1 = int(qs.get("from")), int(qs.get("to"))
    except (TypeError, ValueError):
        return 400, {"detail": "from and to are unix seconds."}
    t0, t1 = (t0 // 1000 if t0 > 1e11 else t0), (t1 // 1000 if t1 > 1e11 else t1)
    if not 0 < t1 - t0 <= 31 * 86400:
        return 400, {"detail": "the range must be positive and at most 31 days."}
    if chain not in RANGE_CHAINS:
        return range_unavailable("investigate/1", mid, chain)
    lim = min(20, max(1, int(qs["limit"]))) if str(qs.get("limit", "")).isdigit() else 5
    sym, dec = meta(chain, token)
    F, source = fills(chain, token, t0, t1)
    if not F:
        return 404, {"detail": "No fills for this market in the range."}
    b0, b1 = block_at(chain, t0), block_at(chain, t1 + 59)
    pools = {f["pool"] for f in F}
    ex_p = exchange_set(chain)
    # wallet aggregation (trader = transaction sender)
    ws = defaultdict(lambda: {"buyUsd": 0.0, "sellUsd": 0.0, "buyAmount": 0.0, "sellAmount": 0.0, "fills": 0, "pools": set(), "first": None, "last": None})
    for f in F:
        w = ws[f["trader"]]
        u = f["usd"] or 0.0
        if f["side"] == "B":
            w["buyUsd"] += u
            w["buyAmount"] += f["amount"] or 0
        else:
            w["sellUsd"] += u
            w["sellAmount"] += f["amount"] or 0
        w["fills"] += 1
        w["pools"].add(f["pool"])
        w["first"] = w["first"] or f
        w["last"] = f
    vol = sum(f["usd"] or 0 for f in F)
    # balances at from / to from the transfer history
    bals = balances_at(chain, token, [b0 - 1, b1], dec)
    hold_from, sup_from = (bals[b0 - 1] if bals else ({}, None))
    hold_to, sup_to = (bals[b1] if bals else ({}, None))
    # candidates: top traders + top holders (either side of the range), venues and exchanges set aside
    top_traders = sorted(ws, key=lambda a: -(ws[a]["buyUsd"] + ws[a]["sellUsd"]))[:80]
    top_holders = sorted(set(hold_from) | set(hold_to), key=lambda a: -max(hold_from.get(a, 0), hold_to.get(a, 0)))[:80]
    roles = {}
    for a in set(top_traders) | set(top_holders):
        cat = label(chain, a).get("category")
        roles[a] = "venue" if (a in pools or a == token or cat in VENUE_CATS) else "exchange" if cat in EXCH_CATS else "wallet"
    cand = [a for a in roles if roles[a] == "wallet"]
    # first block of trading and same-block buys
    first_bn, LF = launch_fills(chain, token)
    fbb = {f["trader"] for f in LF if f["bn"] == first_bn and f["side"] == "B"}
    same_block = defaultdict(list)
    launch_end = (first_bn or 0) + 1000   # same-block timing only means something at launch; liquid markets co-trade in every block
    for f in F:
        if f["side"] == "B" and f["bn"] <= launch_end:
            same_block[f["bn"]].append(f["trader"])
    same_block = {b: list(set(m)) for b, m in same_block.items() if len(set(m)) > 1}
    wstats = {a: {"netUsd": ws[a]["buyUsd"] - ws[a]["sellUsd"], "firstTime": ws[a]["first"]["time"] if ws[a]["first"] else None,
                  "lastTime": ws[a]["last"]["time"] if ws[a]["last"] else None} for a in ws}
    sbs = defaultdict(list)
    for f in F:
        if f["side"] == "A" and f["bn"] <= launch_end:
            sbs[f["bn"]].append(f["trader"])
    sbs = {b: list(set(m)) for b, m in sbs.items() if len(set(m)) > 1}
    tsend = {}
    tp = transfers_path(chain, token)
    if tp:
        for w, snd in con().execute(f"""SELECT ta, arg_min(fa, bn * 100000 + log_index) FROM '{tp}' WHERE ta IN ({','.join("'" + a + "'" for a in cand) or "''"})
                                          GROUP BY ta""").fetchall():
            if snd not in pools and snd not in BURN and snd != token:
                tsend[w] = snd
    clusters, pair, fgroups = build_clusters(chain, cand, wstats, fbb, same_block, sbs, tsend)
    for c in clusters:
        c["id"] = cluster_id(mid, t0, t1, c["members"])
    member = {m: c["id"] for c in clusters for m in c["members"]}
    for c in clusters:
        S = set(c["members"])
        c["buyUsd"] = round(sum(ws[m]["buyUsd"] for m in S if m in ws), 2)
        c["sellUsd"] = round(sum(ws[m]["sellUsd"] for m in S if m in ws), 2)
        c["netUsd"] = round(c["buyUsd"] - c["sellUsd"], 2)
        c["sellers"] = sum(1 for m in S if m in ws and ws[m]["sellUsd"] > ws[m]["buyUsd"])
        c["traders"] = sum(1 for m in S if m in ws)
        c["pctFrom"] = round(100 * sum(hold_from.get(m, 0) for m in S) / sup_from, 3) if sup_from else None
        c["pctTo"] = round(100 * sum(hold_to.get(m, 0) for m in S) / sup_to, 3) if sup_to else None
        c["summary"] = (f"{c['sellers']} of {c['size']} linked wallets sold more than they bought in this range"
                        + (f"; their share went {c['pctFrom']:.1f}% → {c['pctTo']:.1f}%" if c["pctFrom"] is not None and c["pctTo"] is not None else ""))
        remember_cluster(c["id"], {"market": mid, "members": c["members"], "from": t0, "to": t1})
    for g in fgroups:
        g["pctFrom"] = round(100 * sum(hold_from.get(m, 0) for m in g["wallets"]) / sup_from, 3) if sup_from else None
        g["pctTo"] = round(100 * sum(hold_to.get(m, 0) for m in g["wallets"]) / sup_to, 3) if sup_to else None
    fgroups.sort(key=lambda g: -len(g["wallets"]))
    # events: large trades (per tx and trader), LP adds/removes, exchange flows, transfers between linked wallets
    events = []
    per_tx = defaultdict(lambda: {"usd": 0.0, "amount": 0.0})
    both = defaultdict(lambda: {"B": 0.0, "A": 0.0})
    for f in F:
        k = (f["tx"], f["trader"], f["side"])
        per_tx[k]["usd"] += f["usd"] or 0
        per_tx[k]["amount"] += f["amount"] or 0
        per_tx[k]["time"] = f["time"]
        both[(f["tx"], f["trader"])][f["side"]] += f["usd"] or 0
    round_trips = {k for k, v in both.items() if min(v["B"], v["A"]) > 0.5 * max(v["B"], v["A"])}
    usds = sorted(f["usd"] or 0 for f in F)
    med = usds[len(usds) // 2] if usds else 0
    thr = max(5 * med, 0.002 * vol, 100.0)   # large relative to this range's typical fill and its volume
    for (tx, tr, side), v in per_tx.items():
        if (tx, tr) in round_trips:
            continue   # bought and sold in one transaction (arbitrage / routing): not a directional trade
        if v["usd"] >= 100:   # every directional trade competes on size; 'large' is a label, not a filter
            big = v["usd"] >= thr
            events.append({"time": v["time"], "tx": tx, "kind": ("large_" if big else "") + ("buy" if side == "B" else "sell"), "usd": round(v["usd"], 2),
                           "amount": v["amount"], "wallets": [tr], "cluster": member.get(tr), "share": round(v["usd"] / vol, 4) if vol else None})
    T = transfers_in(chain, token, b0, b1, dec)
    swap_tx = {(f["bn"], f["txIndex"]) for f in F}
    ex = set()
    if ex_p:
        adds = {t["from"] for t in T} | {t["to"] for t in T}
        if adds:
            c = con()
            ex = {r[0] for r in c.execute(f"""SELECT '0x' || lower(hex(address)) FROM read_parquet('{ex_p}')
                                               WHERE lower(hex(address)) IN ({','.join("'" + a[2:] + "'" for a in adds)})""").fetchall()}
    px = (sum(f["usd"] or 0 for f in F) / max(1e-18, sum(f["amount"] or 0 for f in F))) if F else 0
    for t in T:
        kind = None
        if (t["bn"], t["txIndex"]) not in swap_tx and (t["to"] in pools or t["from"] in pools):
            kind = "pool_inflow" if t["to"] in pools else "pool_outflow"   # LP add/remove, or a contract-level move: not a swap
        elif t["to"] in ex and t["from"] not in ex:
            kind = "cex_deposit"
        elif t["from"] in ex and t["to"] not in ex:
            kind = "cex_withdrawal"
        elif member.get(t["from"]) and member.get(t["from"]) == member.get(t["to"]):
            kind = "linked_transfer"
        if kind:
            usd = t["amount"] * px
            if kind in ("pool_inflow", "pool_outflow") or usd >= thr / 2:
                events.append({"time": time_at(chain, t["bn"]), "bn": t["bn"], "txIndex": t["txIndex"], "tx": None, "kind": kind, "usd": round(usd, 2),
                               "amount": t["amount"], "wallets": [t["from"], t["to"]], "cluster": member.get(t["from"]) or member.get(t["to"]),
                               "valuation": "range average price",
                               **({"text": "tokens left the pool outside a swap (LP removal or a token-contract transfer)"} if kind == "pool_outflow" else
                                  {"text": "tokens entered the pool outside a swap (LP add or a direct transfer)"} if kind == "pool_inflow" else {})})
    for e in events:   # a pool outflow whose recipient sells the same amount back into that pool soon after: report the pair, in numbers
        if e["kind"] == "pool_outflow":
            back = next((f for f in F if f["trader"] == e["wallets"][1] and f["pool"] == e["wallets"][0] and f["side"] == "A"
                         and 0 <= f["bn"] - e["bn"] <= 50 and abs((f["amount"] or 0) - e["amount"]) <= 0.01 * e["amount"]), None)
            if back:
                e["soldBack"] = {"tx": back["tx"], "blocks": back["bn"] - e["bn"], "usd": round(back["usd"] or 0, 2)}
                e["text"] += f"; the recipient sold the same amount back into this pool {back['bn'] - e['bn']} blocks later for ${back['usd'] or 0:,.2f}"
    # the same transfer-level move repeated (same kind, wallets and amount; e.g. arbitrum-style drains run twice) is one event
    merged, seen = [], {}
    for e in sorted(events, key=lambda e: e["time"]):
        k = (e["kind"], tuple(e["wallets"]), round(e.get("amount") or 0, 6)) if e.get("bn") is not None else None
        if k and k in seen:
            m = seen[k]
            m["repeats"] = m.get("repeats", 1) + 1
            m["usd"] = round(m["usd"] + e["usd"], 2)
            m.setdefault("occurrences", [{"time": m["time"], "bn": m["bn"], "txIndex": m["txIndex"]}]).append(
                {"time": e["time"], "bn": e["bn"], "txIndex": e["txIndex"]})
            continue
        if k:
            seen[k] = e
        merged.append(e)
    events = merged
    events.sort(key=lambda e: -(e["usd"] or 0))
    shown = events[:lim]
    token_l = token.lower()
    for e in shown:
        if not e["tx"] and e.get("bn") is not None:
            e["tx"], snd, called = tx_info(chain, e["bn"], e["txIndex"])
            if e["kind"] in ("pool_outflow", "pool_inflow") and snd:
                e["operator"] = snd
                if called == token_l:
                    e["text"] = (e["text"].split(";")[0] + f"; executed by {snd} calling the token contract directly, not a swap"
                                 + (";" + e["text"].split(";", 1)[1] if ";" in e["text"] else ""))
        for o in e.get("occurrences") or []:
            o["tx"] = tx_hash(chain, o["bn"], o["txIndex"]) if len(e["occurrences"]) <= 5 else None
        if e.get("repeats"):
            e["text"] = (e.get("text") or e["kind"]) + f" ({e['repeats']} times in this range, same amount each time)"
    shown.sort(key=lambda e: e["time"])
    # wallet rows
    rows = []
    top = sorted(ws, key=lambda a: -(ws[a]["buyUsd"] + ws[a]["sellUsd"]))[:60]
    for a in top + sorted(set(member) - set(top)):
        w = ws.get(a) or {"buyUsd": 0.0, "sellUsd": 0.0, "buyAmount": 0.0, "sellAmount": 0.0, "fills": 0, "pools": set(), "first": None, "last": None}
        rows.append({"address": a, "id": f"{chain}:w:{a}", "entity_id": "addr:" + a, "role": roles.get(a) or ("venue" if a in pools else "wallet"),
                     "category": label(chain, a).get("category"), "name": label(chain, a).get("entity"),
                     "buyUsd": round(w["buyUsd"], 2), "sellUsd": round(w["sellUsd"], 2), "netUsd": round(w["buyUsd"] - w["sellUsd"], 2),
                     "buyAmount": w["buyAmount"], "sellAmount": w["sellAmount"], "fills": w["fills"], "pools": sorted(w["pools"]),
                     "firstFill": w["first"] and {"time": w["first"]["time"], "tx": w["first"]["tx"]},
                     "lastFill": w["last"] and {"time": w["last"]["time"], "tx": w["last"]["tx"]},
                     "profile": profile(chain, a) if len(rows) < 12 else None,
                     "cluster": member.get(a), "pctFrom": round(100 * hold_from.get(a, 0) / sup_from, 4) if sup_from else None,
                     "pctTo": round(100 * hold_to.get(a, 0) / sup_to, 4) if sup_to else None})
    launch = None
    if LF and first_bn is not None:
        lb = {}
        for f in LF:
            if f["side"] == "B" and f["bn"] <= first_bn + 1000 and f["trader"] not in lb:
                lb[f["trader"]] = f["bn"]
        L = set(lb)
        launch = {"window": "first 1000 blocks of trading", "fromBlock": first_bn, "wallets": len(L),
                  "pctFrom": round(100 * sum(hold_from.get(a, 0) for a in L) / sup_from, 2) if sup_from else None,
                  "pctTo": round(100 * sum(hold_to.get(a, 0) for a in L) / sup_to, 2) if sup_to else None,
                  "soldInRange": sum(1 for a in L if a in ws and ws[a]["sellUsd"] > ws[a]["buyUsd"]),
                  "firstBlockBuyers": len(fbb),
                  "text": "description only: buying early is not evidence of coordination"}
    no_l2_eth = sum(1 for a in cand if (facts(chain, a).get("eth") or "/").split("/")[-1] in ("0", ""))
    p0, p1, pbasis = range_prices(chain, token, t0, t1, F)
    clusters.sort(key=lambda c: -max(c["pctFrom"] or 0, c["pctTo"] or 0, abs(c["netUsd"]) / max(vol, 1)))
    return 200, {
        "schema": "investigate/1", "market": mid, "chain": chain, "token": token, "symbol": sym, "from": t0, "to": t1, "fromBlock": b0, "toBlock": b1,
        "source": source, "price": {"first": p0, "last": p1, "changePct": round(100 * (p1 / p0 - 1), 2) if p0 else None, "basis": pbasis},
        "volume": {"usd": round(vol, 2), "buyUsd": round(sum(f["usd"] or 0 for f in F if f["side"] == "B"), 2),
                   "sellUsd": round(sum(f["usd"] or 0 for f in F if f["side"] == "A"), 2), "fills": len(F), "traders": len(ws), "pools": sorted(pools)},
        "supply": {"from": sup_from, "to": sup_to, "basis": ("sum of positive balances from the token's full transfer history" if transfers_path(chain, token)
                   else "map snapshot holders plus every transfer since (block-exact)") if bals else None},
        "roundTrips": {"txs": len(round_trips), "usd": round(sum(min(both[k]["B"], both[k]["A"]) for k in round_trips), 2),
                       "note": "transactions where one trader bought and sold the token (arbitrage / routing), left out of key events"},
        "wallets": rows, "clusters": clusters, "funderGroups": fgroups[:10], "launchBuyers": launch, "events": shown, "eventsTotal": len(events),
        "funding": {"candidates": len(cand), "noL2EthSender": no_l2_eth,
                    "note": f"{no_l2_eth} of {len(cand)} wallets never received ETH from another L2 address (bridge deposits arrive without an L2 sender), "
                            "so a shared ETH funder cannot be checked for them on this chain"},
        "notes": ["Everything is joined on raw fills and transfers (tx, pool, block, trader); trader = transaction sender.",
                  "Co-occurrence is not causation: events and clusters are reported with numbers, not as the cause of the move.",
                  "Clusters merge only on a strong link (transfers both ways, shared exchange deposit address, Safe owner, operator/contract) "
                  "or on two independent weak links (shared funder, same-block buy); a shared funder alone never merges wallets."]
                 + ([] if bals else ["Supply shares need the token's transfer history; extract it with pull_token_transfers.sh."])}


def cluster_detail(qs):
    cid = qs.get("cluster") or ""
    c = get_cluster(cid)
    if not c:
        return 404, {"detail": "Unknown cluster id; call investigate for the range first."}
    code, d = investigate({"market": c["market"], "from": c["from"], "to": c["to"], "limit": "20"})
    if code != 200:
        return code, d
    cl = next((x for x in d["clusters"] if x["id"] == cid), None)
    if not cl:
        return 404, {"detail": "The cluster no longer forms with the current data."}
    rows = [w for w in d["wallets"] if w["cluster"] == cid]
    ev = [e for e in d["events"] if e.get("cluster") == cid]
    return 200, {"schema": "cluster/1", "market": c["market"], "from": c["from"], "to": c["to"], "cluster": cl, "wallets": rows, "events": ev, "notes": d["notes"]}


def first_entry(qs):
    mid, cid = (qs.get("market") or "").lower(), qs.get("cluster") or ""
    c = get_cluster(cid)
    chain, token, error = range_market(mid)
    if error:
        return error
    if chain not in RANGE_CHAINS:
        return range_unavailable("first-entry/1", mid, chain)
    if c is not None and c["market"] != mid:
        return 400, {"detail": "This cluster belongs to a different market; reopen its investigation."}
    members = c["members"] if c else [a.lower() for a in (qs.get("wallets") or "").split(",") if ADDR.match(a.lower())]
    if not members:
        return 404, {"detail": "Unknown or expired cluster id; reopen its investigation or provide wallets."}
    fb = first_buys(chain, token, members)
    first = {a: {"time": f["time"], "tx": f["tx"], "block": f["bn"], "pool": f["pool"], "usd": round(f["usd"] or 0, 2), "amount": f["amount"]}
             for a, f in fb.items()}
    rec = first_receipts(chain, token, members, meta(chain, token)[1])
    pls = token_pools(chain, token) if rec else set()
    for i, (a, r) in enumerate(sorted((rec or {}).items(), key=lambda x: x[1]["block"])):
        r["tx"] = tx_hash(chain, r["block"], r["txIndex"]) if i < 25 else None
        r["kind"] = "swap" if r["from"] in pls else "mint" if r["from"] in BURN else "transfer"
    rows = [{"address": m, "id": f"{chain}:w:{m}", "firstBuy": first.get(m), "firstReceive": (rec or {}).get(m)} for m in members]
    got = [r for r in rows if r["firstBuy"]]
    earliest = min(got, key=lambda r: r["firstBuy"]["time"]) if got else None
    ins = [(r["firstBuy"]["time"], "buy", r) for r in rows if r["firstBuy"]] + [(r["firstReceive"]["time"], r["firstReceive"]["kind"], r)
                                                                              for r in rows if r["firstReceive"]]
    t_in, via, r_in = min(ins, key=lambda x: (x[0], x[1] != "buy")) if ins else (None, None, None)
    return 200, {"schema": "first-entry/1", "market": mid, "cluster": cid or None, "members": rows,
                 "first": {"address": earliest["address"], **earliest["firstBuy"]} if earliest else None,
                 "firstIn": {"address": r_in["address"], "via": via, **(r_in["firstBuy"] if via == "buy" else r_in["firstReceive"])} if r_in else None,
                 "focusTime": t_in,
                 "membersWithoutBuys": [r["address"] for r in rows if not r["firstBuy"]],
                 "receipts": "token transfer history" if rec is not None else None,
                 "notes": ["First buy = the member's earliest swap into the token (trader = transaction sender); wallets that only received "
                           "transfers have no buy.",
                           "firstIn = the earliest buy or incoming transfer of any member; focusTime is where the chart should move. "
                           "firstReceive.kind: swap = received from one of the token's pools in a swap sent by another address (a router, "
                           "relayer or smart account), mint = from the zero address, transfer = from another holder."
                           + ("" if rec is not None else " Incoming transfers need the token's transfer history (not extracted for this token), "
                              "so firstIn here covers buys only.")]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        qs = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        import directory_api
        import search_api
        import wallet_pnl_api
        import entities_api
        fn = {"/health": health, "/investigate": investigate, "/cluster": cluster_detail, "/cluster/first-entry": first_entry, **directory_api.ROUTES, **search_api.ROUTES, **wallet_pnl_api.ROUTES, **entities_api.ROUTES}.get(u.path)
        extra = {}
        try:
            if not fn:
                code, body = 404, {"detail": "unknown route"}
            elif SAFE_CANARY and u.path not in ("/health","/protocols","/protocol"):
                code,body=canary_blocked("route:"+u.path)
            elif u.path in HEAVY_ROUTES:
                code, body = run_heavy((u.path, tuple(sorted(qs.items()))), fn, qs)
                if code == 429:
                    extra["Retry-After"] = "5"
            else:
                code, body = fn(qs)
        except (CanaryBlocked,directory_api.CanaryBlocked) as e:
            code,body=canary_blocked(str(e))
        except Exception as e:
            import traceback
            traceback.print_exc()
            code, body = 500, {"detail": "Internal error: " + type(e).__name__}
        b = json.dumps(body, separators=(",", ":"), default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)


def _expire(ctx):
    ctx["expired"] = True
    for c in list(ctx["conns"]):
        try:
            c.interrupt()
        except Exception:
            pass


def guarded(fn, qs, budget_s=None):
    """fn(qs) under the time budget: when it runs out, the request's DuckDB / SQLite queries are interrupted and the client gets 503."""
    budget_s = BUDGET_S if budget_s is None else budget_s
    ctx = {"conns": [], "expired": False, "deadline": time.time() + budget_s}
    _req.ctx = ctx
    timer = threading.Timer(budget_s, _expire, [ctx])
    timer.daemon = True
    timer.start()
    try:
        return fn(qs)
    except CanaryBlocked as e:
        return canary_blocked(str(e))
    except TooHeavy as e:
        if e.detail == "budget":
            return 503, {"error": "too_heavy", "budgetSeconds": budget_s,
                         "detail": f"This investigation did not finish within {budget_s:.0f} s. Narrow the range or pick a less traded market."}
        return 422, {"error": "too_heavy", "detail": e.detail, **e.extra}
    except Exception:
        if ctx["expired"]:
            return 503, {"error": "too_heavy", "budgetSeconds": budget_s,
                         "detail": f"This investigation did not finish within {budget_s:.0f} s. Narrow the range or pick a less traded market."}
        raise
    finally:
        timer.cancel()
        _req.ctx = None
        for c in ctx["conns"]:
            try:
                c.close()
            except Exception:
                pass


def _cached(key):
    with _clock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1], hit[2]
        _cache.pop(key, None)
    return None


def run_heavy(key, fn, qs):
    """One run per distinct request at a time (others wait for it), at most HEAVY_SLOTS runs at once, results cached."""
    hit = _cached(key)
    if hit:
        return hit
    with _clock:
        ev = _inflight.get(key)
        mine = ev is None
        if mine:
            ev = _inflight[key] = threading.Event()
    if not mine:
        ev.wait(BUDGET_S + 5)
        return _cached(key) or (429, {"error": "busy", "detail": "The same investigation is still running; retry in a few seconds."})
    try:
        if not HEAVY_SLOTS.acquire(timeout=QUEUE_WAIT_S):
            return 429, {"error": "busy", "detail": "The investigation service is busy; retry in a few seconds."}
        try:
            code, body = guarded(fn, qs)
        finally:
            HEAVY_SLOTS.release()
        ttl = CACHE_TTL.get(code) or (CACHE_TTL["too_heavy"] if isinstance(body, dict) and body.get("error") == "too_heavy" else 30)
        with _clock:
            _cache[key] = (time.time() + ttl, code, body)
            if len(_cache) > 400:
                for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                    _cache.pop(k, None)
        return code, body
    finally:
        with _clock:
            _inflight.pop(key, None)
        ev.set()


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8898
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    if not SAFE_CANARY:
        import search_api
        threading.Thread(target=search_api.index, daemon=True).start()
        import entities_api
        threading.Thread(target=entities_api.state, daemon=True).start()
    print("em-investigate on", port, flush=True)
    srv.serve_forever()
