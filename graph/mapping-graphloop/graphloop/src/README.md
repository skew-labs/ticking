# Economic Machine graph-loop: native core (em-graphloop-v1)

Code-first wallet-map labeling. A deterministic C++ classifier settles about 97–99 % of nodes. Only uncertain **and** important residual nodes, plus high-impact code candidates, are leased to LLM agents. Leases come as aliased, compact batch packs. The answers are de-aliased, validated deterministically, committed to an fsync'd journal, and propagated to neighbours (the graph loop).

```
mapping Parquet (addresses, edges, emitter_topics, token_stats, dex_pools, dex_trades)
   + econ_actions (optional) + econ/protocols registry
      │  glbuild  (Arrow/Parquet -> CSR + SoA + topics; classifier; sealed, sha256-pinned image)
      ▼
graph image  data/<chain>/graph-<id12>/*.seg + manifest.json      (immutable, mmap or --ram)
      │  glserve  (HTTP/1.1 JSON, 127.0.0.1 or Unix socket)
      ├─ label overlay (8 B/node, lazily backed) ── propagation thread (CAS, deterministic rules)
      ├─ frontier ×2 (address_label, protocol_discovery): 64 shards, priority = uncertainty × importance
      ├─ batches: aliased packs (S/A/X/T/E), alias map kept server-side
      ├─ validators (port of validate.py + protocol.py; CryptoPP Keccak-256)
      └─ WAL (group-commit fdatasync) + snapshots → crash-safe restart
      │  glexport (read-only replay)
      ▼
labels.parquet, label_deltas.parquet, bridge_legs.parquet, protocol_mappings.parquet, labels.jsonl (Galaxy)
```

## Conventions adopted (from `skew:/srv/skew/economic-machine-*`, read-only study)

| Area | Existing EM native code | graph-loop |
|---|---|---|
| Language/build | C++20, plain `g++ -std=c++20 -O2 -I. ... -lcurl -lcryptopp -lsqlite3 -lzstd`; tests at `-O1`, plus a `conformance-sanitize` binary (pump-fast-v1) | C++20 `Makefile` with the same plain g++ lines; `test_graphloop` (-O1) and `test_graphloop-sanitize` (ASan+UBSan) |
| Errors | `need(ok,"reason")` → `std::runtime_error`, short secret-free reasons; `main` try/catch → stderr + exit 1 | identical `need()`; reason codes reuse the validate.py names |
| Output | single JSON object on stdout (probe/bench results) | the same; operational log as JSON lines on stderr |
| Hashes | CryptoPP `Keccak_256` (Ethereum keccak), SHA256; FNV-1a "Fingerprint" only as a diagnostic identity | the same. Keccak is self-tested against the Transfer topic0 at build time, and FNV/mix64 is used only for sharding |
| Durability | `seal_file` (mkostemp → fdatasync → link → unlink → dir fsync), `sync_directory`, `O_NOFOLLOW\|O_CLOEXEC`, `writer.lock` flock `LOCK_EX\|LOCK_NB`, sha256 receipts/pins with `"schema": "economic-machine.…"` | the same helpers (`durable.hpp`). The graph image, snapshots and exports are sha256-pinned manifests (`economic-machine.graphloop.{graph,snapshot,export}.v1`), and the state dir has a single writer |
| Bounds | every input bounded (bodies, items, strings) | HTTP header 64 KiB, body 16 MiB, ≤ 4096 tasks/lease, strings capped in the WAL, loopback-only listener |
| Style | namespaces `machine::economics` / `ticking::evm`, snake_case, lowercase enums, `static_assert` on wire structs | `machine::graphloop`; `static_assert` on segment/label structs |

Deliberate deviations:
- An append-only WAL plus snapshots replaces SQLite, for group-commit throughput.
- An epoll HTTP/1.1 server (with a Unix socket option) replaces the SEQPACKET wire, because the dispatcher is HTTP-based.

## Arrow vs DuckDB export: Arrow chosen

Apache Arrow/Parquet C++ **26.0.0** comes from the official Apache Arrow APT repo (`libarrow-dev libparquet-dev`, Ubuntu 24.04). glbuild reads projected columns one row group at a time and consumes the Arrow buffers directly, with no per-row materialisation (addresses are `BinaryArray::GetView` into the data buffer). The intermediate format a DuckDB export would add is unnecessary.

Only `glbuild`, `glexport` and `glsynth` link Arrow. **`glserve` links only libcryptopp, libzstd and libstdc++**, and serves the mmap'd image. A small serving VM therefore needs no Arrow.

## Graph image (memory layout)

- **Node ids**: 32-bit node id = rank in sorted address order. The address dictionary is a sorted `addr[N]` (20 B) plus a radix `prefix[2^b+1]` over the first b bits (b ≈ log2 N − 3). Lookup costs one prefix load plus about 4 compares.
- **CSR, both directions**:
  - out: `out_off[N+1]` u64, `out_dst[M]` u32, `out_w[M]` u16, `out_t[M]` u8 (type in 5 bits plus `multi_asset` and `econ` flags), `out_asset[M]` u32 (cold).
  - in: the same arrays, without asset.
  - Weights: exact below 32768, log-quantized above (<0.07 % error).
  - Lists are sorted by weight desc, so top-k = first k entries. Duplicate (from,to,type) rows across assets are collapsed, keeping the max-n asset and the multi-asset flag.
- **Edge types**: native, erc20, erc721, erc1155, dex_swap, lending_supply, lending_borrow, lp, bridge, perp, stake, wrap, liquidation, other. The typed econ edges come from `econ_actions` (wallet↔protocol, money direction) when that input is present.
- **Features**: columnar SoA.
  - Dense: tx/token counters, degrees, first/last time, ETH/USD-proxy volume, top-1/top-2 out-share, top-1 in-share, importance.
  - Sparse `(node,key,value)` table for rare attributes: logs, creator, router/sandwich/arb counts, token holders, pool protocol, registry entity, sweep fan-in.
- **Events**: emitted-topic CSR (emitters only), topic dictionary with family bits and names, and topic→emitters CSR (re-enqueue when a protocol mapping is learned).
- **Base labels**: 8 B/node {category, conf, source, subtype, tier, rule}. The mutable overlay is 8 B/node in anonymous `MAP_NORESERVE` memory, so it costs RAM only for touched pages.
- **Measured sizes**:
  - Image: **≈128 B/node + 18 B/edge** (14 B hot + 4 B cold asset id).
  - Serving RSS with `--ram`: ≈ 128 B·N + 14 B·M + frontier (≈ 70 B per frontier member).

## Deterministic classifier (`classify.hpp`)

Rules run in priority order, with no clocks and no randomness:
- Registry (`econ/protocols/contracts.csv`, conf .99).
- System addresses.
- Topic families: Swap/Sync → dex_pool, lending events, bridge events, Safe events, ERC-721/1155 → nft, Transfer + token_stats → token (weth/stable/4626/aToken/stock subtypes), ERC-4337, perp, LP manager, oracle, factory, distributor.
- `dex_trades.router` ≥ 50 → dex_router.
- MEV scan over `dex_trades`:
  - cyclic arbitrage: same tx, first token_in = last token_out;
  - sandwich: same pool+block, X buys → Y same direction → X sells.
  - It also links an operator EOA to its private contract.
- Inferred code: never sends txs but moves ≥ 50 token transfers out (contracts deployed before the mapped range).
- CEX sweep: deposit-like addresses send ≥ 80 % of outflow to ≤ 2 counterparties (the same rule as the validator). A hub with ≥ 25 such sweepers, out-degree ≥ 50 and sent ≥ 100 → cex_hot_wallet candidate, and its sweepers → cex_deposit.
- Whale tiers by USD proxy (ETH, WETH and stablecoin flows).
- EOA default.
- Receive-only.

Hub-derived labels (hot wallets, fan-in/fan-out hubs, MEV links, whales) are *candidates*. When they are important they go to the LLM for confirmation instead of being final.

## Frontier, leases, aliasing

- **Shards**: 64 frontier shards (mutex each), a lazy-deletion max-heap, and a cached top priority per shard. lease() picks the globally best shard without a global lock.
- **Per-node lease**: TTL 600 s by default. A node changed while leased is marked dirty and re-queued on release. Expired leases are re-queued by a 250 ms timer.
- **Dedupe**: `input_hash` = sha256 of the *de-aliased* facts plus counterparty labels. An identical input that was already answered is never re-leased. A rejected answer is retried at most 3 times, with priority × 0.7 per attempt.
- **Priority**: `(1 − conf + 0.25·candidate + 0.5·disputed) × importance`.
  - Importance = ln(1+activity) + ln(1+counterparties) + ½ ln(1+USD/100) + ½ ln(1+sweep).
  - Thresholds: `--llm-quantile` (default q99 of importance), `--confirm-quantile` (q99.9) and `--resolve` (.50) are exposed. Absolute values via `--llm-importance-min` etc.
- **Aliases**: per batch: `S1..Sn` subjects, `A` EOA/receive-only, `X` contracts, `T` tokens, `E` topics. A subject that appears as a counterparty is shown as its `S<k>`. Tools accept and return aliases of the batch, and the alias map grows as tools reveal new nodes.
- **Pack format**: a compact `key=value` fact line per subject plus a shared legend.
  - The vocabulary is defined once in `agent_context.md`, the stable system prefix.
  - Evidence = fact keys or alias names. The server stores the resolved values with real addresses.

## Validators (`validate.hpp`)

Port of `validate.py`/`protocol.py`, keeping the same reason codes:
- Category enum, confidence range.
- `EVIDENCE_KEY_NOT_IN_PACK`, `EVIDENCE_EMPTY`, `ENTITY_NAME_NOT_IN_PACK`.
- Contract-only vs EOA categories (type C/E/R from code evidence).
- token needs an emitted Transfer. dex_pool needs Swap, and V2 needs Sync. A router must not emit pool events. NFT needs transfer events.
- cex_deposit needs sent ≥ 1, top-2 out share ≥ X (0.80) and sent ≤ max(500, 2·recv).
- hot wallet needs sent ≥ 50 and cp ≥ 20. mev needs ≥ 100 txs.
- Soft warnings for lending/bridge/safe events and whale volume.
- New: `REGISTRY_CONFLICT`.
- Protocol discovery:
  - Signature canonicalisation, then **CryptoPP Keccak-256(signature) == topic0**.
  - Layout check: indexed count vs data words, with subset enumeration for static types.
  - Field roles `tN`/`dK`/`em` must index real topics/data words of the sample log, and wallet/asset must be address-shaped words. Without a sample log, roles must be empty.

## Propagation (native, deterministic, CAS-safe)

Rules, run on an ordered background thread. Overlay writes use compare-and-swap, so they never overwrite an agent label:
- Confirmed cex_hot_wallet → its deposit-like sweepers become cex_deposit candidates, conf × 0.9 × share.
- An agent rejecting a classifier hub demotes the deposits that hub produced.
- ≥ 2 agent-confirmed deposits sweeping into a hub → hot-wallet candidate (0.55 + 0.1·k).
- A confirmed MEV contract ↔ its operator EOA.
- A confirmed entity → contracts of the same deployer ("sibling" candidate, conf × 0.8).
- Bridge-labelled contracts produce bridge legs (pack fact `br`, export `bridge_legs.parquet`).
- Learned protocol mappings → other emitters of that topic0 are re-enqueued.

Confidence decays per hop (depth ≤ 2, floor .30). Changed candidates and their top-32 neighbours go back to the frontier; they are never auto-final. After recovery a full pass in ascending node id rebuilds the same state. State = f(base, agent labels).

## Durability

- **WAL**: `state/wal/wal-<seq>.log`, frames `[len][crc32c][seq|ts|kind|body]`. One record per proposal (all task verdicts, evidence and usage), written by a group-commit thread (2 ms window). It runs `write` + `fdatasync`, and only then acknowledges the HTTP proposal.
- **Snapshots**: `state/snap/snap-<seq>.{bin,json}`, sealed and sha256-pinned, every 200k commits or 10 min and at shutdown/recovery. The commit lock is held only to fix S and rotate the WAL. Records are read in chunks under a shared lock, and replay is idempotent per node seq. Older WAL files and all but the last 2 snapshots are removed.
- **Recovery**: latest valid snapshot → replay WAL seq > S. A torn tail (partial frame or CRC mismatch) of the newest file is truncated. WAL records carry 20-byte addresses, so labels survive a graph rebuild (re-resolved by address).
- **Exports**: `glexport` replays read-only (safe while glserve runs) and writes Parquet via tmp + fdatasync + rename, plus a sealed `export-<seq>.json`. Upload with the existing `azcopy` MSI pattern (`blob-sync.sh`) when wanted.

## Tool API (HTTP/1.1 JSON on 127.0.0.1:PORT, or `--unix PATH`)

| Method | Path | Body / query | Result |
|---|---|---|---|
| POST | `/tasks/lease` | `{"n":15,"batch_size":15,"task_type":"address_label"\|"protocol_discovery","ttl_s":600,"agent":"w17"}` | `{"batches":[{"batch_id","task_type","prompt_version","context_sha256","lease_until_ms","user","tasks":[{"task_id","subject":"S1","input_hash","keys":[...]}],"est_input_tokens"}]}` |
| POST | `/proposals` | `{"batch_id","provider","model","usage":{"prompt_tokens","completion_tokens","cached_tokens"},"output":"<raw model content or JSON>"}` | after fsync: `{"accepted","rejected","missing","results":[{"task_id","subject","address","verdict":"accepted\|accepted_not_applied\|rejected\|missing","category","confidence","reasons":[...],"warnings":[...]}]}` |
| POST | `/tasks/release` | `{"batch_id"}` | release the leases (provider failure) |
| GET | `/tools/neighbors` | `batch=&ref=S1\|X3` or `addr=0x…`, `dir=out\|in\|both`, `types=erc20,native`, `limit=16` | top neighbours `{ref,type,category,confidence,entity,edge,n,share}` |
| GET | `/tools/path` | `batch=&from=&to=` (aliases or addresses), `max_depth≤8`, `types`, `undirected=1` | bounded bidirectional typed BFS: hubs not expanded, fan-out 128 (1024 at endpoints), budget 20k |
| GET | `/tools/feature_pack` | `batch=&ref=` or `addr=`, `task_type` | the same fact line and legend as a lease |
| GET | `/status` | | graph, thresholds, frontier sizes, commits/s (10 s, 60 s), per-provider/model accept rate and tokens/task, reject-reason histogram, WAL, snapshot, tool latency, RSS |
| GET | `/coverage`, `/labels?addr=`, `/context`, `/admin/sample?category=&k=`, `/admin/frontier_top?k=`; POST `/admin/snapshot` | | coverage %, one label with agent evidence, the system prompt, inspection |

Output schemas accepted by `/proposals` (tolerant parser; code fences stripped):
- address_label: `{"r":[[s,c,q,["key",...],"entity"]]}`. Strict JSON-schema form: `{"r":[{"s":int,"c":int 0-13,"q":int 0-9,"e":[string],"n":string}]}`. q → confidence q/10+0.05.
- protocol_discovery: `{"p":[[s,f,q,[["E1","Sig(...)",a,"wallet","asset","amount"]],["key",...]]]}`.

## Measured numbers (`arb`, AMD EPYC 9V74 Zen 4, NVMe RAID; serve on 4 workers / one CCD)

| | Arbitrum r022 (real, 1 range) | synthetic 5M/50M | Robinhood-scale synthetic 27M/365M |
|---|---|---|---|
| nodes / CSR edges | 10,581,407 / 37,891,348 | 5,000,000 / 49,996,300 | 27,000,000 / 364,991,931 |
| build (threads), peak RSS | 21.9 s (16), 5.1 GB | 13.3 s (16), 5.8 GB | 86.7 s (32), 28.7 GB |
| image | 2.05 GB (193 B/node incl. edges) | 1.54 GB | 10.02 GB (128 B/node + 18 B/edge) |
| classifier throughput | 8.3 M nodes/s | 9.1 M nodes/s | 8.9 M nodes/s |
| resolved by code (conf ≥ .50) | **99.03 %** | 96.9 % (planted recall 100 % on all 8 planted classes) | 96.8 % |
| LLM frontier @ q99 importance | 4,704 address + 2,986 protocol (0.07 %) | | |
| graph open / ready | 125 ms mmap / 2.5 s (frontier fill) | | 5.7 s `--ram` load, frontier 27M in 11.6 s (stress mode) |
| lease+propose (32 clients, batch 15, fsync WAL) | **25,546 tasks/s**; lease p50 5.1 ms / p99 39 ms; propose p50 9.0 / p99 59 ms | 23,402 tasks/s | **21,346 tasks/s**; lease p50 5.2 / p99 15 ms; propose p50 9.0 / p99 28 ms (13,064/s with a 14-core DuckDB job on the same CCD) |
| tool calls (neighbors/path/feature_pack mix) | | server p50 ≤ 65 µs, p99 ≤ 524 µs; client p50 88 / p99 736 µs | server p50 ≤ 65 µs, p99 ≤ 262 µs; client p50 88 / p99 433 µs (`--ram` under page-cache churn: client p99 764 µs) |
| serve RSS | 186 MB idle (mmap) → 3.1 GB with 10.5M-member stress frontier | 2.3 GB stress | 9.0 GB `--ram` (normal thresholds) |
| WAL | ≈ 6 proposal records / fdatasync | | |
| crash (kill -9 mid-load) | 171,144 and 234,855 acked commits → **0 missing**. Recovery 494 ms (snapshot 224k records + 651 WAL records); restart 5.1 s | | |
| export | 21.6 s, 1.55 GB RSS (1.04M label rows, 234k deltas, 118k bridge legs) | | |

Tests: `make test` → 284 checks (keccak vs SHA3, CRC32C, signature canonicalisation, layout/field-role checks, every validator rule, output parsing, aliasing, frontier lease/TTL/dirty/dedupe, WAL torn tail, snapshot sha256 rejection). They pass in both the -O1 and ASan/UBSan builds. `tests/crash_test.sh DATA PORT 15` runs the kill -9 test, and `EXTRA="--snapshot-every 20000"` exercises the snapshot+WAL path.

Data notes:
- The real Robinhood map (26.9M addresses / 365.1M edges / 19.3M topic rows, on `rh`) could not be moved to `arb`. The Mac relay runs at 2.4 MB/s, and staging through Blob was denied by policy. The synthetic map at the same scale was used instead.
- `arb_r022` is a single 10M-block range. Contracts deployed earlier have no creation row there, which is why the inferred-code rule exists.

## LLM cost (measured pack sizes, batch of 15)

- **Input**: address_label ≈ **106 tokens/node**. protocol_discovery ≈ 174 tokens/node, mostly the full topic0 hashes. The system prompt (`agent_context.md`, 7.6 kB) is ≈ 1,900 tokens, ≈ 127/node uncached, and is identical for every provider, so prompt caching applies.
- **Output**: ≈ 15–25 tokens/node (codes, not prose). Muse adds ≈ 250–500 hidden reasoning tokens per *request* (dispatch.py note), i.e. ≈ 27/node.
- **Prices**: from dispatch.py, Luna $0.10/$0.50 and Muse $0.10/$0.20 per M in/out. DeepSeek v4.1 flash on BAZE is **not priced yet**; $0.10/$0.40 is assumed, plus BAZE's ~5-credit masking fee per call (amortised over 15 nodes).

| per 100k nodes | input (no cache → cached prefix) | output | total |
|---|---|---|---|
| Luna | $2.33 → $1.38 | $0.85 | **$2.2–3.2** |
| Muse | $2.33 → $1.38 | $0.88 | **$2.3–3.2** |
| DeepSeek (assumed price) | $2.33 → $1.19 | $0.68 | **$1.9–3.0** + BAZE fee × 6,667 calls |

At q99 the r022 frontier is about 7.7k tasks (≈ $0.2). Full Arbitrum at a similar residual rate (≈ 0.1 % at q99–q90) is ≈ 0.1M tasks ≈ $3. Labelling all residual nodes (≈ 1–3 %) costs ≈ $30–90.

## Small-VM sizing (after the big VMs are deleted)

- **Serving RAM (`--ram`, latency-safe)** ≈ 128 B·N + 14 B·M + frontier + ~1 GB.
  - Robinhood (27M/365M): **≈ 9–10 GB measured**.
  - Full Arbitrum: *estimate*, N ≈ 80–120M, M ≈ 1–2B aggregated edges, giving ≈ 25–45 GB.
  - **Both chains: 64 GB VM** (e.g. Azure E8as_v5).
- **mmap mode** runs on less (32 GB) at higher tail latency, because cold pages come from NVMe.
- **Building** is the heavy step: ≈ 80 B per input edge row (28.7 GB for 365M). A full-Arbitrum image (≈ 1.5–2B rows → ≈ 120–160 GB peak) **must be built on `arb` before deletion**. Ship the sealed image directory (≈ 128 B/node + 18 B/edge on disk) and the `state/` dir to the small VM.

## Build and run

```bash
# deps (Ubuntu 24.04): Apache Arrow APT repo, then
sudo apt-get install -y libarrow-dev libparquet-dev libcrypto++-dev libzstd-dev nlohmann-json3-dev
cd /data/graphloop/src && make -j8 && make test
# build an image (registry = econ/protocols/{contracts,events,topic_dict}.csv)
build/glbuild --chain robinhood --chain-id 4663 --map /data/mapping/robinhood --registry /data/graphloop/registry \
  --out /data/graphloop/data/robinhood --threads 16 --cpus 0-15 [--econ /data/econ/out]
# serve (loopback only; one state dir per chain; single writer)
build/glserve --data /data/graphloop/data/robinhood --state /data/graphloop/state/robinhood --port 8710 \
  --workers 4 --cpus 0,2,4,6 --context agent_context.md --ram [--unix /run/graphloop-rh.sock] [--llm-quantile 0.99]
# export for UI / Blob
build/glexport --data /data/graphloop/data/robinhood --state /data/graphloop/state/robinhood --out /data/graphloop/exports/robinhood
# benchmarks / tests
build/glsynth --out /data/graphloop/synth/s5m --nodes 5000000 --edges 50000000
build/glbench --port 8710 --mode propose --clients 32 --batch 15 --seconds 30
build/glbench --port 8710 --mode tools --graph <graph dir> --clients 4 --seconds 15
tests/crash_test.sh /data/graphloop/data/arb_r022 8713 15
```

## What the dispatcher must change (`skew:/srv/skew/em-labeling`)

1. **Source of work**: instead of `--input` feature-pack JSONL plus `prepare_pack`, loop: `POST /tasks/lease {"n":B,"batch_size":B,"task_type":…,"agent":worker_id}` with B = 10–20, and make one LLM request per returned batch.
2. **Prompt**:
   - system = `GET /context` (agent_context.md) verbatim, first and identical for Luna, Muse and DeepSeek. Check `context_sha256` against each lease.
   - user = `batch.user` verbatim.
   - The text contains no hex addresses. The dispatcher's own A1/T1/X1 aliasing must be **skipped** for graph-loop tasks, because the server already aliased them; do not re-alias.
3. **Schema**: for strict providers use `response_format` json_schema with the object form `{"r":[{"s","c","q","e","n"}]}` (all required, `additionalProperties:false`). Otherwise use JSON mode with the array form. Size `max_tokens` at ≈ 40 × batch (+ Muse's reasoning allowance).
4. **Submit**: `POST /proposals {"batch_id","provider","model","usage":{prompt_tokens,completion_tokens,cached_tokens},"output":<raw content>}`.
   - The server parses, de-aliases, validates, journals (fsync) and answers per-task verdicts. Local `validate.py` is not needed for these tasks; its rules run server-side with the same reason codes.
   - Keep the dispatcher journal for request/response bytes and spend.
5. **Failure handling**: transport/provider error → `POST /tasks/release {"batch_id"}`. A batch_id is single-use: after its verdict it returns 404/409. Leases expire after `ttl_s` (default 600).
6. **Tools (optional)**: expose `neighbors`/`path`/`feature_pack` as LLM tools by forwarding to `/tools/*?batch=<batch_id>&ref=<alias>`, so answers stay aliased.
7. **Monitoring**: use `/status` (providers.*.accept_rate, tokens_in_per_task, reject_reasons, commits.per_sec_*, frontier.*.queued) and `/coverage`. Spend is still capped by the dispatcher's Budget.
8. **Network**: the server is loopback-only. Run the dispatcher workers on the same host as glserve, or tunnel (`ssh -L 8710:127.0.0.1:8710`). Do not open it publicly.

## Known limitations

- Protocol discovery has no sample logs yet, so roles must be "" and only signature/action/family are verified. A logs-scan step (`samples.seg`) is the next addition.
- Sweep-hub detection also fires on bridge makers (e.g. an Orbiter-style maker among the top Arbitrum hubs). This is why hubs are confirmation candidates.
- The econ_actions typed-edge path and the full-chain Arbitrum map have not run on real data yet. They were not produced at test time.
