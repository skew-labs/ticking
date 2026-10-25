# blob_bytes.py <prefix>: total bytes under a Blob prefix (never prints the SAS).
import re, sys, urllib.parse, urllib.request
SAS = open("/root/.em-blob.sas").read().strip().lstrip("?"); A = "https://arbitrum.blob.core.windows.net/em-mapping"
p = sys.argv[1]; m = None; t = 0
while True:
    q = f"restype=container&comp=list&prefix={urllib.parse.quote(p)}&maxresults=5000" + (f"&marker={urllib.parse.quote(m)}" if m else "")
    x = urllib.request.urlopen(f"{A}?{q}&{SAS}", timeout=120).read().decode()
    t += sum(int(s) for s in re.findall(r"<Content-Length>(\d+)</Content-Length>", x))
    mm = re.search(r"<NextMarker>(.*?)</NextMarker>", x); m = mm.group(1) if mm and mm.group(1) else None
    if not m: break
print(t)
