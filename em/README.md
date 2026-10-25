# em

Economic Machine v2 (source only).

| Subdir | Role |
|---|---|
| `core/` | C++ EM core (`CMakeLists.txt`) |
| `serve/` | HTTP serve build tree |
| `graph-src/` / `graphloop-core/` | Graph integration sources |
| `ops/` | Fetch / publish / schedule scripts |
| `verify/` / `bench/` | Comparison and latency helpers |

Ledger segments and compact `*.db` files are not committed. See [docs/ARCHITECTURE-AGENTS.md](../docs/ARCHITECTURE-AGENTS.md).
