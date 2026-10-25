#!/usr/bin/env python3
"""compare_api.py SPECS.jsonl OUTDIR: field-by-field comparison of EMV2 answers against today's served answers.

SPECS.jsonl rows: {"name": ..., "old": URL, "new": URL [, "old_pinned": URL]}   (old = bridge/invscreen/glserve, new = emv2-serve)
  old_pinned: the same old query limited to the map's end (e.g. trace ...&to=<map end time>) where the old service supports it.
--as-of-block N --as-of-time T: the map end (V2 phase 1 serves the map only). A difference is classed time_skew, not fatal, when
  the new answer equals old_pinned at that path, or the old value at that path is a block/time after the map end (activity the
  daily delta added after the map). time_skew is reported separately and does not fail the request.
Rules (design 10.4): integers and raw/decimal strings exactly equal; floats (USD, shares) relative error <= 1e-6; lists compared
in order (ranked answers) with the first differing index reported; keys listed in IGNORE are timing/version metadata.
Writes OUTDIR/API.json (per request: ok, diffs[:30], latency of both) and prints a summary. Sequential, one request at a time.
"""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

IGNORE = {"elapsed_ms", "elapsedMs", "generatedAt", "generated_at", "cache", "cached", "servedBy", "served_by", "timing", "latencyMs",
          "tier", "engine", "build_id", "buildId", "inputHash", "dataVersion", "asOf", "as_of", "freshness", "realtime"}
REL = 1e-6


def fetch(url):
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            body, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        body, code = e.read(), e.code
    ms = (time.perf_counter() - t) * 1000
    try:
        return code, json.loads(body), ms
    except ValueError:
        return code, {"_raw": body[:300].decode("utf-8", "replace")}, ms


def diff(a, b, path, out):
    if len(out) >= 30:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k in IGNORE:
                continue
            if k not in a or k not in b:
                out.append({"path": f"{path}.{k}", "old": a.get(k, "<missing>"), "new": b.get(k, "<missing>")})
            else:
                diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append({"path": f"{path}[len]", "old": len(a), "new": len(b)})
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out)
    elif isinstance(a, bool) or isinstance(b, bool) or isinstance(a, str) or isinstance(b, str) or a is None or b is None:
        if a != b:
            out.append({"path": path, "old": a, "new": b})
    elif isinstance(a, int) and isinstance(b, int):
        if a != b:
            out.append({"path": path, "old": a, "new": b})
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if not (math.isclose(a, b, rel_tol=REL, abs_tol=1e-9)):
            out.append({"path": path, "old": a, "new": b, "rel": abs(a - b) / max(abs(a), abs(b), 1e-300)})
    elif a != b:
        out.append({"path": path, "old": a, "new": b})


def walk(j, path):
    for part in path.lstrip("$").replace("[", ".[").split("."):
        if not part:
            continue
        if part.startswith("["):
            if part == "[len]":
                return len(j) if isinstance(j, list) else None
            i = int(part[1:-1])
            j = j[i] if isinstance(j, list) and i < len(j) else None
        else:
            j = j.get(part) if isinstance(j, dict) else None
        if j is None:
            return None
    return j


def is_skew(d, pinned, as_block, as_time):
    if pinned is not None and d["path"] not in ("<status>",) and walk(pinned, d["path"]) == d["new"]:
        return True
    leaf = d["path"].rsplit(".", 1)[-1].lower()
    v = d["old"]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if as_block and "block" in leaf and v > as_block:
            return True
        if as_time and "time" in leaf and v > as_time:
            return True
    return False


def main():
    args = [a for a in sys.argv[1:]]
    as_block = int(args[args.index("--as-of-block") + 1]) if "--as-of-block" in args else None
    as_time = int(args[args.index("--as-of-time") + 1]) if "--as-of-time" in args else None
    # --v2-chains a,b: chains the EMV2 server has loaded; per-chain lists in the old answer are cut to these (coverage, not a difference)
    v2c = set(args[args.index("--v2-chains") + 1].split(",")) if "--v2-chains" in args else None
    pos = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith(("--as-of", "--v2-chains")))]
    specs = [json.loads(l) for l in open(pos[0]) if l.strip()]
    out = pos[1]
    os.makedirs(out, exist_ok=True)
    res = []
    for s in specs:
        oc, oj, oms = fetch(s["old"])
        nc, nj, nms = fetch(s["new"])
        pinned = fetch(s["old_pinned"])[1] if s.get("old_pinned") else None
        if v2c is not None:
            for j in (oj, pinned):
                if isinstance(j, dict) and isinstance(j.get("chains"), list):
                    j["chains"] = [c for c in j["chains"] if not isinstance(c, dict) or c.get("chain") in v2c]
        d = []
        if oc != nc:
            d.append({"path": "<status>", "old": oc, "new": nc})
        diff(oj, nj, "$", d)
        skew = [x for x in d if is_skew(x, pinned, as_block, as_time)]
        fatal = [x for x in d if x not in skew]
        res.append({"name": s["name"], "ok": not fatal, "old_ms": round(oms, 2), "new_ms": round(nms, 2), "diffs": fatal, "time_skew": skew})
        print(("PASS " if not fatal else "FAIL ") + s["name"], f"old {oms:.1f}ms new {nms:.1f}ms", f"time_skew {len(skew)}", (fatal[0] if fatal else ""))
    ok = all(r["ok"] for r in res)
    json.dump({"ok": ok, "asOf": {"block": as_block, "time": as_time}, "n": len(res), "failed": sum(not r["ok"] for r in res),
               "time_skew_diffs": sum(len(r["time_skew"]) for r in res), "results": res}, open(f"{out}/API.json", "w"), indent=1, default=str)
    print("API", "PASS" if ok else "FAIL", f"{sum(r['ok'] for r in res)}/{len(res)}")


if __name__ == "__main__":
    main()
