# agents

Latest agent-facing services and Muse / track stack.

| Subdir | Role |
|---|---|
| `track/` | Chart-only tracker, Muse track mode, hop→chart markers |
| `muse/` | Muse FE helpers + prepare scripts |
| `bridge/` | EM ↔ ticking public flow bridge |
| `mcp/` | MCP gateway + EM v1 adapter |
| `context/` | Context layer in front of intelligence |
| `realtime/` | Sec-bars writers + Arb fast-feed early paint |

See [docs/ARCHITECTURE-AGENTS.md](../docs/ARCHITECTURE-AGENTS.md).
