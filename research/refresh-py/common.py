"""Shared config and helpers for the Economic Machine incremental refresh (runs on skew).

Data sources for blocks after the snapshot:
  gap      /srv/skew/em-charts/tail/<chain>/{logs,token_transfers,transactions}/<t>_<from>-<to>.parquet
           (no blocks table: block times come from em-charts data/block_minutes_<chain>.parquet)
  realtime /srv/skew/em-realtime/out/<chain>/<table>/... listed by OK manifests (all five tables)
"""
import atexit
import glob
import json
import os
import re
import shutil
import sys
import time

import duckdb

ROOT = "/srv/skew/em-refresh"
SRC = f"{ROOT}/src"
STATE = f"{ROOT}/state"
DATA = "/srv/skew/em-data"
TAIL = "/srv/skew/em-charts/tail"
RT = "/srv/skew/em-realtime/out"
BLOCK_MINUTES = "/srv/skew/em-charts/data/block_minutes_{chain}.parquet"
TMP = f"{DATA}/tmp/refresh"
FLOOR_GB = 80

CHAINS = {
    "robinhood": {"id": 4663, "key": "rh", "snap_end": 88098834, "tail_end": 91447999, "db1": "econ2.duckdb",
                  "rpc": "https://robinhood-rpc.publicnode.com", "glserve": 8710,
                  # public endpoints (same set as em-realtime); rpc.py rotates across them
                  "rpcs": ["https://robinhood-rpc.publicnode.com", "https://robinhood.drpc.org", "https://rpc.mainnet.chain.robinhood.com"]},
    "arbitrum": {"id": 42161, "key": "arb", "snap_end": 512450747, "tail_end": 515849999, "db1": "econ.duckdb",
                 "rpc": "https://arbitrum-one-rpc.publicnode.com", "glserve": 8711,
                 "rpcs": ["https://arbitrum-one-rpc.publicnode.com", "https://arbitrum.drpc.org", "https://arb1.arbitrum.io/rpc"]},
    "arc": {"id": 5042, "key": "arc", "snap_end": 26859999, "tail_end": 26859999, "db1": "econ.duckdb",   # base build up to the cut (em-arc)
            "rpc": "https://arc-rpc.publicnode.com", "glserve": 8714, "rpcs": ["https://arc.drpc.org", "https://rpc.mainnet.arc.io", "https://arc-rpc.publicnode.com"]},
}
TABLES = ["blocks", "transactions", "logs", "token_transfers", "contract_creations"]
T0 = time.time()


def log(msg):
    print("%s [%6.0fs] %s" % (time.strftime("%H:%M:%S"), time.time() - T0, msg), file=sys.stderr, flush=True)


def warn(msg):
    """A step finished with degraded output (e.g. stale RPC-derived values). refresh.py points EM_WARN_FILE at the
    current step's warnings file, so this also works from subprocesses."""
    log("WARNING: " + msg)
    path = os.environ.get("EM_WARN_FILE")
    if path:
        with open(path, "a") as f:
            f.write(msg.replace("\n", " ") + "\n")


def free_gb(path=DATA):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def need_space(gb=FLOOR_GB, what=""):
    f = free_gb()
    if f < gb:
        raise SystemExit(f"abort {what}: only {f:.0f} GB free (< {gb} GB)")


_DUCK_TMP = {}


def duck_tmp():
    """Per-process DuckDB spill directory. DuckDB gives its spill files the same names in every process and deletes
    them on close, so processes sharing one temp_directory (refresh + em-labeldelta) destroyed each other's spills
    ("Could not read enough bytes from file .../duckdb_temp_storage_S64K-0.tmp", 2026-10-19)."""
    if os.getpid() in _DUCK_TMP:
        return _DUCK_TMP[os.getpid()]
    base = f"{TMP}/duck"
    os.makedirs(base, exist_ok=True)
    for d in os.listdir(base):            # directories of processes that are gone
        if d.startswith("p") and d[1:].isdigit() and not os.path.exists(f"/proc/{d[1:]}"):
            shutil.rmtree(f"{base}/{d}", ignore_errors=True)
    p = f"{base}/p{os.getpid()}"
    os.makedirs(p, exist_ok=True)
    atexit.register(shutil.rmtree, p, True)
    _DUCK_TMP[os.getpid()] = p
    return p


# skew's refresh unit (80-safe.conf) caps every DuckDB memory_limit and the spill size; unset (the VM) = the values below
DUCK_MEM_CAP = os.environ.get("EM_DUCK_MEM_CAP")
DUCK_TEMP_MAX = os.environ.get("EM_DUCK_TEMP_MAX", "60GB")


def _gb(v):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*GB\s*", str(v), re.I)
    return float(m.group(1)) if m else None


def cap_mem(mem):
    """mem, or EM_DUCK_MEM_CAP when that is lower."""
    a, b = _gb(mem), _gb(DUCK_MEM_CAP)
    return DUCK_MEM_CAP if a is not None and b is not None and b < a else mem


def connect(path=":memory:", threads=3, mem="8GB", read_only=False):
    con = duckdb.connect(path, read_only=read_only)
    con.execute("SET TimeZone='UTC'")       # day/month truncation must match the v1 runs (UTC hosts)
    con.execute(f"SET threads={threads}; SET memory_limit='{cap_mem(mem)}'; SET temp_directory='{duck_tmp()}';"
                f"SET preserve_insertion_order=false; SET max_temp_directory_size='{DUCK_TEMP_MAX}'; SET parquet_metadata_cache=true;")
    return con


def load_state(chain):
    p = f"{STATE}/{chain}.json"
    if os.path.exists(p):
        return json.load(open(p))
    return {}


def save_state(chain, st):
    os.makedirs(STATE, exist_ok=True)
    p = f"{STATE}/{chain}.json"
    with open(p + ".tmp", "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)


_RANGE = re.compile(r"_(\d+)-(\d+)\.parquet$")


def tail_files(chain, table, lo, hi):
    out = []
    for f in sorted(glob.glob(f"{TAIL}/{chain}/{table}/*.parquet")):
        m = _RANGE.search(f)
        a, b = int(m.group(1)), int(m.group(2))
        if b >= lo and a <= hi:
            out.append(f)
    return out


def rt_chunks(chain):
    """[(from, to, {table: path})] for OK realtime manifests, sorted."""
    res = []
    for m in glob.glob(f"{RT}/{chain}/manifests/*.json"):
        try:
            d = json.load(open(m))
        except Exception:
            continue
        if d.get("status") != "OK":
            continue
        paths = {}
        for t in TABLES:
            f = (d.get("files") or {}).get(t)
            if not f:
                break
            p = f["path"] if os.path.isabs(f["path"]) else os.path.join(RT, chain, f["path"])
            if not os.path.exists(p):
                break
            paths[t] = p
        if len(paths) == len(TABLES):
            res.append((d["range"]["from"], d["range"]["to"], paths))
    res.sort()
    return res


def tip(chain):
    """Highest block with a contiguous OK realtime chunk from the tail end."""
    nxt = CHAINS[chain]["tail_end"] + 1
    for a, b, _ in rt_chunks(chain):
        if a > nxt:
            break
        nxt = max(nxt, b + 1)
    return nxt - 1


def q(paths):
    return "[" + ",".join("'%s'" % p for p in paths) + "]"


def bind_inc_views(con, chain, lo, hi, prefix=""):
    """Create views blocks/transactions/logs/token_transfers/contract_creations over [lo, hi] from gap + realtime
    chunks (only realtime chunks fully inside a contiguous OK run are used). Returns the number of source files."""
    cfg = CHAINS[chain]
    te = cfg["tail_end"]
    parts = {t: [] for t in TABLES}
    n = 0
    if lo <= te:
        for t in ("logs", "token_transfers", "transactions"):
            fs = tail_files(chain, t, lo, min(hi, te))
            n += len(fs)
            if fs:
                col = "block_number"
                parts[t].append(f"SELECT * FROM read_parquet({q(fs)}) WHERE {col} BETWEEN {lo} AND {min(hi, te)}")
        bm = BLOCK_MINUTES.format(chain=chain)
        # synthetic block rows for the gap: timestamp interpolated inside each UTC minute (minute precision)
        parts["blocks"].append(f"""
            SELECT b::UBIGINT AS number, (minute::BIGINT + ((b - first_block::BIGINT) * 60) // greatest(last_block::BIGINT - first_block::BIGINT + 1, 1))::UBIGINT AS timestamp
            FROM (SELECT minute, first_block, last_block,
                         unnest(range(greatest(first_block::BIGINT, {lo}), least(last_block::BIGINT, {min(hi, te)}) + 1)) AS b
                  FROM read_parquet('{bm}') WHERE last_block >= {lo} AND first_block <= {min(hi, te)})""")
    rts = [c for c in rt_chunks(chain) if c[1] >= max(lo, te + 1) and c[0] <= hi]
    for t in TABLES:
        fs = [c[2][t] for c in rts]
        if fs:
            col = "number" if t == "blocks" else "block_number"
            sel = "number, timestamp" if t == "blocks" else "*"
            parts[t].append(f"SELECT {sel} FROM read_parquet({q(fs)}) WHERE {col} BETWEEN {max(lo, te + 1)} AND {hi}")
    n += len(rts) * len(TABLES)
    # empty typed fallbacks (schema of the realtime/extractor Parquet)
    empty = {
        "blocks": "SELECT NULL::UBIGINT AS number, NULL::UBIGINT AS timestamp WHERE false",
        "contract_creations": "SELECT NULL::UBIGINT AS block_number, NULL::UINTEGER AS tx_index, NULL::BLOB AS address, "
                              "NULL::BLOB AS creator, NULL::BLOB AS tx_from WHERE false",
    }
    for t in TABLES:
        body = " UNION ALL BY NAME ".join(parts[t]) if parts[t] else empty.get(t)
        if body is None:
            raise SystemExit(f"no {t} source for {chain} [{lo},{hi}]")
        cid = cfg["id"]
        con.execute(f"CREATE OR REPLACE VIEW {prefix}{t} AS SELECT {cid}::INTEGER AS chain, * FROM ({body})")
    return n


def hardlink_tree(src, dst):
    """Mirror a directory of immutable Parquet files with hard links (no extra space)."""
    for root, _, files in os.walk(src):
        rel = os.path.relpath(root, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for f in files:
            s, d = os.path.join(root, f), os.path.join(dst, rel, f)
            if not os.path.exists(d):
                os.link(s, d)


def run_sql(con, path, params, label=None, skip=None):
    """Execute an econ SQL template (same statement splitting as econ/py/run.py, incl. the per-branch acts insert)."""
    acts_ddl = """CREATE OR REPLACE TEMP TABLE acts (block_number UBIGINT, tx_index UINTEGER, log_index UINTEGER, sub INTEGER,
      contract VARCHAR, family VARCHAR, action VARCHAR, wallet_ev VARCHAR, asset VARCHAR, amount_raw HUGEINT, counterparty VARCHAR,
      extra VARCHAR, pos_kind VARCHAR, pos_sign INTEGER, pos_abs HUGEINT, pos_key VARCHAR, pos_shares HUGEINT, pos_liq HUGEINT)"""
    sql = open(path).read()
    for k, v in params.items():
        sql = sql.replace("${" + k + "}", str(v))
    t = time.time()
    stmts = [s.strip() for s in sql.split(";\n") if s.strip() and not all(
        l.strip().startswith("--") or not l.strip() for l in s.strip().splitlines())]
    expanded = []
    for s in stmts:
        if s.startswith("CREATE OR REPLACE TEMP TABLE acts AS"):
            expanded.append(acts_ddl)
            body = s[len("CREATE OR REPLACE TEMP TABLE acts AS"):]
            for br in body.split("UNION ALL BY NAME"):
                br = "\n".join(l for l in br.splitlines() if not l.strip().startswith("--")).strip()
                if br:
                    expanded.append("INSERT INTO acts BY NAME " + br)
        else:
            expanded.append(s)
    for s in expanded:
        if skip and skip(s):
            continue
        t1 = time.time()
        # single-file COPY outputs are written as <file>.tmp and renamed, so readers (em-charts analytics, the agent)
        # never see a partial file
        m = re.search(r"\bTO '([^']+)'", s) if s.lstrip().upper().startswith("COPY") and "PARTITION_BY" not in s else None
        if m:
            s = s.replace(f"TO '{m.group(1)}'", f"TO '{m.group(1)}.tmp'", 1)
        try:
            con.execute(s)
            if m:
                os.replace(m.group(1) + ".tmp", m.group(1))
        except Exception as e:
            log(f"FAILED in {path}:\n{s[:500]}\n{e}")
            raise
        dt = time.time() - t1
        if dt > 20:
            log("   %7.1fs  %s" % (dt, " ".join(s.split())[:110]))
    log(f"[{label or os.path.basename(path)}] {time.time() - t:.1f}s")
