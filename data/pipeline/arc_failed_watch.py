"""arc_failed_watch.py [--until HH:MM]: hand Arc chunks the VMs gave up on to skew right away (controller, 2026-10-19 night).

Every 10 min until --until (UTC, default 05:05):
  1. list Blob realtime/arc/manifests for the PLAN ranges without DONE; FAILED manifests are small, so the listing sizes
     pick the candidates and only those are read (status + failure detail);
  2. a chunk whose FAILED manifest has not changed for STALE_MIN minutes is one its VM stopped retrying (a VM that still
     retries re-uploads it every 5 min); it is re-extracted on skew receipts-only (PublicNode + dRPC from skew's IP) into
     out/arc-raw and uploaded at once;
  3. a range that is contiguous in Blob gets its DONE marker (the Arc bring-up waits for all 24).
Log: logs/arc-failed-watch.log. The SAS never appears in it.
"""
import concurrent.futures as cf
import email.utils
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

B = "https://arbitrum.blob.core.windows.net/em-mapping"
R = "/srv/skew/em-realtime"
OUT = R + "/out/arc-raw"
AZ = "/root/bin/azcopy"
OP = R + "/bin/emrpc-op"
STALE_MIN = 20
SINCE = 1792449480      # 2026-10-19 22:38 UTC, this VM session's start: older FAILED manifests are earlier sessions' leftovers
SMALL = 1200            # bytes: FAILED manifests are a few hundred bytes, OK ones a few KB
ENV = dict(os.environ, HOME=os.environ.get("HOME") or "/root")


def sas():
    return open("/root/.em-blob.sas").read().strip().lstrip("?")


def log(*a):
    print(time.strftime("%FT%TZ", time.gmtime()), *a, flush=True)


def listing(prefix):
    out, marker = [], ""
    while True:
        u = f"{B}?restype=container&comp=list&prefix={prefix}&maxresults=5000&{sas()}" + (f"&marker={marker}" if marker else "")
        x = urllib.request.urlopen(u, timeout=120).read().decode()
        for blob in re.findall(r"<Blob>(.*?)</Blob>", x, re.S):
            n = re.search(r"<Name>([^<]+)</Name>", blob).group(1)
            size = int(re.search(r"<Content-Length>(\d+)</Content-Length>", blob).group(1))
            lm = email.utils.parsedate_to_datetime(re.search(r"<Last-Modified>([^<]+)</Last-Modified>", blob).group(1)).timestamp()
            out.append((n, size, lm))
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", x)
        if not m:
            return out
        marker = m.group(1)


def get_json(name):
    try:
        return json.loads(urllib.request.urlopen(f"{B}/{name}?{sas()}", timeout=60).read())
    except Exception:
        return None


def open_ranges():
    plan = [tuple(map(int, l.split())) for l in urllib.request.urlopen(f"{B}/arc/rpc-ranges/PLAN?{sas()}", timeout=60).read().decode().splitlines() if l.strip()]
    done = {n.rsplit("DONE.", 1)[1] for n, _, _ in listing("arc/rpc-ranges/DONE.")}
    return [(a, b) for a, b in plan if f"{a}-{b}" not in done]


def chunk_of(name):
    m = re.search(r"(\d{10})-(\d{10})\.json$", name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def contiguous(ms, lo, hi):
    """ms: {(from, to): status}; True when OK chunks cover lo..hi without a hole."""
    nxt = lo
    for (a, b) in sorted(k for k, s in ms.items() if s == "OK"):
        if a > nxt:
            return False
        nxt = max(nxt, b + 1)
    return nxt > hi


def redo(a, b):
    cmd = ["nice", "-n", "10", OP, "--chain-id", "5042", "--receipts-only", "--chunk", "1000", "--retries", "12", "--from", str(a), "--to", str(b),
           "--out", OUT, "--rpc", "https://arc-rpc.publicnode.com#workers=16", "--rpc", "https://arc.drpc.org#workers=4"]
    rc = subprocess.run(cmd, stdout=open(f"{R}/logs/arc-failed-redo.log", "a"), stderr=subprocess.STDOUT, env=ENV, timeout=3600).returncode
    tag = f"{a:010d}-{b:010d}"
    m = f"{OUT}/manifests/{tag}.json"
    ok = os.path.exists(m) and json.load(open(m)).get("status") == "OK"
    if ok:   # upload this chunk now (manifest last, so readers never see an OK manifest before its files)
        for f in sorted(glob.glob(f"{OUT}/*/*_{tag}.parquet")) + [m]:
            rel = os.path.relpath(f, OUT)
            subprocess.run([AZ, "copy", f, f"{B}/realtime/arc/{rel}?{sas()}", "--overwrite=true", "--put-md5", "--log-level", "ERROR"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
    return rc, ok


def one_pass():
    rngs = open_ranges()
    if not rngs:
        log("all PLAN ranges DONE")
        return True
    names = [x for x in listing("realtime/arc/manifests/") if chunk_of(x[0]) and any(a <= chunk_of(x[0])[0] <= b for a, b in rngs)]
    small = [x for x in names if x[1] < SMALL]
    with cf.ThreadPoolExecutor(32) as ex:
        js = dict(zip([x[0] for x in small], ex.map(lambda x: get_json(x[0]), small)))
    st = {chunk_of(n): ("OK" if s >= SMALL else ((js.get(n) or {}).get("status") or "?")) for n, s, _ in names}
    now = time.time()
    stale = [(n, lm, (js.get(n) or {}).get("failure", {}).get("detail", "")) for n, s, lm in small
             if (js.get(n) or {}).get("status") == "FAILED" and SINCE <= lm and now - lm > STALE_MIN * 60]
    log(f"open ranges {len(rngs)}, chunks listed {len(names)}, FAILED {sum(1 for v in st.values() if v == 'FAILED')}, stale FAILED {len(stale)}")
    if DRY:
        for n, lm, detail in stale[:5]:
            log(f"would redo {n} ({int((now - lm) / 60)} min old): {detail[:90]}")
        return False
    for n, lm, detail in stale:
        a, b = chunk_of(n)
        rc, ok = redo(a, b)
        st[(a, b)] = "OK" if ok else "FAILED"
        log(f"redo {a}-{b} (failed {int((now - lm) / 60)} min ago: {detail[:90]}) -> rc={rc} ok={ok}")
    for a, b in rngs:
        if contiguous({k: v for k, v in st.items() if a <= k[0] <= b}, a, b):
            open("/tmp/arc-watch-done", "w").write("done\n")
            subprocess.run([AZ, "copy", "/tmp/arc-watch-done", f"{B}/arc/rpc-ranges/DONE.{a}-{b}?{sas()}", "--log-level", "ERROR"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
            log(f"range {a}-{b} contiguous in Blob: DONE written")
    return False


DRY = "--dry-run" in sys.argv


def main():
    until = sys.argv[sys.argv.index("--until") + 1] if "--until" in sys.argv else "05:05"
    hh, mm = map(int, until.split(":"))
    while True:
        t = time.gmtime()
        if (t.tm_hour, t.tm_min) >= (hh, mm) and t.tm_hour < 12:
            log(f"reached {until} UTC: the 05:05 handoff takes over")
            return
        try:
            if one_pass() or DRY:
                return
        except Exception as e:
            log("pass failed:", str(e).replace(sas(), "SAS")[:200])
        time.sleep(600)


if __name__ == "__main__":
    main()
