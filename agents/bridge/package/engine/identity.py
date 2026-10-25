"""Network identities for the private read bridge: one contract, a store per network.

Solana keeps its base58 mint/pool identity. Arbitrum One and Robinhood Chain use
lowercase 0x addresses; a DEX pool is its 20-byte address or a 32-byte V4 pool id.
Mixed-case input is normalised by the caller, never guessed here.
"""
import re

NETWORKS = ('solana', 'arbitrum', 'robinhood')
EVM = frozenset(('arbitrum', 'robinhood'))
EVM_ADDRESS = re.compile(r'0x[0-9a-f]{40}')
EVM_POOL = re.compile(r'0x(?:[0-9a-f]{40}|[0-9a-f]{64})')
EVM_TX = re.compile(r'0x[0-9a-f]{64}')


def network_of(value):
    value = value or 'solana'
    if value not in NETWORKS:
        raise ValueError('Unknown network')
    return value


def valid_address(network, value):
    """Exact token identity on one network; pools are checked with valid_pool."""
    if not isinstance(value, str):
        return False
    if network == 'solana':
        from server import valid_mint
        return valid_mint(value)
    return network in EVM and bool(EVM_ADDRESS.fullmatch(value))


def valid_pool(network, value):
    if not isinstance(value, str):
        return False
    if network == 'solana':
        from server import valid_mint
        return valid_mint(value)
    return network in EVM and bool(EVM_POOL.fullmatch(value))
