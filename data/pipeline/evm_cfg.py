"""evm_cfg.py: per-chain config of the compact-served EVM chains (Option A, owner 2026-10-19).

Imported by em-charts (common.py tail hook), em-galaxy (evm_compact.py) and bringup_hist.py. A chain becomes visible
to a consumer only when join/enabled.json lists it with that consumer's flag (written by em-evm-rt-bringup):
  {"<chain>": {"buckets": <map part files>, "cut_block": <first block of the 30-day fills window>,
               "history_max_block": <n>, "charts": true|false, "lookups": true|false}}
Anchors: USD stables, ETH (WETH, plus native only where the native currency is ETH), BTC; checked on-chain
(symbol/decimals) before use. Polygon (POL) and HyperEVM (HYPE) have no native anchor.
"""
import json
import os
import time

NATIVE = "0x0000000000000000000000000000000000000000"
JOIN = "/srv/skew/em-realtime/join"
CHAIN_IDS = {"ethereum": 1, "base": 8453, "optimism": 10, "linea": 59144, "polygon": 137, "unichain": 130, "hyperevm": 999}
RPC = {"ethereum": "https://ethereum-rpc.publicnode.com", "base": "https://base-rpc.publicnode.com",
       "optimism": "https://optimism-rpc.publicnode.com", "linea": "https://linea-rpc.publicnode.com",
       "polygon": "https://polygon-bor-rpc.publicnode.com", "unichain": "https://unichain-rpc.publicnode.com",
       "hyperevm": "https://hyperliquid-rpc.publicnode.com"}
ANCHORS = {
    "ethereum": [("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "usd", 1, "USDC"), ("0xdac17f958d2ee523a2206206994597c13d831ec7", "usd", 2, "USDT"),
                 ("0x6b175474e89094c44da98b954eedeac495271d0f", "usd", 3, "DAI"), ("0xdc035d45d973e3ec169d2276ddab16f1e407384f", "usd", 4, "USDS"),
                 ("0x6c3ea9036406852006290770bedfcaba0e23a0e8", "usd", 5, "PYUSD"), ("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "eth", 10, "WETH"),
                 (NATIVE, "eth", 11, "ETH"), ("0x2260fac5e5542a773aa44fbcfedf7c193bc2c599", "btc", 20, "WBTC"),
                 ("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf", "btc", 21, "cbBTC")],
    "base": [("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "usd", 1, "USDC"), ("0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca", "usd", 2, "USDbC"),
             ("0x50c5725949a6f0c72e6c4a641f24049a917db0cb", "usd", 3, "DAI"), ("0xfde4c96c8593536e31f229ea8f37b2ada2699bb2", "usd", 4, "USDT"),
             ("0x4200000000000000000000000000000000000006", "eth", 10, "WETH"), (NATIVE, "eth", 11, "ETH"),
             ("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf", "btc", 20, "cbBTC")],
    "optimism": [("0x0b2c639c533813f4aa9d7837caf62653d097ff85", "usd", 1, "USDC"), ("0x7f5c764cbc14f9669b88837ca1490cca17c31607", "usd", 2, "USDC.e"),
                 ("0x94b008aa00579c1307b0ef2c499ad98a8ce58e58", "usd", 3, "USDT"), ("0xda10009cbd5d07dd0cecc66161fc93d7c9000da1", "usd", 4, "DAI"),
                 ("0x4200000000000000000000000000000000000006", "eth", 10, "WETH"), (NATIVE, "eth", 11, "ETH"),
                 ("0x68f180fcce6836688e9084f035309e29bf0a2095", "btc", 20, "WBTC")],
    "linea": [("0x176211869ca2b568f2a7d4ee941e073a821ee1ff", "usd", 1, "USDC"), ("0xa219439258ca9da29e9cc4ce5596924745e12b93", "usd", 2, "USDT"),
              ("0xe5d7c2a44ffddf6b295a15c148167daaaf5cf34f", "eth", 10, "WETH"), (NATIVE, "eth", 11, "ETH"),
              ("0x3aab2285ddcddad8edf438c1bab47e1a9d05a9b4", "btc", 20, "WBTC")],
    "polygon": [("0x3c499c542cef5e3811e1192ce70d8cc03d5c3359", "usd", 1, "USDC"), ("0x2791bca1f2de4661ed88a30c99a7a9449aa84174", "usd", 2, "USDC.e"),
                ("0xc2132d05d31c914a87c6611c10748aeb04b58e8f", "usd", 3, "USDT"), ("0x8f3cf7ad23cd3cadbd9735aff958023239c6a063", "usd", 4, "DAI"),
                ("0x7ceb23fd6bc0add59e62ac25578270cff1b9f619", "eth", 10, "WETH"), ("0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6", "btc", 20, "WBTC")],
    "unichain": [("0x078d782b760474a361dda0af3839290b0ef57ad6", "usd", 1, "USDC"), ("0x9151434b16b9763660705744891fa906f660ecc5", "usd", 2, "USDT0"),
                 ("0x4200000000000000000000000000000000000006", "eth", 10, "WETH"), (NATIVE, "eth", 11, "ETH")],
    "hyperevm": [("0xb8ce59fc3717ada4c02eadf9682a9e934f625ebb", "usd", 1, "USDT0"), ("0xb88339cb7199b77e23db6e890353e22632ba630f", "usd", 2, "USDC"),
                 ("0xbe6727b535545c67d5caa73dea54865b92cf7907", "eth", 10, "UETH"), ("0x9fdbda0a5e284c32744d2f17ee5c74b284993463", "btc", 20, "UBTC")],
}
NATIVE_NOT_ETH = {"polygon": "POL", "hyperevm": "HYPE"}   # em-charts names the native token ETH: flagged there
MIN_USD = {"ethereum": 5_000_000, "base": 2_000_000, "optimism": 1_000_000, "linea": 500_000, "polygon": 1_000_000,
           "unichain": 500_000, "hyperevm": 500_000}
FILLS_DAYS = 30
_cache = [0.0, {}]


def enabled():
    """{chain: entry} from join/enabled.json, re-read at most every 60 s."""
    if time.time() - _cache[0] > 60:
        try:
            _cache[1] = json.load(open(os.path.join(JOIN, "enabled.json")))
        except (OSError, ValueError):
            _cache[1] = {}
        _cache[0] = time.time()
    return _cache[1]


def charts_enabled():
    return [c for c, e in enabled().items() if e.get("charts")]


def lookups_enabled():
    return {c: e for c, e in enabled().items() if e.get("lookups")}
