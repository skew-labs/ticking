#!/usr/bin/env python3
"""em_backup.py: daily backup of the Economic Machine state on skew to Blob em-mapping/_backup/skew-state/<YYYYMMDD>/ (7 days kept).

  SQLite (online, sqlite3 backup API, so a writer is never blocked): the production account DB, the staging account DB and
  galaxy.db. Folders (tar): graph-loop state (WAL + snapshots), label-delta state, bring-up join state, refresh state.
  Every file is zstd-compressed, uploaded with --put-md5 and checked against Blob (size + MD5); manifest.json lists
  sha256 / bytes per file. Secrets are never copied: *.env, *.token, *.key, *.pem and session files are skipped (they are
  re-issued, not restored). Dates older than KEEP_DAYS are removed from Blob after a good upload.
Run by em-map-backup.timer through em_guard.sh --no-lock (quiet IO window, capped IO). Exit 0 ok, 1 failed.
"""
import base64
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request

B = "https://arbitrum.blob.core.windows.net/em-mapping"
PREFIX = "_backup/skew-state"
KEEP_DAYS = 7
TMP = "/srv/skew/em-data/tmp/backup"
AZ = shutil.which("azcopy") or "/root/bin/azcopy"
SQLITE = {"account-prod.sqlite3": "/srv/skew/em-account-prod/state/account.sqlite3",
          "account-staging.sqlite3": "/srv/skew/em-account/state/account.sqlite3",
          "galaxy.db": "/srv/skew/em-galaxy/galaxy.db"}
DIRS = {"graphloop-state": "/srv/skew/em-graphloop/state", "labeldelta-glstate": "/srv/skew/em-labeldelta/glstate",
        "labeldelta-state": "/srv/skew/em-labeldelta/state", "realtime-join": "/srv/skew/em-realtime/join",
        "refresh-state": "/srv/skew/em-refresh/state"}
SECRET = ("*.env", "*.token", "*.key", "*.pem", "*session*.json", "*.sas", "*secret*")
FLOOR_GB = 62


def log(*a):
    print(dt.datetime.now(dt.timezone.utc).strftime("%FT%TZ"), "em_backup:", *a, flush=True)


def sas():
    return open("/root/.em-blob.sas").read().strip().lstrip("?")


def free_gb():
    st = os.statvfs("/")
    return st.f_bavail * st.f_frsize / 1e9


def az(*args):
    env = dict(os.environ, HOME=os.environ.get("HOME") or "/root", AZCOPY_BUFFER_GB="0.5")
    r = subprocess.run([AZ, *args, "--log-level", "ERROR"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError("azcopy %s rc=%d %s" % (args[0], r.returncode, r.stderr.replace(sas(), "SAS")[-200:]))


def blob_list(prefix):
    out, marker = {}, ""
    while True:
        x = urllib.request.urlopen("%s?restype=container&comp=list&maxresults=5000&prefix=%s%s&%s" % (
            B, urllib.parse.quote(prefix), "&marker=" + marker if marker else "", sas()), timeout=60).read().decode()
        for b in re.findall(r"<Blob>(.*?)</Blob>", x, re.S):
            n = re.search(r"<Name>([^<]+)</Name>", b).group(1)
            md5 = re.search(r"<Content-MD5>([^<]*)</Content-MD5>", b)
            out[n] = (int(re.search(r"<Content-Length>(\d+)</Content-Length>", b).group(1)), md5.group(1) if md5 else None)
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", x)
        if not m:
            return out
        marker = m.group(1)


def digest(p):
    h, m = hashlib.sha256(), hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 22), b""):
            h.update(c)
            m.update(c)
    return h.hexdigest(), base64.b64encode(m.digest()).decode()


def zst(src):
    subprocess.run(["zstd", "-q", "-T2", "-6", "--rm", "-f", src, "-o", src + ".zst"], check=True)
    return src + ".zst"


def main():
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    need = sum(os.path.getsize(p) for p in SQLITE.values() if os.path.isfile(p)) / 1e9 * 1.5
    if free_gb() - need < FLOOR_GB:
        log("free %.0f GB - %.1f GB needed < %d GB floor: skipped" % (free_gb(), need, FLOOR_GB))
        return 1
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    files = []
    try:
        for name, path in SQLITE.items():
            if not os.path.isfile(path):
                log("missing", path)
                continue
            dst = os.path.join(TMP, name)
            src, out = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30), sqlite3.connect(dst)
            with out:
                src.backup(out, pages=4096)
            src.close()
            out.close()
            files.append(zst(dst))
        for name, path in DIRS.items():
            if not os.path.isdir(path):
                continue
            dst = os.path.join(TMP, name + ".tar")
            with tarfile.open(dst, "w") as t:
                t.add(path, arcname=name, filter=lambda ti: None if any(fnmatch.fnmatch(os.path.basename(ti.name), s) for s in SECRET) else ti)
            files.append(zst(dst))
        man = {"schema": "em-backup/1", "host": "skew", "day": day, "at": dt.datetime.now(dt.timezone.utc).strftime("%FT%TZ"), "files": {}}
        for f in files:
            sha, md5 = digest(f)
            man["files"][os.path.basename(f)] = {"bytes": os.path.getsize(f), "sha256": sha, "md5": md5}
        mp = os.path.join(TMP, "manifest.json")
        json.dump(man, open(mp, "w"), indent=1)
        pre = "%s/%s" % (PREFIX, day)
        az("copy", TMP + "/*", "%s/%s/?%s" % (B, pre, sas()), "--put-md5", "--overwrite", "true", "--cap-mbps", "160")
        have = blob_list(pre + "/")
        bad = [n for n, f in man["files"].items() if have.get("%s/%s" % (pre, n)) != (f["bytes"], f["md5"])]
        if bad or "%s/manifest.json" % pre not in have:
            log("Blob check FAILED:", bad or "manifest missing")
            return 1
        log("uploaded %s: %d files, %.2f GB, Blob size+MD5 match" % (pre, len(files), sum(f["bytes"] for f in man["files"].values()) / 1e9))
        cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=KEEP_DAYS)).strftime("%Y%m%d")
        for d in sorted({n.split("/")[2] for n in blob_list(PREFIX + "/") if n.count("/") >= 3}):
            if re.fullmatch(r"\d{8}", d) and d < cut:
                az("rm", "%s/%s/%s?%s" % (B, PREFIX, d, sas()), "--recursive")
                log("removed backup day", d, "(older than %d days)" % KEEP_DAYS)
        return 0
    finally:
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("FAILED:", str(e).replace(sas(), "SAS")[:300])
        sys.exit(1)
