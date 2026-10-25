# em-extract / em-trace / em-rpc-extract

High-throughput extractor that reads an **Arbitrum Nitro chain database directly from disk**
(official snapshot: Pebble key-value store + geth ancient freezer, Nitro v3.x, incl. the
`archive-path` snapshot) and writes compact, verified, chunked **Parquet (zstd)** for wallet mapping.

* `em-extract` – blocks, transactions, logs, token transfers, contract creations, straight from
  `l2chaindata` (no node, no RPC). Every block is verified (header hash, parent link, tx root,
  receipt root, uncle hash, logs bloom) before it is written.
* `em-rpc-extract` – the same tables for blocks that are not in a snapshot (snapshot head -> tip,
  then `--follow`), fetched over JSON-RPC and verified exactly like `em-extract` (see below).
* `em-trace` – native-ETH internal transfers from `debug_traceBlockByNumber` + `callTracer`
  against a running Nitro archive node.

Both tools are chunked, parallel, atomic (tmp file + fsync + rename) and resumable through
per-chunk JSON manifests.

## Dependency: OffchainLabs go-ethereum fork

`go.mod` replaces `github.com/ethereum/go-ethereum` with the exact commit used by the latest
Nitro release:

| | |
|---|---|
| Nitro release | `v3.12.1` (2026-10-02), submodule `go-ethereum` |
| Fork commit | `OffchainLabs/go-ethereum@5eee8f546e7602b7deeef7909f291bbebc77e233` (2026-09-16) |
| Module pseudo-version | `v0.0.0-20260916182944-5eee8f546e76` |
| Pebble | `cockroachdb/pebble v1.1.5` (same as Nitro v3.12.1, pure Go) |

So all Arbitrum tx types decode with Nitro's own code: `ArbitrumDepositTx`(100),
`ArbitrumUnsignedTx`(101), `ArbitrumContractTx`(102), `ArbitrumRetryTx`(104),
`ArbitrumSubmitRetryableTx`(105), `ArbitrumInternalTx`(106), `ArbitrumLegacyTx`(120 – every
pre-Nitro Classic tx) plus legacy / 2930 / 1559 / 7702 types, and storage receipts in both the
Nitro format and the Classic-import format.

## Build

Go >= 1.24 (tested with go1.27.2). Binaries are static, pure Go (`CGO_ENABLED=0`).

```bash
# on the Mac (Go installed at ~/.local/go)
export PATH=$HOME/.local/go/bin:$PATH
cd ~/em-mapping/extractor
make linux-amd64          # -> bin/linux-amd64/em-extract, bin/linux-amd64/em-trace
# equivalent:
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags '-s -w' -o bin/linux-amd64/em-extract ./cmd/em-extract
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags '-s -w' -o bin/linux-amd64/em-trace   ./cmd/em-trace
CGO_ENABLED=0 go test ./...
```

Optional, on the VM itself (needs Go + gcc): `make native` builds with cgo, which switches geth's
signature recovery from the pure-Go decred secp256k1 to libsecp256k1. Sender recovery is ~57 %
of CPU in the pure-Go build (profiled), so this is the main speed knob; it was not measured here
because cgo linking is unavailable on the build Mac.

## How it reads the database (read-only)

* `--datadir` may be `.../l2chaindata`, its parent (`.../nitro`), or a snapshot root
  (`/data/arb1`); the tool locates `l2chaindata` (bounded search) and the chain freezer at
  `l2chaindata/ancient/chain` (or the legacy `l2chaindata/ancient`). `--ancient` overrides.
* **Frozen blocks** are read by an internal freezer reader (`internal/freezer`) that opens every
  index / data / meta file `O_RDONLY` and uses `pread`. It deliberately does not use
  `rawdb.NewChainFreezer`: geth's read-only freezer still takes a lock, `MkdirAll`s, requires all
  tables it knows (incl. the new `bals` table) and its repair path can delete "leftover" files.
  Reads are batched (`--batch` blocks per `pread` range per table) and snappy-decoded per item.
  Tail pruning (`meta` virtual tail / deleted items) and `flushOffset` are honoured.
* **Recent blocks** (not yet frozen, typically the last ~90k) are read from Pebble opened with
  `ReadOnly: true` through geth's rawdb accessors (canonical hash -> header / body / receipts).
  A goleveldb chain DB is detected and opened read-only too.
* Pebble takes its `LOCK` file even read-only (flock, no content change). Consequences: the
  snapshot directory must be writable for that one file, and **Nitro must not be running on the
  same directory** (the open fails, which is the desired protection).

## Verification (`--verify`, default true)

Per block, any failure marks the whole chunk `FAILED` with a reason; its Parquet files are
removed, nothing is ever silently skipped:

| check | reason code |
|---|---|
| block data present in freezer / kv | `MISSING_DATA` |
| header RLP decodes, `header.number == n` | `HEADER_RLP`, `HEADER_NUMBER` |
| `keccak(header RLP) == canonical hash` (hashes table / kv `number->hash`) | `HEADER_HASH` |
| `header.parentHash == hash(n-1)` within the chunk and against the canonical hash of `from-1` | `PARENT_HASH` |
| `DeriveSha(txs) == header.txRoot` | `TX_ROOT` |
| `CalcUncleHash(uncles) == header.uncleHash` | `UNCLE_HASH` |
| `len(receipts) == len(txs)`, status in {0,1}, cumulative gas monotonic | `RECEIPT_COUNT`, `RECEIPT_RLP`, `RECEIPT_GAS` |
| `DeriveSha(receipts) == header.receiptRoot` | `RECEIPT_ROOT` |
| `OR(bloom(receipts)) == header.logsBloom` (`--verify-bloom`, default on) | `LOG_BLOOM` |
| sender recovery | `SENDER` (or `--on-sender-error=zero`: 0x0 + counted/listed in the manifest, like Nitro RPC) |

`--verify=false` skips hash/root/parent checks (decode errors still fail the chunk). A manifest
produced without verification never satisfies a later verifying run (it is redone).

## Output layout and schema

```
<out>/blocks/blocks_<from10>-<to10>.parquet
<out>/transactions/transactions_<from10>-<to10>.parquet
<out>/logs/logs_<...>.parquet
<out>/token_transfers/token_transfers_<...>.parquet
<out>/contract_creations/contract_creations_<...>.parquet
<out>/manifests/<from10>-<to10>.json          # written last; marks the chunk done
```
Chunks are aligned to multiples of `--chunk` (e.g. `0000100000-0000199999`).

Encodings (also stored in each file's key/value metadata):

* addresses: `FIXED_LEN_BYTE_ARRAY(20)`; hashes / topics: `FIXED_LEN_BYTE_ARRAY(32)`.
* **uint256 amounts** (`value`, `deposit_value`, `amount`, `token_id`, trace `value`):
  `FIXED_LEN_BYTE_ARRAY(32)` **unsigned big-endian** – lossless for the full uint256 range,
  fixed width, and byte order == numeric order (min/max stats and sorting work). Python:
  `int.from_bytes(b, "big")`; DuckDB: `'0x' || lower(hex(amount))`.
* gas, gas prices, base fee, nonces: `INT64` annotated `UINT_64`. A value that does not fit
  uint64 fails the chunk (`UINT64_OVERFLOW`) instead of being truncated.
* Pointer fields below are nullable; zstd (level "default"), dictionary encoding on
  low-cardinality columns (`logs.address/topic0`, `token_transfers.token/standard`).

| table | columns |
|---|---|
| blocks | number, hash, parent_hash, timestamp, tx_count, gas_used, gas_limit, base_fee?, l1_block_number?, arbos_version?, send_count?, miner, is_classic, rlp_size |
| transactions | block_number, tx_index, hash, type, from, to?, value, deposit_value? (type 105 only: tx `value` is 0 there), nonce, gas_limit, gas_used, status, contract_address?, effective_gas_price?, gas_used_for_l1, method_id? (4 bytes), input_size, log_count |
| logs | block_number, tx_index, log_index (block-level, as RPC), address, topic0..topic3?, topic_count, data (**full, no cap**) |
| token_transfers | block_number, tx_index, log_index, batch_index, standard, token, from, to, operator?, token_id?, amount |
| contract_creations | block_number, tx_index, address, creator, tx_type (tx-level creations with status 1; internal creations come from em-trace) |

Derivation rules (mirroring Nitro's RPC, checked against arb1 RPC fixtures):

* `from`: `types.Sender` with the fork's `arbitrumSigner` (latest signer for the chain id):
  explicit `From` for types 100/101/102/104/105, ArbOS (`0x…a4b05`) for 106, the Sender override
  or ECDSA recovery of the wrapped legacy tx for 120, ECDSA otherwise.
* `gas_used`: cumulative-gas delta; for Classic (120) the stored per-tx gasUsed (Classic
  cumulative gas is not monotonic with gasUsed).
* `effective_gas_price`: Classic -> the stored `EffectiveGasPrice`; Nitro -> base fee, or
  `tx.EffectiveGasPrice(baseFee)` when the header's "collect tips" flag is set (ArbOS 9 / flag bit).
* `contract_address`: stored receipt value for 120 and Arbitrum types; `CREATE(sender, nonce)` for
  other creations. Present even for failed creations (RPC semantics); `contract_creations` only
  lists status-1 txs.
* `l1_block_number`: Nitro -> header mixDigest (ArbOS header info); Classic -> the first tx's
  L1 block number, carried forward over empty Classic blocks (walks back ≤ 50 blocks at chunk start,
  like `arbClassicL1BlockNumber`).
* `is_classic`: `number < 22207817` on arb1. Block 22207817 is the Nitro genesis (Nitro header,
  no txs, `l1_block_number = 0` as RPC reports).
* token_transfers (only for receipts with status 1; dirty address padding / bad ABI offsets are
  counted as `malformed`, other shapes as `transfer_shape_unrecognized`, never guessed):
  * `Transfer(address,address,uint256)` with 3 topics + 32-byte data -> `ERC20`;
    with 4 topics + empty data -> `ERC721` (token_id = topic3, amount = 1).
  * `TransferSingle` -> `ERC1155_SINGLE`; `TransferBatch` -> `ERC1155_BATCH`, one row per
    element (`batch_index`), arrays ABI-decoded with bounds and equal-length checks.
  * `Deposit(address,uint256)` / `Withdrawal(address,uint256)` -> `WETH_DEPOSIT` (from = 0x0) /
    `WETH_WITHDRAWAL` (to = 0x0), **only for allowlisted emitters** because the same signature is
    used by many vaults: default allowlist on 42161 is `0x82aF49447D8a07e3bd95BD0d56f35241523fBab1`;
    `--weth a,b,c` replaces it, `--weth any` decodes every emitter, `--weth none` disables.
    Note Arbitrum's canonical WETH (aeWETH) mints/burns via ERC20 `Transfer` from/to 0x0, which is
    already captured as `ERC20`; dedupe on `(block_number, log_index)` if you enable `any`.

### Manifest (`manifests/<range>.json`)

`tool, version, dependency, chain_id, range{from,to}, status (OK|FAILED), failure{block,reason,detail},
verify{enabled, checks[], blocks_verified, prev_chunk_link}, counts{table:rows},
files{table:{path, rows, bytes, sha256}}, stats{tx_types, classic_blocks, kv_blocks, failed_transactions,
token_decode, rlp_bytes, disk_bytes, read_sec, decode_sec, write_sec, sender_errors},
warnings[], elapsed_sec, blocks_per_sec, created_at, extra{first_block_hash, first_parent_hash, last_block_hash}`.

**Resume**: a chunk is skipped iff its manifest exists, matches tool/chain/range, has status `OK`,
was verified (when `--verify`), and every listed file exists with the recorded size and sha256
(`--resume-check=size` skips re-hashing). Otherwise it is redone from scratch (stale files and
`*.tmp-*` are removed first). `--force` redoes everything. Exit code 2 if any chunk failed.

## em-trace

```
em-trace --rpc http://127.0.0.1:8547 --chain 42161 --from F --to T --workers N --chunk 10000 --out DIR
```
Per block: `debug_traceBlockByNumber(n, {"tracer":"callTracer","tracerConfig":{"onlyTopCall":false}})`
(`--tracer-timeout 120s` adds the tracer `timeout` option; default node timeout is 5 s).

`native_internal` rows: `block_number, tx_index, tx_hash, trace_path, depth, call_type, from, to, value`
* only frames at depth > 0 with value > 0 of type `CALL`, `CREATE`, `CREATE2`, `SELFDESTRUCT`;
* `DELEGATECALL` / `CALLCODE` / `STATICCALL` frames never emit (their children still can);
* a frame with `error` **and its whole subtree** are excluded; failed txs (top frame `error`) are
  excluded entirely;
* `trace_path` = dotted child indices from the tx's top frame (`"3.0"` = first child of the 4th
  child), `depth` = number of components.

`internal_creations` (extra, useful for EOA-vs-contract): successful `CREATE`/`CREATE2` frames at
depth > 0 regardless of value: `block_number, tx_index, tx_hash, trace_path, call_type, creator, address, value`.

Blocks `< 22207818` on arb1 (Classic + Nitro genesis, `--trace-start` to override) are never
called: they are recorded in the manifest as `trace_unavailable{from,to,count,reason:"CLASSIC_PRE_NITRO"}`
(a chunk that is entirely Classic gets status `TRACE_UNAVAILABLE` and empty files). Manifests also
carry `stats.rpc_latency{calls, retries, bytes, mean_ms, p50_ms, p90_ms, p99_ms, max_ms}` and filter
counters (failed txs, reverted subtrees, delegate/static/callcode value frames, Nitro
`before/afterEVMTransfers` seen – these are not EVM frames and are not emitted). Transport errors
and RPC errors are retried (`--retries`, exponential backoff); persistent errors fail the chunk
(`RPC_ERROR` / `RPC_TRANSPORT` / `TRACE_DECODE`). The RPC URL is never written to logs/manifests
beyond scheme + host.

## em-rpc-extract (tail blocks over JSON-RPC)

```
em-rpc-extract --chain 4663 --rpc 'URL[#batch=B&workers=W&rps=R]' [--rpc ...] --from F [--to T] \
               --chunk 10000 --parallel 6 [--follow --follow-chunk 1000 --lag 20] \
               [--link-dir /data/out/robinhood] --out DIR
```

Per block it fetches `eth_getBlockByNumber(n, true)` + `eth_getBlockReceipts(n)` (JSON-RPC batches,
`--batch` blocks = 2·batch calls per HTTP request; `0` = no batching), rebuilds the **raw database
representation** with the fork types (`internal/rpcsrc/convert.go`, the production version of the
fixture rebuild: header from JSON, txs via the fork's `Transaction.UnmarshalJSON` incl. types
100–106, Classic 120 via `NewArbitrumLegacyTx` with the receipt's effectiveGasPrice / l1BlockNumber
and a sender override for unsigned txs, storage receipts incl. Arbitrum contractAddress and
`multiGasUsed`) and feeds it to the **same decoder, writers and manifests** as `em-extract`
(`extract.Run` takes any `extract.Source`). So every block is verified (keccak(header) == RPC hash,
parent link, tx root, uncle hash, receipt root, bloom) and the output is interchangeable:

* tests (`internal/rpcsrc`): on the synthetic Nitro DB served by a fake RPC with injected 429 /
  Retry-After, batch-size rejection, nulls and per-item rate errors, the rebuilt header/body/receipt
  RLP is byte-identical to the DB, the Parquet files are **byte-identical (same sha256)** to
  `em-extract`'s and `em-extract` resume accepts the chunks; tampered receipts fail the chunk.
* real data: Robinhood blocks 84,000,000–84,009,999 re-extracted from a node equal the snapshot
  chunk rows in all five tables (DuckDB `EXCEPT ALL` both ways = 0 rows).

Manifests keep `tool: em-extract` (so resume/validation is shared) and add
`extra.source = "rpc"`, `extra.source_tool = "em-rpc-extract"`, `extra.rpc_endpoints` (scheme+host
only; API keys in URLs are never logged). After each run the chunk links
(`first_parent_hash` == previous `last_block_hash`) are checked across `--out` and every `--link-dir`
(e.g. the em-extract snapshot output, so the first tail block is anchored to the snapshot head);
`--link-dir` manifests are only read.

Endpoints / throttling: every endpoint has its own workers that pull blocks from one shared queue
(faster endpoints take more work; failed blocks go back to the front and may be served by another
endpoint). HTTP 429 / 503-rate / JSON-RPC rate errors (-32005, -32029, "rate limit", ...) pause the
endpoint for exactly `Retry-After` when given; without it an AIMD pacer cuts that endpoint's
request rate (only when 429s repeat) and grows it again while requests succeed. "Batch too large" /
`-32003 response too large` halve that endpoint's batch (down to unbatched). `null` results (endpoint
behind) are soft retries. Nothing is ever skipped: a block that still fails after `--retries` real
errors (or `--rate-retries` throttled attempts) fails its chunk (`MISSING_DATA` with the RPC error),
which a rerun redoes (resume = same rules as em-extract).

`--to` omitted = lowest endpoint head − `--lag`. `--follow`: after the bulk range, poll the head every
`--poll` (2 s) and write aligned `--follow-chunk` chunks once they are `--lag` blocks below the head;
with `--to` it stops there (useful with a syncing local node). `--bench N --bench-time 40s
--max-calls 2800` measures blocks/s per endpoint without keeping output. `--procs` caps cores.

Measured 2026-10-17 (verification passed in every run; blocks/s, batch/workers per endpoint):

| endpoint | from Mac | from Azure VM |
|---|---|---|
| RH `robinhood-rpc.publicnode.com` b1 | 133 (w8), 361 (w24) | **745 (w24)** |
| RH `robinhood.drpc.org` b1 (free: ≤3 calls/batch) | 49 (w8), 153 (w24) | **364 (w24)** |
| RH `4663.rpc.thirdweb.com` b10 w8 | 12 | |
| RH `rpc.mainnet.chain.robinhood.com` b10 w8 | 2.3 (429s) | |
| RH local Nitro node b20 w8, 4 cores | | 1,201 |
| ARB `arbitrum.drpc.org` b1 | 61 (w8), 154 (w24) | 154 (w24), **220 (w48)** |
| ARB `arb-one.api.pocket.network` b10 | 25 (w8/w24, capped) | ~25–130 burst |
| ARB `42161.rpc.thirdweb.com` b10 w8 | 20 | 21 |
| ARB `arbitrum-one-rpc.publicnode.com` b1 w8 | 10 | 20 (≈10 req/s cap) |
| ARB `arbitrum.gateway.tenderly.co` b10 w8 | 4 | |
| ARB `arb1.arbitrum.io/rpc` b10 w8 | 1.7–2.5 (429s) | |
| ARB local Nitro node b20 w8, 4 cores | | 2,252 |
| ARB 5 public endpoints combined (one IP) | | 377 (40 s burst) |

Not usable without a key / plan: blastapi (no eth_getBlockReceipts), 1rpc, nodies, onfinality,
blockpi, ankr, zan, meowrpc (0.3 blocks/s), lava (discontinued). Limits are per IP, so jobs on
different machines add up. CPU: ≈1.3 ms (arb1) / ≈2.9 ms (Robinhood) per block for JSON decode +
verify + Parquet.

## Azure VM runbook (NVMe RAID at /data, snapshot extracted to /data/arb1)

```bash
# copy binaries
scp bin/linux-amd64/em-extract bin/linux-amd64/em-trace azvm:/data/bin/
ssh azvm
ulimit -n 1048576            # the tool also raises its soft limit to the hard limit
mkdir -p /data/em

# 1) sanity: detection, frozen range, head, stored chain config
/data/bin/em-extract --datadir /data/arb1 --chain 42161 --info

# 2) benchmark (no output kept): recent-history and Classic samples
/data/bin/em-extract --datadir /data/arb1 --chain 42161 --bench 2000000 --from 400000000 --workers 60 --out /data/em/bench
/data/bin/em-extract --datadir /data/arb1 --chain 42161 --bench 2000000 --from 10000000  --workers 60 --out /data/em/bench

# 3) full extraction (resumable: rerun the same command after any interruption)
nohup /data/bin/em-extract --datadir /data/arb1 --chain 42161 --from 0 \
      --workers 60 --chunk 100000 --out /data/em/arb1 > /data/em/extract.log 2>&1 &
grep -E 'FAILED|summary' /data/em/extract.log

# 4) traces (after em-extract finished; then start Nitro on the snapshot as an archive node
#    with the debug API, e.g. --http.api=eth,net,web3,arb,debug, listening on 127.0.0.1:8547)
HEAD=$(curl -s -X POST -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}' http://127.0.0.1:8547 | jq -r .result)
nohup /data/bin/em-trace --rpc http://127.0.0.1:8547 --chain 42161 --from 22207818 --to $((HEAD)) \
      --workers 32 --chunk 10000 --tracer-timeout 120s --out /data/em/arb1-trace > /data/em/trace.log 2>&1 &
```

Chain 4663: same commands with `--chain 4663` (no Classic range; trace start 1; pass
`--weth <addr>` for its WETH, there is no default allowlist).

Recommended worker counts:

* `em-extract`: `--workers` = vCPUs − 4 (60 on a 64-vCPU VM). The job is CPU-bound (decode +
  verify ≈ 98 % of worker time in the benchmark, I/O ≈ 1.5 KB/block read from disk); lower it only
  if `iostat -x 5` shows the RAID saturated. `--chunk 100000` (≈ 5 150 chunks for arb1) keeps
  memory at roughly ≤ 100 MB per worker.
* `em-trace`: the node does the work; start with 16–32 and increase while node CPU has headroom
  and p99 latency in the manifests stays well below `--http-timeout`.

### Throughput (measured vs. assumed)

Measured on the build Mac (Apple M5, 4P+6E cores, pure-Go build) with a synthetic Nitro-shaped DB
(7.2 tx and 20 logs per block, 95 % frozen): 814 blocks/s with 1 worker, 2 346 with 4, 3 486 with
10; bytes/block: RLP 5 493, disk read 1 554, Parquet 392. CPU profile: secp256k1 sender recovery
≈ 57 %, keccak/RLP/trie/bloom most of the rest, Parquet writing ≈ 2 %.

Assumption for the >20k blocks/s target on 64 vCPUs: ~400–600 blocks/s per x86 vCPU on
recent-history blocks (≈4–7 signed txs/block) in the pure-Go build -> ~25–38k blocks/s; Classic and
early-Nitro blocks (1–2 txs) are several times faster; NVMe RAID ≥ 1 GB/s (needed ≈ 50 MB/s);
`make native` (libsecp256k1) adds headroom. Verify with `--bench` before the full run.

## Tests

`CGO_ENABLED=0 go test ./...`

* `internal/decode`: **real arb1 fixtures** (`testdata/rpc/*.json`, captured with
  `eth_getBlockByNumber`+`eth_getBlockReceipts`; `debug_getRaw*` is not exposed on the public RPC)
  for Classic blocks 1 000 000 and 22 204 650, Nitro genesis 22 207 817, first Nitro block
  22 207 818, retryable block 22 209 702 (types 104/105/106) and recent block 515 626 437: headers,
  txs and storage receipts are rebuilt with fork types and pushed through the production decoder
  with verification on; the official block hash, tx root, receipt root and bloom are reproduced and
  every derived field (from, to, value, nonce, gas, gasUsed, status, contractAddress,
  effectiveGasPrice, gasUsedForL1, log indices, l1BlockNumber, sendCount) equals the RPC value.
  Plus corruption detection, ERC20 vs ERC721 by topic count, ERC1155 single/batch (incl. malformed
  arrays), WETH allowlist, Arbitrum tx-type senders (100/101/102/104/105/106/120 + legacy/1559)
  after a body-RLP round trip, sender-error policy.
* `internal/freezer`: byte-for-byte equality with geth's own freezer, tail pruning, a hand-built
  multi-file table with deleted tail items.
* `internal/extract`: synthetic Nitro DB (Classic + Nitro, freezer + Pebble, written by geth),
  auto-detection, full run, resume (all skipped), tampered file / missing manifest -> only those
  chunks redone, no-verify manifests not accepted, receipt-root corruption -> `FAILED` chunk with
  reason, no files left, retried on resume.
* `internal/rpcsrc`: fixture conversion through the production converter, fake-RPC end-to-end
  equality with em-extract (byte-identical Parquet), byte-identical raw RLP, tampered endpoint ->
  FAILED chunk, Retry-After honoured, endpoint option parsing.
* `internal/trace`: reverted-subtree / delegatecall / failed-tx filter on constructed callTracer
  JSON, tracer errors, Classic `TRACE_UNAVAILABLE` without RPC calls, resume, RPC failure +
  latency stats (fake node).

`go run ./internal/testchain/gen -out /tmp/synth/l2chaindata -blocks 40000 -frozen 38000 -nitro-genesis 2000`
builds a synthetic DB for smoke tests / local benchmarks (`--nitro-genesis 2000` on em-extract).

## Known limitations / risks

* Not yet run against the real arb1 snapshot (no access from the build machine). The storage
  formats are decoded by the same fork code Nitro v3.12.1 uses, and roots are re-verified per
  block, so a format surprise shows up as a `FAILED` chunk with a reason, not as bad rows.
* Pebble must be able to take its `LOCK` file (writable dir, Nitro stopped). A snapshot produced by
  a newer Pebble format than v1.1.5 would fail to open (Nitro v3.12.1 uses the same version).
* Internal (contract-initiated) creations and native-ETH internal transfers are only available
  via em-trace (requires an archive node); Nitro `before/afterEVMTransfers` (fee/deposit
  bookkeeping) are counted but not emitted.
* `effective_gas_price` follows Nitro RPC semantics (base fee when tips are not collected),
  not the tx's max fee.
