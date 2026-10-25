#!/bin/bash
# run.sh <arbitrum|robinhood>: tip follower over public RPC (Economic Machine real-time extraction).
# chart-cache 2026-10-23: robinhood follow-chunk1000→100 lag20→8 (match arb tip publish cadence; tip SoT)
# markets-faster 2026-10-23: eth follow-chunk10/lag6/poll2s; linea chunk25/lag10/poll1s; base+op chunk200/lag12 (chunk50 aborted — re-cut from --from).
# Resumes right after the newest OK manifest, so a restart never re-cuts ranges already written in 1000-block chunks.
R=/srv/skew/em-realtime; B=$R/bin/em-rpc-extract
resume() { # resume <out_dir> <default_from>  (find-based: avoids ARG_MAX on 10k+ manifests)
  local last; last=$(find "$1/manifests" -maxdepth 1 -name "*.json" -type f -print0 2>/dev/null \
    | xargs -0 grep -l "\"status\": \"OK\"" 2>/dev/null \
    | sed "s#.*/##; s/\.json\$//" | awk -F- "{print \$2+0}" | sort -n | tail -n 1)
  [ -n "$last" ] && echo $((last + 1)) || echo $2; }
case "$1" in
  arbitrum) exec $B --chain 42161 --procs 2 --parallel 4 --chunk 1000 --follow --follow-chunk 50 --lag 8 --poll 1s \
      --rpc "https://arbitrum-one-rpc.publicnode.com#batch=1&workers=16" --rpc "https://arbitrum.drpc.org#batch=1&workers=4" \
      --rpc "https://arb1.arbitrum.io/rpc#batch=1&workers=2" --from $(resume $R/out/arbitrum 515850000) --out $R/out/arbitrum ;;
  robinhood) exec $B --chain 4663 --procs 2 --parallel 4 --chunk 1000 --follow --follow-chunk 100 --lag 8 --poll 1s \
      --rpc "https://robinhood-rpc.publicnode.com#batch=1&workers=32" --rpc "https://robinhood.drpc.org#batch=1&workers=4" \
      --rpc "https://rpc.mainnet.chain.robinhood.com#batch=1&workers=4" --from $(resume $R/out/robinhood 91448000) --out $R/out/robinhood ;;
  arc) exec $B --chain 5042 --nitro-genesis 0 --procs 2 --parallel 4 --chunk 1000 --follow --follow-chunk 100 --lag 20 --poll 1s \
      --rpc "https://arc-rpc.publicnode.com#batch=1&workers=24" --rpc "https://arc.drpc.org#batch=1&workers=4" \
      --rpc "https://rpc.mainnet.arc.io#batch=1&workers=4" --from $(f=$(resume $R/out/arc-raw 0); [ $f -lt 26800000 ] && f=26800000; echo $f) --out $R/out/arc-raw ;;   # tip only: 2613000..26799999 = VM backfill (em-arc/arc_range.sh)   # raw; out/arc = USDC-normalised view (em-arc/py/arc_norm.py)
  ethereum) exec $B --chain 1 --nitro-genesis 0 --weth 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2 --procs 1 --parallel 2 --chunk 1000 --follow --follow-chunk 10 --lag 6 --poll 2s \
      --rpc "https://ethereum-rpc.publicnode.com#batch=1&workers=8" --from $(resume $R/out/ethereum 26220000) --out $R/out/ethereum ;;   # history = reth snapshot head 26220503
  linea) exec $B --chain 59144 --nitro-genesis 0 --weth none --procs 1 --parallel 2 --chunk 1000 --follow --follow-chunk 25 --lag 10 --poll 1s \
      --rpc "https://linea-rpc.publicnode.com#batch=1&workers=8" --rpc "https://rpc.linea.build#batch=1&workers=4" \
      --from $(resume $R/out/linea 32356000) --out $R/out/linea ;;   # history = PublicNode geth part tar to 32356908
  hyperevm) exec $B --chain 999 --nitro-genesis 0 --weth none --procs 1 --parallel 2 --chunk 1000 --follow --follow-chunk 200 --lag 20 --poll 1s \
      --rpc "https://hyperliquid-rpc.publicnode.com#batch=1&workers=8" --rpc "https://rpc.hyperliquid.xyz/evm#batch=1&workers=2" \
      --from $(resume $R/out/hyperevm 48957000) --out $R/out/hyperevm ;;   # em-hyperevm log backfill runs backward from 48957884
  # OP-stack chains: emrpc-op (deposit txs 0x7E; resumes on its own from the OK manifests in --out)
  base) exec $R/bin/emrpc-op --chain-id 8453 --follow --chunk 50 --lag 12 --poll 2 --from 52981400 --out $R/out/base \
      --rpc "https://base-rpc.publicnode.com#workers=8" --rpc "https://mainnet.base.org#workers=3" --rpc "https://gateway.tenderly.co/public/base#workers=6" ;;   # history = Base v2 snapshot head 52789327
  optimism) exec $R/bin/emrpc-op --chain-id 10 --follow --chunk 50 --lag 12 --poll 2 --from 158576600 --out $R/out/optimism \
      --rpc "https://optimism-rpc.publicnode.com#workers=12" --rpc "https://gateway.tenderly.co/public/optimism#workers=6" ;;   # history = op-reth datadir 2026-10-16 (head after 158250000 = 2026-10-15 21:12Z); mainnet.optimism.io lacks eth_getBlockReceipts; PublicNode serves only the recent window, Tenderly public serves older blocks
  unichain) exec $R/bin/emrpc-op --chain-id 130 --follow --chunk 200 --lag 30 --poll 2 --from 61675000 --out $R/out/unichain \
      --rpc "https://unichain-rpc.publicnode.com#workers=32" --rpc "https://mainnet.unichain.org#workers=8" ;;   # tip only: history 0..61669999 = VM range backfill (evm-history/bin/rpc_range.sh)
  polygon) exec $B --chain 137 --nitro-genesis 0 --weth none --procs 2 --parallel 3 --chunk 1000 --follow --follow-chunk 200 --lag 64 --poll 2s \
      --rpc "https://polygon-bor-rpc.publicnode.com#batch=1&workers=12" \
      --from $(resume $R/out/polygon 95775000) --out $R/out/polygon ;;   # history = PublicNode bor part tar to 95775111; 0x7f state-sync via eth_getRawTransactionByBlockNumberAndIndex
  # X Layer (196) OP-stack era from 42,810,021; history = VM ranges (evm-history/bin/xlayer_range.sh), skew = tip only
  xlayer) exec $R/bin/emrpc-op --chain-id 196 --follow --chunk 200 --lag 30 --poll 2 --from 73656000 --out $R/out/xlayer \
      --rpc "https://rpc.xlayer.tech#workers=4" --rpc "https://xlayerrpc.okx.com#workers=4" --rpc "https://196.rpc.thirdweb.com#workers=1" ;;
esac
