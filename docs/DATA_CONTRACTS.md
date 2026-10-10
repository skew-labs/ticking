# Data contracts

## Identity and units

Resolve a mint/pool or an explicitly namespaced venue instrument. Preserve network, venue, market, base/quote units, price series, aggregation policy and history policy. A ticker alone does not equate a perpetual with a tokenized stock or an underlying asset.

## Executions

The native decoder accepts supported instruction/result layouts, rejects failed results, and checks the relationship between account changes and actual swap context. The engine orders retained events using source slot/transaction/event coordinates and stable identity. Conflicting duplicates are errors. Root advancement and branch retraction are explicit inputs, not inferred from wall-clock age.

## Candles and references

Candles require ordered timestamps, interval alignment, finite positive prices, `low <= open/close <= high`, and nonnegative quantities. Provider-specific calendars remain part of source identity. A mutable current bar can change; mature-bar reconciliation is a correction, not an append with a new identity.

Reference synthesis combines compatible, attributed observations using freshness, liquidity and price-consistency policy. Execution candles and indicative references remain distinct. A carried or estimated minute cannot gain trade count/volume by interpolation. The public readers can aggregate complete same-source constituent intervals but cannot reconstruct unknown historical execution extrema.

## Durability and delivery

Original framed inputs retain receive time, integrity checks and bounded lengths. Durable markers cannot advance past failed writes/syncs. Archive compression verifies replay equivalence; deleting original material is a separate operator decision, never a side effect of publishing this repository.

The read plane watches committed revisions. Shared stream hubs encode one market update for multiple viewers. A delta names the expected base revision. Cursor mismatch, missing identity or changed units force a snapshot/rebase. Last-good display preservation does not label old data as live. Book data is a bounded snapshot and does not promise every order lifecycle event.

## Coverage

Source lag, collector processing, durability, publication and browser presentation are different measurements. Offline throughput and live-source latency are reported separately. A supported adapter or successful sample does not establish complete mainnet coverage, whole-history availability or a commercial redistribution grant.
