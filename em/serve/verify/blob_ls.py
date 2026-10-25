#!/usr/bin/env python3
"""blob_ls.py PREFIX [--delim] [--max N]: list blob names, sizes, tiers under PREFIX in em-mapping. Never prints the SAS."""
import re, sys, urllib.parse, urllib.request
SAS = open('/root/.em-blob.sas').read().strip().lstrip('?')
C = 'https://arbitrum.blob.core.windows.net/em-mapping'
prefix = sys.argv[1]; delim = '--delim' in sys.argv
mx = int(sys.argv[sys.argv.index('--max') + 1]) if '--max' in sys.argv else 10**9
marker = None; n = 0
while True:
    q = f"restype=container&comp=list&prefix={urllib.parse.quote(prefix)}&maxresults=5000" + ("&delimiter=/" if delim else "") + (f"&marker={urllib.parse.quote(marker)}" if marker else "")
    x = urllib.request.urlopen(f"{C}?{q}&{SAS}", timeout=120).read().decode()
    for p in re.findall(r"<BlobPrefix><Name>(.*?)</Name></BlobPrefix>", x):
        print("DIR ", p)
    for b in re.findall(r"<Blob>(.*?)</Blob>", x, re.S):
        name = re.search(r"<Name>(.*?)</Name>", b).group(1); size = int(re.search(r"<Content-Length>(\d+)</Content-Length>", b).group(1))
        lm = re.search(r"<Last-Modified>(.*?)</Last-Modified>", b).group(1)
        print(f"{size:>14d} {lm} {name}"); n += 1
        if n >= mx: sys.exit(0)
    m = re.search(r"<NextMarker>(.*?)</NextMarker>", x); marker = m.group(1) if m and m.group(1) else None
    if not marker: break
