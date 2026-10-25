# ------------------------------------------------------------------------------------------------
# EMV2 shadow (emv2/bridge/apply_bridge_emv2.py; design: design/EM-ENGINE-V2.md §8 API, §10 dual run). Per route and chain the
# bridge answers from the current services (old), answers the same and compares emv2-serve in the background (shadow), or
# answers from emv2-serve and falls back to the current services on any V2 failure (new).
# No emv2-bridge.json next to this file (or $EM_EMV2_CONFIG) = every route old: each request takes exactly the path it took
# before this block. The file is reread every 30 s, so switching a route needs no restart:
#   {"serve": "127.0.0.1:8740", "default": "old", "routes": {"holders-at": {"arc": "shadow", "*": "old"}, "trace": "shadow"},
#    "sample": 1.0, "min_gap_ms": 0, "timeout_ms": 1500, "queue": 128, "log_max_mb": 64, "log_keep": 5}
# Shadow never touches the answer: the old body is sent first, then a bounded queue (dropped when full) asks emv2-serve the same
# path and query and appends one JSONL line per comparison to <logs>/emv2-shadow.jsonl (rotated by size). Integers and strings
# must match exactly, other numbers within a relative 1e-6, lists in order; time-of-answer fields are not compared.

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
               "logoUrl", "icon", "icon_url", "credit", "logoCredit", "locked"}
EMV2_CRITICAL = ("status", "missing", "type", "length", "int", "raw", "value")


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
                       "timeout": min(5.0, max(0.1, float(raw.get("timeout_ms", 1500)) / 1000)), "queue": min(1024, max(1, int(raw.get("queue", 128)))),
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
        return r.status, r.getheader("X-EMV2-Build", ""), body, (time.perf_counter() - t0) * 1000
    finally:
        c.close()


def emv2_compare(job):
    cfg = EMV2["cfg"]
    if not cfg:
        return
    rec = {"ts": round(time.time(), 3), "route": job["route"], "chain": job["chain"], "target": job["target"][:400],
           "old_code": job["code"], "old_ms": round(job["ms"], 2)}
    try:
        code, build, body, ms = emv2_ask(cfg, job["target"])
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
        out, seen = [], [0]
        emv2_diff(job["obj"], new, "", out, seen)
        crit = [d for d in out if d[0] in EMV2_CRITICAL]
        rec.update(leaves=seen[0], critical=len(crit), extra=len(out) - len(crit), verdict="critical" if crit else "match",
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
        code, build, body, ms = emv2_ask(cfg, target)
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


_emv2_api_old = Handler.api


def _emv2_api(self, path, query, qs):
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
                emv2_submit(cfg, {"route": route, "chain": chain, "target": target, "code": cap["code"], "obj": cap["obj"],
                                  "ms": (time.perf_counter() - t0) * 1000})


Handler.api = _emv2_api
