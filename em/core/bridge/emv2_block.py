# ------------------------------------------------------------------------------------------------
# EMV2 shadow (emv2/bridge/apply_bridge_emv2.py; design: design/EM-ENGINE-V2.md §8 API, §10 dual run). Per route and chain the
# bridge answers from the current services (old), answers the same and compares emv2-serve in the background (shadow), or
# answers from emv2-serve and falls back to the current services on any V2 failure (new).
# No emv2-bridge.json next to this file (or $EM_EMV2_CONFIG) = every route old: each request takes exactly the path it took
# before this block. The file is reread every 30 s, so switching a route needs no restart:
#   {"serve": "127.0.0.1:8740", "default": "old", "routes": {"holders-at": {"arc": "shadow", "*": "old"}, "trace": "shadow"},
#    "sample": 1.0, "min_gap_ms": 0, "timeout_ms": 2000, "queue": 128, "log_max_mb": 64, "log_keep": 5, "align_old": true}
# Shadow never touches the answer: the old body is sent first, then a bounded queue (dropped when full) asks emv2-serve the same
# path and query and appends one JSONL line per comparison to <logs>/emv2-shadow.jsonl (rotated by size). Integers and strings
# must match exactly, other numbers within a relative 1e-6, lists in order; time-of-answer fields are not compared.
# Time: V2 phase 1 is built to the map's last block, the current services add daily deltas. Every V2 answer carries `as_of`
# (block and time, or header X-EMV2-As-Of: <block>:<time>). Routes with a time parameter (holders-at block, trace-graph to)
# are asked of the current upstream again at that point (align_old); elsewhere old list items after as_of are left out. A
# difference left over while the old answer shows activity after as_of is `time_skew`, counted apart and never critical.

import queue as _emv2_queue
import random as _emv2_random

EMV2_PATH = os.environ.get("EM_EMV2_CONFIG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "emv2-bridge.json")
EMV2 = {"loaded": 0.0, "cfg": None, "q": None, "lock": threading.Lock(), "last": {}, "stats": defaultdict(int), "stats_at": time.time()}
EMV2_MODES = ("old", "shadow", "new")
EMV2_ROUTES = {"/api/public/em/holders-at": "holders-at", "/api/public/em/wallet-trades": "wallet-trades",
               "/api/public/em/acquisition": "acquisition", "/api/public/em/address-chains": "address-chains",
               "/api/public/em/trace-graph": "trace-graph", "/api/public/em/tools/wallet": "tools/wallet",
               "/api/public/em/tools/holders": "tools/holders", "/api/public/em/tools/trace": "tools/trace"}
EMV2_NEIGHBORS = re.compile(r"/api/public/em/g/neighbors/([a-z][a-z0-9]{1,15})/\d{1,9}")
EMV2_CHAIN = re.compile(r"[a-z][a-z0-9]{1,15}")
EMV2_IGNORE = {"generated_at", "generatedAt", "as_of_now", "now", "served_at", "servedAt", "cached", "cache", "cache_age_s", "age_s",
               "took_ms", "elapsed_ms", "latency_ms", "request_id", "engine", "build", "build_id", "tier", "logo", "logos", "logo_url",
               "logoUrl", "icon", "icon_url", "credit", "logoCredit", "locked", "elapsed_ms", "as_of", "inputHash", "dataVersion"}
# Per route: fields whose source differs by design in V2 (left out), and phase-1 gaps where V2 answers null (counted apart).
EMV2_SKIP = {"holders-at": re.compile(r"(^|\.)snaps\[\d+\]\.source$"), "address-chains": re.compile(r"(^|\.)kindBasis$"),
             "trace": re.compile(r"(^|\.)asOf\[\d+\]\.source$"), "trace-graph": re.compile(r"^notes(\[\d+\])?$"),
             "path": re.compile(r"^(visited|max_depth|budget_exhausted)$|^edges\[\d+\]\.(edge|type)$|^nodes\[\d+\]\.(confidence|type|entity|hub)$")}
EMV2_GAPS = {"holders-at": re.compile(r"(^|\.)class$"), "address-chains": re.compile(r"(^|\.)entity$")}
EMV2_CRITICAL = ("status", "missing", "type", "length", "int", "raw", "value")
EMV2_BLOCK_KEYS = ("block", "blockNumber", "block_number", "lastBlock", "last_block")
EMV2_TIME_KEYS = ("time", "ts", "timestamp", "lastTime", "last_time", "lastSeen", "last_seen")
EMV2_ALIGN = {"/api/public/em/holders-at": ("block", ("block", "t")), "/api/public/em/trace-graph": ("to", ("to",))}


def emv2_cfg():
    if time.time() - EMV2["loaded"] > 30:
        cfg = None
        try:
            with open(EMV2_PATH) as f:
                raw = json.load(f)
            routes = raw.get("routes") or {}
            modes = [m for r in routes.values() for m in (r.values() if isinstance(r, dict) else [r])] + [raw.get("default", "old")]
            if all(m in EMV2_MODES for m in modes):
                cfg = {"addr": hostport(raw.get("serve", "127.0.0.1:8740")), "default": raw.get("default", "old"), "routes": routes,
                       "sample": min(1.0, max(0.0, float(raw.get("sample", 1.0)))), "gap": max(0.0, float(raw.get("min_gap_ms", 0)) / 1000),
                       "timeout": min(5.0, max(0.1, float(raw.get("timeout_ms", 2000)) / 1000)),
                       "align_old": bool(raw.get("align_old", True)), "queue": min(1024, max(1, int(raw.get("queue", 128)))),
                       "log_max": min(1024, max(1, int(raw.get("log_max_mb", 64)))) << 20, "log_keep": min(20, max(1, int(raw.get("log_keep", 5))))}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            cfg = None
        EMV2.update(loaded=time.time(), cfg=cfg)
    return EMV2["cfg"]


def emv2_route(path, qs):
    """(route, chain) for a request this block may handle, else None. chain is '*' when the route spans chains."""
    m = EMV2_NEIGHBORS.fullmatch(path)
    if m:
        return "neighbors", m.group(1)
    if path == "/api/public/em/trace":
        return ("path" if qs.get("to") else "trace"), (qs.get("chain") or "?").lower()
    route = EMV2_ROUTES.get(path)
    if not route:
        return None
    if route == "address-chains":
        return route, "*"
    chain = (qs.get("chain") or "").lower()
    if not chain and ":" in (qs.get("market") or ""):
        chain = qs["market"].split(":", 1)[0].lower()
    return route, (chain if EMV2_CHAIN.fullmatch(chain or "") else "?")


def emv2_mode(cfg, route, chain):
    r = cfg["routes"].get(route)
    if isinstance(r, dict):
        return r.get(chain) or r.get("*") or cfg["default"]
    return r or cfg["default"]


def emv2_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def emv2_diff(a, b, path, out, seen):
    """Field-by-field comparison of the old answer `a` with the V2 answer `b`; appends (kind, path, old, new) to out."""
    if seen[0] > 400_000:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k in EMV2_IGNORE:
                continue
            p = "%s.%s" % (path, k) if path else str(k)
            if k not in b:
                out.append(("missing", p, a[k], None))
            elif k not in a:
                out.append(("extra", p, None, b[k]))
            else:
                emv2_diff(a[k], b[k], p, out, seen)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(("length", path, len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            emv2_diff(x, y, "%s[%d]" % (path, i), out, seen)
        return
    seen[0] += 1
    if emv2_num(a) and emv2_num(b):
        if a == b:
            return
        if isinstance(a, float) or isinstance(b, float):
            if abs(a - b) <= 1e-6 * max(abs(a), abs(b)):
                return
            out.append(("value", path, a, b))
        else:
            out.append(("int", path, a, b))
        return
    if type(a) is not type(b) and not (a is None or b is None) or (a is None) != (b is None):
        out.append(("type", path, a, b))
    elif a != b:
        out.append(("raw", path, a, b))


def emv2_short(v):
    s = json.dumps(v, default=str)
    return s if len(s) <= 120 else s[:117] + "..."


def emv2_write(rec):
    cfg = EMV2["cfg"] or {"log_max": 64 << 20, "log_keep": 5}
    where = str(getattr(LOG, "name", ""))   # the bridge's --logs directory once main() opened bridge.log; '<stderr>' before
    fn = os.path.join(os.path.dirname(os.path.abspath(where)) if where and not where.startswith("<") else os.path.dirname(os.path.abspath(__file__)),
                      "emv2-shadow.jsonl")
    line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
    with EMV2["lock"]:
        try:
            if os.path.exists(fn) and os.path.getsize(fn) + len(line) > cfg["log_max"]:
                for i in range(cfg["log_keep"] - 1, 0, -1):
                    if os.path.exists("%s.%d" % (fn, i)):
                        os.replace("%s.%d" % (fn, i), "%s.%d" % (fn, i + 1))
                os.replace(fn, fn + ".1")
            with open(fn, "a") as f:
                f.write(line)
        except OSError as e:
            EMV2["stats"]["log_error"] += 1
            if EMV2["stats"]["log_error"] == 1:
                log("emv2 shadow log %r" % e)


def emv2_ask(cfg, target):
    t0 = time.perf_counter()
    c = http.client.HTTPConnection(*cfg["addr"], timeout=cfg["timeout"])
    try:
        c.request("GET", target, headers={"Accept": "application/json", "X-EMV2-Shadow": "1"})
        r = c.getresponse()
        body = r.read(20_000_000)
        return r.status, r.getheader("X-EMV2-Build", ""), body, (time.perf_counter() - t0) * 1000, r.getheader("X-EMV2-As-Of", "")
    finally:
        c.close()


def emv2_asof(new, header, chain=None):
    """(block, time) of the V2 answer: its `as_of` field (int block, {block, time}, or {<chain>: {block, time}} on answers that
    span chains), else the X-EMV2-As-Of header."""
    a = new.get("as_of") if isinstance(new, dict) else None
    if isinstance(a, dict) and "block" not in a and isinstance(a.get(chain), dict):
        a = a[chain]
    if isinstance(a, dict):
        b, t = a.get("block"), a.get("time")
    elif emv2_num(a):
        b, t = a, None
    else:
        parts = (header or "").split(":")
        b, t = (parts + [None, None])[:2]
    try:
        return (int(b) if b not in (None, "") else None), (int(t) if t not in (None, "") else None)
    except (TypeError, ValueError):
        return None, None


def emv2_after(item, blk, t):
    """True when a dict row records activity after the V2 point (a block or a time past it)."""
    if not isinstance(item, dict):
        return False
    for k in EMV2_BLOCK_KEYS:
        if blk is not None and emv2_num(item.get(k)) and item[k] > blk:
            return True
    for k in EMV2_TIME_KEYS:
        if t is not None and emv2_num(item.get(k)) and item[k] > t:
            return True
    return False


def emv2_trim(obj, blk, t, n):
    """Copy of the old answer without list rows after the V2 point; n[0] counts the rows left out."""
    if isinstance(obj, dict):
        return {k: emv2_trim(v, blk, t, n) for k, v in obj.items()}
    if isinstance(obj, list):
        keep = []
        for x in obj:
            if emv2_after(x, blk, t):
                n[0] += 1
            else:
                keep.append(emv2_trim(x, blk, t, n))
        return keep
    return obj


def emv2_old_at(job, blk, t):
    """The current upstream's answer at the V2 point for routes that take one (holders-at block, trace-graph to), or None."""
    spec = EMV2_ALIGN.get(job["path"])
    if not spec or job.get("qs") is None:
        return None
    param, own = spec
    if any(job["qs"].get(k) for k in own):
        return None   # the request already named its own point in time: the old answer is at that point
    val = blk if param == "block" else t
    if val is None:
        return None
    try:
        data, _ = invscreen_route(job["path"], dict(job["qs"], **{param: str(val)}))
        return data
    except (HTTPError, OSError, ValueError):
        return None


def emv2_covered(cfg):
    """Chains emv2-serve has loaded (its /health `chains`), refreshed every 60 s; None when unknown."""
    now = time.time()
    if now - EMV2.get("cov_at", 0) > 60:
        cov = None
        try:
            code, _, body, _, _ = emv2_ask(cfg, "/health")
            d = json.loads(body) if code == 200 else {}
            cov = set(d.get("chains") or {}) if isinstance(d.get("chains"), dict) else None
        except (OSError, http.client.HTTPException, ValueError):
            cov = None
        EMV2.update(cov=cov, cov_at=now)
    return EMV2.get("cov")


def emv2_only_covered(old, covered, n):
    """address-chains limited to the chains V2 has loaded (rows `chains[].chain`); n[0] counts rows left out."""
    if not isinstance(old, dict) or not isinstance(old.get("chains"), list) or covered is None:
        return old
    keep = [c for c in old["chains"] if not isinstance(c, dict) or c.get("chain") in covered]
    n[0] += len(old["chains"]) - len(keep)
    return dict(old, chains=keep)


def emv2_drop_xc(obj, chain, n):
    """trace-graph without bridge legs (links kind=bridge, nodes on other chains): V2 phase 1 has no xc legs."""
    if not isinstance(obj, dict) or not isinstance(obj.get("links"), list):
        return obj
    links = [l for l in obj["links"] if not (isinstance(l, dict) and l.get("kind") == "bridge")]
    nodes = [x for x in obj.get("nodes") or [] if not (isinstance(x, dict) and x.get("chain") not in (None, chain))]
    n[0] += len(obj["links"]) - len(links) + len(obj.get("nodes") or []) - len(nodes)
    return dict(obj, links=links, nodes=nodes) if n[0] else obj


def emv2_compare(job):
    cfg = EMV2["cfg"]
    if not cfg:
        return
    rec = {"ts": round(time.time(), 3), "route": job["route"], "chain": job["chain"], "target": job["target"][:400],
           "old_code": job["code"], "old_ms": round(job["ms"], 2)}
    try:
        code, build, body, ms, asof_h = emv2_ask(cfg, job["target"])
        rec.update(new_code=code, new_ms=round(ms, 2), build=build[:80])
    except (OSError, http.client.HTTPException) as e:
        rec.update(new_code=0, verdict="v2_down", error=type(e).__name__)
        EMV2["stats"]["v2_down"] += 1
        return emv2_write(rec)
    if code == 501:
        rec["verdict"] = "unsupported"
    elif code == 504:
        rec["verdict"] = "v2_budget"
    elif code == 200 and job["code"] in (400, 404):
        rec["verdict"] = "v2_only"      # the current services do not cover this chain or market; V2 does (not a regression)
    elif code == 200 and job["code"] in (500, 502, 503, 504):
        rec["verdict"] = "old_failed"   # the current upstream failed this time; nothing to compare
    elif job["code"] == 200 and isinstance(job["obj"], dict) and job["obj"].get("available") is False:
        rec["verdict"] = "old_unavailable"   # the current service answers "Preparing" for this chain or route; nothing to compare
    elif code != job["code"]:
        rec.update(verdict="critical", critical=1, diffs=[["status", "", job["code"], code]])
    elif code != 200:
        rec["verdict"] = "same_status"
    else:
        try:
            new = json.loads(body)
        except ValueError:
            rec.update(verdict="critical", critical=1, diffs=[["type", "", "json", "not json"]])
            return emv2_write(rec)
        blk, t = emv2_asof(new, asof_h, job["chain"])
        old, align, after = job["obj"], "none", [0]
        if blk is not None or t is not None:
            rec["as_of"] = [blk, t]
            at = emv2_old_at(job, blk, t) if cfg.get("align_old", True) else None
            if at is not None:
                old, align = at, "param"
            else:
                old, align = emv2_trim(job["obj"], blk, t, after), "trim"
        xc, outside = [0], [0]
        if job["route"] == "trace-graph":
            old = emv2_drop_xc(old, job["chain"], xc)
        if job["chain"] == "*":
            old = emv2_only_covered(old, emv2_covered(cfg), outside)
            rec["outside_v2"] = outside[0]
            if outside[0] and not (old or {}).get("chains") and not (new.get("chains") if isinstance(new, dict) else None):
                rec["verdict"] = "outside_v2"   # every chain the current answer lists is one V2 has not loaded yet
                EMV2["stats"]["outside_v2"] += 1
                return emv2_write(rec)
        if job["route"] == "path" and isinstance(new, dict) and isinstance(old, dict) and old.get("found"):
            # V2 does not cross hubs (degree > 20,000: routers, exchanges, pools). When the old path runs through one that V2 names,
            # the difference is semantic: V2 answers "only via a hub" (not found) or a longer path that avoids it.
            hubs = {b.get("address") for b in new.get("blockedByHub") or [] if isinstance(b, dict)}
            mids = {n.get("address") for n in (old.get("nodes") or [])[1:-1] if isinstance(n, dict)}
            worse = (not new.get("found")) or (isinstance(new.get("hops"), int) and isinstance(old.get("hops"), int) and new["hops"] > old["hops"])
            if worse and hubs & mids:
                rec.update(verdict="via_hub", critical=0, hub=sorted(hubs & mids)[:3], old_hops=old.get("hops"), new_hops=new.get("hops"))
                EMV2["stats"]["via_hub"] += 1
                return emv2_write(rec)
        if job["route"] == "path" and isinstance(new, dict) and isinstance(old, dict):
            # shortest paths are not unique and the two engines may pick different middle nodes: compare what is determined,
            # found and hops, and (below) that V2's own path is a chain of edges from `from` to `to`
            ok = isinstance(new.get("nodes"), list) and isinstance(new.get("edges"), list)
            if new.get("found") and ok:
                ns = [n.get("id") for n in new["nodes"] if isinstance(n, dict)]
                es = [(e.get("source"), e.get("target")) for e in new["edges"] if isinstance(e, dict)]
                chain_ok = len(ns) == len(es) + 1 and all(es[i] == (ns[i], ns[i + 1]) for i in range(len(es))) and \
                    ns[0].endswith(":" + str(new.get("from"))) and ns[-1].endswith(":" + str(new.get("to")))
                if not chain_ok:
                    rec.update(verdict="critical", critical=1, diffs=[["value", "path", "chain of edges", "broken"]])
                    EMV2["stats"]["critical"] += 1
                    return emv2_write(rec)
            if new.get("found") and old.get("found") and isinstance(new.get("hops"), int) and isinstance(old.get("hops"), int) and new["hops"] < old["hops"]:
                # V2 found a shorter path and its edge chain was just checked: not a regression (the old engine's typed search skipped it)
                rec.update(verdict="match", critical=0, v2_shorter=1, old_hops=old["hops"], new_hops=new["hops"])
                EMV2["stats"]["match"] += 1
                return emv2_write(rec)
            old = {"found": bool(old.get("found")), "hops": old.get("hops")}
            new = {"found": bool(new.get("found")), "hops": new.get("hops")}
        out, seen = [], [0]
        emv2_diff(old, new, "", out, seen)
        if outside[0]:
            out = [d for d in out if d[1] != "available"]   # V2's own flag reflects only the chains it has loaded
        skip, gaps = EMV2_SKIP.get(job["route"]), EMV2_GAPS.get(job["route"])
        if skip:
            out = [d for d in out if not skip.search(d[1])]
        if gaps:
            out = [("known_gap",) + tuple(d[1:]) if d[3] is None and gaps.search(d[1]) else d for d in out]
        crit = [d for d in out if d[0] in EMV2_CRITICAL]
        skew = bool(crit) and align == "trim" and after[0] > 0
        verdict = "time_skew" if skew else "critical" if crit else "xc_missing" if xc[0] else "match"
        rec.update(leaves=seen[0], critical=0 if skew else len(crit), time_skew=len(crit) if skew else 0, extra=len(out) - len(crit),
                   known_gap=sum(1 for d in out if d[0] == "known_gap"), xc_missing=xc[0], align=align, after_as_of=after[0], verdict=verdict,
                   diffs=[[k, p, emv2_short(x), emv2_short(y)] for k, p, x, y in (crit or out)[:20]])
    EMV2["stats"][rec["verdict"]] += 1
    emv2_write(rec)


def emv2_worker():
    while True:
        job = EMV2["q"].get()
        try:
            emv2_compare(job)
        except Exception as e:   # a comparison bug must never reach the bridge
            EMV2["stats"]["compare_error"] += 1
            emv2_write({"ts": round(time.time(), 3), "route": job.get("route"), "chain": job.get("chain"), "verdict": "compare_error",
                        "error": repr(e)[:200]})
        finally:
            EMV2["q"].task_done()


def emv2_submit(cfg, job):
    now = time.time()
    with EMV2["lock"]:
        if EMV2["q"] is None:
            EMV2["q"] = _emv2_queue.Queue(maxsize=cfg["queue"])
            threading.Thread(target=emv2_worker, name="emv2-shadow", daemon=True).start()
        key = (job["route"], job["chain"])
        if cfg["gap"] and now - EMV2["last"].get(key, 0) < cfg["gap"]:
            EMV2["stats"]["skipped_gap"] += 1
            return
        EMV2["last"][key] = now
        stats_due = now - EMV2["stats_at"] > 300
        if stats_due:
            snap = dict(EMV2["stats"], queue=EMV2["q"].qsize())
            EMV2["stats"].clear()
            EMV2["stats_at"] = now
    if stats_due:
        emv2_write({"ts": round(now, 3), "kind": "stats", **snap})
    try:
        EMV2["q"].put_nowait(job)
    except _emv2_queue.Full:
        EMV2["stats"]["dropped"] += 1


def emv2_new(handler, cfg, route, chain, target):
    """Answer from emv2-serve; False (nothing sent) on any V2 failure so the caller falls back to the old path."""
    try:
        code, build, body, ms, _ = emv2_ask(cfg, target)
        obj = json.loads(body) if code == 200 else None
    except (OSError, http.client.HTTPException, ValueError):
        code, obj, ms, build = 0, None, 0.0, ""
    if code != 200 or not isinstance(obj, (dict, list)):
        EMV2["stats"]["new_fallback"] += 1
        emv2_write({"ts": round(time.time(), 3), "route": route, "chain": chain, "target": target[:400], "verdict": "new_fallback",
                    "new_code": code})
        return False
    handler.send_json(200, obj, cache="public, max-age=30", extra={"X-EMV2-Build": build[:80]} if build else None)
    EMV2["stats"]["new_served"] += 1
    return True


# EMV2-only routes: no current service answers them, so "old" (and no config) keeps the bridge's 404; mode "new" in
# emv2-bridge.json (routes: {"accumulators": "new"}) serves them from emv2-serve. tools/accumulators is the assistant card built
# from the same answer (design/EM-ENGINE-V2.md; FINAL-PRODUCTION-PLAN.md A.5).
EMV2_ONLY = {"/api/public/em/accumulators": "accumulators", "/api/public/em/tools/accumulators": "accumulators",
             "/api/public/em/graph": "graph", "/api/public/em/featured": "featured",
             "/api/public/em/tokens": "tokens", "/api/public/em/capabilities": "capabilities"}
EMV2_ONLY_PARAM = {"market": re.compile(r"[a-z][a-z0-9]{1,15}:0x[0-9a-f]{40}"), "window": re.compile(r"1h|24h|7d|30d|90d|all"),
                   "from": re.compile(r"[0-9]{9,13}"), "to": re.compile(r"[0-9]{9,13}"), "limit": re.compile(r"[1-9][0-9]?"),
                   "address": re.compile(r"0x[0-9a-f]{40}"), "entity": re.compile(r"[^\x00-\x1f]{1,120}"), "chain": re.compile(r"[a-z][a-z0-9]{1,15}"),
                   "hops": re.compile(r"[12]"), "min_usd": re.compile(r"[0-9]{1,15}(\.[0-9]{1,6})?"),
                   "symbol": re.compile(r"[^\x00-\x1f&#?/\\]{1,40}")}
EMV2_FEATURED = os.environ.get("EM_EMV2_FEATURED", "/srv/skew/emv2/featured")
EMV2_XC_GRADE = {"exact": "proven", "structural": "matched", "heuristic": "inferred"}


def emv2_featured(handler, qs):
    """Static home snapshot (tools/emv2_featured.py, daily): <dir>/<chain>.json or index.json. No upstream call: zero wait."""
    chain = (qs.get("chain") or "").lower()
    if chain and not EMV2_ONLY_PARAM["chain"].fullmatch(chain):
        return handler.send_json(400, {"detail": "chain is a registry id."})
    path = os.path.join(EMV2_FEATURED, (chain or "index") + ".json")
    try:
        st = os.stat(path)
        cached = EMV2["last"].get(path)
        if not cached or cached[0] != st.st_mtime_ns:
            with open(path) as f:
                cached = (st.st_mtime_ns, json.load(f))
            EMV2["last"][path] = cached
    except (OSError, ValueError):
        return handler.send_json(404, {"detail": "The featured snapshot for this chain is being prepared."})
    return handler.send_json(200, cached[1], cache="public, max-age=300")


EMV2_XC_WAIT_S = float(os.environ.get("EM_EMV2_XC_WAIT_S", "0.12"))
EMV2_XC_PENDING = {}
EMV2_XC_LOCK = threading.Lock()


def emv2_xc_get(up):
    """em-xc wallet-xc through the 300 s cache, waiting at most EMV2_XC_WAIT_S: a first ask that em-xc answers slowly (it took
    200 ms - 1.1 s, 2026-10-21) returns the graph without bridges and the fetch finishes in the background for the next ask.
    Returns (answer or None, pending)."""
    key = "emv2xc:" + up
    with EMV2_XC_LOCK:
        job = EMV2_XC_PENDING.get(key)
        if job is None:
            job = (threading.Event(), {})
            EMV2_XC_PENDING[key] = job

            def run(done=job[0], box=job[1]):
                try:
                    box["v"] = CACHE.get(key, 300, lambda: fetch_json(CFG["xc"], up, timeout=2))
                except Exception as e:   # noqa: BLE001 - reported to the caller
                    box["e"] = e
                finally:
                    done.set()
                    with EMV2_XC_LOCK:
                        EMV2_XC_PENDING.pop(key, None)
            threading.Thread(target=run, daemon=True).start()
    if not job[0].wait(EMV2_XC_WAIT_S):
        return None, True
    if "e" in job[1]:
        raise job[1]["e"]
    return job[1].get("v"), False


def emv2_xc_merge(obj, address):
    """Cross-chain bridges for the asked wallet from em-xc (matched legs) added to a graph/1 answer as kind "bridge" edges."""
    try:
        up = "/wallet-xc?" + urllib.parse.urlencode({"address": address, "limit": 20})
        xc, pending = emv2_xc_get(up)
    except Exception:   # the graph answer stands without bridges
        obj.setdefault("notes", []).append("Cross-chain bridges are temporarily unavailable.")
        return obj
    if pending:
        obj["xcPending"] = True
        obj.setdefault("notes", []).append("Cross-chain bridges are still loading for this wallet; ask again in a moment to include them.")
        return obj
    ids = {n.get("id") for n in obj.get("nodes") or []}
    names = {}
    try:
        names = {int(c.get("chainId")): c.get("id") for c in chains_status().get("chains") or [] if c.get("chainId")}
    except Exception:
        pass
    added = 0
    for leg in ((xc or {}).get("bridges") or {}).get("legs") or []:
        sc, dc = names.get(leg.get("src_chain")), names.get(leg.get("dst_chain"))
        sw, dw = (leg.get("src_wallet") or "").lower(), (leg.get("dst_wallet") or "").lower()
        if not (sc and dc and EMV2_ONLY_PARAM["address"].fullmatch(sw) and EMV2_ONLY_PARAM["address"].fullmatch(dw)):
            continue
        a, b = sc + ":" + sw, dc + ":" + dw
        for nid, ch, ad in ((a, sc, sw), (b, dc, dw)):
            if nid not in ids:
                ids.add(nid)
                obj.setdefault("nodes", []).append({"id": nid, "address": ad, "chain": ch, "entity": None, "entityGrade": None, "category": "unknown",
                                                    "categoryName": "Unclassified", "kind": "wallet", "isContract": None, "isHub": False, "stop": False,
                                                    "hop": 1, "held": None, "markKey": None, "mark": None, "chainMark": None, "firstSeen": None,
                                                    "lastSeen": None, "via": "xc"})
        obj.setdefault("edges", []).append({"source": a, "target": b, "chain": sc + "->" + dc, "kind": "bridge", "bridge": leg.get("bridge"),
                                            "asset": {"symbol": None, "address": leg.get("src_asset"), "decimals": None}, "amountRaw": None,
                                            "amount": leg.get("amount") if isinstance(leg.get("amount"), (int, float)) else None,
                                            "usd": leg.get("usd") if isinstance(leg.get("usd"), (int, float)) else None,
                                            "usdReason": None if isinstance(leg.get("usd"), (int, float)) else "bridge leg without a USD value",
                                            "n": 1, "firstTime": leg.get("src_ts"), "lastTime": leg.get("dst_ts"), "grade": EMV2_XC_GRADE.get(leg.get("class"), "judged"),
                                            "matchClass": leg.get("class"), "hop": 1})
        added += 1
    obj["xcLegs"] = added
    return obj


def emv2_card(a, qs):
    """tools/accumulators card: who bought (net) and who sold (net) a token over a window, numbers only."""
    def row(r):
        who = r.get("entity") or (r.get("category") if r.get("category") not in (None, "unknown") else None)
        return {"address": r.get("address"), "label": who, "kind": r.get("kind"), "grade": r.get("grade"), "side": r.get("side"),
                "delta": r.get("delta"), "deltaRaw": r.get("deltaRaw"), "balanceEnd": r.get("balanceEnd"), "pctOfSupply": r.get("pctOfSupply"),
                "isContract": r.get("isContract")}
    sym = a.get("symbol") or (a.get("market") or "").split(":")[-1][:10]
    w = a.get("window") or {}
    buyers, sellers = [row(r) for r in a.get("accumulators") or []], [row(r) for r in a.get("distributors") or []]
    span = qs.get("window") or "%sd" % max(1, round((int(w.get("to") or 0) - int(w.get("from") or 0)) / 86400))
    if a.get("available") is False:   # held (independent RPC check failed) or preparing: say why, never "no buyer"
        msg = "%s: %s" % (sym, a.get("reason") or "the balances of this token are being prepared.")
    elif buyers:
        b = buyers[0]
        msg = "%s: the largest net buyer over %s is %s%s, +%s (%s%% of supply)." % (
            sym, span, (b["address"] or "")[:6] + "…" + (b["address"] or "")[-4:], " (%s)" % b["label"] if b["label"] else "",
            "{:,.4g}".format(b["delta"] or 0), b["pctOfSupply"] if b["pctOfSupply"] is not None else "?")
    else:
        msg = "%s: no wallet's balance rose over %s." % (sym, span)
    return {"kind": "accumulators", "title": "Who is accumulating · " + sym, "market": a.get("market"), "symbol": a.get("symbol"),
            "network": (a.get("market") or "").split(":")[0], "window": w, "method": a.get("method"), "complete": a.get("complete"),
            "buyers": buyers, "sellers": sellers, "message": msg, "available": a.get("available", False), "reason": a.get("reason"),
            "held": bool(a.get("held")), "reasonKo": a.get("reasonKo"),
            "note": "Exact net balance change per address from the token's transfer history (Economic Machine V2); pools, routers and "
                    "exchanges are included and labelled by kind."}


def emv2_only(handler, cfg, path, qs):
    if path == "/api/public/em/featured":
        return emv2_featured(handler, qs)
    q = {}
    for k, v in qs.items():
        rx = EMV2_ONLY_PARAM.get(k)
        if rx is None or not rx.fullmatch((v or "").lower()):
            return handler.send_json(400, {"detail": "Invalid parameter: " + k})
        q[k] = v if k == "entity" else v.lower()
    if path == "/api/public/em/graph":
        if ("address" in q) == ("entity" in q):
            return handler.send_json(400, {"detail": "give exactly one of address or entity."})
        # window=24h/7d/30d: emv2-serve answers exact hop-1 token flows inside the window from the ledger (totalsScope "window");
        # on a chain without a ledger it answers 400 reason window_totals_preparing itself, passed on below
        target = "/api/public/em/graph?" + urllib.parse.urlencode(sorted(q.items()))
    elif path == "/api/public/em/capabilities":
        if q:
            return handler.send_json(400, {"detail": "capabilities takes no parameters."})
        target = "/api/public/em/capabilities"
    elif path == "/api/public/em/tokens":
        if "symbol" not in q or set(q) - {"symbol", "chain", "limit"}:
            return handler.send_json(400, {"detail": "give symbol (and optionally chain, limit)."})
        q["symbol"] = qs.get("symbol")   # symbols are matched case-insensitively by emv2-serve; keep the asked spelling
        target = "/api/public/em/tokens?" + urllib.parse.urlencode(sorted(q.items()))
    elif "market" not in q:
        return handler.send_json(400, {"detail": "market must be <chain>:<0x token address>."})
    else:
        target = "/api/public/em/accumulators?" + urllib.parse.urlencode(sorted(q.items()))
    try:
        code, build, body, ms, _ = emv2_ask(cfg, target)
        obj = json.loads(body) if code in (200, 400, 501) else None
    except (OSError, http.client.HTTPException, ValueError):
        code, obj, build = 0, None, ""
    EMV2["stats"]["only_" + str(code)] += 1
    if code == 200 and isinstance(obj, dict):
        if path == "/api/public/em/graph":
            out = emv2_xc_merge(obj, q["address"]) if "address" in q else obj
        else:
            out = emv2_card(obj, q) if path.endswith("/tools/accumulators") else obj
        cache = "no-store" if isinstance(out, dict) and out.get("xcPending") else "public, max-age=60"   # a bridge-less first answer is not kept
        return handler.send_json(200, out, cache=cache, extra={"X-EMV2-Build": build[:80]} if build else None)
    if code in (400, 501) and isinstance(obj, dict):
        out = {"detail": obj.get("detail") or obj.get("error") or "unsupported"}
        if obj.get("reason"):
            out["reason"] = obj["reason"]
        return handler.send_json(code, out)
    return handler.send_json(503, {"detail": "Economic Machine data is unavailable right now."})


_emv2_api_old = Handler.api


def _emv2_api(self, path, query, qs):
    if self.command == "GET" and path in EMV2_ONLY:
        cfg = emv2_cfg()
        if cfg and emv2_mode(cfg, EMV2_ONLY[path], "*") == "new":
            return emv2_only(self, cfg, path, qs)
        return _emv2_api_old(self, path, query, qs)   # unchanged: the bridge's own 404
    hit = emv2_route(path, qs) if self.command == "GET" else None
    cfg = emv2_cfg() if hit else None
    mode = emv2_mode(cfg, *hit) if cfg else "old"
    if mode == "old":
        return _emv2_api_old(self, path, query, qs)
    route, chain = hit
    target = path + ("?" + query if query else "")
    if mode == "new" and emv2_new(self, cfg, route, chain, target):
        return None
    if mode == "shadow" and cfg["sample"] < 1.0 and _emv2_random.random() >= cfg["sample"]:
        return _emv2_api_old(self, path, query, qs)
    cap, send = {}, self.send_json

    def capture(code, obj, *a, **k):
        if "code" not in cap:
            cap.update(code=code, obj=obj)
        return send(code, obj, *a, **k)

    self.send_json = capture
    t0 = time.perf_counter()
    try:
        return _emv2_api_old(self, path, query, qs)
    except HTTPError as e:
        cap.setdefault("code", e.code)
        cap.setdefault("obj", None)
        raise
    finally:
        del self.send_json
        if mode == "shadow" and "code" in cap:
            locked = isinstance(cap["obj"], dict) and "locked" in cap["obj"]
            if locked:
                EMV2["stats"]["skipped_gated"] += 1
            else:
                emv2_submit(cfg, {"route": route, "chain": chain, "target": target, "path": path, "qs": dict(qs), "code": cap["code"],
                                  "obj": cap["obj"], "ms": (time.perf_counter() - t0) * 1000})


Handler.api = _emv2_api
