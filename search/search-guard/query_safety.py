"""Shared public-input boundary; normal sentences are not recovery phrases."""
import json,re,unicodedata
from pathlib import Path
WORDS=frozenset(json.loads(Path(__file__).with_name('bip39-english.json').read_text()))
def public_query(q):
 if type(q) is not str or not 1<=len(q.strip())<=400:raise ValueError('Use a request between 1 and 400 characters.')
 q=q.strip();tokens=re.findall('[a-z]+',unicodedata.normalize('NFKD',q).lower())
 phrase=len(tokens) in (12,15,18,21,24) and all(t in WORDS for t in tokens)
 if phrase or re.search(r'(?i)private[ _-]?key|secret[ _-]?key|recovery[ _-]?phrase|seed[ _-]?phrase|mnemonic|sk-[a-zA-Z0-9]{16}|bearer\s|비밀키|시드\s*구문|복구\s*구문',q) or re.search(r'(?<![0-9A-Za-z])(?<!0x)[0-9a-fA-F]{64}(?![0-9A-Za-z])',q):raise ValueError('Use public information only. Never enter keys or recovery phrases.')
 if re.search('[\x00-\x08\x0b\x0c\x0e-\x1f]',q):raise ValueError('Unsupported characters.')
 return q
