#!/usr/bin/env python3
"""emv2_inputs.py (VM, as root): describe a build's Blob inputs for `emv2-manifest build-id` without hashing terabytes.

    sudo python3 emv2_inputs.py <out.json> <blob-prefix> [<blob-prefix> ...]

Lists every blob under each prefix and writes [{"name": <blob path>, "sha256": "md5:<Content-MD5>" | "etag:<ETag>", "bytes", "rows": 0}]
sorted by name, so the same inputs always give the same build_id. Prints a one-line summary per prefix. Never prints the SAS.
"""
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ACCOUNT = "https://arbitrum.blob.core.windows.net/em-mapping"
SAS = open("/root/.em-blob.sas").read().strip().lstrip("?")


def listing(prefix):
    marker = None
    while True:
        q = f"restype=container&comp=list&prefix={urllib.parse.quote(prefix)}&maxresults=5000" + (f"&marker={urllib.parse.quote(marker)}" if marker else "")
        with urllib.request.urlopen(f"{ACCOUNT}?{q}&{SAS}", timeout=120) as r:
            x = r.read().decode()
        for b in re.findall(r"<Blob>(.*?)</Blob>", x, re.S):
            name = re.search(r"<Name>(.*?)</Name>", b).group(1)
            size = int(re.search(r"<Content-Length>(\d+)</Content-Length>", b).group(1))
            md5 = re.search(r"<Content-MD5>(.+?)</Content-MD5>", b)
            etag = re.search(r"<Etag>(.*?)</Etag>", b)
            yield name, size, ("md5:" + md5.group(1)) if md5 else ("etag:" + (etag.group(1).strip('"') if etag else ""))
        m = re.search(r"<NextMarker>(.*?)</NextMarker>", x)
        marker = m.group(1) if m and m.group(1) else None
        if not marker:
            return


def local(d):
    # A local input directory (inputs that are not on Blob, e.g. arc's map copied from skew): sha256 every file. Small inputs only.
    for root, _, files in sorted(os.walk(d)):
        for f in sorted(files):
            p = os.path.join(root, f)
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for b in iter(lambda: fh.read(16 << 20), b""):
                    h.update(b)
            yield "local:" + p, os.path.getsize(p), h.hexdigest()


def main():
    out, prefixes = sys.argv[1], sys.argv[2:]
    rows = []
    for p in prefixes:
        src = local(p) if p.startswith("/") else listing(p)
        got = [dict(name=n, sha256=h, bytes=s, rows=0) for n, s, h in src
               if not re.search(r"(\.tmp(-\d+)?$|/_temporary/|\.part-\d+$)", n)]
        print(f"{p}: {len(got)} blobs, {sum(g['bytes'] for g in got) / 1e9:.2f} GB")
        if not got:
            raise SystemExit(f"no inputs under {p}")
        rows += got
    rows.sort(key=lambda r: r["name"])
    json.dump(rows, open(out, "w"), indent=0)
    print(f"wrote {out}: {len(rows)} inputs, {sum(r['bytes'] for r in rows) / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
