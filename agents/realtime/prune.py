"""prune.py: free skew disk by deleting real-time Parquet that is already safe in Blob realtime/<chain>/ (same size and
Content-MD5 as the local file), keeping the newest KEEP blocks of each chain locally for the tip consumers.
Manifests are never deleted (the followers resume from them). Only the chains listed in KEEP are touched.
Below FLOOR_GB free space the keep window halves. Run by em-map-rt-prune.timer."""
import base64, glob, hashlib, json, os, shutil, sys, time, urllib.error, urllib.request

R = "/srv/skew/em-realtime/out"
B = "https://arbitrum.blob.core.windows.net/em-mapping/realtime"
KEEP = {"ethereum": 14_400, "base": 86_400, "optimism": 86_400, "unichain": 172_800,   # ~2 days of blocks
        "linea": 86_400, "hyperevm": 172_800, "polygon": 86_400, "xlayer": 172_800}
FLOOR_GB = 65

def md5b64(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return base64.b64encode(h.digest()).decode()

def safe_in_blob(chain, rel, local, sas):
    req = urllib.request.Request(f"{B}/{chain}/{rel}?{sas}", method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(r.headers.get("Content-Length", -1)) == os.path.getsize(local) and r.headers.get("Content-MD5") == md5b64(local)
    except urllib.error.HTTPError:
        return False

def main():
    sas = open("/root/.em-blob.sas").read().strip()
    free = shutil.disk_usage("/").free / 1e9
    scale = 0.5 if free < FLOOR_GB else 1.0
    for chain, keep in KEEP.items():
        d = os.path.join(R, chain)
        if not os.path.isdir(d):
            continue
        ms = []
        for mf in glob.glob(f"{d}/manifests/*.json"):
            try:
                m = json.load(open(mf))
            except Exception:
                continue
            if m.get("status") == "OK":
                ms.append(m)
        if not ms:
            continue
        newest = max(m["range"]["to"] for m in ms)
        cut = newest - int(keep * scale)
        n = freed = kept = 0
        for m in ms:
            if m["range"]["to"] >= cut:
                continue
            for t, f in m.get("files", {}).items():
                p = f["path"] if os.path.isabs(f["path"]) else os.path.join(d, f["path"])
                if not os.path.exists(p):
                    continue
                rel = os.path.relpath(p, d)
                if safe_in_blob(chain, rel, p, sas):
                    freed += os.path.getsize(p); os.remove(p); n += 1
                else:
                    kept += 1
        ts = time.strftime("%FT%TZ", time.gmtime())
        print(f"{ts} {chain} newest {newest} cut {cut} pruned {n} files {freed/1e9:.2f} GB, not yet in Blob {kept}, free {free:.0f} GB", flush=True)

main()
