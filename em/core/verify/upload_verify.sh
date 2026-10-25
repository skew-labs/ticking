#!/bin/bash
# upload_verify.sh [DIR...]: copy verify artefacts from /data/emv2-dev/verify to Blob emv2/verify/20261020/<same relative path> (VM, root),
# default golden results scripts; then list the destination and check every local file's size and MD5. azcopy redacts the SAS in its
# logs and nothing here prints it. Re-runnable: unchanged files are skipped (--overwrite=ifSourceNewer).
set -euo pipefail
cd /data/emv2-dev/verify
DIRS=("${@:-golden results scripts}"); [ $# -eq 0 ] && DIRS=(golden results scripts)
DST="https://arbitrum.blob.core.windows.net/em-mapping/emv2/verify/20261020"
SAS=$(cat /root/.em-blob.sas); SAS=${SAS#\?}
mkdir -p scripts && cp -p *.py *.sh *.sql scripts/ 2>/dev/null || true
[ -f REPORT.md ] && cp -p REPORT.md scripts/ || true
for d in "${DIRS[@]}"; do
  azcopy copy "$d" "$DST/?$SAS" --recursive --put-md5 --overwrite=ifSourceNewer --exclude-pattern="*.tmp;*.parts" --log-level=ERROR >/dev/null 2>&1 \
    || echo "azcopy reported a problem for $d (checked below)"
done
python3 - "${DIRS[@]}" <<'PY'
import base64, hashlib, os, re, sys, urllib.parse, urllib.request
SAS = open('/root/.em-blob.sas').read().strip().lstrip('?')
C = 'https://arbitrum.blob.core.windows.net/em-mapping'; pre = 'emv2/verify/20261020/'
remote, marker = {}, None
while True:
    q = f"restype=container&comp=list&prefix={urllib.parse.quote(pre)}&maxresults=5000" + (f"&marker={urllib.parse.quote(marker)}" if marker else "")
    x = urllib.request.urlopen(f"{C}?{q}&{SAS}", timeout=120).read().decode()
    for b in re.findall(r"<Blob>(.*?)</Blob>", x, re.S):
        n = re.search(r"<Name>(.*?)</Name>", b).group(1); m5 = re.search(r"<Content-MD5>(.*?)</Content-MD5>", b)
        remote[n[len(pre):]] = (int(re.search(r"<Content-Length>(\d+)</Content-Length>", b).group(1)), m5.group(1) if m5 else None)
    m = re.search(r"<NextMarker>(.*?)</NextMarker>", x); marker = m.group(1) if m and m.group(1) else None
    if not marker: break
bad, n, tot = [], 0, 0
for d in sys.argv[1:]:
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if not x.endswith('.parts')]
        for f in files:
            if f.endswith('.tmp'): continue
            p = os.path.join(root, f); n += 1; tot += os.path.getsize(p)
            h = hashlib.md5()
            with open(p, 'rb') as fh:
                for blk in iter(lambda: fh.read(1 << 22), b''): h.update(blk)
            r = remote.get(p)
            if not r or r[0] != os.path.getsize(p) or (r[1] and r[1] != base64.b64encode(h.digest()).decode()): bad.append((p, r))
print(f"local files {n} bytes {tot} | remote objects {len(remote)} | missing or different {len(bad)}", bad[:5])
sys.exit(1 if bad else 0)
PY
