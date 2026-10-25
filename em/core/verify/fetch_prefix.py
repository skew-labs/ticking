#!/usr/bin/env python3
"""fetch_prefix.py PREFIX OUTDIR [THREADS]: download every blob under PREFIX into OUTDIR (flat, by basename), skipping files already
present with the same size. Never prints the SAS."""
import concurrent.futures as cf, os, re, sys, urllib.parse, urllib.request
SAS = open('/root/.em-blob.sas').read().strip().lstrip('?')
C = 'https://arbitrum.blob.core.windows.net/em-mapping'
pre, out = sys.argv[1], sys.argv[2]; th = int(sys.argv[3]) if len(sys.argv) > 3 else 32
os.makedirs(out, exist_ok=True)
items, marker = [], None
while True:
    q = f"restype=container&comp=list&prefix={urllib.parse.quote(pre)}&maxresults=5000" + (f"&marker={urllib.parse.quote(marker)}" if marker else "")
    x = urllib.request.urlopen(f"{C}?{q}&{SAS}", timeout=120).read().decode()
    for b in re.findall(r"<Blob>(.*?)</Blob>", x, re.S):
        n = re.search(r"<Name>(.*?)</Name>", b).group(1); s = int(re.search(r"<Content-Length>(\d+)</Content-Length>", b).group(1))
        if s > 0: items.append((n, s))
    m = re.search(r"<NextMarker>(.*?)</NextMarker>", x); marker = m.group(1) if m and m.group(1) else None
    if not marker: break
def get(it):
    n, s = it; p = os.path.join(out, os.path.basename(n))
    if os.path.exists(p) and os.path.getsize(p) == s: return 0
    d = urllib.request.urlopen(f"{C}/{urllib.parse.quote(n)}?{SAS}", timeout=300).read()
    assert len(d) == s, n
    open(p + '.part', 'wb').write(d); os.replace(p + '.part', p); return s
with cf.ThreadPoolExecutor(th) as ex:
    got = sum(ex.map(get, items))
print(f"{len(items)} blobs, {got} bytes fetched -> {out}")
