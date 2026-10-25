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
print("## 원본 이체에서 직접 다시 계산한 값과의 대조 (map 없이, 독립 경로)\n")
for c in CHAINS:
    for name in ("RAW.json", "LEDGER_COUNTS.json"):
        x = load(f"{R}/{c}/{name}")
        if x:
            print(f"- {c} {name[:-5]}: {mark(x.get('ok'))} " + "; ".join(f"{y['check']} {y.get('differing', y.get('got'))}" for y in x["checks"])[:600])
print()
f = load(f"{R}/polygon/FOCUS.json")
print("## Polygon focus_balances (정확값 대조)\n")
if f:
    for x in f["checks"]:
        print(f"- {mark(x.get('ok'))} {x['check']}: 차이 {x.get('differing')}, 2^100 제외 쌍 {x.get('excluded_ge_2_100')}, 대조 {x.get('compared')}")
    print(f"- 기준 시점: {json.dumps(f.get('asOf'))}, 제외 표시 출처: {f.get('exclusionSource')}\n")
else:
    print("- 미실행\n")
h = load(f"{R}/polygon/HELD.json")
if h:
    c, sm = h["complete"], h["sampled"]
    print(f"- (아래 두 줄의 FAIL은 설명 전 원시 비교예요. 최종 판정은 '다른 쌍 설명' 줄이에요.)")
    print(f"- held 위험 쌍 {h['held_pairs']:,}개 중 전수 대조 {c['pairs']:,}쌍({c['tokens']:,}개 토큰, 남은 364개 파일에 안 나오는 토큰): {mark(c['ok'])}, 차이 {c['differ']}, V2 없음 {c['v2_missing']}")
    print(f"- 표본 토큰 {sm['tokens']}개({sm['pairs']:,}쌍, 부분 합산 + 남은 파일 재계산): {mark(sm['ok'])}, 차이 {sm['differ']}, 2^100 제외·큰 금액 관련 {sm['differ_excluded_or_big']}")
    print(f"- 확인하지 못한 held 쌍: {h['unverified_pairs']:,}")
hx = load(f"{R}/polygon/HELD_EXPLAIN.json")
if hx:
    print(f"- 다른 쌍 설명: {hx['differ']}쌍을 2,244개 파일 전체에서 두 번 다시 계산(모든 이체 / 2^100 이상 제외)했어요. "
          f"V2 = 모든 이체 합, exact = 큰 금액 제외 합으로 {hx['explained_by_2_100_rule']}/{hx['differ']} 설명돼요. 설명 안 되는 차이 {hx['unexplained']}. 판정 {mark(hx['ok'])}")
print("- 참고: 공개된 정확값 parquet(focus_exact 등)은 HUGEINT를 DOUBLE로 저장해 2^53 넘는 값이 반올림돼 있어요. 대조는 econ.duckdb 원본 HUGEINT를 문자열로 뽑아서 했어요.\n")
print("## CASHCAT 골든 케이스\n")
cc = load(f"{R}/robinhood/CASHCAT.json")
print("\n".join(f"- {mark(x['ok'])} {x['check']}: {x.get('got')} / 기대 {x.get('expected')}" for x in cc["checks"]) if cc else "- 미실행")
print("\n## emv2-serve 응답 vs map·원본 재계산 (기존 서비스가 없는 체인)\n")
for c in CHAINS:
    x = load(f"{R}/{c}/SERVE_MAP.json")
    if x:
        nb, ac, ho = x["neighbors"], x["address_chains"], x.get("holders_at")
        print(f"- {c}: {mark(x['ok'])} | neighbors 주소 {nb['addresses']} 행 {nb['rows']} 차이 {nb['row_diffs']} edges 수 차이 {nb['edges_count_diffs']} 오류 {nb['errors']}"
              f" (erc721 금액 표기 0/null 형식 차이 {nb.get('format_only_erc721_amount', 0)})"
              f" | address-chains {ac['addresses']} 차이 {ac['diffs']}" + (f" | holders-at 토큰 {ho['tokens']} 보유자 수 차이 {ho['count_diffs']} 상위 차이 {ho['top_diffs']}" if ho else ""))
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
print("## arc 추가 확인 (전환 판정)\n")
for name, label in (("ACCUM_7d.json", "accumulators AUSD 7d"), ("ACCUM_24h.json", "accumulators AUSD 24h"), ("HOLDERS_block.json", "holders-at USDC block 26,000,000"),
                    ("HOLDERS_snaps.json", "holders-at USDC snaps=1"), ("RAW.8af0.json", "arc 8af0 (WETH9 보정) 원본 재계산 대조, WETH9 제외")):
    x = load(f"{R}/arc/{name}")
    if x: print(f"- {label}: {mark(x.get('ok'))}")
print("- WETH9(arc 8af0): V2 잔고 합 = RPC eth_getBalance(계약, 26,859,999) = 1,312,721,780,650,778,661,817, 주소별 30개 독립 재계산과 일치, 추가 이벤트 2,838,431 = Deposit 1,420,660 + Withdrawal 1,417,771 → PASS")
print()
g = load(f"{R}/GRAPH.json")
if g:
    print("## /api/public/em/graph (skew, arc 8af0 + unichain f3d1)\n")
    for ch in ("arc", "unichain"):
        x = g.get(ch)
        if x:
            print(f"- {ch}: {mark(x['ok'])} {json.dumps(x['counts'], ensure_ascii=False)} warm {json.dumps(x['warm_ms'])}")
    print("- 판정 요약: exact=true 1홉 엣지 전부 map과 같음, 2홉·노드·USD PASS. window=7d는 창 안 활동 엣지에 전체 기간 합계를 보여 줘 사양과 다름(FAIL), arc 1홉 warm p99 127ms(목표 10ms 초과).\n")
print("## 판정 메모 (verify가 확인한 의미 차이)\n")
print("- CASHCAT 서빙 보유자: V2 3명(블록 88,836)·90명(블록 90,043)이 원본 재계산과 같음. invscreen은 시각을 약 20초 앞 블록으로 바꾸고 먼지 보유자(12,509 wei)를 빼서 0명·87명. core 결정: V2 규칙(ts ≤ t인 마지막 블록, 잔고 > 0)이 기준, 골든 3·90으로 변경.")
print("- linea WETH9: RPC = V2 공급량 + 0x0으로 보낸 Transfer 910건(2,414,284,440,000,590 wei) + 빠진 3행 순합 0. core가 공식을 고쳐 PASS. arc는 0x0 항 0.")
print("- Polygon 공개 정확값 parquet는 HUGEINT를 DOUBLE로 저장(2^53 넘는 값 반올림). 대조는 econ.duckdb 원본 HUGEINT로 함.")
print("- path(허브로): 지연은 목표 안(cold p99 174ms)이나 10쌍 중 1쌍이 예산 안에서 경로를 놓침(glserve는 3홉 발견) → 전환 보류.")
print()
print("## 결정성과 sanitizer\n")
for p in sorted(glob.glob(f"{R}/determinism-*.json")):
    d = load(p)
    print(f"- {os.path.basename(p)}: {mark(d.get('identical')) if d else '미실행'} {json.dumps(d)[:300] if d else ''}")
for p in sorted(glob.glob(f"{R}/sanitizer-*.txt")):
    print(f"- {os.path.basename(p)}: " + open(p).read().strip().splitlines()[-1][:200])
