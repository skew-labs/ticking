#!/bin/bash
# upload_verify.sh: copy /data/emv2-dev/verify/{golden,results,*.py,*.sh,*.sql} to Blob emv2/verify/20261020/ (VM, root), then list the
# destination and check every file's size and MD5 against local. Never prints the SAS (azcopy redacts sig in its logs).
set -euo pipefail
cd /data/emv2-dev/verify
DST="https://arbitrum.blob.core.windows.net/em-mapping/emv2/verify/20261020"
SAS=$(cat /root/.em-blob.sas); SAS=${SAS#\?}
mkdir -p /data/emv2-dev/verify/upload
rm -rf upload/stage && mkdir -p upload/stage && cp -r golden upload/stage/ && { [ -d results ] && cp -r results upload/stage/ || true; } && cp *.py *.sh *.sql upload/stage/ 2>/dev/null || true
[ -f REPORT.md ] && cp REPORT.md upload/stage/ || true
azcopy copy "upload/stage/*" "$DST?$SAS" --recursive --put-md5 --overwrite=false --log-level=WARNING >/dev/null 2>&1 || true
python3 - <<'PY'
import base64, hashlib, os, re, urllib.parse, urllib.request
SAS = open('/root/.em-blob.sas').read().strip().lstrip('?')
C = 'https://arbitrum.blob.core.windows.net/em-mapping'; pre = 'emv2/verify/20261020/'
remote, marker = {}, None
while True:
    q = f"restype=container&comp=list&prefix={urllib.parse.quote(pre)}&maxresults=5000" + (f"&marker={urllib.parse.quote(marker)}" if marker else "")
    x = urllib.request.urlopen(f"{C}?{q}&{SAS}", timeout=120).read().decode()
    for b in re.findall(r"<Blob>(.*?)</Blob>", x, re.S):
        n = re.search(r"<Name>(.*?)</Name>", b).group(1)
        remote[n[len(pre):]] = (int(re.search(r"<Content-Length>(\d+)</Content-Length>", b).group(1)), (re.search(r"<Content-MD5>(.*?)</Content-MD5>", b) or [None, None])[1])
    m = re.search(r"<NextMarker>(.*?)</NextMarker>", x); marker = m.group(1) if m and m.group(1) else None
    if not marker: break
bad, n, tot = [], 0, 0
for root, _, files in os.walk('upload/stage'):
    for f in files:
        p = os.path.join(root, f); rel = os.path.relpath(p, 'upload/stage'); n += 1; tot += os.path.getsize(p)
        h = hashlib.md5()
        with open(p, 'rb') as fh:
            for blk in iter(lambda: fh.read(1 << 22), b''): h.update(blk)
        md5 = base64.b64encode(h.digest()).decode()
        r = remote.get(rel)
        if not r or r[0] != os.path.getsize(p) or (r[1] and r[1] != md5): bad.append((rel, r))
print(f"files {n} bytes {tot} remote {len(remote)} mismatched {len(bad)}", bad[:5])
PY
