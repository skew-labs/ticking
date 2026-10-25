"""rpc_handoff.py <chain> [--dry-run | --list]: finish a VM range backfill on skew at $0 after the VMs are stopped.
--list only writes the handoff list (Blob <plan>/HANDOFF.json: from, to, contiguous_ok_to, handoff_from per range).

For every line "<from> <to>" of Blob <chain>/rpc-ranges/PLAN without a DONE.<from>-<to> marker:
  1. handoff block = end of the contiguous run of OK manifests in Blob <chain>/extraction/manifests from <from>, plus 1
     (manifest contents are read: FAILED manifests are uploaded too, so names alone are not enough);
  2. extract handoff..to over public RPC with the same tool and verification (emrpc-op for OP-stack chains) into
     /data/out/<chain> (manifest paths then match the VM layout, so the owner build later reads them as-is);
  3. azcopy sync to Blob <chain>/extraction, delete each local Parquet only after its Blob copy matches (size + MD5),
     check contiguity again over the whole PLAN range, then write DONE.<from>-<to>.
Chunks are block-aligned and named by range, so a chunk is never cut twice and nothing is duplicated.
"""
import base64
import concurrent.futures as cf
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

B = "https://arbitrum.blob.core.windows.net/em-mapping"
AZ = "/root/bin/azcopy"
OP = "/srv/skew/em-realtime/bin/emrpc-op"
# per chain: PLAN / DONE prefix, Blob manifests + upload target, local out dir, prune after upload, extractor command
CFG = {
    "unichain": {"plan": "unichain/rpc-ranges", "dest": "unichain/extraction", "out": "/data/out/unichain", "prune": True,
                 "cmd": [OP, "--chain-id", "130", "--chunk", "1000", "--retries", "12",
                         "--rpc", "https://unichain-rpc.publicnode.com#workers=48", "--rpc", "https://mainnet.unichain.org#workers=8"]},
    # Arc: raw chunks in the skew follower's layout; kept local (em-arc-bringup reads out/arc-raw).
    "arc": {"plan": "arc/rpc-ranges", "dest": "realtime/arc", "out": "/srv/skew/em-realtime/out/arc-raw", "prune": False,
            # receipts-only, as the VM workers (em-arc/arc_range.sh v2): header + receipts from PublicNode, bodies later
            "cmd": [OP, "--chain-id", "5042", "--receipts-only", "--chunk", "1000", "--retries", "12",
                    "--rpc", "https://arc-rpc.publicnode.com#workers=16", "--rpc", "https://arc.drpc.org#workers=4"]},   # dRPC: retry path for PublicNode LOG_BLOOM blocks
}
ENV = dict(os.environ, HOME=os.environ.get("HOME") or "/root")


def sas():
    return open("/root/.em-blob.sas").read().strip().lstrip("?")


def log(*a):
    print(time.strftime("%FT%TZ", time.gmtime()), *a, flush=True)


def get(path):
    with urllib.request.urlopen(f"{B}/{path}?{sas()}", timeout=60) as r:
        return r.read()


def exists(path):
    try:
        urllib.request.urlopen(urllib.request.Request(f"{B}/{path}?{sas()}", method="HEAD"), timeout=30)
        return True
    except urllib.error.HTTPError:
        return False


def names(prefix):
    out, marker = [], ""
    while True:
        u = f"{B}?restype=container&comp=list&prefix={prefix}&maxresults=5000&{sas()}"
        x = urllib.request.urlopen(u + (f"&marker={marker}" if marker else ""), timeout=120).read().decode()
        out += re.findall(r"<Name>([^<]+)</Name>", x)
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", x)
        if not m:
            return out
        marker = m.group(1)


def contiguous_end(chain, lo, hi):
    """Last block of the contiguous OK run from lo (lo - 1 when the first chunk is missing or not OK)."""
    ms = []
    for n in names(f"{CFG[chain]['dest']}/manifests/"):
        m = re.search(r"(\d{10})-(\d{10})\.json$", n)
        if m and lo <= int(m.group(1)) <= hi:
            ms.append((int(m.group(1)), int(m.group(2)), n))
    ms.sort()

    def status(n):
        try:
            return json.loads(get(n)).get("status")
        except Exception:
            return None
    with cf.ThreadPoolExecutor(32) as ex:
        st = list(ex.map(lambda r: status(r[2]), ms))
    nxt = lo
    for (a, b, _), s in zip(ms, st):
        if a != nxt or s != "OK":
            break
        nxt = b + 1
    return nxt - 1


def md5b64(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return base64.b64encode(h.digest()).decode()


def upload_and_prune(chain, out):
    dest = CFG[chain]["dest"]
    rc = subprocess.run([AZ, "sync", out, f"{B}/{dest}?{sas()}", "--recursive", "--exclude-pattern", "*.tmp*", "--put-md5",
                         "--log-level", "ERROR"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV).returncode
    freed = 0
    if not CFG[chain]["prune"]:
        return rc, 0
    for p in glob.glob(f"{out}/*/*.parquet"):
        rel = os.path.relpath(p, out)
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{B}/{dest}/{rel}?{sas()}", method="HEAD"), timeout=30) as r:
                if int(r.headers.get("Content-Length", -1)) == os.path.getsize(p) and r.headers.get("Content-MD5") == md5b64(p):
                    freed += os.path.getsize(p)
                    os.remove(p)
        except urllib.error.HTTPError:
            pass
    return rc, freed


def main():
    chain, dry, only_list = sys.argv[1], "--dry-run" in sys.argv, "--list" in sys.argv
    c = CFG[chain]
    plan = [tuple(map(int, l.split())) for l in get(f"{c['plan']}/PLAN").decode().splitlines() if l.strip()]
    out = c["out"]
    todo = []
    for lo, hi in plan:
        if exists(f"{c['plan']}/DONE.{lo}-{hi}"):
            continue
        end = contiguous_end(chain, lo, hi)
        todo.append({"from": lo, "to": hi, "contiguous_ok_to": end, "handoff_from": end + 1, "blocks_left": hi - end})
        log(chain, lo, hi, f"contiguous OK in Blob through {end}: handoff from {end + 1} ({hi - end} blocks left)")
    if not dry:
        open("/tmp/rpc-handoff.json", "w").write(json.dumps({"chain": chain, "at": int(time.time()), "ranges": todo}, indent=1))
        subprocess.run([AZ, "copy", "/tmp/rpc-handoff.json", f"{B}/{c['plan']}/HANDOFF.json?{sas()}", "--overwrite=true", "--log-level", "ERROR"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
        log(chain, f"handoff list: {len(todo)} ranges, {sum(t['blocks_left'] for t in todo)} blocks -> Blob {c['plan']}/HANDOFF.json")
    if dry or only_list:
        return
    for t in todo:
        lo, hi, end = t["from"], t["to"], t["contiguous_ok_to"]
        if end < hi:
            os.makedirs(out, exist_ok=True)
            rc = 1
            for _ in range(4):
                rc = subprocess.run(c["cmd"] + ["--from", str(end + 1), "--to", str(hi), "--out", out], env=ENV).returncode
                urc, freed = upload_and_prune(chain, out)
                log(chain, lo, hi, f"extract rc={rc}, upload rc={urc}, freed {freed / 1e9:.2f} GB")
                if rc == 0:
                    break
        if contiguous_end(chain, lo, hi) >= hi:
            open("/tmp/rpc-handoff-done", "w").write("done\n")
            subprocess.run([AZ, "copy", "/tmp/rpc-handoff-done", f"{B}/{c['plan']}/DONE.{lo}-{hi}?{sas()}", "--log-level", "ERROR"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
            log(chain, lo, hi, "DONE written")
        else:
            log(chain, lo, hi, "still has holes: rerun rpc_handoff.py")


if __name__ == "__main__":
    main()
