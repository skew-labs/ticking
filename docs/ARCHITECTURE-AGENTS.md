# Agent system architecture

How search, research, the Economic Machine (EM), the graph loop, and the latest agents fit together.
Runtime hosts: **t1 (London)** for tip/hot charts, EM serve, search-guard, and the router engine; **skew / Amsterdam edge** for large EM ledger and compact DBs.

This document describes the code in `search/`, `research/`, `em/`, `graph/`, and `agents/`.

## Big picture

```mermaid
flowchart TB
  subgraph edge [User / Frontend]
    FE[ticking.dev FE]
  end

  subgraph t1 [t1 London]
    SG[search-guard<br/>intelligence resolve]
    CTX[context layer :19445]
    MCP[MCP gateway]
    BR[EM ticking bridge]
    TR[track / muse · chart markers]
    CH[:8895 charts edge]
    RT[realtime · sec-bars · fast-feed]
    RTR[aggregator router :8782/:8783]
  end

  subgraph skew [skew Amsterdam]
    EM[EM v2 serve :19470]
    GL[graph-loop glserve]
    LAB[research labels / refresh]
  end

  FE -->|GET /api/public/em/search| SG
  FE -->|POST /api/public/intelligence/resolve| CTX
  CTX --> SG
  SG -->|handoffs: muse · research · graph-loop · chart| FE
  FE -->|wallet / asset flow| BR
  BR --> EM
  FE -->|candles / markers| CH
  TR -->|hop → chartMarkers| CH
  RT --> CH
  GL --> EM
  LAB --> EM
  RTR -->|/api/router/*| FE
```

## Perceive → Plan → Act

The wallet-hub / Muse path uses a three-step contract (see historical `PERCEIVE_API` notes; product surface is chart-centric).

```mermaid
sequenceDiagram
  participant U as User
  participant SG as search-guard
  participant P as Perceive
  participant PL as Plan
  participant A as Act / Show

  U->>SG: natural language / search
  SG->>SG: entity intent + handoff
  SG->>P: subject wallet|token, window, chains
  P-->>PL: hops transfers/swaps/bridges + hub
  PL-->>A: track mode / research / graph-loop request
  A-->>U: chart markers, Flow answer, or investigate route
```

| Step | Code | Role |
|---|---|---|
| Perceive | `agents/track/`, hop tables via EM serve | Load transfer / swap / bridge hops for a subject and window |
| Plan | `search/search-guard/` (`planner_intent.py`, `assistant_task_flow.py`) | Choose Muse / research / graph-loop / chart-assist handoff |
| Act / Show | `agents/muse/`, `agents/track/muse_track_mode.py`, `hops_to_chart_markers.py` | Paint Flow answers and chart markers; open investigate routes |

## Search → workspace

```mermaid
flowchart LR
  Q[query] --> OV[search overlay<br/>suggest_overlay.py]
  Q --> INV[investigate search APIs]
  Q --> SG[search-guard-7]
  SG --> RES[intelligence resolve]
  RES --> WS[workspace route<br/>#/investigate · #/terminal · Muse]
```

| Path | Directory | Notes |
|---|---|---|
| Typeahead / EM search | `search/overlay/`, public `/api/public/em/search` | Suggest + protocol prefetch |
| Investigate search | `search/investigate/` | Entity / wallet PnL / directory APIs |
| Search-guard | `search/search-guard/` | Versioned resolve pipeline; handoffs `muse-agent`, `research-assist`, `graph-loop`, `chart-assist` |
| Context | `agents/context/` | nginx → context → intelligence |

## Research ↔ Economic Machine

```mermaid
flowchart LR
  SQL[research/refresh-sql] --> PY[research/refresh-py]
  PY --> LAB[research/labels scripts]
  EXT[research/extractor Go] --> EMCORE[em/core C++]
  EMCORE --> LEDGER[ledger segments on skew]
  LEDGER --> SERVE[em/serve :19470]
  SERVE --> BR[agents/bridge]
  SERVE --> MCP[agents/mcp]
```

| Path | Directory | Notes |
|---|---|---|
| EM core / serve | `em/core`, `em/serve`, `em/graph-src` | C++ Economic Machine v2: compact stores, HTTP serve |
| Ops / verify | `em/ops`, `em/verify`, `em/bench` | Publish, fetch, compare helpers (no ledger blobs in git) |
| Refresh pipeline | `research/refresh-py`, `research/refresh-sql` | SQL stages + Python refresh / galaxy / narratives |
| Labels | `research/labels` | Seed / registry builders (parquet outputs not committed) |
| Extractor | `research/extractor` | Go RPC / trace extractors (source only) |
| Bridge | `agents/bridge` | Public wallet-flow / asset-flow facing FE |
| MCP | `agents/mcp` | Tool gateway + EM v1 adapter |

Public EM surfaces used by the product include `/api/public/em/v1/*` (tunneled to serve) and `/api/public/em/wallet-flow/*`, `/api/public/em/asset-flow/*` (bridge).

## Graph loop ↔ charts

```mermaid
flowchart LR
  GLSRC[graph/graphloop C++] --> GLServe[glserve]
  GLServe --> EM[EM graph APIs]
  HOPS[hop rows] --> MARK[agents/track/hops_to_chart_markers.py]
  MARK --> CH[charting :8895]
  FF[agents/realtime/arb-fastfeed] -->|early paint| CH
  SEC[agents/realtime/sec-bars] --> CH
```

| Path | Directory | Notes |
|---|---|---|
| Graph-loop engine | `graph/graphloop` | C++ build / serve / export (`glserve`, `glbuild`, …) |
| Mapping graphloop | `graph/mapping-graphloop` | Smaller mapping-side helpers |
| Track autonomy | `agents/track` | Muse track mode, tip/hot fills, hop→markers, serve_track |
| Realtime | `agents/realtime` | 1s bars writers, Arb fast-feed consumer (early paint only; tip/hot remains SoT) |

## Latest agent stack (Muse / track)

| Piece | Directory | Role |
|---|---|---|
| Muse FE helpers | `agents/muse/fe` | Investigate / assistant / wallet-flow chart / agent tools (JS) |
| Muse prepare | `agents/muse/*.py` | Release packaging helpers |
| Track server | `agents/track/serve_track.py` | Chart-only tracker backend |
| Autonomy | `agents/track/em_track_autonomy.py` | Perceive→Plan→Act track loop |

## Ports and APIs (as found in code / wiring)

| Port / path | Component |
|---|---|
| `:8782` / `:8783` | Aggregator best / fast quote (also under `aggregator/`) |
| `:8895` | Charts edge |
| `:19470` | EM v2 serve (`/healthz`, `/v1/search`, `/v1/address/…`) |
| `:19445` / `:19444` | Context → intelligence |
| `:19451` | Holdings / flow proxy (bridge) |
| `:19480` | Tracker chart-only (when enabled) |
| `https://api.ticking.dev/api/router/*` | Public router |
| `https://ticking.dev/api/public/em/*` | Public EM / flow / search |

Exact bind addresses are deployment-local; this repo ships the source and contracts, not live credentials.

## What is not in git

Databases, ledger segments, parquet dumps, `node_modules`, `.venv`, `.env`, API keys, logs, and STATUS dumps with private host detail are excluded. Rebuild data on the servers; compile C++/Go binaries from the sources here.
