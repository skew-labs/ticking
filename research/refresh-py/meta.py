#!/usr/bin/env python3
"""Contract/token metadata via Multicall3 eth_call at latest block (local node).
Called from run.py steps `meta` (protocol getters) and `tokens` (ERC20 symbol/name/decimals).
Every result is cached in table meta_calls(address, getter, ok, raw) inside econ.duckdb, so
re-runs never repeat calls; total top-level eth_call count is printed (budget <= 2,000)."""
import os
import sys

from Crypto.Hash import keccak

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rpc  # noqa: E402


def sel(sig):
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return h.hexdigest()[:8]


def ensure(con):
    con.execute("CREATE TABLE IF NOT EXISTS meta_calls (address VARCHAR, getter VARCHAR, ok BOOLEAN, raw BLOB)")


def call_many(con, pairs):
    """pairs: list of (address, getter_sig). Skips cached pairs."""
    ensure(con)
    have = set(con.execute("SELECT address || '|' || getter FROM meta_calls").fetchall())
    have = {h[0] for h in have}
    todo = [(a, g) for a, g in dict.fromkeys(pairs) if a and (a + "|" + g) not in have]
    if not todo:
        return 0
    res = rpc.aggregate3([(a, sel(g)) for a, g in todo], chunk=150)
    rows = [(a, g, ok, b) for (a, g), (ok, b) in zip(todo, res)]
    con.executemany("INSERT INTO meta_calls VALUES (?, ?, ?, ?)", rows)
    return len(todo)


def run_meta(con, chain):
    q = lambda s: [r[0] for r in con.execute(s).fetchall()]  # noqa: E731
    pairs = []
    pairs += [(a, "baseToken()") for a in q("SELECT comet FROM dim_comets")]
    pairs += [(a, "underlying()") for a in q("SELECT ctoken FROM dim_ctokens")]
    pairs += [(a, "asset()") for a in q("SELECT vault FROM dim_vaults ORDER BY n DESC LIMIT 600")]
    pairs += [(a, "token()") for a in q("SELECT oft FROM dim_oft ORDER BY n DESC LIMIT 400")]
    for g in ("description()", "decimals()"):
        pairs += [(a, g) for a in q("SELECT aggregator FROM dim_aggregators ORDER BY n DESC LIMIT 300")]
    n = call_many(con, pairs)
    print(f"meta: {n} sub-calls, {rpc.CALLS['n']} eth_calls", file=sys.stderr)


def run_tokens(con, limit):
    q = lambda s: [r[0] for r in con.execute(s).fetchall()]  # noqa: E731
    toks = q(f"SELECT token FROM token_candidates ORDER BY score DESC LIMIT {limit}")
    pairs = [(t, g) for t in toks for g in ("symbol()", "name()", "decimals()")]
    n = call_many(con, pairs)
    print(f"tokens: {n} sub-calls, {rpc.CALLS['n']} eth_calls", file=sys.stderr)


def eth_balances(con, limit=500):
    """Native ETH balance at latest block for the top `limit` sheet wallets via Multicall3.getEthBalance."""
    con.execute("CREATE TABLE IF NOT EXISTS meta_eth_balance (wallet VARCHAR, balance_wei HUGEINT)")
    ws = [r[0] for r in con.execute(f"""
        SELECT s.wallet FROM sheet_wallets s LEFT JOIN (SELECT wallet, sum(usd) u FROM liquid_usd GROUP BY 1) l USING (wallet)
        WHERE s.wallet NOT IN (SELECT wallet FROM meta_eth_balance) ORDER BY coalesce(l.u, 0) DESC LIMIT {limit}""").fetchall()]
    if not ws:
        return
    s = sel("getEthBalance(address)")
    res = rpc.aggregate3([(rpc.MC3, s + rpc.enc_addr(w)) for w in ws], chunk=200)
    rows = [(w, str(int.from_bytes(b[:32], "big"))) for w, (ok, b) in zip(ws, res) if ok and len(b) >= 32]
    con.executemany("INSERT INTO meta_eth_balance VALUES (?, ?::HUGEINT)", rows)
    print(f"eth balances: {len(rows)} wallets, {rpc.CALLS['n']} eth_calls", file=sys.stderr)
