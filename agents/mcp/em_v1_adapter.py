"""DRAFT (not live-wired): em-serve /v1 adapter for the 6-chain production target.

Production target chains (always): robinhood, linea, arc, unichain, ethereum, base.
Default OFF: EM_V1 unset -> every call returns None and callers keep legacy sources
(em-lookup :18790, bridge :8896, Blockscout, charts).

Per-chain feature flags (fail-open):
  EM_V1_CHAINS=robinhood,linea,arc,unichain,ethereum,base   # which chains may use v1
  EM_V1_CHAIN_<NAME>=0|1                                     # optional override per chain
  EM_V1_REQUIRE_COMPACT=1                                    # if set, skip chains without .done
                                                             # (checked only via /v1/chains/stats
                                                             #  addr_source containing em-unified)

Compact schema (Skew, addresses.parquet) fields we map from /v1/address:
  chain, address, address_hex, first_block, first_time, last_block, last_time,
  tx_sent, tx_sent_failed, tx_received, tx_received_failed,
  native_value_sent, native_value_received, native_value_overflow,
  token_transfers_out, token_transfers_in,
  distinct_counterparties, out_degree, in_degree, is_contract

Switch requires: (1) tunnel ticking1:18791 -> DEST 127.0.0.1:19470 healthy,
(2) EM_V1=http://127.0.0.1:18791 on consumer units, (3) Skew go-ahead after
ethereum/base compact DONE + final dump. Do NOT flip live until then.
"""
from __future__ import annotations
import os, time
from datetime import datetime
from typing import Any, Optional

try:
    import httpx
except ImportError:  # allow import on hosts without httpx until wired
    httpx = None  # type: ignore

# --- feature flags ---
V1 = os.environ.get('EM_V1', '').rstrip('/')          # empty = adapter disabled (default)
SIX = ('robinhood', 'linea', 'arc', 'unichain', 'ethereum', 'base')
_DEFAULT_CHAINS = ','.join(SIX)
CHAINS_V1 = set(filter(None, os.environ.get('EM_V1_CHAINS', _DEFAULT_CHAINS).split(',')))
TO = float(os.environ.get('EM_V1_TIMEOUT', '1.5'))    # v1 p50 ~4ms; cut early on DEST spikes
REQUIRE_COMPACT = os.environ.get('EM_V1_REQUIRE_COMPACT', '0') == '1'

# optional per-chain overrides: EM_V1_CHAIN_ETHEREUM=0 disables ethereum even if in EM_V1_CHAINS
def _chain_flag(chain: str) -> bool:
    key = 'EM_V1_CHAIN_' + chain.upper().replace('-', '_')
    if key in os.environ:
        return os.environ[key] not in ('0', 'false', 'False', '')
    return chain in CHAINS_V1

# runtime compact readiness cache from /v1/chains/stats (refreshed ~60s)
_compact_ok: dict[str, bool] = {}
_compact_at = 0.0
_COMPACT_TTL = 60.0

_c = None
if httpx is not None:
    _c = httpx.AsyncClient(base_url=V1 or 'http://127.0.0.1:1', timeout=TO)

_down_until = 0.0
_fails = 0


def enabled() -> bool:
    return bool(V1)


def target_chains() -> tuple[str, ...]:
    return SIX


def active_chains() -> list[str]:
    return [c for c in SIX if _ok_chain(c)]


def _ok_chain(chain: str) -> bool:
    if not V1 or not _chain_flag(chain):
        return False
    if REQUIRE_COMPACT and chain in _compact_ok and not _compact_ok[chain]:
        return False
    return True


async def refresh_compact_flags(force: bool = False) -> dict[str, bool]:
    """Populate _compact_ok from /v1/chains/stats. ethereum/base stay False until em-unified."""
    global _compact_at
    now = time.time()
    if not force and now - _compact_at < _COMPACT_TTL and _compact_ok:
        return dict(_compact_ok)
    j = await _get('/v1/chains/stats')
    if not j:
        return dict(_compact_ok)
    for row in j.get('chains') or []:
        name = (row.get('chain') or '').lower()
        src = (row.get('addr_source') or '') or ''
        # Ready when em-unified compact is the address source (Skew .done paths)
        _compact_ok[name] = ('em-unified' in src) and bool(row.get('addresses'))
    # ensure all six keys exist
    for c in SIX:
        _compact_ok.setdefault(c, False)
    _compact_at = now
    return dict(_compact_ok)


async def _get(path: str, **params) -> Optional[dict]:
    global _fails, _down_until
    if not V1 or time.time() < _down_until or _c is None:
        return None
    try:
        r = await _c.get(path, params=params or None)
        j = r.json() if r.status_code == 200 else None
        if not j or (isinstance(j, dict) and 'error' in j):
            raise ValueError(getattr(r, 'status_code', 0))
        _fails = 0
        return j
    except Exception:
        _fails += 1
        if _fails >= 3:
            _down_until = time.time() + 30
            _fails = 0
        return None


def _ts(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp())
    except Exception:
        return None


def _activity_from_chain_row(c: dict) -> dict:
    """Map compact /v1 address chain row -> engine activity shape."""
    return {
        'tx_sent': c.get('tx_sent'),
        'tx_sent_failed': c.get('tx_sent_failed'),
        'tx_received': c.get('tx_received'),
        'tx_received_failed': c.get('tx_received_failed'),
        'native_value_sent': c.get('native_value_sent'),
        'native_value_received': c.get('native_value_received'),
        'native_value_overflow': c.get('native_value_overflow'),
        'token_transfers_out': c.get('token_transfers_out'),
        'token_transfers_in': c.get('token_transfers_in'),
        'distinct_counterparties': c.get('distinct_counterparties'),
        'out_degree': c.get('out_degree'),
        'in_degree': c.get('in_degree'),
        'is_contract': c.get('is_contract'),
        'first_block': c.get('first_block'),
        'last_block': c.get('last_block'),
        'first_time': c.get('first_time') or c.get('first_seen'),
        'last_time': c.get('last_time') or c.get('last_seen'),
    }


async def identity(addr: str, chain: str) -> Optional[dict]:
    """-> em-identity/1 subset. totalUsd NOT from v1 (usd coverage still sparse) -> keep em-lookup."""
    if not _ok_chain(chain):
        return None
    a = await _get(f'/v1/address/{addr}', chains=chain)
    if not a or not a.get('chains'):
        return None
    c = a['chains'][0]
    act = _activity_from_chain_row(c)
    return {
        'schema': 'em-identity/1',
        'address': addr,
        'address_hex': c.get('address_hex') or a.get('address_hex'),
        'identity': a.get('identity'),
        'ens': a.get('ens'),
        'labels': [{'chain': chain, **l} for l in (c.get('labels') or [])],
        'firstSeen': _ts(act.get('first_time')),
        'lastSeen': _ts(act.get('last_time')),
        'firstSeenByChain': {chain: _ts(act.get('first_time'))},
        'activity': act,
        '_src': 'em-serve/v1',
        '_chain': chain,
        'as_of': a.get('as_of'),
    }


async def holdings(addr: str, chain: str) -> Optional[list]:
    """-> list of {token,symbol,decimals,balance_raw,usd,exact}. Empty -> None (fail-open)."""
    if not _ok_chain(chain):
        return None
    h = await _get(f'/v1/address/{addr}/holdings', chains=chain)
    if not h:
        return None
    out = []
    for x in h.get('holdings') or []:
        kind = x.get('kind')
        if kind == 'native':
            out.append({
                'token': None, 'symbol': x.get('symbol') or 'ETH', 'decimals': x.get('decimals') or 18,
                'balance_raw': x.get('balance_raw'), 'usd': x.get('usd'),
                'exact': x.get('balance_exact', False), 'kind': 'native',
            })
        elif kind == 'erc20' and x.get('decimals') is not None:
            out.append({
                'token': x.get('token'), 'symbol': x.get('symbol'), 'decimals': x.get('decimals'),
                'balance_raw': x.get('balance_raw'), 'usd': x.get('usd'),
                'exact': x.get('balance_exact', False), 'kind': 'erc20',
            })
    return out or None


async def counterparties(addr: str, chain: str) -> Optional[list]:
    if not _ok_chain(chain):
        return None
    j = await _get(f'/v1/address/{addr}/counterparties', chains=chain)
    if not j or chain in (j.get('chains_without_edges') or []):
        return None
    rows = [{
        'address': c['counterparty'],
        'in': c.get('count_in', 0),
        'out': c.get('count_out', 0),
        'label': c.get('label'),
        'first_block': c.get('first_block'),
        'last_block': c.get('last_block'),
    } for c in j.get('counterparties') or []]
    return rows or None


async def graph(addr: str, chain: str, depth: int = 1) -> Optional[dict]:
    if not _ok_chain(chain):
        return None
    j = await _get(f'/v1/address/{addr}/graph', chains=chain, depth=depth)
    return {'nodes': j['nodes'], 'edges': j['edges']} if j and j.get('edges') else None


async def token(chain: str, addr: str) -> Optional[dict]:
    if not _ok_chain(chain):
        return None
    return await _get(f'/v1/token/{chain}/{addr}')


async def prefix_lookup(prefix: str, chains: Optional[str] = None) -> Optional[str]:
    """Unique 0x completion across active v1 chains. None = use bridge :8896."""
    if not V1 or len(prefix) < 12:
        return None
    params: dict[str, Any] = {'q': prefix}
    # Prefer active (flagged) chains; ethereum/base fail-open if not in stats yet
    use = chains or ','.join(active_chains() or SIX)
    if use:
        params['chains'] = use
    j = await _get('/v1/search', **params)
    full = {
        r['ref'].lower()
        for r in (j or {}).get('results', [])
        if r.get('kind') == 'address' and str(r.get('ref', '')).lower().startswith(prefix.lower())
    }
    return full.pop() if len(full) == 1 else None


async def search(q: str, chains: Optional[str] = None) -> Optional[dict]:
    """Raw /v1/search passthrough for MCP / intelligence. None on failure."""
    if not V1:
        return None
    params: dict[str, Any] = {'q': q}
    if chains:
        params['chains'] = chains
    return await _get('/v1/search', **params)


async def chains_stats() -> Optional[dict]:
    if not V1:
        return None
    return await _get('/v1/chains/stats')


async def health() -> Optional[dict]:
    if not V1:
        return None
    return await _get('/healthz')
