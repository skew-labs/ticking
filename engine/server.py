"""Identity helpers extracted from the private service; no community server."""

import re

ALPHABET='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

MINT_RE=re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')

class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message

def b58decode(value):
    n = 0
    for char in value:
        n = n * 58 + ALPHABET.index(char)
    return b'\0' * (len(value) - len(value.lstrip('1'))) + (n.to_bytes((n.bit_length()+7)//8, 'big') if n else b'')

def valid_mint(value):
    try:
        return bool(MINT_RE.fullmatch(value)) and len(b58decode(value)) == 32
    except (ValueError, TypeError):
        return False
