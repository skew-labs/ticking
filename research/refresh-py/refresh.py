#!/usr/bin/env python3
"""Economic Machine incremental refresh: econ -> map delta -> sheets/fixes -> galaxy.db -> cards, per chain.

usage: refresh.py [--chains robinhood,arbitrum] [--cards-every-h 20] [--min-blocks 2000] [--rpc-budget 2000]
                  [--no-galaxy] [--no-cards] [--no-traders]

Resumes from state/<chain>.json (econ_block = last block whose econ actions are written). Every run, per chain:
  actions     econ_v3 stage/dims/meta/gmx2/actions for (econ_block, tip]      (tip = contiguous OK realtime chunks)
  tokens      token metadata for new action parts (parts whose tokens step failed are retried by later runs)
  map_delta   map_delta over (snapshot, econ_block]  (cumulative; small)
  positions / prices / sheets / focus   econ_v3 over the full history (as of econ_block)
  fix_v3      sheet_rows/sheet_summary/lp_positions_valued .v3 + as_of.json
  galaxy      galaxy_v3 -> live galaxy.db (idempotent; shared lock + retried short transactions) + index.json/manifest
  focus_copy  focus CSVs -> /srv/skew/em-galaxy/focus/<chain>/
  cards       cards_v3 -> index/<chain>/cards.duckdb (at most every --cards-every-h hours; copies the file)
  traders     /srv/skew/em-traders/traders.py -> em-data/traders/<chain>/ + em-graphloop/export/<chain>/labels_traders.parquet
A failed step never stops the run: steps that need its output are skipped, everything else still runs, and the next
run retries (state only advances on success). Per-step status: state/status.json (live, rewritten after every step)
and one line per run in state/status_history.jsonl. RPC (rpc.py endpoint pool) is capped at --rpc-budget HTTP requests
per run, retries included; per-host counters are in status.json. Exit 0 = every step ok/degraded/skipped, 3 = some step
failed (the unit treats 3 as success so the timer keeps going; read status.json), 1 = the orchestrator itself crashed.
Labels for addresses created after the snapshot are not refreshed here: the graph-loop image is immutable and rebuilding
it needs ~30-160 GB RAM (see /srv/skew/em-refresh/README.md); em-labeldelta labels them in a separate delta image.
"""
import argparse
import collections
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CHAINS, DATA, ROOT, STATE, free_gb, load_state, log, save_state, tip  # noqa: E402

PY = f"{ROOT}/.venv/bin/python"
GALAXY = "/srv/skew/em-galaxy"
TRADERS = "/srv/skew/em-traders"  # top traders / smart money (own venv: duckdb 1.4.5 + numpy + pyarrow, C ledger kernel)
STATUS = f"{STATE}/status.json"
HISTORY = f"{STATE}/status_history.jsonl"
RPC_RUN = f"{STATE}/rpc_run.json"
GOOD = ("ok", "degraded")


def utc(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t or time.time()))


def redact(s):
    """Status/error text keeps endpoint host names only (URLs may carry keys)."""
    return re.sub(r"(https?://[^/\s:]+)[^\s'\"]*", r"\1", str(s))


class RefreshInterrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum


class Skip(Exception):
    """Raised by a step that has nothing to do (status 'skipped' with this reason)."""


def sh(args, env=None):
    """Run a script, streaming its stderr into the log; on failure the error carries the script's last output line."""
    log("run: " + " ".join(args[1:]))
    tail = collections.deque(maxlen=12)
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, bufsize=1)
    for line in p.stdout:
        sys.stderr.write(line)
        if line.strip():
            tail.append(line.rstrip())
    rc = p.wait()
    sys.stderr.flush()
    if rc:
        last = next((x for x in reversed(tail) if not x.startswith(" ") and "Traceback" not in x), tail[-1] if tail else "")
        raise RuntimeError(f"{os.path.basename(args[1])} exited {rc}: {last[-300:]}")


class Run:
    """Per-step status of one refresh run, written to state/status.json after every step."""

    def __init__(self, chains, rpc_budget):
        self.t0 = time.time()
        self.doc = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "started_utc": utc(), "finished_utc": None,
                    "pid": os.getpid(), "result": "running", "free_gb_start": round(free_gb()),
                    "global": {"steps": {}}, "chains": {c: {"steps": {}} for c in chains},
                    "rpc": {"budget": rpc_budget}}
        with open(RPC_RUN + ".tmp", "w") as f:
            json.dump({"requests": 0, "budget": rpc_budget, "by_host": {}}, f)
        os.replace(RPC_RUN + ".tmp", RPC_RUN)
        os.environ["RPC_RUN_FILE"] = RPC_RUN
        os.environ["RPC_RUN_BUDGET"] = str(rpc_budget)
        self.save()

    def scope(self, name):
        return self.doc["global"] if name == "global" else self.doc["chains"][name]

    def rpc_requests(self):
        try:
            return json.load(open(RPC_RUN)).get("requests", 0)
        except (OSError, ValueError):
            return 0

    def save(self):
        try:
            r = json.load(open(RPC_RUN))
            self.doc["rpc"].update({k: r.get(k) for k in ("requests", "by_host", "budget_exhausted") if k in r})
        except (OSError, ValueError):
            pass
        with open(STATUS + ".tmp", "w") as f:
            json.dump(self.doc, f, indent=1)
        os.replace(STATUS + ".tmp", STATUS)

    def status(self, scope, name):
        return self.scope(scope)["steps"].get(name, {}).get("status")

    def step(self, scope, name, fn, needs=()):
        """Run fn() as step `name`; returns True when it ended ok/degraded. Never raises."""
        steps = self.scope(scope)["steps"]
        miss = [n for n in needs if self.status(scope, n) not in GOOD]
        if miss:
            steps[name] = {"status": "skipped", "reason": "needs " + ", ".join(f"{n} ({self.status(scope, n) or 'not run'})" for n in miss)}
            log(f"[{scope}] {name}: skipped ({steps[name]['reason']})")
            self.save()
            return False
        wf = f"{STATE}/.warn.{scope}.{name}"
        if os.path.exists(wf):
            os.remove(wf)
        os.environ["EM_WARN_FILE"] = wf
        t, r0 = time.time(), self.rpc_requests()
        rec = steps[name] = {"status": "running", "started_utc": utc(t)}
        self.save()
        log(f"[{scope}] {name}: start")
        try:
            res = fn()
            warns = [x.strip() for x in open(wf)] if os.path.exists(wf) else []
            rec["status"] = "degraded" if warns else "ok"
            if warns:
                rec["warnings"] = [redact(x) for x in warns[:20]]
            if isinstance(res, dict):
                rec["result"] = res
        except Skip as e:
            rec.update(status="skipped", reason=str(e))
        except (Exception, SystemExit) as e:  # noqa: BLE001  (need_space / missing sources raise SystemExit)
            rec.update(status="failed", error=redact(f"{type(e).__name__}: {e}")[:500])
            log(f"[{scope}] {name}: FAILED\n{redact(traceback.format_exc())}")
        finally:
            os.environ.pop("EM_WARN_FILE", None)
            if os.path.exists(wf):
                os.remove(wf)
        rec["seconds"] = round(time.time() - t, 1)
        if self.rpc_requests() > r0:
            rec["rpc_requests"] = self.rpc_requests() - r0
        log(f"[{scope}] {name}: {rec['status']} in {rec['seconds']}s" + (f" ({rec.get('reason') or rec.get('error')})" if rec["status"] in ("skipped", "failed") else ""))
        self.save()
        return rec["status"] in GOOD

    def interrupt(self, signum):
        for scope in [self.doc["global"], *self.doc["chains"].values()]:
            for step in scope["steps"].values():
                if step.get("status") == "running":
                    step.update(status="interrupted", reason="Process received " + signal.Signals(signum).name)
        self.doc["global"]["steps"]["interrupted"] = {"status": "failed", "signal": signum}
        self.finish()

    def finish(self):
        all_steps = [s for sc in [self.doc["global"]] + list(self.doc["chains"].values()) for s in sc["steps"].values()]
        st = {s["status"] for s in all_steps}
        self.doc["result"] = "failed" if st & {"failed", "interrupted", "running"} else "degraded" if "degraded" in st else "ok"
        self.doc["finished_utc"] = utc()
        self.doc["seconds"] = round(time.time() - self.t0)
        self.doc["free_gb_end"] = round(free_gb())
        self.save()
        with open(HISTORY, "a") as f:
            f.write(json.dumps({"run_id": self.doc["run_id"], "result": self.doc["result"], "seconds": self.doc["seconds"],
                                "rpc_requests": self.doc["rpc"].get("requests"),
                                "chains": {c: {"as_of_block": v.get("as_of_block"), **{k: s["status"] for k, s in v["steps"].items()}}
                                           for c, v in self.doc["chains"].items()},
                                "global": {k: s["status"] for k, s in self.doc["global"]["steps"].items()}}) + "\n")
        return self.doc["result"]


def delta_to(chain):
    p = f"{DATA}/map/{chain}/_delta/meta.json"
    return json.load(open(p)).get("to") if os.path.exists(p) else None


def run_chain(R, chain, a):
    import econ_v3
    import map_delta
    st = load_state(chain)
    snap = CHAINS[chain]["snap_end"]
    info = R.scope(chain)
    info.update({"econ_block_before": st.get("econ_block", snap), "as_of_block_before": st.get("as_of_block")})
    cur = {}

    def get_tip():
        cur["tip"] = tip(chain)
        info["tip"] = cur["tip"]
        return {"tip": cur["tip"]}
    if not R.step(chain, "tip", get_tip):
        cur["tip"] = st.get("econ_block", snap)
    log(f"=== {chain}: econ_block={st.get('econ_block', snap)} tip={cur['tip']} free={free_gb():.0f}GB")

    def actions():
        lo, hi = st.get("econ_block", snap) + 1, cur["tip"]
        if hi - lo + 1 < a.min_blocks:
            raise Skip(f"only {max(0, hi - lo + 1)} new blocks (< {a.min_blocks})")
        econ_v3.run(chain, lo, hi, ["stage", "dims", "meta", "gmx2", "actions"])
        # pending_lo = first block whose day-level DEX prices are not yet recomputed (survives failed runs)
        st.update({"econ_block": hi, "econ_actions_at": int(time.time()), "last_inc_lo": lo,
                   "pending_lo": min(st.get("pending_lo", lo), lo), "tokens_pending": sorted(set(st.get("tokens_pending", [])) | {lo})})
        save_state(chain, st)
        return {"from": lo, "to": hi}
    R.step(chain, "actions", actions)

    def tokens():
        los = st.get("tokens_pending") or []
        if not los:
            raise Skip("no new action parts")
        econ_v3.run(chain, min(los), st["econ_block"], ["tokens"], token_los=los)
        st["tokens_pending"] = []
        save_state(chain, st)
        return {"parts": los}
    R.step(chain, "tokens", tokens)

    eb = st.get("econ_block", snap)
    info["econ_block"] = eb

    def mdelta():
        if delta_to(chain) == eb:
            raise Skip(f"delta already at block {eb}")
        map_delta.build(chain, eb)
        return {"to": eb}
    R.step(chain, "map_delta", mdelta)

    # econ outputs over the full history; only when they are behind the actions (state advances on fix_v3 success)
    behind = st.get("as_of_block") != eb
    plo = st.get("pending_lo", st.get("last_inc_lo", snap + 1))

    def econ(stepname):
        def f():
            if not behind:
                raise Skip(f"econ outputs already as of block {eb}")
            if stepname == "sheets" and delta_to(chain) != eb:
                raise RuntimeError(f"map delta ends at {delta_to(chain)}, expected {eb}; sheets not published")
            econ_v3.run(chain, plo, eb, [stepname])
        return f
    R.step(chain, "positions", econ("positions"))
    if R.step(chain, "prices", econ("prices")):
        st.pop("pending_lo", None)
        save_state(chain, st)
    R.step(chain, "sheets", econ("sheets"), needs=("positions", "prices"))
    R.step(chain, "focus", econ("focus"), needs=("sheets",))

    def fix():
        sh([PY, f"{ROOT}/py/fix_v3.py", chain, "--as-of-block", str(eb)])
        st.update({"as_of_block": eb, "as_of_at": int(time.time())})
        save_state(chain, st)
        return {"as_of_block": eb}
    R.step(chain, "fix_v3", fix, needs=("sheets",))
    info["as_of_block"] = st.get("as_of_block")

    def galaxy():
        if a.no_galaxy:
            raise Skip("--no-galaxy")
        want = [st.get("as_of_block"), delta_to(chain)]
        if want != [eb, eb]:
            raise RuntimeError(f"mismatched sheet/delta blocks {want}, expected {eb}; galaxy not published")
        if st.get("galaxy_applied") == want:
            raise Skip(f"galaxy.db already as of block {want[0]} with delta to {want[1]}")
        sh([PY, f"{ROOT}/py/galaxy_v3.py", f"{GALAXY}/galaxy.db", chain])
        st.update({"galaxy_applied": want, "galaxy_at": int(time.time())})
        save_state(chain, st)
        return {"as_of_block": want[0], "delta_to": want[1]}
    R.step(chain, "galaxy", galaxy)

    def focus_copy():
        fdir = f"{GALAXY}/focus/{chain}"
        if not os.path.isdir(fdir):
            raise Skip(f"{fdir} missing")
        n = 0
        for f in os.listdir(f"{DATA}/econ/{chain}/v3"):
            if f.startswith("focus_") and f.endswith(".csv") and os.path.exists(f"{fdir}/{f}"):
                shutil.copyfile(f"{DATA}/econ/{chain}/v3/{f}", f"{fdir}/{f}.tmp")
                os.replace(f"{fdir}/{f}.tmp", f"{fdir}/{f}")
                n += 1
        return {"files": n}
    R.step(chain, "focus_copy", focus_copy, needs=("focus",))

    def cards():
        if a.no_cards:
            raise Skip("--no-cards")
        age = (time.time() - st.get("cards_at", 0)) / 3600
        if age <= a.cards_every_h:
            raise Skip(f"refreshed {age:.1f} h ago (every {a.cards_every_h:g} h)")
        if R.status(chain, "map_delta") == "failed":
            raise RuntimeError("map delta failed in this run")
        import cards_v3
        cards_v3.refresh(chain)
        st.update({"cards_at": int(time.time()), "cards_block": delta_to(chain)})
        save_state(chain, st)
        return {"block": st["cards_block"]}
    R.step(chain, "cards", cards)
    log(f"=== {chain}: econ_block {eb}, as of block {st.get('as_of_block')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", default="robinhood,arbitrum")
    ap.add_argument("--cards-every-h", type=float, default=20)
    ap.add_argument("--min-blocks", type=int, default=2000)
    ap.add_argument("--rpc-budget", type=int, default=2000, help="HTTP requests to public RPC per run, retries included")
    ap.add_argument("--no-galaxy", action="store_true")
    ap.add_argument("--no-cards", action="store_true")
    ap.add_argument("--no-traders", action="store_true")
    a = ap.parse_args()
    os.makedirs(STATE, exist_ok=True)
    lock = open(f"{STATE}/refresh.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another refresh is running; exit")
        return 0
    chains = a.chains.split(",")
    R = Run(chains, a.rpc_budget)
    def interrupted(signum, frame):
        # BaseException escapes step's recoverable-error handler. Never continue
        # later mutation steps after the guard has asked the process to stop.
        R.interrupt(signum)
        raise RefreshInterrupted(signum)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)

    # known entities (treasury / foundation / DAO / team multisigs from public sources): re-applied when the list changes
    def known():
        import apply_known
        f = f"{STATE}/known_entities_applied.json"
        prev = json.load(open(f)).get("sha256") if os.path.exists(f) else None
        if not os.path.exists(apply_known.CSV) or apply_known.digest() == prev:
            raise Skip("known_entities.csv unchanged")
        return apply_known.main()
    R.step("global", "known_entities", known)
    for chain in chains:
        r0 = R.rpc_requests()
        try:
            run_chain(R, chain, a)
        except (Exception, SystemExit) as e:  # noqa: BLE001  (steps never raise; this is the orchestration itself)
            R.scope(chain)["steps"]["orchestrator"] = {"status": "failed", "error": redact(f"{type(e).__name__}: {e}")[:500]}
            log(f"{chain}: FAILED\n{redact(traceback.format_exc())}")
        R.scope(chain)["rpc_requests"] = R.rpc_requests() - r0

        # trader PnL / leaderboards / labels: runs even when the chain had nothing new, so the durable copy of the
        # em-charts hot fills (pruned after 3 days) keeps up
        def traders():
            if a.no_traders or not os.path.exists(f"{TRADERS}/traders.py"):
                raise Skip("--no-traders" if a.no_traders else "traders.py missing")
            sh([f"{TRADERS}/.venv/bin/python", f"{TRADERS}/traders.py", chain], env=dict(os.environ, TR_MEM="4GB"))
            st = load_state(chain)
            st["traders_at"] = int(time.time())
            save_state(chain, st)
        R.step(chain, "traders", traders)
        R.save()
    result = R.finish()
    summary = {c: load_state(c) for c in chains}
    with open(f"{STATE}/summary.json.tmp", "w") as f:
        json.dump(summary, f, indent=1)
    os.replace(f"{STATE}/summary.json.tmp", f"{STATE}/summary.json")
    log(f"run {R.doc['run_id']}: {result} in {R.doc['seconds']}s, {R.doc['rpc'].get('requests')} RPC requests")
    return 3 if result == "failed" else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RefreshInterrupted as e:
        sys.exit(128 + e.signum)
