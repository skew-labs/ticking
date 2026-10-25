#!/usr/bin/env python3
"""emv2_fetch.py (skew, as root): bring one published EMV2 build from Blob to skew, fetching only the objects skew lacks.

    nice -n 19 ionice -c3 python3 emv2_fetch.py <chain> <build_id> [--dry-run] [--floor-gb 60] [--max-gb N]

Layout under /srv/skew/emv2/<chain>/:
    objects/<sha256>            content-addressed files (shared by every build of the chain)
    builds/<build_id>/<name>    hard links into objects/ plus manifest.json
    current -> builds/<id>      switched with one rename; emv2-serve re-reads it on SIGHUP
Refuses (exit 2) when free space after the download would fall below the floor (default 60 GB) or the download exceeds --max-gb.
Every object is streamed to a temp file, sha256-checked, then renamed into objects/; a failed check leaves nothing behind.
Egress (skew is outside Azure) is logged at $0.087/GB. Never prints the SAS.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

ACCOUNT = "https://arbitrum.blob.core.windows.net/em-mapping"
ROOT = "/srv/skew/emv2"
EGRESS_USD_PER_GB = 0.087
OWNER = "ubuntu"


def log(*a):
    line = " ".join(str(x) for x in a)
    print(time.strftime("%H:%M:%S", time.gmtime()), line, flush=True)
    os.makedirs(f"{ROOT}/logs", exist_ok=True)
    with open(f"{ROOT}/logs/fetch.log", "a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + line + "\n")


def url(sas, name):
    return f"{ACCOUNT}/{urllib.parse.quote(name)}?{sas}"


def get_json(sas, name):
    with urllib.request.urlopen(url(sas, name), timeout=120) as r:
        return json.loads(r.read())


def fetch_object(sas, chain, sha, size, dest_dir):
    final = os.path.join(dest_dir, sha)
    tmp = final + f".part-{os.getpid()}"
    h = hashlib.sha256()
    with urllib.request.urlopen(url(sas, f"{chain}/emv2/objects/{sha}"), timeout=300) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(8 << 20)
            if not b:
                break
            h.update(b)
            f.write(b)
        f.flush()
        os.fsync(f.fileno())
    if os.path.getsize(tmp) != size or h.hexdigest() != sha:
        os.unlink(tmp)
        raise SystemExit(f"object {sha[:12]} failed verification (size or sha256); nothing kept")
    os.chmod(tmp, 0o444)
    os.rename(tmp, final)


def file_list(man):
    fs = man["files"]
    if isinstance(fs, dict):
        return [dict(v, name=k) for k, v in fs.items()]
    return list(fs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chain")
    ap.add_argument("build_id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--floor-gb", type=float, default=60.0)
    ap.add_argument("--max-gb", type=float, default=40.0)
    ap.add_argument("--no-switch", action="store_true", help="fetch and stage the build but leave current where it is")
    ap.add_argument("--exclude-kinds", default="", help="comma-separated manifest kinds to leave on Blob (cold tier), e.g. g.edge.exact,g.hub.cold")
    a = ap.parse_args()
    sas = open("/root/.em-blob.sas").read().strip().lstrip("?")
    base = f"{ROOT}/{a.chain}"
    objs = f"{base}/objects"
    man = get_json(sas, f"{a.chain}/emv2/{a.build_id}/manifest.json")
    pub = get_json(sas, f"{a.chain}/emv2/{a.build_id}/PUBLISHED.json")
    assert man["chain"] == a.chain and man["build_id"] == a.build_id, "manifest does not match the request"
    files = file_list(man)
    skip = {k for k in a.exclude_kinds.split(",") if k}
    if skip:
        left = [f for f in files if f.get("kind") in skip]
        files = [f for f in files if f.get("kind") not in skip]
        log(f"leaving {len(left)} files ({sum(f['bytes'] for f in left) / 1e9:.2f} GB) of kinds {sorted(skip)} on Blob")
    need = {}
    for f in files:
        if not os.path.exists(os.path.join(objs, f["sha256"])):
            need[f["sha256"]] = f["bytes"]
    total = sum(f["bytes"] for f in files)
    miss = sum(need.values())
    free = shutil.disk_usage("/srv/skew").free
    after = free - miss
    log(f"{a.chain}/{a.build_id}: build {total / 1e9:.2f} GB, missing {len(need)} objects {miss / 1e9:.2f} GB, "
        f"egress ~${miss / 1e9 * EGRESS_USD_PER_GB:.2f}, free {free / 1e9:.1f} GB -> {after / 1e9:.1f} GB (floor {a.floor_gb} GB), "
        f"published {pub.get('published_utc')}")
    if after < a.floor_gb * 1e9:
        log(f"REFUSED {a.chain}: free space would fall below the floor")
        raise SystemExit(2)
    if miss > a.max_gb * 1e9:
        log(f"REFUSED {a.chain}: download {miss / 1e9:.1f} GB exceeds --max-gb {a.max_gb}")
        raise SystemExit(2)
    if a.dry_run:
        return
    os.makedirs(objs, exist_ok=True)
    os.makedirs(f"{base}/builds", exist_ok=True)
    for i, (sha, size) in enumerate(sorted(need.items(), key=lambda kv: -kv[1]), 1):
        fetch_object(sas, a.chain, sha, size, objs)
        if i % 20 == 0 or size > 1e9:
            log(f"  {i}/{len(need)} objects")
    stage = f"{base}/builds/.{a.build_id}.staging"
    final = f"{base}/builds/{a.build_id}"
    if os.path.exists(stage):
        shutil.rmtree(stage)
    for f in files:
        p = os.path.join(stage, f["name"])
        assert os.path.abspath(p).startswith(stage + "/"), f["name"]
        os.makedirs(os.path.dirname(p), exist_ok=True)
        os.link(os.path.join(objs, f["sha256"]), p)
    with open(os.path.join(stage, "manifest.json"), "w") as fh:
        json.dump(man, fh)
    if not os.path.exists(final):
        os.rename(stage, final)
    else:
        shutil.rmtree(stage)
    subprocess.run(["chown", "-R", f"{OWNER}:{OWNER}", base], check=True)
    if a.no_switch:
        log(f"{a.chain}: staged {final}; current unchanged")
        return
    tmp_link = f"{base}/current.tmp"
    if os.path.lexists(tmp_link):
        os.unlink(tmp_link)
    os.symlink(f"builds/{a.build_id}", tmp_link)
    os.rename(tmp_link, f"{base}/current")
    r = subprocess.run(["systemctl", "is-active", "--quiet", "emv2-serve"])
    if r.returncode == 0:
        subprocess.run(["systemctl", "reload", "emv2-serve"], check=True)
        log(f"{a.chain}: current -> {a.build_id}, SIGHUP sent")
    else:
        log(f"{a.chain}: current -> {a.build_id} (emv2-serve not running)")
    log(f"{a.chain}: done, free now {shutil.disk_usage('/srv/skew').free / 1e9:.1f} GB")


if __name__ == "__main__":
    main()
