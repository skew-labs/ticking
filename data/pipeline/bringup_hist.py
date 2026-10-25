"""bringup_hist.py: join each EVM history chain to its skew follower once <chain>/OWNER_DONE lands, then build the Option A
compact serving layer for it (owner 2026-10-19: compact serving on skew, deep history read from Blob on demand).

Per chain, resumable stages (state in join/<chain>.json; a failed stage is retried on the next 5-min pass, up to 3 times):
  join     history max block from Blob <chain>/extraction/manifests; any gap to the follower is extracted over RPC into
           out/<chain>; rule: real-time rows count only for block_number > history_max_block
  econ     econ files -> em-data/econ/<chain>/            (compact_build.py; DuckDB 4 threads / 8 GB / 15 GB temp)
  labels   graph-loop labels sorted by address -> em-data/compact/<chain>/labels.parquet
  minutes  block -> minute map (35 days) -> em-charts/data/block_minutes_<chain>.parquet
  fills    DEX fills window sized by measurement (<= 5 GB, 2-30 days) -> em-data/map/<chain>/dex_trades + dex_pools
  resync   follower chunks after the history end back from Blob if the pruner moved them
  charts   em-charts bars + deep store for the window (compact_run.py: bars, recent, tail, db; qa)
  top      per-address summary of the 2M most active addresses -> em-data/compact/<chain>/top_addresses.parquet
  enable   join/enabled.json: lookups (agent full_map + labels, galaxy wallet cards: dynamic, no restart once the hooks
           are loaded) and charts (em-charts serves the chain after em-map-charts + worker restart)
Guards: every heavy stage needs >= 75 GB free (the 60 GB floor plus margin) and stays inside a 15 GB per-chain budget.
Restarts are never done here unless EM_AUTO_RESTART=1: needed restarts go to join/RESTART_NEEDED.json and are applied by
apply_compact_restarts.sh (one script for the owner / controller).
"""
import fcntl
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

R = "/srv/skew/em-realtime"
J = R + "/join"
B = "https://arbitrum.blob.core.windows.net/em-mapping"
AZ = "/root/bin/azcopy"
VPY = "/srv/skew/em-galaxy/.venv/bin/python"
NICE = ["nice", "-n", "19", "ionice", "-c3"]
PREFLIGHT_GB, BUDGET_GB = 75, 15
GO = R + "/bin/em-rpc-extract"
OP = R + "/bin/emrpc-op"
GAP = {
    "ethereum": [GO, "--chain", "1", "--nitro-genesis", "0", "--weth", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "--procs", "1", "--parallel", "2",
                 "--chunk", "1000", "--rpc", "https://ethereum-rpc.publicnode.com#batch=1&workers=8"],
    "linea": [GO, "--chain", "59144", "--nitro-genesis", "0", "--weth", "none", "--procs", "1", "--parallel", "2", "--chunk", "1000",
              "--rpc", "https://linea-rpc.publicnode.com#batch=1&workers=8", "--rpc", "https://rpc.linea.build#batch=1&workers=4"],
    "polygon": [GO, "--chain", "137", "--nitro-genesis", "0", "--weth", "none", "--procs", "1", "--parallel", "2", "--chunk", "1000",
                "--rpc", "https://polygon-bor-rpc.publicnode.com#batch=1&workers=8"],
    "base": [OP, "--chain-id", "8453", "--chunk", "1000", "--rpc", "https://gateway.tenderly.co/public/base#workers=6", "--rpc", "https://mainnet.base.org#workers=3"],
    "optimism": [OP, "--chain-id", "10", "--chunk", "1000", "--rpc", "https://gateway.tenderly.co/public/optimism#workers=6"],
    # history = VM range backfill over RPC (evm-history/bin/rpc_range.sh, PLAN in Blob unichain/rpc-ranges/)
    "unichain": [OP, "--chain-id", "130", "--chunk", "1000", "--rpc", "https://unichain-rpc.publicnode.com#workers=20",
                 "--rpc", "https://mainnet.unichain.org#workers=6"],
    # X Layer: OP-stack era only (zkEVM era <= 42,810,020 not mapped); history = VM ranges (evm-history/bin/xlayer_range.sh)
    "xlayer": [OP, "--chain-id", "196", "--chunk", "1000", "--rpc", "https://rpc.xlayer.tech#workers=4", "--rpc", "https://xlayerrpc.okx.com#workers=4"],
}
STAGES = ["econ", "sheets", "held", "labels", "research", "minutes", "fills", "resync", "charts", "top"]
# first block of each chain's history (owner decisions): OP Mainnet only from Bedrock's first block in the op-reth
# datadir (earlier receipts are cut in the official snapshot); every other chain from genesis
HIST_START = {"optimism": 145_229_150, "xlayer": 42_810_021}
# source files per stage: when their Blob ETag changes (a VM or the Muse pass re-uploads), the stage and its
# dependents run again, also on chains that are already done
SOURCES = {"econ": ["econ/token_meta.parquet", "econ/balance_sheets.parquet"], "labels": ["graphloop/export/labels.parquet"],
           "research": ["graphloop/export/research_labels.parquet"], "sheets": ["econ/balance_sheets.parquet"], "held": ["econ/held_rows.parquet"],
           "fills": ["map/checks.json"], "top": ["map/checks.json"]}
DEPENDENTS = {"econ": ["charts"], "fills": ["charts"], "minutes": ["fills", "charts"]}
NEEDS = {"charts": ("econ", "minutes", "fills", "resync"), "fills": ("minutes",), "sheets": ("econ",), "held": ("econ",)}
ENV = dict(os.environ, HOME=os.environ.get("HOME") or "/root", EM_THREADS="4")


def log(*a):
    print(time.strftime("%FT%TZ", time.gmtime()), *a, flush=True)


def sas():
    return open("/root/.em-blob.sas").read().strip()


def get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def exists(path):
    try:
        urllib.request.urlopen(urllib.request.Request(f"{B}/{path}?{sas()}", method="HEAD"), timeout=30)
        return True
    except Exception:
        return False


def blob_names(prefix):
    names, marker = [], ""
    while True:
        u = f"{B}?restype=container&comp=list&prefix={prefix}&maxresults=5000&{sas()}" + (f"&marker={marker}" if marker else "")
        x = get(u).decode()
        names += re.findall(r"<Name>([^<]+)</Name>", x)
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", x)
        if not m:
            return names
        marker = m.group(1)


def etag(path):
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{B}/{path}?{sas()}", method="HEAD"), timeout=30) as r:
            return r.headers.get("ETag")
    except Exception:
        return None


def fingerprint(chain, stage):
    return [etag(f"{chain}/{p}") for p in SOURCES.get(stage, [])]


def refresh(chain, st):
    """Reset stages whose Blob sources changed since they ran (with their dependents). Returns the reset stages."""
    reset = []
    for stage in SOURCES:
        s = st["stages"].get(stage) or {}
        if "fp" not in s:
            continue
        fp = fingerprint(chain, stage)
        if fp != s["fp"]:
            for x in [stage] + DEPENDENTS.get(stage, []):
                st["stages"][x] = {"reset": f"source changed ({stage})", "at": int(time.time())}
                reset.append(x)
    if reset:
        st["done"] = None
        log(chain, "sources changed: re-running", sorted(set(reset)))
    return reset


# skew shares its disk with production Ticking: a heavy subprocess is paused (SIGSTOP on its process group) while the
# root device's write latency is above PAUSE_MS, and resumed once it stays below RESUME_MS for two checks
PAUSE_MS, RESUME_MS = 300, 200   # controller 2026-10-20: 300 ms once the big writers carry IOWriteBandwidthMax
MAX_PAUSE_S, RUN_S = 600, 60     # others can keep the latency high: after 10 min paused, run 60 s so the job still advances


def _root_dev():
    st = os.stat("/")
    return os.major(st.st_dev), os.minor(st.st_dev)


def _write_stats(dev):
    for l in open("/proc/diskstats"):
        f = l.split()
        if (int(f[0]), int(f[1])) == dev:
            return int(f[7]), int(f[10])      # writes completed, ms spent writing
    return 0, 0


def throttled(cmd, timeout=4 * 3600, **kw):
    """Run cmd like subprocess.run, pausing it while the disk write latency is high. Returns (rc, stdout or None)."""
    dev = _root_dev()
    p = subprocess.Popen(cmd, start_new_session=True, **kw)
    t0, paused, calm, last = time.time(), False, 0, _write_stats(dev)
    paused_at, run_until = 0.0, 0.0
    while p.poll() is None:
        time.sleep(2)
        cur = _write_stats(dev)
        dn, dms = cur[0] - last[0], cur[1] - last[1]
        last = cur
        lat = dms / dn if dn > 0 else 0.0
        if paused and time.time() - paused_at > MAX_PAUSE_S:
            os.killpg(p.pid, signal.SIGCONT)
            paused, run_until = False, time.time() + RUN_S
            log(f"paused {MAX_PAUSE_S} s with latency still {lat:.0f} ms: running {RUN_S} s to keep progress")
            continue
        if not paused and lat > PAUSE_MS and time.time() > run_until:
            os.killpg(p.pid, signal.SIGSTOP)
            paused, calm, paused_at = True, 0, time.time()
            log(f"disk write latency {lat:.0f} ms > {PAUSE_MS} ms: pausing {os.path.basename(cmd[min(len(cmd) - 1, len(NICE) + 1)])}")
        elif paused:
            calm = calm + 1 if lat < RESUME_MS else 0
            if calm >= 2:
                os.killpg(p.pid, signal.SIGCONT)
                paused = False
                log(f"disk write latency {lat:.0f} ms: resuming")
        if time.time() - t0 > timeout:
            os.killpg(p.pid, signal.SIGCONT)
            p.kill()
            raise RuntimeError(f"timeout after {timeout} s")
    out = p.stdout.read() if p.stdout else None
    return p.returncode, out


HEAVY_LOCK = "/run/em-heavy.lock"   # controller 2026-10-20: one heavy EM job (DuckDB of several GB) at a time on skew


class heavy(object):
    """Hold the skew-wide heavy-job lock (flock(2), the same lock flock(1) takes) for one stage."""
    def __init__(self, what):
        self.what = what

    def __enter__(self):
        # em-map-refresh (econ owner) has priority: never start a heavy stage while it is starting / running / queued on the lock
        waited = False
        while subprocess.run(["systemctl", "is-active", "-q", "em-map-refresh"]).returncode == 0 or \
                subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", "em-map-refresh"], capture_output=True, text=True).stdout.strip() == "activating":
            if not waited:
                log(f"em-map-refresh is running: {self.what} waits for it")
                waited = True
            time.sleep(60)
        self.f = open(HEAVY_LOCK, "a")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"waiting for {HEAVY_LOCK} ({self.what}): another heavy EM job holds it")
            fcntl.flock(self.f, fcntl.LOCK_EX)
            log(f"got {HEAVY_LOCK} ({self.what})")
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def free_gb():
    return shutil.disk_usage("/").free / 1e9


def du(p):
    if os.path.isfile(p):
        return os.path.getsize(p)
    return sum(os.path.getsize(f) for f in glob.glob(p + "/**", recursive=True) if os.path.isfile(f)) if os.path.isdir(p) else 0


def footprint(chain):
    parts = {"compact": f"/srv/skew/em-data/compact/{chain}", "econ": f"/srv/skew/em-data/econ/{chain}",
             "map_window": f"/srv/skew/em-data/map/{chain}", "charts": f"/srv/skew/em-charts/data/{chain}",
             "block_minutes": f"/srv/skew/em-charts/data/block_minutes_{chain}.parquet"}
    d = {k: du(v) for k, v in parts.items()}
    d["total"] = sum(d.values())
    return d


def save(chain, st):
    tmp = f"{J}/{chain}.json.tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, f"{J}/{chain}.json")


# ---------------------------------------------------------------- join (history <-> follower)
def history_max(chain):
    """End of the contiguous run of OK history manifests from the first chunk (manifest contents are read: a FAILED
    manifest, e.g. a snapshot chunk the VM could not fetch, ends the history there; the gap fill covers the rest).
    OK chunks after a hole mean the history itself is incomplete: that is an error for the owner VM, not a gap."""
    import concurrent.futures as cf
    rng = sorted((int(a), int(b), n) for n in blob_names(f"{chain}/extraction/manifests/")
                 for a, b in re.findall(r"(\d{10})-(\d{10})\.json$", n))
    if not rng:
        return None

    def status(n):
        for k in range(5):   # a failed GET is not a hole: retry, and refuse to judge if it stays unreadable
            try:
                return json.loads(get(f"{B}/{n}?{sas()}")).get("status")
            except Exception:
                time.sleep(2 * (k + 1))
        return None
    with cf.ThreadPoolExecutor(16) as ex:
        st = list(ex.map(lambda r: status(r[2]), rng))
    unread = [r[2] for r, s in zip(rng, st) if s is None]
    if unread:
        raise RuntimeError(f"{len(unread)} history manifests unreadable (transient; retried next pass), first {unread[0]}")
    start = HIST_START.get(chain, 0)
    if not start - start % 1000 <= rng[0][0] <= start:   # the first chunk may start at the 1000-aligned block before it
        raise RuntimeError(f"history starts at {rng[0][0]}, expected {start}")
    nxt = rng[0][0]
    for (a, b, _), s in zip(rng, st):
        if a > nxt or s != "OK":
            break
        nxt = max(nxt, b + 1)
    later = [(a, b) for (a, b, _), s in zip(rng, st) if s == "OK" and a > nxt]
    if later:
        raise RuntimeError(f"history has a hole at {nxt}: {len(later)} OK chunks after it (first {later[0]})")
    return nxt - 1


def follower_min(chain, after):
    """First block of the follower's OK chunks that reach past `after` (the history end)."""
    ok = []
    for f in glob.glob(f"{R}/out/{chain}/manifests/*.json"):
        try:
            m = json.load(open(f))
        except Exception:
            continue
        if m.get("status") == "OK" and m["range"]["to"] > after:
            ok.append(m["range"]["from"])
    return min(ok) if ok else None


def s_join(chain, st):
    hmax = history_max(chain)
    if hmax is None:
        raise RuntimeError("OWNER_DONE but no history manifests")
    fmin = follower_min(chain, hmax)
    st.update(history_max_block=hmax, follower_first_block=fmin)
    if fmin is not None and fmin > hmax + 1:
        log(chain, f"gap {hmax + 1}..{fmin - 1}: extracting over RPC")
        cmd = NICE + GAP[chain] + ["--from", str(hmax + 1), "--to", str(fmin - 1), "--out", f"{R}/out/{chain}"]
        with heavy(f"{chain} gap fill"):
            rc, _ = throttled(cmd, stdout=open(f"{R}/logs/gap-{chain}.log", "a"), stderr=subprocess.STDOUT, env=ENV)
        fmin = follower_min(chain, hmax)
        st.update(gap_fill_rc=rc, follower_first_block=fmin)
        if rc != 0 or fmin is None or fmin > hmax + 1:
            raise RuntimeError(f"gap fill rc={rc}, first real-time block {fmin} > {hmax + 1}")
    st.update(realtime_from=hmax + 1, rule="use real-time rows only for block_number > history_max_block",
              blob_history=f"{chain}/extraction", blob_realtime=f"realtime/{chain}", local_realtime=f"{R}/out/{chain}")
    return {"history_max_block": hmax}


# ---------------------------------------------------------------- compact stages
def run_json(cmd, cwd=None, timeout=4 * 3600):
    errf = f"{R}/logs/compact_build.err"
    rc, out = throttled(NICE + cmd, timeout=timeout, cwd=cwd, env=ENV, stdout=subprocess.PIPE, stderr=open(errf, "w"), text=True)
    lines = [l for l in (out or "").splitlines() if l.startswith("{")]
    r = json.loads(lines[-1]) if lines else {}
    if rc != 0 or not r.get("ok", rc == 0):
        raise RuntimeError((r.get("error") or open(errf).read()[-400:] or f"rc={rc}").replace(sas(), "SAS"))
    return r


def s_compact(chain, st, stage):
    return run_json([VPY, f"{R}/compact_build.py", chain, stage, str(st["history_max_block"])])


def s_charts(chain, st):
    C = "/srv/skew/em-charts"
    rids = sorted(int(x.split("range_id=")[1]) for x in glob.glob(f"/srv/skew/em-data/map/{chain}/dex_trades/range_id=*"))
    if not rids:
        raise RuntimeError("no fills window")
    out = {}
    for step, args in (("bars", [str(r) for r in rids]), ("recent", []), ("tail", []), ("db", [])):
        t0 = time.time()
        rc, _ = throttled(NICE + [VPY, "compact_run.py", chain, step] + args, cwd=C, env=ENV,
                          stdout=open(f"{C}/logs/compact_{chain}_{step}.log", "w"), stderr=subprocess.STDOUT)
        if rc != 0:
            raise RuntimeError(f"charts {step} rc={rc} (em-charts/logs/compact_{chain}_{step}.log)")
        out[step] = round(time.time() - t0)
    db = f"{C}/data/{chain}/candles.duckdb"
    qa = subprocess.run(NICE + [VPY, "qa.py", db, "--top", "10"], cwd=C, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    open(f"{C}/logs/qa_{chain}.json", "w").write(qa.stdout)
    out.update(db_bytes=os.path.getsize(db), qa_rc=qa.returncode)
    return out


def buckets(chain):
    n = len([x for x in blob_names(f"{chain}/map/addresses/") if re.search(r"part-\d+\.parquet$", x)])
    t = len([x for x in blob_names(f"{chain}/map/address_tokens/") if re.search(r"part-\d+\.parquet$", x)])
    return n if n and n == t and n & (n - 1) == 0 and n <= 256 else 0


def enable(chain, st):
    p = f"{J}/enabled.json"
    try:
        en = json.load(open(p))
    except (OSError, ValueError):
        en = {}
    ok = lambda s: (st.get("stages", {}).get(s) or {}).get("ok")
    e = {"buckets": buckets(chain), "history_max_block": st["history_max_block"],
         "cut_block": ((st["stages"].get("fills") or {}).get("result") or {}).get("cut_block"),
         "lookups": bool(ok("labels")), "charts": bool(ok("charts")), "at": int(time.time())}
    e["lookups"] = e["lookups"] or e["buckets"] > 0
    newly_charts = e["charts"] and not (en.get(chain) or {}).get("charts")
    en[chain] = e
    json.dump(en, open(p + ".tmp", "w"), indent=1)
    os.replace(p + ".tmp", p)
    need = []
    if newly_charts:
        need += ["em-map-charts", "em-map-charts-worker"]
    if e["lookups"] and not os.path.exists(f"{J}/galaxy_hooks_loaded"):
        need += ["em-map-galaxy"]
    if need:
        rp = f"{J}/RESTART_NEEDED.json"
        try:
            rq = json.load(open(rp))
        except (OSError, ValueError):
            rq = {"units": [], "reasons": []}
        rq["units"] = sorted(set(rq["units"]) | set(need))
        rq["reasons"].append(f"{chain}: " + ("charts built; " if newly_charts else "") + ("lookups enabled (hooks load once)" if "em-map-galaxy" in need else ""))
        json.dump(rq, open(rp, "w"), indent=1)
        if os.environ.get("EM_AUTO_RESTART") == "1":
            subprocess.run(["bash", f"{R}/apply_compact_restarts.sh"], env=ENV)
    return e


def bring_up(chain, st):
    st.setdefault("stages", {})
    st.pop("error", None)       # a failure from an earlier pass is retried, not kept
    refresh(chain, st)
    if not (st["stages"].get("join") or {}).get("ok"):
        st["stages"]["join"] = {"ok": True, "result": s_join(chain, st), "at": int(time.time())}
        save(chain, st)
    for stage in STAGES:
        s = st["stages"].get(stage) or {}
        if s.get("ok") or s.get("attempts", 0) >= 3:
            continue
        if any(not (st["stages"].get(d) or {}).get("ok") for d in NEEDS.get(stage, ())):
            st["stages"][stage] = dict(s, skipped="needs " + ",".join(NEEDS[stage]))
            continue
        fp = footprint(chain)["total"] / 1e9
        if free_gb() < PREFLIGHT_GB:
            st["stages"][stage] = dict(s, deferred=f"free {free_gb():.0f} GB < {PREFLIGHT_GB} GB pre-flight", at=int(time.time()))
            save(chain, st)
            return False                      # retry on the next pass
        if fp > BUDGET_GB:
            st["stages"][stage] = dict(s, skipped=f"chain footprint {fp:.1f} GB > {BUDGET_GB} GB budget", attempts=3)
            continue
        log(chain, "stage", stage)
        t0 = time.time()
        try:
            fp = fingerprint(chain, stage)
            with heavy(f"{chain} {stage}"):
                r = s_charts(chain, st) if stage == "charts" else s_compact(chain, st, stage)
            st["stages"][stage] = {"ok": True, "result": r, "sec": round(time.time() - t0), "at": int(time.time()), "fp": fp}
        except Exception as e:
            st["stages"][stage] = {"ok": False, "error": str(e)[:400], "attempts": s.get("attempts", 0) + 1, "at": int(time.time()),
                                   "fp": fingerprint(chain, stage)}   # a changed source retries a failed stage too
            log(chain, "stage", stage, "FAILED:", str(e)[:300])
        save(chain, st)
    st["enabled"] = enable(chain, st)
    st["footprint"] = footprint(chain)
    pending = [x for x in STAGES if not (st["stages"].get(x) or {}).get("ok") and (st["stages"].get(x) or {}).get("attempts", 0) < 3]
    st["done"] = int(time.time()) if not pending else None
    save(chain, st)
    log(chain, "enabled:", json.dumps(st["enabled"]), "footprint GB:", round(st["footprint"]["total"] / 1e9, 2), "pending:", pending)
    return not pending


def main():
    os.makedirs(J, exist_ok=True)
    while True:
        pending = 0
        for chain in GAP:
            jf = f"{J}/{chain}.json"
            try:
                st = json.load(open(jf))
            except (OSError, ValueError):
                st = {"chain": chain}
            if not exists(f"{chain}/OWNER_DONE"):
                if not st.get("done"):
                    pending += 1
                continue
            if st.get("done"):
                new = [x for x in STAGES if x not in st.get("stages", {})]   # a stage added after the chain was served
                if not refresh(chain, st) and not new:   # served: only re-run when a source in Blob changed
                    continue
                st["done"] = None
                save(chain, st)
            pending += 1
            st.setdefault("owner_done_seen", int(time.time()))
            try:
                if bring_up(chain, st):
                    pending -= 1
            except Exception as e:
                log(chain, "FAILED:", str(e)[:300])
                st["error"] = str(e)[:400]
                save(chain, st)
        if pending == 0:
            log("all history chains joined and served; watching their Blob sources for re-uploads")
        time.sleep(300)


if __name__ == "__main__":
    main()
