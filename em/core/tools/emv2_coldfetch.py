#!/usr/bin/env python3
"""emv2_coldfetch.py (skew, root): the network half of the EMV2 cold tier (design/EMV2-NO-NVME.md §3, coldmap.hpp).

A cold segment F is a sparse local cache of its Blob object plus F.cold, one byte per 64 KiB block (1 = present in F).
emv2-serve has no network: on a fault in a missing block it writes "<path>\\t<block>\\n" to the FIFO <root>/.coldfetch.fifo and waits
for the byte to turn 1. This daemon fetches the block's group with an HTTP Range read, writes it into F, then sets the bytes.
Fetched groups are kept in an LRU bounded by --cap-gb; eviction clears the bytes first, then punches the hole.

    emv2_coldfetch.py attach <chain> <build_id> [--kinds g.edge.exact,g.hub.cold,g.node.cold]   files of the build absent locally
    emv2_coldfetch.py daemon [--cap-gb 8] [--workers 24]                                           (emv2-coldfetch.service)
    emv2_coldfetch.py hot <chain> <build_id> <name>...                                            whole files made local
    emv2_coldfetch.py stats

Layout: <root>/<chain>/cold/<sha256> (+ .cold) hard-linked as <root>/<chain>/builds/<id>/<name> (+ .cold); <root>/.cold/COLD.json
lists every cold object. Never prints the SAS.
"""
import argparse
import collections
import ctypes
import grp
import http.client
import json
import os
import queue
import stat
import sys
import threading
import time
import urllib.parse

ROOT = os.environ.get("EMV2_ROOT", "/srv/skew/emv2")
HOST = "arbitrum.blob.core.windows.net"
CONTAINER = "em-mapping"
SAS_FILE = "/root/.em-blob.sas"
BLOCK = 64 << 10
GROUP = 4                      # blocks per Range read (256 KiB)
FIFO = os.path.join(ROOT, ".coldfetch.fifo")
REG = os.path.join(ROOT, ".cold", "COLD.json")
STATS = os.path.join(ROOT, ".cold", "stats.json")
OWNER = "ubuntu"
FALLOC_FL_KEEP_SIZE, FALLOC_FL_PUNCH_HOLE = 0x01, 0x02
libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_long, ctypes.c_long]


def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), *a, flush=True)


def sas():
    return open(SAS_FILE).read().strip().lstrip("?")


class Blob:
    """One keep-alive HTTPS connection (one per worker thread)."""
    def __init__(self):
        self.c = None
        self.sas = sas()

    def get(self, name, start, end):
        path = "/%s/%s?%s" % (CONTAINER, urllib.parse.quote(name), self.sas)
        for attempt in range(8):
            try:
                if self.c is None:
                    self.c = http.client.HTTPSConnection(HOST, timeout=30)
                self.c.request("GET", path, headers={"Range": "bytes=%d-%d" % (start, end - 1), "x-ms-version": "2021-08-06"})
                r = self.c.getresponse()
                body = r.read()
                if r.status in (200, 206) and len(body) == end - start:
                    return body
                if r.status == 403:   # SAS rotated: re-read it once per attempt
                    self.sas = sas(); path = "/%s/%s?%s" % (CONTAINER, urllib.parse.quote(name), self.sas)
                raise OSError("blob status %d, %d bytes" % (r.status, len(body)))
            except (OSError, http.client.HTTPException) as e:
                try:
                    self.c.close()
                except Exception:
                    pass
                self.c = None
                time.sleep(min(8, 0.2 * 2 ** attempt))
                err = str(e)[:120]
        raise OSError("range read failed: " + err)


def load_reg():
    try:
        return json.load(open(REG))
    except (OSError, ValueError):
        return {"objects": {}}


def save_reg(reg):
    os.makedirs(os.path.dirname(REG), exist_ok=True)
    t = REG + ".tmp"
    with open(t, "w") as f:
        json.dump(reg, f, indent=1)
    os.replace(t, REG)


def chown_owner(p):
    try:
        import pwd
        os.chown(p, pwd.getpwnam(OWNER).pw_uid, grp.getgrnam(OWNER).gr_gid)
    except (KeyError, OSError):
        pass


def fetch_into(blob, obj, gidx):
    """Fetch block group gidx of obj into its cache file and mark its blocks present. Returns bytes fetched."""
    size = obj["bytes"]
    start = gidx * GROUP * BLOCK
    end = min(size, start + GROUP * BLOCK)
    if start >= end:
        return 0
    data = blob.get(obj["blob"], start, end)
    fd = os.open(obj["cache"], os.O_WRONLY)
    try:
        os.pwrite(fd, data, start)
    finally:
        os.close(fd)
    b0, b1 = start // BLOCK, (end + BLOCK - 1) // BLOCK
    mfd = os.open(obj["cache"] + ".cold", os.O_WRONLY)
    try:
        os.pwrite(mfd, b"\x01" * (b1 - b0), b0)   # after the data: a reader that sees 1 finds the bytes in the page cache
    finally:
        os.close(mfd)
    return end - start


def attach(a):
    base = os.path.join(ROOT, a.chain)
    bdir = os.path.join(base, "builds", a.build_id)
    man = json.load(open(os.path.join(bdir, "manifest.json")))
    kinds = set(a.kinds.split(",")) if a.kinds else None
    reg = load_reg()
    cdir = os.path.join(base, "cold")
    os.makedirs(cdir, exist_ok=True)
    blob = Blob()
    n = 0
    for name, f in sorted(man["files"].items()):
        if kinds is not None and f.get("kind") not in kinds:
            continue
        link = os.path.join(bdir, name)
        if os.path.exists(link) and not os.path.exists(link + ".cold"):
            continue   # already local (hot)
        sha, size = f["sha256"], int(f["bytes"])
        cache = os.path.join(cdir, sha)
        if not os.path.exists(cache):
            with open(cache, "wb") as fh:
                fh.truncate(size)
        nblk = (size + BLOCK - 1) // BLOCK
        if not os.path.exists(cache + ".cold"):
            with open(cache + ".cold", "wb") as fh:
                fh.truncate(nblk)
        for p in (cache, cache + ".cold"):
            chown_owner(p); os.chmod(p, 0o644)
        obj = {"chain": a.chain, "blob": "%s/emv2/objects/%s" % (a.chain, sha), "bytes": size, "cache": cache, "kind": f.get("kind")}
        # the header and trailer groups are read when the build opens: fetch them now (and never evict them)
        last = (nblk - 1) // GROUP
        for g in sorted({0, last}):
            fetch_into(blob, obj, g)
        obj["pinned"] = sorted({0, last})
        for src, dst in ((cache, link), (cache + ".cold", link + ".cold")):
            if not os.path.exists(dst):
                os.link(src, dst)
        reg["objects"][cache] = obj
        n += 1
        log("attached", name, "%.2f GB" % (size / 1e9), "blocks", nblk)
    save_reg(reg)
    log("attach done:", n, "cold files for", a.chain, a.build_id)


def make_hot(a):
    """Bring whole cold files local (e.g. the offset tables every cold read starts with): parallel Range reads into
    <chain>/objects/<sha>, sha256 checked, then the build's link points at it and the cold cache and byte map go away.
    emv2-serve maps the hot file on its next start / reload."""
    import concurrent.futures
    import hashlib
    reg = load_reg()
    base = os.path.join(ROOT, a.chain)
    bdir = os.path.join(base, "builds", a.build_id)
    man = json.load(open(os.path.join(bdir, "manifest.json")))
    for name in a.names:
        f = man["files"][name]
        sha, size = f["sha256"], int(f["bytes"])
        final = os.path.join(base, "objects", sha)
        if not os.path.exists(final):
            tmp = final + ".part-%d" % os.getpid()
            with open(tmp, "wb") as fh:
                fh.truncate(size)
            step = 8 << 20
            local = threading.local()

            def piece(off):
                if not hasattr(local, "b"):
                    local.b = Blob()
                data = local.b.get("%s/emv2/objects/%s" % (a.chain, sha), off, min(size, off + step))
                fd = os.open(tmp, os.O_WRONLY)
                try:
                    os.pwrite(fd, data, off)
                finally:
                    os.close(fd)
            t0 = time.time()
            with concurrent.futures.ThreadPoolExecutor(16) as ex:
                list(ex.map(piece, range(0, size, step)))
            h = hashlib.sha256()
            with open(tmp, "rb") as fh:
                for blk in iter(lambda: fh.read(16 << 20), b""):
                    h.update(blk)
            if h.hexdigest() != sha:
                os.unlink(tmp); sys.exit("sha256 mismatch: " + name)
            os.chmod(tmp, 0o444); chown_owner(tmp); os.rename(tmp, final)
            log("fetched", name, "%.2f GB in %.0f s" % (size / 1e9, time.time() - t0))
        link = os.path.join(bdir, name)
        tl = link + ".hot-tmp"
        if os.path.exists(tl):
            os.unlink(tl)
        os.link(final, tl); os.replace(tl, link)
        if os.path.exists(link + ".cold"):
            os.unlink(link + ".cold")
        cache = os.path.join(base, "cold", sha)
        reg["objects"].pop(cache, None)
        save_reg(reg)
        for p in (cache, cache + ".cold"):
            if os.path.exists(p):
                os.unlink(p)
        log("hot:", name)


class Daemon:
    def __init__(self, a):
        self.cap = int(a.cap_gb * (1 << 30))
        self.q = queue.Queue()
        self.inflight = set()
        self.mu = threading.Lock()
        self.lru = collections.OrderedDict()   # (cache, gidx) -> bytes
        self.resident = 0
        self.by_ino = {}
        self.reg_mtime = 0
        self.n_req = self.n_fetch = self.n_evict = 0
        self.bytes_fetched = 0
        self.lat = collections.deque(maxlen=2000)
        self.workers = a.workers

    def reload(self):
        try:
            m = os.stat(REG).st_mtime_ns
        except OSError:
            return
        if m == self.reg_mtime:
            return
        self.reg_mtime = m
        reg = load_reg()
        by = {}
        for cache, obj in reg["objects"].items():
            try:
                st = os.stat(cache)
            except OSError:
                continue
            by[(st.st_dev, st.st_ino)] = obj
            with self.mu:   # account blocks already present (from an earlier run) as the oldest entries
                try:
                    present = open(cache + ".cold", "rb").read()
                except OSError:
                    continue
                for g in range(0, (len(present) + GROUP - 1) // GROUP):
                    key = (cache, g)
                    if key in self.lru or g in obj.get("pinned", []):
                        continue
                    if any(present[g * GROUP:(g + 1) * GROUP]):
                        self.lru[key] = GROUP * BLOCK; self.resident += GROUP * BLOCK
                        self.lru.move_to_end(key, last=False)
        self.by_ino = by
        log("registry:", len(by), "cold objects; resident %.2f GB" % (self.resident / 2 ** 30))

    def evict(self):
        while self.resident > self.cap and self.lru:
            with self.mu:
                (cache, g), nb = self.lru.popitem(last=False)
                self.resident -= nb
            try:
                size = os.stat(cache).st_size
                b0 = g * GROUP; b1 = min((size + BLOCK - 1) // BLOCK, b0 + GROUP)
                mfd = os.open(cache + ".cold", os.O_WRONLY)
                os.pwrite(mfd, b"\x00" * (b1 - b0), b0)   # readers stop trusting the bytes before they go
                os.close(mfd)
                fd = os.open(cache, os.O_WRONLY)
                if libc.fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE, b0 * BLOCK, (b1 - b0) * BLOCK) != 0:
                    log("punch hole failed errno", ctypes.get_errno())
                os.close(fd)
                self.n_evict += 1
            except OSError as e:
                log("evict", cache, g, e)

    def worker(self):
        blob = Blob()
        while True:
            obj, g = self.q.get()
            t = time.monotonic()
            try:
                nb = fetch_into(blob, obj, g)
                with self.mu:
                    key = (obj["cache"], g)
                    if key not in self.lru and g not in obj.get("pinned", []):
                        self.lru[key] = nb; self.resident += nb
                    self.n_fetch += 1; self.bytes_fetched += nb
                    self.lat.append((time.monotonic() - t) * 1000)
                self.evict()
            except Exception as e:
                log("fetch failed", obj["blob"].split("/")[-1][:12], g, e)
            finally:
                with self.mu:
                    self.inflight.discard((obj["cache"], g))

    def stats_loop(self):
        while True:
            time.sleep(30)
            self.reload()
            with self.mu:
                lat = sorted(self.lat)
                s = {"t": int(time.time()), "requests": self.n_req, "fetches": self.n_fetch, "evictions": self.n_evict,
                     "fetched_gb": round(self.bytes_fetched / 1e9, 3), "resident_gb": round(self.resident / 2 ** 30, 3),
                     "cap_gb": round(self.cap / 2 ** 30, 1), "objects": len(self.by_ino),
                     "fetch_ms_p50": round(lat[len(lat) // 2], 1) if lat else None,
                     "fetch_ms_p99": round(lat[int(len(lat) * 0.99)], 1) if lat else None}
            t = STATS + ".tmp"
            with open(t, "w") as f:
                json.dump(s, f)
            os.replace(t, STATS)

    def run(self):
        os.makedirs(os.path.dirname(REG), exist_ok=True)
        if not os.path.exists(FIFO):
            os.mkfifo(FIFO, 0o660)
        if not stat.S_ISFIFO(os.stat(FIFO).st_mode):
            sys.exit("not a FIFO: " + FIFO)
        os.chmod(FIFO, 0o660)
        try:
            os.chown(FIFO, 0, grp.getgrnam(OWNER).gr_gid)
        except (KeyError, OSError):
            pass
        self.reload()
        for _ in range(self.workers):
            threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.stats_loop, daemon=True).start()
        fd = os.open(FIFO, os.O_RDWR)   # O_RDWR: never sees EOF when the last writer closes
        buf = b""
        log("daemon up: cap %.1f GB, %d workers, fifo %s" % (self.cap / 2 ** 30, self.workers, FIFO))
        while True:
            chunk = os.read(fd, 65536)
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                try:
                    p, idx = line.decode().split("\t")
                    idx = int(idx)
                    st = os.stat(p)
                except (ValueError, OSError):
                    continue
                obj = self.by_ino.get((st.st_dev, st.st_ino))
                if obj is None:
                    self.reload(); obj = self.by_ino.get((st.st_dev, st.st_ino))
                    if obj is None:
                        continue
                g = idx // GROUP
                key = (obj["cache"], g)
                with self.mu:
                    self.n_req += 1
                    if key in self.inflight:
                        continue
                    if key in self.lru:
                        self.lru.move_to_end(key)   # asked again while present: the serve evicted its copy; keep it warm
                    self.inflight.add(key)
                self.q.put((obj, g))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("attach"); p.add_argument("chain"); p.add_argument("build_id"); p.add_argument("--kinds", default="")
    p = sp.add_parser("daemon"); p.add_argument("--cap-gb", type=float, default=8.0); p.add_argument("--workers", type=int, default=24)
    p = sp.add_parser("hot"); p.add_argument("chain"); p.add_argument("build_id"); p.add_argument("names", nargs="+")
    sp.add_parser("stats")
    a = ap.parse_args()
    if a.cmd == "attach":
        attach(a)
    elif a.cmd == "hot":
        make_hot(a)
    elif a.cmd == "daemon":
        Daemon(a).run()
    else:
        print(open(STATS).read())


main()
