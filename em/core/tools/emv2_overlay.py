#!/usr/bin/env python3
"""emv2_overlay.py CHAIN OUT_DIR --labels L.parquet [--labels L2.parquet] [--tokens token_meta.parquet]
Writes the address-keyed overlay (include/machine/engine/overlay.hpp) read by emv2-serve: ovl.label.seg, ovl.token.seg, ovl.str.seg
in the V2 segment format (payload + 64-byte EMV2SEG1 trailer) and overlay.json. Several label files: for one address the row with
the highest confidence wins (ties: the later file). Writes into OUT_DIR.tmp-<pid>, then renames over OUT_DIR. Run with a pyarrow
python (skew: /srv/skew/em-refresh/.venv/bin/python)."""
import hashlib, json, os, shutil, struct, sys, time
import pyarrow.parquet as pq

KIND = {"ovl.label": 7, "ovl.str": 8, "ovl.token": 9, "ovl.entity": 10}
CATS = ["cex_hot_wallet", "cex_deposit", "bridge", "dex_router", "dex_pool", "lending", "token_contract", "nft_contract", "mev_bot",
        "multisig_or_safe", "contract_other", "eoa_user", "eoa_whale", "unknown"]
SOURCES = ["none", "classifier", "registry", "propagation", "agent", "manual"]


def seg(path, name, payload, count, elem):
    # SegTrailer: payload_bytes, count, aux, aux2 (u64), kind, elem, codec, version (u32), magic[8], reserved[8]
    tr = struct.pack("<QQQQIIII8s8s", len(payload), count, 0, 0, KIND[name], elem, 0, 1, b"EMV2SEG1", b"\0" * 8)
    data = payload + tr
    with open(os.path.join(path, name + ".seg"), "wb") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    return {"name": name + ".seg", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "count": count}


def addr_bytes(v):
    if isinstance(v, (bytes, bytearray)) and len(v) == 20: return bytes(v)
    if isinstance(v, str):
        s = v[2:] if v.startswith("0x") else v
        if len(s) == 40: return bytes.fromhex(s)
    return None


def main():
    a = sys.argv[1:]
    if len(a) < 2: sys.exit(__doc__)
    chain, out = a[0], a[1]; labels, tokens = [], None
    i = 2
    while i < len(a):
        if a[i] == "--labels": labels.append(a[i + 1]); i += 2
        elif a[i] == "--tokens": tokens = a[i + 1]; i += 2
        else: sys.exit("unknown option " + a[i])
    strs = bytearray(); best = {}
    for li, p in enumerate(labels):
        t = pq.read_table(p, columns=["address", "category_code", "confidence", "source", "tier", "entity_name"])
        cols = [t.column(c).to_pylist() for c in ("address", "category_code", "confidence", "source", "tier", "entity_name")]
        for ad, cat, conf, src, tier, ent in zip(*cols):
            b = addr_bytes(ad)
            if b is None or cat is None or cat >= len(CATS): continue
            conf = float(conf or 0.0); old = best.get(b)
            if old is None or conf >= old[1]:
                best[b] = (int(cat), conf, SOURCES.index(src) if src in SOURCES else 0, int(tier or 0), (ent or "")[:200])
    lab = bytearray()
    for b in sorted(best):
        cat, conf, src, tier, ent = best[b]
        e = ent.encode("utf-8")[:65535]; off = len(strs); strs += e
        lab += struct.pack("<20sBBBBIHH", b, cat, max(0, min(255, round(conf * 255))), src, tier, off, len(e), 0)
    def fnv(name):
        h = 14695981039346656037
        for c in name.lower().encode("utf-8"):
            h ^= c; h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        return h
    ent_rows = sorted((fnv(v[4]), b) for b, v in best.items() if v[4])
    ent = bytearray()
    for h, b in ent_rows: ent += struct.pack("<Q20sI", h, b, 0)
    tok = bytearray(); ntok = 0
    if tokens:
        t = pq.read_table(tokens, columns=["token", "symbol", "name", "decimals"])
        rows = {}
        for ad, sym, name, dec in zip(*(t.column(c).to_pylist() for c in ("token", "symbol", "name", "decimals"))):
            b = addr_bytes(ad)
            if b is not None and b not in rows: rows[b] = ((sym or "")[:64], (name or "")[:128], 255 if dec is None or not 0 <= dec <= 254 else int(dec))
        for b in sorted(rows):
            sym, name, dec = rows[b]; s, n = sym.encode("utf-8"), name.encode("utf-8"); off = len(strs); strs += s + n
            tok += struct.pack("<20sBBHIHH", b, dec, 0, len(s), off, len(n), 0); ntok += 1
    tmp = out + ".tmp-%d" % os.getpid(); os.makedirs(tmp)
    files = [seg(tmp, "ovl.label", bytes(lab), len(best), 32), seg(tmp, "ovl.token", bytes(tok), ntok, 32), seg(tmp, "ovl.str", bytes(strs), len(strs), 0),
             seg(tmp, "ovl.entity", bytes(ent), len(ent_rows), 32)]
    meta = {"schema": "economic-machine.emv2.overlay.v1", "chain": chain, "created_at": int(time.time()), "labels": len(best), "tokens": ntok, "entities": len(ent_rows),
            "sources": labels + ([tokens] if tokens else []), "files": files}
    with open(os.path.join(tmp, "overlay.json"), "w") as f: json.dump(meta, f, indent=1)
    if os.path.exists(out): shutil.rmtree(out + ".old", ignore_errors=True); os.rename(out, out + ".old")
    os.rename(tmp, out); shutil.rmtree(out + ".old", ignore_errors=True)
    print(json.dumps({"chain": chain, "labels": len(best), "tokens": ntok, "str_bytes": len(strs)}))


if __name__ == "__main__":
    main()
