# ADR 0001: Robinhood Chain only, existing pools only

- Status: accepted
- Date: 2026-10

## Context
Ticking is a spot terminal for tokenized stocks, RWAs and on-chain tokens on Robinhood Chain (4663).
Supporting several chains and venue types spreads engine work thin. Running our own AMM, MM or RFQ would add
inventory and counterparty risk.

## Decision
- The spot router targets **Robinhood Chain only**.
- It routes only through **existing on-chain DEX pools**. Ticking runs no AMM, market maker or RFQ of its own.
- USDG and USDC are both supported as quote assets.

## Consequences
- The engine models exactly one chain's venues, and models them exactly.
- Price quality depends on public on-chain liquidity.
