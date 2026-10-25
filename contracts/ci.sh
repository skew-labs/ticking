#!/usr/bin/env bash
# Contracts CI gate. No keys, no txs. RPC URL is read from env/.env.rpc and never echoed.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.foundry/bin:$(cd .. && pwd)/.venv/bin:$PATH"
[ -f ../.env.rpc ] && { set -a; . ../.env.rpc; set +a; }
export RH_RPC="${RH_QN_HTTP:-https://rpc.mainnet.chain.robinhood.com}"
export FORK_BLOCK="${FORK_BLOCK:-$(cast block-number -r https://rpc.mainnet.chain.robinhood.com)}"
redact() { sed -E 's#https?://[^ "]*quiknode[^ "]*#<RPC>#g'; }
echo "== build";            forge build --sizes 2>&1 | grep -E "TickingRouter " || true
echo "== fmt";              forge fmt --check
echo "== solhint";          solhint --max-warnings 0 src/TickingRouter.sol
echo "== unit+fuzz+invariant"; forge test --no-match-path "test/Fork*" 2>&1 | redact | tail -3
echo "== fork @ $FORK_BLOCK";  FOUNDRY_PROFILE=fork forge test --match-path "test/Fork*.sol" 2>&1 | redact | grep -E "Suite result|FAIL"
echo "== halmos";           FOUNDRY_DYNAMIC_TEST_LINKING=false halmos --contract SymbolicTest 2>&1 | grep -E "^\[|result"
echo "== slither gate (no High/Medium)"
rm -f /tmp/ci_slither.json
FOUNDRY_DYNAMIC_TEST_LINKING=false slither . --filter-paths "lib/|test/" --json /tmp/ci_slither.json >/dev/null 2>&1 || true
python3 - <<'P'
import json,sys
d=json.load(open('/tmp/ci_slither.json'))['results'].get('detectors',[])
bad=[x for x in d if x['impact'] in ('High','Medium')]
print(len(d),'findings;',len(bad),'High/Medium')
sys.exit(1 if bad else 0)
P
echo "CI OK"
