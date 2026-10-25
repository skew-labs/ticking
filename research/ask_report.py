"""ask_report.py: the daily "what Ask could not answer" report from the EM layer's question log (no user identifiers in it).

usage: ask_report.py <em-questions.jsonl> <out dir> [YYYY-MM-DD]     (default: yesterday, UTC)

Writes <out>/ask-<day>.md and ask-<day>.json: totals by outcome (answered, preparing, no_tool, error, rate_limited), latency
p50/p95, and the top 30 unanswered questions grouped by their normalised text (0x addresses, numbers and case folded), with
counts and the latest wording. Read-only on the log; the log rotates itself at 32 MB (.jsonl.1 is read too).
"""
import collections
import datetime as dt
import json
import re
import sys
from pathlib import Path

UNANSWERED = ("preparing", "no_tool", "error")


def norm(q):
    q = re.sub(r"0x[0-9a-fA-F]{40}", "0x…", q)
    q = re.sub(r"[1-9A-HJ-NP-Za-km-z]{32,44}", "<address>", q)
    q = re.sub(r"\d+(?:[.,]\d+)?", "#", q)
    return re.sub(r"\s+", " ", q).strip().lower()


def main(log, out, day=None):
    day = day or (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
    t0 = int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp())
    recs = []
    for p in (Path(str(log) + ".1"), Path(log)):
        if p.exists():
            for line in p.read_text(errors="replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if t0 <= r.get("t", 0) < t0 + 86400:
                    recs.append(r)
    by = collections.Counter(r.get("outcome") for r in recs)
    ms = sorted(r.get("ms") or 0 for r in recs)
    pct = lambda p: ms[min(len(ms) - 1, int(len(ms) * p))] if ms else None
    groups = collections.defaultdict(lambda: {"n": 0, "outcomes": collections.Counter(), "last": "", "t": 0})
    for r in recs:
        if r.get("outcome") in UNANSWERED:
            g = groups[norm(r.get("q") or "")]
            g["n"] += 1
            g["outcomes"][r["outcome"]] += 1
            if r.get("t", 0) >= g["t"]:
                g["last"], g["t"] = r.get("q") or "", r.get("t", 0)
    top = sorted(groups.items(), key=lambda kv: (-kv[1]["n"], kv[0]))[:30]
    rep = {"day": day, "questions": len(recs), "outcomes": dict(by), "latencyMs": {"p50": pct(0.5), "p95": pct(0.95)},
           "unanswered": [{"pattern": k, "count": g["n"], "outcomes": dict(g["outcomes"]), "latest": g["last"]} for k, g in top]}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"ask-{day}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n")
    lines = [f"# Ask가 답하지 못한 질문 ({day}, UTC)", "",
             f"- 질문 {len(recs)}건: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])),
             f"- 지연: p50 {rep['latencyMs']['p50']} ms, p95 {rep['latencyMs']['p95']} ms", "",
             "| # | 횟수 | 결과 | 최근 문장 |", "|---|---|---|---|"]
    for i, u in enumerate(rep["unanswered"], 1):
        lines.append("| %d | %d | %s | %s |" % (i, u["count"], ", ".join(f"{k} {v}" for k, v in u["outcomes"].items()),
                                                u["latest"].replace("|", "\\|")[:160]))
    if not rep["unanswered"]:
        lines.append("| — | 0 | — | 답하지 못한 질문 없음 |")
    (out / f"ask-{day}.md").write_text("\n".join(lines) + "\n")
    print(out / f"ask-{day}.md", len(recs), dict(by))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
