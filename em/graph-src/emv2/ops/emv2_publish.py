#!/usr/bin/env python3
"""emv2_publish.py (VM, as root): publish one EMV2 build from local disk to Blob, content-addressed, then re-verify.

    sudo python3 emv2_publish.py /data/emv2-out/<chain>/<build_id>

Input: <dir>/manifest.json {"chain", "build_id", "files": [{"name", "bytes", "sha256", "kind"}, ...], ...} with each file at <dir>/<name>.
Output on Blob (container em-mapping):
    <chain>/emv2/objects/<sha256>               one blob per distinct file content (skipped when already present with the same size)
    <chain>/emv2/<build_id>/manifest.json       the manifest as given
    <chain>/emv2/<build_id>/PUBLISHED.json      receipt: object count, bytes, uploaded vs reused, verification result
Steps: (1) re-hash every local file and require size and sha256 to match the manifest; (2) list the objects prefix; (3) azcopy the
missing objects with --put-md5; (4) list again and require every object's size and Content-MD5 to equal the local file's; (5) upload
the manifest and receipt. Exit 1 on any mismatch, before the manifest is published, so a half-published build is never visible.
Never prints the SAS.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ACCOUNT = "https://arbitrum.blob.core.windows.net/em-mapping"
SAS = open("/root/.em-blob.sas").read().strip().lstrip("?")
CAP_MBPS = os.environ.get("EMV2_CAP_MBPS", "2000")


def log(*a):
    print(time.strftime("%H:%M:%S", time.gmtime()), *a, flush=True)


def blob_url(name):
    return f"{ACCOUNT}/{urllib.parse.quote(name)}?{SAS}"


def list_prefix(prefix):
    out, marker = {}, None
    while True:
        q = f"restype=container&comp=list&prefix={urllib.parse.quote(prefix)}&maxresults=5000" + (f"&marker={urllib.parse.quote(marker)}" if marker else "")
        with urllib.request.urlopen(f"{ACCOUNT}?{q}&{SAS}", timeout=120) as r:
            x = r.read().decode()
        for b in re.findall(r"<Blob>(.*?)</Blob>", x, re.S):
            name = re.search(r"<Name>(.*?)</Name>", b).group(1)
            size = int(re.search(r"<Content-Length>(\d+)</Content-Length>", b).group(1))
            md5 = re.search(r"<Content-MD5>(.*?)</Content-MD5>", b)
            out[name] = (size, md5.group(1) if md5 else "")
        m = re.search(r"<NextMarker>(.*?)</NextMarker>", x)
        marker = m.group(1) if m and m.group(1) else None
        if not marker:
            return out


def hash_file(path):
    s, m = hashlib.sha256(), hashlib.md5()
    with open(path, "rb", buffering=0) as f:
        while True:
            b = f.read(16 << 20)
            if not b:
                break
            s.update(b)
            m.update(b)
    return s.hexdigest(), base64.b64encode(m.digest()).decode()


def put_small(name, data):
    req = urllib.request.Request(blob_url(name), data=data, method="PUT",
                                 headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "application/json",
                                          "Content-MD5": base64.b64encode(hashlib.md5(data).digest()).decode()})
    urllib.request.urlopen(req, timeout=120).read()


def azcopy(src, name):
    env = dict(os.environ, AZCOPY_BUFFER_GB="1", HOME="/root", AZCOPY_LOG_LOCATION="/data/emv2-dev/azcopy-logs")
    os.makedirs(env["AZCOPY_LOG_LOCATION"], exist_ok=True)
    r = subprocess.run(["azcopy", "copy", src, blob_url(name), "--put-md5", "--overwrite=false", "--cap-mbps", CAP_MBPS,
                        "--log-level", "ERROR", "--output-level", "essential"], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"azcopy failed for {os.path.basename(src)} (exit {r.returncode}); details in {env['AZCOPY_LOG_LOCATION']}")


def main():
    d = os.path.abspath(sys.argv[1])
    man = json.load(open(os.path.join(d, "manifest.json")))
    chain, build = man["chain"], man["build_id"]
    assert re.fullmatch(r"[a-z][a-z0-9]{1,15}", chain) and re.fullmatch(r"[A-Za-z0-9._-]{4,80}", build), (chain, build)
    files = man["files"]
    log(f"{chain}/{build}: {len(files)} files, {sum(f['bytes'] for f in files) / 1e9:.2f} GB")
    local = {}
    for f in files:
        p = os.path.join(d, f["name"])
        size = os.path.getsize(p)
        sha, md5 = hash_file(p)
        if size != f["bytes"] or sha != f["sha256"]:
            raise SystemExit(f"local mismatch {f['name']}: size {size} vs {f['bytes']}, sha {sha[:12]} vs {f['sha256'][:12]}")
        local[sha] = (p, size, md5)
    log(f"local hashes ok: {len(local)} distinct objects")
    pre = f"{chain}/emv2/objects/"
    have = list_prefix(pre)
    up = reused = 0
    for sha, (p, size, md5) in local.items():
        got = have.get(pre + sha)
        if got and got[0] == size and got[1] == md5:
            reused += 1
            continue
        if got:
            raise SystemExit(f"object {sha[:12]} exists with different size or MD5; refusing to overwrite")
        azcopy(p, pre + sha)
        up += 1
    after = list_prefix(pre)
    bad = [sha for sha, (p, size, md5) in local.items() if after.get(pre + sha) != (size, md5)]
    if bad:
        raise SystemExit(f"verification failed for {len(bad)} objects, e.g. {bad[0][:12]}; manifest not published")
    up_bytes = sum(local[s][1] for s in local if pre + s not in have)
    receipt = dict(chain=chain, build_id=build, objects=len(local), bytes=sum(v[1] for v in local.values()),
                   uploaded=up, uploaded_bytes=up_bytes, reused=reused, verified="size+content-md5 per object, sha256 recomputed locally",
                   published_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    put_small(f"{chain}/emv2/{build}/manifest.json", open(os.path.join(d, "manifest.json"), "rb").read())
    put_small(f"{chain}/emv2/{build}/PUBLISHED.json", json.dumps(receipt, indent=1).encode())
    chk = list_prefix(f"{chain}/emv2/{build}/")
    assert f"{chain}/emv2/{build}/manifest.json" in chk and f"{chain}/emv2/{build}/PUBLISHED.json" in chk, "manifest upload not visible"
    json.dump(receipt, open(os.path.join(d, "PUBLISHED.json"), "w"), indent=1)
    log("published", json.dumps(receipt))


if __name__ == "__main__":
    main()
