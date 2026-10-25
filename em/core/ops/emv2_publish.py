#!/usr/bin/env python3
"""emv2_publish.py (VM, as root): publish one EMV2 build from local disk to Blob, content-addressed, then re-verify.

    sudo python3 emv2_publish.py /data/emv2-out/<chain>/<build_id>

Input: <dir>/manifest.json (schema economic-machine.emv2.build.v1): {"chain", "build_id", "files": {"<name>": {"bytes", "sha256", ...}}, ...}
with each file at <dir>/<name>; the older list form [{"name", "bytes", "sha256"}] is also accepted. When `emv2-manifest` is available
(EMV2_MANIFEST or /data/emv2-dev/opsbuild/build/emv2-manifest) its `verify` must pass first.
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


def file_list(man):
    fs = man["files"]
    if isinstance(fs, dict):
        return [dict(v, name=k) for k, v in fs.items()]
    return list(fs)


def core_verify(d):
    tool = os.environ.get("EMV2_MANIFEST", "/data/emv2-dev/opsbuild/build/emv2-manifest")
    if not os.access(tool, os.X_OK):
        log(f"emv2-manifest not found at {tool}; relying on the local sha256 pass only")
        return
    r = subprocess.run([tool, "verify", d], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"emv2-manifest verify failed: {(r.stderr or r.stdout).strip()[:300]}")
    log("emv2-manifest verify ok", r.stdout.strip()[:200])


def main():
    d = os.path.abspath(sys.argv[1])
    augment = "--augment" in sys.argv[2:]
    man = json.load(open(os.path.join(d, "manifest.json")))
    chain, build = man["chain"], man["build_id"]
    if augment:
        return publish_augment(d, man, chain, build)
    core_verify(d)
    # A ledger built with --allow-missing (rows skipped because an address is not in the dictionary) is refused unless core
    # approved that exact build: EMV2_ALLOW_MISSING_OK=<build_id> plus EMV2_ALLOW_MISSING_REASON.
    lf = (man.get("modules", {}).get("ledger") or {}) if isinstance(man.get("modules"), dict) else {}
    lf = lf.get("facts", lf) if isinstance(lf, dict) else {}
    missing = int(lf.get("n_addr_missing", 0) or 0)
    exception = None
    if missing or lf.get("allow_missing"):
        if os.environ.get("EMV2_ALLOW_MISSING_OK") != build:
            raise SystemExit(f"{chain}/{build}: ledger skipped {missing} rows (allow_missing); refusing to publish without core's approval for this build")
        exception = dict(allow_missing=True, n_addr_missing=missing, approved_build=build, reason=os.environ.get("EMV2_ALLOW_MISSING_REASON", ""))
        log(f"allow_missing exception approved for {build}: {missing} rows")
    assert re.fullmatch(r"[a-z][a-z0-9]{1,15}", chain) and re.fullmatch(r"[A-Za-z0-9._-]{4,80}", build), (chain, build)
    files = file_list(man)
    log(f"{chain}/{build}: {len(files)} files, {sum(f['bytes'] for f in files) / 1e9:.2f} GB")
    local = {}
    for f in files:
        p = os.path.join(d, f["name"])
        assert os.path.abspath(p).startswith(d + "/"), f["name"]
        size = os.path.getsize(p)
        sha, md5 = hash_file(p)
        if size != f["bytes"] or sha != f["sha256"]:
            raise SystemExit(f"local mismatch {f['name']}: size {size} vs {f['bytes']}, sha {sha[:12]} vs {f['sha256'][:12]}")
        local[sha] = (p, size, md5)
    log(f"local hashes ok: {len(local)} distinct objects")
    # Never replace a published manifest with different content (core rule 12:46): a new layout must get a new build_id.
    mine = base64.b64encode(hashlib.md5(open(os.path.join(d, "manifest.json"), "rb").read()).digest()).decode()
    prev = list_prefix(f"{chain}/emv2/{build}/manifest.json").get(f"{chain}/emv2/{build}/manifest.json")
    if prev and prev[1] != mine:
        raise SystemExit(f"{chain}/emv2/{build}/manifest.json already published with different content; refusing to overwrite")
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
    if exception:
        receipt["exception"] = exception
    put_small(f"{chain}/emv2/{build}/manifest.json", open(os.path.join(d, "manifest.json"), "rb").read())
    put_small(f"{chain}/emv2/{build}/PUBLISHED.json", json.dumps(receipt, indent=1).encode())
    chk = list_prefix(f"{chain}/emv2/{build}/")
    assert f"{chain}/emv2/{build}/manifest.json" in chk and f"{chain}/emv2/{build}/PUBLISHED.json" in chk, "manifest upload not visible"
    json.dump(receipt, open(os.path.join(d, "PUBLISHED.json"), "w"), indent=1)
    log("published", json.dumps(receipt))


def publish_augment(d, man, chain, build):
    """--augment: the build already exists on Blob and only gains files (e.g. blk.time for a graph-only build).
    Files missing locally must already be on Blob (content-addressed, same size); every published file must keep its
    sha256; the previous manifest is kept as manifest.prev-<UTC>.json before the new one replaces it."""
    files = file_list(man)
    url = f"{chain}/emv2/{build}/manifest.json"
    prev_list = list_prefix(url).get(url)
    if not prev_list:
        raise SystemExit(f"{url} is not published; --augment only extends a published build")
    with urllib.request.urlopen(blob_url(url), timeout=120) as r:
        prev_raw = r.read()
    prev = {f["name"]: f for f in file_list(json.loads(prev_raw))}
    new = {f["name"]: f for f in files}
    changed = [n for n in prev if n not in new or new[n]["sha256"] != prev[n]["sha256"] or new[n]["bytes"] != prev[n]["bytes"]]
    if changed:
        raise SystemExit(f"--augment refused: {len(changed)} published files would change or disappear, e.g. {changed[:3]}")
    added = [new[n] for n in new if n not in prev]
    pre = f"{chain}/emv2/objects/"
    have = list_prefix(pre)
    up = 0
    for f in added:
        p = os.path.join(d, f["name"])
        sha, md5 = hash_file(p)
        if os.path.getsize(p) != f["bytes"] or sha != f["sha256"]:
            raise SystemExit(f"local mismatch {f['name']}")
        got = have.get(pre + sha)
        if got and got[0] == f["bytes"]:
            continue
        azcopy(p, pre + sha); up += 1
    after = list_prefix(pre)
    missing = [n for n, f in new.items() if after.get(pre + f["sha256"], (None,))[0] != f["bytes"]]
    if missing:
        raise SystemExit(f"--augment: {len(missing)} objects not on Blob with the right size, e.g. {missing[:3]}; manifest not replaced")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    put_small(f"{chain}/emv2/{build}/manifest.prev-{stamp}.json", prev_raw)
    put_small(url, open(os.path.join(d, "manifest.json"), "rb").read())
    receipt = dict(chain=chain, build_id=build, augment=True, added=[f["name"] for f in added], uploaded=up,
                   previous_manifest=f"manifest.prev-{stamp}.json", objects=len(new), published_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    put_small(f"{chain}/emv2/{build}/PUBLISHED.augment-{stamp}.json", json.dumps(receipt, indent=1).encode())
    log("augmented", json.dumps(receipt))


if __name__ == "__main__":
    main()
