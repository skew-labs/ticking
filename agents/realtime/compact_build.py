"""compact_build.py <chain> <stage> [history_max_block]: one Option A compact-serving stage (run by em-evm-rt-bringup).

The heavy map / econ / labels are built on the VMs and stay in Blob em-mapping/<chain>/; this pulls only what skew serves:
  econ     econ files the agent / charts read -> em-data/econ/<chain>/ (balance sheets, summary, token_meta, prices, focus CSVs)
  sheets   a balance_sheets.parquet too big to copy: 32 wallet buckets, sorted, card columns -> em-data/compact/<chain>/sheets/
  held     the econ check's row-level holds (held_rows.parquet) -> em-data/econ/<chain>/
  labels   graph-loop labels, sorted by address (row-group pruning for point lookups) -> em-data/compact/<chain>/labels.parquet
  research named-institution labels (research_labels.parquet), sorted by address; missing is not an error
  top      per-address summary of the 2M most active addresses -> em-data/compact/<chain>/top_addresses.parquet
  minutes  block -> UTC minute map over the last 35 days of history -> em-charts/data/block_minutes_<chain>.parquet
  fills    DEX fills window for charts, sized by measurement (FILLS_GB budget, 2-30 days)
           -> em-data/map/<chain>/dex_trades/range_id=*/ + dex_pools/
  resync   follower chunks (logs, blocks, transactions) after the history end that the pruner already moved to Blob
Prints one JSON result line; exits non-zero on failure. DuckDB: 4 threads, 8 GB, temp <= 15 GB; never prints the SAS.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

import duckdb

sys.path.insert(0, "/srv/skew/em-realtime")
import evm_cfg  # noqa: E402

B = "https://arbitrum.blob.core.windows.net/em-mapping"
EM = "/srv/skew/em-data"
COMPACT = EM + "/compact"
CHARTS = "/srv/skew/em-charts/data"
RT = "/srv/skew/em-realtime/out"
AZ = "/root/bin/azcopy"
FILLS_GB = 5.0
ECON_FILES = ["balance_sheets.parquet", "balance_sheet_summary.parquet", "token_meta.parquet", "prices_daily.parquet",
              "sheet_summary_top2000.csv", "focus_stablecoins.csv", "coverage_chain.csv", "coverage_top200.csv"]
ECON_MAX_GB = 4.0


def sas():
    return open("/root/.em-blob.sas").read().strip().lstrip("?")


def scrub(t):
    return str(t).replace(sas(), "SAS")


TEST_MAP = json.loads(os.environ.get("EMC_TEST_MAP", "{}"))   # tests only: {"<chain>/map/": "map/<chain>/"} for older Blob layouts


def tr(path):
    for a, b in TEST_MAP.items():
        if path.startswith(a):
            return b + path[len(a):]
    return path


def url(path):
    return f"{B}/{tr(path)}?{sas()}"


def blob_list(prefix):
    out, marker = [], ""
    while True:
        u = f"https://arbitrum.blob.core.windows.net/em-mapping?restype=container&comp=list&prefix={tr(prefix)}&maxresults=5000&{sas()}"
        x = urllib.request.urlopen(u + (f"&marker={marker}" if marker else ""), timeout=120).read().decode()
        found = re.findall(r"<Name>([^<]+)</Name>.*?<Content-Length>(\d+)</Content-Length>", x, re.S)
        rev = {v: k for k, v in TEST_MAP.items()}
        for n, sz in found:
            for v, k in rev.items():
                if n.startswith(v):
                    n = k + n[len(v):]
            out.append((n, int(sz)))
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", x)
        if not m:
            return out
        marker = m.group(1)


def con():
    for d in glob.glob("/srv/skew/em-realtime/tmp/duck-*"):   # spill left by a killed run (OOM / stop): its pid is gone
        pid = d.rsplit("-", 1)[-1]
        if pid.isdigit() and int(pid) != os.getpid() and not os.path.exists(f"/proc/{pid}"):
            shutil.rmtree(d, ignore_errors=True)
    c = duckdb.connect()
    tmp = f"/srv/skew/em-realtime/tmp/duck-{os.getpid()}"
    os.makedirs(tmp, exist_ok=True)
    mem = os.environ.get("EM_COMPACT_MEM", "8GB")   # the bring-up unit lowers it when skew memory is tight (it spills instead)
    c.execute(f"SET threads=4; SET memory_limit='{mem}'; SET temp_directory='{tmp}'; SET max_temp_directory_size='15GB';"
              "SET preserve_insertion_order=false; SET enable_progress_bar=false; SET home_directory='/root'")
    c.execute("LOAD httpfs")
    return c


def fetch(path, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    rc = subprocess.run([AZ, "copy", url(path), dst + ".tmp", "--log-level", "ERROR", "--cap-mbps", "160"],   # ~20 MB/s: skew's disk is shared
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=dict(os.environ, HOME=os.environ.get("HOME") or "/root", AZCOPY_BUFFER_GB="0.5")).returncode   # systemd units have no HOME; azcopy RAM <= ~1 GB
    if rc != 0:
        raise RuntimeError(f"azcopy rc={rc} for {path}")
    os.replace(dst + ".tmp", dst)
    return os.path.getsize(dst)


def du(p):
    return sum(os.path.getsize(f) for f in glob.glob(p + "/**", recursive=True) if os.path.isfile(f)) if os.path.isdir(p) else (os.path.getsize(p) if os.path.isfile(p) else 0)


def s_econ(chain, hmax):
    have = {os.path.basename(n): s for n, s in blob_list(f"{chain}/econ/") if n.count("/") == 2}
    got, skipped = {}, {}
    for f in ECON_FILES:
        if f not in have:
            skipped[f] = "missing"
        elif have[f] > ECON_MAX_GB * 1e9:
            skipped[f] = f"{have[f] / 1e9:.1f} GB > {ECON_MAX_GB} GB cap (served from Blob)"
        else:
            got[f] = fetch(f"{chain}/econ/{f}", f"{EM}/econ/{chain}/{f}")
    if "token_meta.parquet" not in got:
        raise RuntimeError(f"token_meta.parquet {skipped.get('token_meta.parquet')}")
    return {"bytes": sum(got.values()), "files": got, "skipped": skipped}


def s_sheets(chain, hmax):
    """balance_sheets.parquet above the econ copy cap (Base 6.6 GB): a local copy in 32 wallet buckets (last byte mod 32, as the
    map), each sorted by wallet with the card columns only, so a wallet sheet is one small row-group read. No-op when the full
    file is local. The download is deleted afterwards."""
    if os.path.exists(f"{EM}/econ/{chain}/balance_sheets.parquet"):
        return {"bytes": 0, "skipped": "full balance_sheets.parquet is local"}
    if not [n for n, _ in blob_list(f"{chain}/econ/balance_sheets.parquet")]:
        return {"bytes": 0, "missing": True}
    dl = f"{COMPACT}/{chain}/balance_sheets.download.parquet"
    fetch(f"{chain}/econ/balance_sheets.parquet", dl)
    dst = f"{COMPACT}/{chain}/sheets"
    tmp = dst + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    c = con()
    cols = "lower(wallet) AS wallet, category, protocol, asset, symbol, amount, usd"
    split = tmp + "/split"
    try:   # one pass over the big file into 32 unsorted buckets, then each small bucket sorted on its own
        c.execute(f"""COPY (SELECT {cols}, ('0x' || right(lower(wallet), 2))::INTEGER % 32 AS b FROM read_parquet('{dl}'))
                      TO '{split}' (FORMAT parquet, PARTITION_BY (b), COMPRESSION zstd)""")
        for b in range(32):
            c.execute(f"""COPY (SELECT {cols.replace("lower(wallet) AS wallet", "wallet")} FROM read_parquet('{split}/b={b}/*.parquet')
                                ORDER BY wallet) TO '{tmp}/part-{b}.parquet' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 50000)""")
            shutil.rmtree(f"{split}/b={b}")
        os.rmdir(split)
        n = c.execute(f"SELECT count(*) FROM read_parquet('{tmp}/part-*.parquet')").fetchone()[0]
        want = c.execute(f"SELECT count(*) FROM read_parquet('{dl}')").fetchone()[0]
        if n != want:
            raise RuntimeError(f"bucketed {n} rows, source has {want}")
    finally:
        os.remove(dl)
    shutil.rmtree(dst, ignore_errors=True)
    os.replace(tmp, dst)
    return {"bytes": du(dst), "rows": n, "buckets": 32}


def s_held(chain, hmax):
    """econ/<chain>/held_rows.parquet: the econ check's row-level holds (wallet-sorted; evm_compact reads it per wallet, no
    restart). Missing in Blob is not an error. Its own stage, so a new upload does not re-run econ and the charts."""
    if not [n for n, _ in blob_list(f"{chain}/econ/held_rows.parquet")]:
        return {"bytes": 0, "missing": True}
    return {"bytes": fetch(f"{chain}/econ/held_rows.parquet", f"{EM}/econ/{chain}/held_rows.parquet")}


def s_labels(chain, hmax):
    tmp = f"{COMPACT}/{chain}/labels.unsorted.parquet"
    fetch(f"{chain}/graphloop/export/labels.parquet", tmp)
    dst = f"{COMPACT}/{chain}/labels.parquet"
    c = con()
    c.execute(f"COPY (SELECT * FROM read_parquet('{tmp}') ORDER BY address) TO '{dst}.tmp' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)")
    n = c.execute(f"SELECT count(*) FROM read_parquet('{dst}.tmp')").fetchone()[0]
    os.replace(dst + ".tmp", dst)
    os.remove(tmp)
    return {"bytes": os.path.getsize(dst), "rows": n}


def s_research(chain, hmax):
    """Research labels (named institutions) next to labels.parquet; missing is fine (the ETag watch picks it up later)."""
    if not [n for n, _ in blob_list(f"{chain}/graphloop/export/research_labels.parquet")]:
        return {"bytes": 0, "missing": True}
    tmp = f"{COMPACT}/{chain}/research_labels.unsorted.parquet"
    fetch(f"{chain}/graphloop/export/research_labels.parquet", tmp)
    dst = f"{COMPACT}/{chain}/research_labels.parquet"
    c = con()
    c.execute(f"COPY (SELECT * FROM read_parquet('{tmp}') ORDER BY address) TO '{dst}.tmp' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)")
    n = c.execute(f"SELECT count(*) FROM read_parquet('{dst}.tmp')").fetchone()[0]
    os.replace(dst + ".tmp", dst)
    os.remove(tmp)
    return {"bytes": os.path.getsize(dst), "rows": n}


def s_top(chain, hmax):
    parts = [n for n, _ in blob_list(f"{chain}/map/addresses/") if n.endswith(".parquet")]
    if not parts:
        raise RuntimeError("no map addresses in Blob")
    dst = f"{COMPACT}/{chain}/top_addresses.parquet"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    c = con()
    src = "[" + ",".join(f"'{url(p)}'" for p in parts) + "]"
    try:
        c.execute(f"""COPY (SELECT address, first_block, first_time, last_block, last_time, tx_sent, tx_received, token_transfers_out,
                                   token_transfers_in, distinct_counterparties, is_contract, dex_trades
                            FROM read_parquet({src})
                            ORDER BY tx_sent + tx_received + token_transfers_in + token_transfers_out DESC LIMIT 2000000)
                      TO '{dst}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
    except Exception as e:
        raise RuntimeError(scrub(e)[:300])
    os.replace(dst + ".tmp", dst)
    return {"bytes": os.path.getsize(dst), "rows": 2000000, "parts": len(parts)}


def ranges(prefix, table):
    out = []
    for n, s in blob_list(prefix):
        m = re.search(table + r"_(\d+)-(\d+)\.parquet$", n)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), n, s))
    return sorted(out)


def s_minutes(chain, hmax):
    files = ranges(f"{chain}/extraction/blocks/", "blocks")
    if not files:
        raise RuntimeError("no history blocks in Blob")
    c = con()
    last = files[-1]
    lo, hi, t_lo, t_hi = c.execute(f"SELECT min(number), max(number), min(timestamp), max(timestamp) FROM read_parquet('{url(last[2])}')").fetchone()
    bps = max((hi - lo) / max(t_hi - t_lo, 1), 1e-3)
    want = int(hmax - bps * 35 * 86400)
    sel = [f for f in files if f[1] >= want]
    src = "[" + ",".join(f"'{url(f[2])}'" for f in sel) + "]"
    dst = f"{CHARTS}/block_minutes_{chain}.parquet"
    try:
        c.execute(f"""COPY (SELECT timestamp // 60 * 60 AS minute, min(number) AS first_block, max(number) AS last_block, count(*)::UINTEGER AS n
                            FROM read_parquet({src}) WHERE number >= {want} GROUP BY 1 ORDER BY 1)
                      TO '{dst}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
    except Exception as e:
        raise RuntimeError(scrub(e)[:300])
    os.replace(dst + ".tmp", dst)
    return {"bytes": os.path.getsize(dst), "files": len(sel), "blocks_per_s": round(bps, 3), "from_block": want}


def s_fills(chain, hmax):
    c = con()
    bm = f"{CHARTS}/block_minutes_{chain}.parquet"
    # measure back from the history end (the minute map can run past it once the follower extends it)
    t_end = c.execute(f"SELECT max(minute) FROM read_parquet('{bm}') WHERE {hmax} = 0 OR first_block <= {hmax}").fetchone()[0]

    def cut(days):
        return c.execute(f"SELECT coalesce(min(first_block), 0) FROM read_parquet('{bm}') WHERE minute >= {t_end - days * 86400}").fetchone()[0]
    parts = [(n, s) for n, s in blob_list(f"{chain}/map/dex_trades/") if n.endswith(".parquet")]
    rid = lambda n: int(re.search(r"range_id=(\d+)", n).group(1))
    if not parts:
        raise RuntimeError("no dex_trades in Blob")
    c1 = cut(1)
    last = [n for n, s in parts if rid(n) >= c1 // 10_000_000]
    if not last:   # the newest dex_trades range ends before the history's last day: measure on the newest range there is
        top = max(rid(n) for n, _ in parts)
        last = [n for n, s in parts if rid(n) == top]
        c1 = c.execute(f"SELECT max(block_number) FROM read_parquet([{','.join(repr(url(n)) for n in last)}])").fetchone()[0] - 86400
    src1 = "[" + ",".join(f"'{url(n)}'" for n in last) + "]"
    rows_day = c.execute(f"SELECT count(*) FROM read_parquet({src1}) WHERE block_number >= {c1}").fetchone()[0]
    tot_rows = c.execute(f"SELECT count(*) FROM read_parquet({src1})").fetchone()[0] or 1
    bytes_row = sum(s for n, s in parts if n in last) / tot_rows
    per_day = rows_day * bytes_row
    days = int(max(2, min(evm_cfg.FILLS_DAYS, FILLS_GB * 1e9 // max(per_day, 1))))
    cb = cut(days)
    sel = [n for n, s in parts if rid(n) >= cb // 10_000_000]
    src = "[" + ",".join(f"'{url(n)}'" for n in sel) + "]"
    dst = f"{EM}/map/{chain}/dex_trades"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.rmtree(dst + ".tmp", ignore_errors=True)
    try:
        c.execute(f"COPY (SELECT * FROM read_parquet({src}) WHERE block_number >= {cb}) TO '{dst}.tmp' "
                  "(FORMAT parquet, COMPRESSION zstd, PARTITION_BY (range_id))")
    except Exception as e:
        raise RuntimeError(scrub(e)[:300])
    shutil.rmtree(dst, ignore_errors=True)
    os.replace(dst + ".tmp", dst)
    pools = [n for n, _ in blob_list(f"{chain}/map/dex_pools/") if n.endswith(".parquet")]
    pb = sum(fetch(n, f"{EM}/map/{chain}/dex_pools/{os.path.basename(n)}") for n in pools)
    return {"bytes": du(dst) + pb, "days": days, "cut_block": cb, "rows_per_day": rows_day, "est_bytes_per_day": int(per_day), "pools_files": len(pools)}


def s_resync(chain, hmax):
    """Follower chunks after the history end must be local for tail_bars realtime: restore any the pruner moved."""
    got = 0
    for mf in glob.glob(f"{RT}/{chain}/manifests/*.json"):
        m = json.load(open(mf))
        if m.get("status") != "OK" or m["range"]["to"] <= hmax:
            continue
        for t in ("logs", "blocks", "transactions"):
            f = (m.get("files") or {}).get(t)
            if not f:
                continue
            p = f["path"] if os.path.isabs(f["path"]) else os.path.join(RT, chain, f["path"])
            if not os.path.exists(p):
                got += fetch(f"realtime/{chain}/{os.path.relpath(p, os.path.join(RT, chain))}", p)
    return {"bytes": got}


STAGES = {"econ": s_econ, "sheets": s_sheets, "held": s_held, "labels": s_labels, "research": s_research, "top": s_top, "minutes": s_minutes, "fills": s_fills, "resync": s_resync}

if __name__ == "__main__":
    chain, stage = sys.argv[1], sys.argv[2]
    hmax = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    t0 = time.time()
    try:
        r = STAGES[stage](chain, hmax)
    except Exception as e:
        print(json.dumps({"stage": stage, "ok": False, "error": scrub(e)[:400], "sec": round(time.time() - t0)}))
        sys.exit(1)
    print(json.dumps(dict(r, stage=stage, ok=True, sec=round(time.time() - t0)), default=str))
