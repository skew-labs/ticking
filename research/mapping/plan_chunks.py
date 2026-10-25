#!/usr/bin/env python3
"""Plan per-range inputs for build_map.sh from em-extract manifests.

A chunk is usable iff manifests/<from>-<to>.json exists, status == OK, and every table file it
lists exists with the recorded size (in-progress *.tmp-* files are never referenced by
manifests).  Chunks are grouped into ranges of RANGE blocks.  For each range a signature
(sha1 over chunk ranges + file sha256s) lets the runner skip ranges whose inputs are unchanged.

usage: plan_chunks.py <out_dir> <plan_dir> <range_size> <max_block|-> <partial 0|1> [min_block]
writes <plan_dir>/ranges.tsv  (rid  n_chunks  expected  complete  signature  first  last)
       <plan_dir>/r<rid>.<table>.list (one absolute parquet path per line)
       <plan_dir>/r<rid>.counts.tsv   (per chunk, same order: transactions token_transfers logs)
"""
import glob, hashlib, json, os, sys

TABLES = ("blocks", "transactions", "logs", "token_transfers", "contract_creations")


def main():
    out, plan, rsize, max_block, partial = sys.argv[1:6]
    min_block = int(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] not in ("-", "") else 0
    rsize = int(rsize)
    max_block = None if max_block in ("-", "") else int(max_block)
    partial = partial == "1"
    os.makedirs(plan, exist_ok=True)
    for f in (glob.glob(os.path.join(plan, "r*.list")) + glob.glob(os.path.join(plan, "r*.counts.tsv"))
              + glob.glob(os.path.join(plan, "ranges.tsv"))):
        os.remove(f)
    ranges = {}
    chunk_size = None
    bad = 0
    for mf in sorted(glob.glob(os.path.join(out, "manifests", "*.json"))):
        try:
            with open(mf) as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            bad += 1
            continue
        if m.get("status") != "OK":
            continue
        f, t = m["range"]["from"], m["range"]["to"]
        if max_block is not None and t >= max_block:
            continue
        if f < min_block:
            continue
        files = m.get("files", {})
        ok = True
        paths = {}
        for tb in TABLES:
            fi = files.get(tb)
            if not fi:
                ok = False
                break
            p = os.path.join(out, fi["path"])
            try:
                if os.path.getsize(p) != fi["bytes"]:
                    ok = False
                    break
            except OSError:
                ok = False
                break
            paths[tb] = (p, fi.get("sha256", ""))
        if not ok:
            bad += 1
            continue
        chunk_size = chunk_size or (t - f + 1)
        rid = f // rsize
        c = m.get("counts", {})
        paths["_counts"] = (c.get("transactions", 0), c.get("token_transfers", 0), c.get("logs", 0))
        ranges.setdefault(rid, []).append((f, t, paths))
    expected = rsize // chunk_size if chunk_size else 0
    with open(os.path.join(plan, "ranges.tsv"), "w") as tsv:
        for rid in sorted(ranges):
            chunks = sorted(ranges[rid])
            complete = len(chunks) == expected
            if max_block is not None and (rid + 1) * rsize > max_block:
                # range cut by MAX_BLOCK: complete iff it covers every chunk below max_block
                complete = len(chunks) == (max_block - rid * rsize) // chunk_size
            if not complete and not partial:
                continue
            h = hashlib.sha1()
            for f, t, paths in chunks:
                h.update(f"{f}-{t}".encode())
                for tb in TABLES:
                    h.update(paths[tb][1].encode())
            for tb in TABLES:
                with open(os.path.join(plan, f"r{rid:03d}.{tb}.list"), "w") as lf:
                    for _, _, paths in chunks:
                        lf.write(paths[tb][0] + "\n")
            with open(os.path.join(plan, f"r{rid:03d}.counts.tsv"), "w") as cf:
                for _, _, paths in chunks:
                    cf.write("%d\t%d\t%d\n" % paths["_counts"])
            tsv.write(f"{rid:03d}\t{len(chunks)}\t{expected}\t{int(complete)}\t{h.hexdigest()}\t"
                      f"{chunks[0][0]}\t{chunks[-1][1]}\n")
    if bad:
        print(f"plan: {bad} manifests unreadable or with missing/mismatched files (skipped)", file=sys.stderr)


if __name__ == "__main__":
    main()
