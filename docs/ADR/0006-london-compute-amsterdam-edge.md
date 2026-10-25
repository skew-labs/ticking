# ADR 0006: London for compute and data, Amsterdam for the edge

- Status: accepted
- Date: 2026-10

## Context
The engine, followers and candle builds need dedicated cores, memory and disk. Public delivery needs to be light and isolated from them.

## Decision
- The **London** bare-metal server runs the routing engine, the data pipeline and the chart backend.
- The **Amsterdam** server handles frontend delivery and the light public API. It reaches London only
  through **read-only loopback tunnels**.

## Consequences
- Heavy jobs cannot starve the public edge, and the edge has no write path into the data stores.
