#!/usr/bin/env python3
"""make_report.py [RESULTS_DIR] > REPORT.md: one Korean summary of every verify result present (per-chain rows, counts, Polygon focus,
CASHCAT, API, latency, determinism). Missing pieces are listed as 미실행 with the reason file if any."""
import glob
import json
import os
import sys

R = sys.argv[1] if len(sys.argv) > 1 else "results"
CHAINS = ["arc", "linea", "unichain", "optimism", "robinhood", "arbitrum", "ethereum", "base", "polygon"]


def load(p):
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return None


def mark(ok):
    return "PASS" if ok is True else "FAIL" if ok is False else "미실행"


print("# EMV2 1단계 검증 보고 (verify)\n")
print("기준값: VM `/data/emv2-dev/verify/golden/`, Blob `emv2/verify/20261020/golden/`. map 표본(체인당 주소 약 1,900개)과 checks.json, CASHCAT, Base RECHECK, Polygon 정확값.\n")
print("## 체인별 결과\n")
print("| 체인 | 행 대조 (edges·nodes·bal·token_stats) | 체인 합계 (checks 등식) | 실패 항목 | time_skew 행 |")
print("|---|---|---|---|---|")
for c in CHAINS:
    r, k = load(f"{R}/{c}/RESULT.json"), load(f"{R}/{c}/COUNTS.json")
    fails = [x["check"] for x in (r or {}).get("checks", []) if not x.get("ok")] + [x["check"] for x in (k or {}).get("checks", []) if not x.get("ok")]
    skew = (r or {}).get("meta", {}).get("bal_time_skew_rows")
    print(f"| {c} | {mark(r and r.get('ok'))} | {mark(k and k.get('ok'))} | {'; '.join(fails[:4]) or '-'} | {skew if skew is not None else '-'} |")
print()
for c in CHAINS:
    r = load(f"{R}/{c}/RESULT.json")
    if not r:
        continue
    m = r.get("meta", {})
    print(f"### {c}\n")
    for x in r.get("checks", []):
        extra = {kk: x[kk] for kk in ("differing", "compared", "got", "expected", "error") if kk in x}
        print(f"- {mark(x.get('ok'))} {x['check']} {json.dumps(extra, ensure_ascii=False)}")
    if m.get("net_approx_vs_exact"):
        print(f"- 측정: map net_approx(double) vs V2 정수 잔고 {json.dumps(m['net_approx_vs_exact'])}")
    if "erc1155_in_v2_bal" in m:
        print(f"- V2 bal의 erc1155 포함: {m['erc1155_in_v2_bal']}")
    k = load(f"{R}/{c}/COUNTS.json")
    if k:
        for x in k["checks"]:
            print(f"- {mark(x['ok'])} 합계 {x['check']}: {x['got']} / 기대 {x['expected']}")
        if k.get("not_covered"):
            print(f"- V2가 다시 계산하지 않는 등식: {', '.join(k['not_covered'])}")
    print()
f = load(f"{R}/polygon/FOCUS.json")
print("## Polygon focus_balances (정확값 대조)\n")
if f:
    for x in f["checks"]:
        print(f"- {mark(x.get('ok'))} {x['check']}: 차이 {x.get('differing')}, 2^100 제외 쌍 {x.get('excluded_ge_2_100')}, 대조 {x.get('compared')}")
    print(f"- 기준 시점: {json.dumps(f.get('asOf'))}, 제외 표시 출처: {f.get('exclusionSource')}\n")
else:
    print("- 미실행\n")
print("## CASHCAT 골든 케이스\n")
cc = load(f"{R}/robinhood/CASHCAT.json")
print("\n".join(f"- {mark(x['ok'])} {x['check']}: {x.get('got')} / 기대 {x.get('expected')}" for x in cc["checks"]) if cc else "- 미실행")
print("\n## API 대조 (emv2-serve vs 지금 서빙)\n")
for p in sorted(glob.glob(f"{R}/api-*/API.json")):
    a = load(p)
    if a:
        print(f"- {os.path.basename(os.path.dirname(p))}: {mark(a['ok'])} ({a['n'] - a['failed']}/{a['n']}), time_skew 차이 {a.get('time_skew_diffs', 0)}")
print("\n## 지연 (p50 / p99, ms)\n")
for p in sorted(glob.glob(f"{R}/bench-*.json")):
    b = load(p)
    if not b:
        continue
    print(f"**{os.path.basename(p)}** (시작 {b.get('startedAt')}, load {json.dumps(b.get('load'))})\n")
    print("| 대상 | 조회 | n | cold p50 | cold p99 | warm p50 | warm p99 | 응답 코드 |")
    print("|---|---|---|---|---|---|---|---|")
    for x in b["rows"]:
        print(f"| {x['target']} | {x['query']} | {x['n']} | {x['cold_p50']} | {x['cold_p99']} | {x['warm_p50']} | {x['warm_p99']} | {x['codes']} |")
    print()
print("## 결정성과 sanitizer\n")
for p in sorted(glob.glob(f"{R}/determinism-*.json")):
    d = load(p)
    print(f"- {os.path.basename(p)}: {mark(d.get('identical')) if d else '미실행'} {json.dumps(d)[:300] if d else ''}")
for p in sorted(glob.glob(f"{R}/sanitizer-*.txt")):
    print(f"- {os.path.basename(p)}: " + open(p).read().strip().splitlines()[-1][:200])
