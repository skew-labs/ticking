#!/usr/bin/env bash
# Correctness gates (eth_call only). Exit non-zero on any failure.
set -euo pipefail; cd "$(dirname "$0")"
(cd core && cargo build --release -q)
# 1) contract fork tests (UNAUDITED router; fork of RH mainnet, no txs)
(cd contracts && PATH=$HOME/.foundry/bin:$PATH forge test -q)
# 2) live engine: 0-wei single-pool fuzz + quote==fill bundle proof + latency budget (needs engine on :8782)
DUR=${SOAK_SECS:-300} .venv/bin/python service/soak.py "${SOAK_SECS:-300}"
.venv/bin/python - <<'PY'
import json; R=json.load(open('data/live/soak.json'))
p=lambda v,q: sorted(v)[int((len(v)-1)*q)]
fails=[]
if R['proof_fail']: fails.append(f"proof failures {len(R['proof_fail'])}")
if R['fuzz_fail']: fails.append(f"fuzz failures {len(R['fuzz_fail'])}")
if p(R['fast_cmp'],.99)>2000: fails.append(f"meme fast compute p99 {p(R['fast_cmp'],.99)}us > 2000us")
if p(R['best_cmp'],.99)>200000: fails.append(f"stock best compute p99 {p(R['best_cmp'],.99)}us > 200ms")
if p(R['lag'],.99)>20: fails.append(f"lag p99 {p(R['lag'],.99)} blocks > 20")
print('GATES', 'FAIL: '+'; '.join(fails) if fails else 'PASS'); raise SystemExit(1 if fails else 0)
PY
# 3) regression vs Kyber (fair same-block) — optional, slow: .venv/bin/python harness/compare_fair.py
