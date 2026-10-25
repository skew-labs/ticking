#!/usr/bin/env python3
"""Summarise the bridge's EMV2 shadow log (emv2-shadow.jsonl and its rotated .1 … .N) per route and chain, and judge each
route against the switch rule (design/EM-ENGINE-V2.md §10): at least 10,000 compared requests over at least 7 days with
zero critical differences (time_skew, a difference explained by activity after the V2 map end, is shown apart and is not
critical) and a V2 failure rate (down, over budget) of at most 0.1 %. Unsupported answers (501) are counted
but are neither a pass nor a failure, and so are answers only V2 has (v2_only: the current services
answer 400/404 for that chain or market), where the current upstream failed (old_failed), or where the current service
answers available:false, "Preparing" (old_unavailable).
usage: emv2_shadow_report.py <logs dir> [--since <unix ts>] [--json] [--diffs N]"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict

NEED_N, NEED_DAYS, MAX_FAIL = 10_000, 7.0, 0.001


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))], 2)


def main():
    logs = sys.argv[1]
    since = float(sys.argv[sys.argv.index("--since") + 1]) if "--since" in sys.argv else 0.0
    ndiffs = int(sys.argv[sys.argv.index("--diffs") + 1]) if "--diffs" in sys.argv else 3
    files = sorted(glob.glob(os.path.join(logs, "emv2-shadow.jsonl*")), key=lambda f: (f.count("."), f), reverse=True)
    rows, stats = defaultdict(lambda: {"v": Counter(), "old": [], "new": [], "t0": None, "t1": None, "diffs": Counter(), "ex": [], "gap": 0}), Counter()
    for fn in files:
        with open(fn, errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    stats["bad_line"] += 1
                    continue
                if r.get("ts", 0) < since:
                    continue
                if r.get("kind") == "stats":
                    stats.update({k: v for k, v in r.items() if isinstance(v, int) and k not in ("ts", "queue")})
                    continue
                g = rows[(r.get("route"), r.get("chain"))]
                g["v"][r.get("verdict")] += 1
                g["t0"] = min(g["t0"] or r["ts"], r["ts"])
                g["t1"] = max(g["t1"] or r["ts"], r["ts"])
                g["gap"] += r.get("known_gap") or 0
                if r.get("verdict") in ("match", "critical", "time_skew", "xc_missing"):
                    g["old"].append(r.get("old_ms") or 0)
                    g["new"].append(r.get("new_ms") or 0)
                for d in r.get("diffs") or []:
                    if d[0] in ("status", "missing", "type", "length", "int", "raw", "value"):
                        g["diffs"][(d[0], d[1])] += 1
                if r.get("verdict") == "critical" and len(g["ex"]) < ndiffs:
                    g["ex"].append({"target": r.get("target"), "diffs": (r.get("diffs") or [])[:3]})
    out = []
    for (route, chain), g in sorted(rows.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        v = g["v"]
        compared = v["match"] + v["critical"] + v["time_skew"] + v["xc_missing"]
        fails = v["v2_down"] + v["v2_budget"]
        days = ((g["t1"] or 0) - (g["t0"] or 0)) / 86400
        total = compared + fails
        if v["critical"]:
            verdict = "fail: %d critical" % v["critical"]
        elif total and fails / total > MAX_FAIL:
            verdict = "fail: V2 failures %.2f%%" % (100 * fails / total)
        elif compared >= NEED_N and days >= NEED_DAYS:
            verdict = "pass: ready for new"
        elif not compared and v["unsupported"]:
            verdict = "unsupported by V2"
        elif not compared and v["old_unavailable"]:
            verdict = "current service says Preparing (old_unavailable): %d" % v["old_unavailable"]
        elif not compared and v["v2_only"]:
            verdict = "V2 only (current services do not cover it): %d" % v["v2_only"]
        else:
            verdict = "running: %d/%d requests, %.1f/%.0f days" % (compared, NEED_N, days, NEED_DAYS)
        if v["time_skew"] and compared:
            verdict += " (time_skew %.0f%%: differences explained by activity after the V2 map end)" % (100 * v["time_skew"] / compared)
        out.append({"route": route, "chain": chain, "compared": compared, "match": v["match"], "critical": v["critical"],
                    "time_skew": v["time_skew"], "xc_missing": v["xc_missing"], "known_gap_fields": g["gap"], "unsupported": v["unsupported"], "v2_down": v["v2_down"], "v2_budget": v["v2_budget"], "same_status": v["same_status"],
                    "v2_only": v["v2_only"], "old_failed": v["old_failed"], "old_unavailable": v["old_unavailable"],
                    "compare_error": v["compare_error"], "days": round(days, 2),
                    "old_p50": pct(g["old"], .5), "old_p99": pct(g["old"], .99), "new_p50": pct(g["new"], .5), "new_p99": pct(g["new"], .99),
                    "top_diffs": [{"kind": k, "field": p, "n": n} for (k, p), n in g["diffs"].most_common(5)], "examples": g["ex"],
                    "verdict": verdict})
    if "--json" in sys.argv:
        print(json.dumps({"routes": out, "counters": stats, "files": files}, indent=1))
        return
    print("%-15s %-10s %8s %6s %6s %6s %6s %6s %6s %9s %9s %9s %9s  %s" % ("route", "chain", "compared", "crit", "skew", "unsupp", "down",
                                                                           "budget", "days", "old p50", "old p99", "v2 p50", "v2 p99", "verdict"))
    for o in out:
        print("%-15s %-10s %8d %6d %6d %6d %6d %6d %6.2f %9s %9s %9s %9s  %s" % (o["route"], o["chain"], o["compared"], o["critical"], o["time_skew"],
                                                                                o["unsupported"], o["v2_down"], o["v2_budget"], o["days"], o["old_p50"],
                                                                                o["old_p99"], o["new_p50"], o["new_p99"], o["verdict"]))
        if o["xc_missing"] or o["known_gap_fields"]:
            print("    xc_missing answers %d (bridge legs not in V2 phase 1); known_gap fields %d (V2 null by design: class, entity)"
                  % (o["xc_missing"], o["known_gap_fields"]))
        for d in o["top_diffs"]:
            print("    %-7s %-60s x%d" % (d["kind"], d["field"][:60], d["n"]))
    if stats:
        print("counters (5-min stats lines):", dict(stats))


if __name__ == "__main__":
    main()
