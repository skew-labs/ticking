"""okx_por.py <por.zip> <out.parquet>: OKX's official Proof-of-Reserves address file -> verified EVM addresses on our chains.

Each EVM row carries `message` ("I am an OKX address") and a signature by that address over OKX's EVM hash
keccak256("\x19Ethereum Signed Message:\n32" || keccak256(message)) (okx/proof-of-reserves, MIT). We recover the signer with a pure-Python secp256k1 implementation and keep only rows whose recovered address equals the listed address
(grade official, evidence: snapshot height, message, signature, coins and amounts).
"""
import csv
import io
import sys
import zipfile

import duckdb
from Crypto.Hash import keccak

NET = {"ETH": "ethereum", "ARBITRUM": "arbitrum", "ETH-ROBINHOOD": "robinhood", "BASE": "base", "OPTIMISM": "optimism",
       "POLYGON": "polygon", "ETH_LINEA": "linea", "XLAYER": "xlayer"}

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def k256(b):
    h = keccak.new(digest_bits=256); h.update(b); return h.digest()


def jadd(a, b):
    if a is None: return b
    if b is None: return a
    x1, y1, z1 = a; x2, y2, z2 = b
    z1s, z2s = z1 * z1 % P, z2 * z2 % P
    u1, u2 = x1 * z2s % P, x2 * z1s % P
    s1, s2 = y1 * z2s * z2 % P, y2 * z1s * z1 % P
    if u1 == u2:
        return jdbl(a) if s1 == s2 else None
    h, r = (u2 - u1) % P, (s2 - s1) % P
    h2 = h * h % P; h3 = h * h2 % P; u1h2 = u1 * h2 % P
    x3 = (r * r - h3 - 2 * u1h2) % P
    return x3, (r * (u1h2 - x3) - s1 * h3) % P, h * z1 * z2 % P


def jdbl(a):
    if a is None: return None
    x, y, z = a
    if y == 0: return None
    ys = y * y % P; s = 4 * x * ys % P; m = 3 * x * x % P
    x3 = (m * m - 2 * s) % P
    return x3, (m * (s - x3) - 8 * ys * ys) % P, 2 * y * z % P


def jmul(pt, k):
    acc, add = None, (pt[0], pt[1], 1)
    while k:
        if k & 1: acc = jadd(acc, add)
        add = jdbl(add); k >>= 1
    return acc


def affine(a):
    x, y, z = a; zi = pow(z, P - 2, P); zi2 = zi * zi % P
    return x * zi2 % P, y * zi2 * zi % P


def recover(msg, sig):
    """EIP-191 personal_sign: returns the 20-byte signer or None."""
    if len(sig) != 65: return None
    r, s, v = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:64], "big"), sig[64]
    v = v - 27 if v >= 27 else v
    if not (0 < r < N and 0 < s < N) or v not in (0, 1): return None
    # OKX's EVM scheme (okx/proof-of-reserves common/hash.go HashEvmCoinTypeMsg, MIT):
    # keccak256("\x19Ethereum Signed Message:\n32" || keccak256(message))
    z = int.from_bytes(k256(b"\x19Ethereum Signed Message:\n32" + k256(msg)), "big")
    x = r
    y2 = (pow(x, 3, P) + 7) % P; y = pow(y2, (P + 1) // 4, P)
    if y * y % P != y2: return None
    if y & 1 != v: y = P - y
    ri = pow(r, N - 2, N)
    q = jadd(jmul((x, y), s * ri % N), jmul(G, (-z * ri) % N))
    if q is None: return None
    qx, qy = affine(q)
    return k256(qx.to_bytes(32, "big") + qy.to_bytes(32, "big"))[-20:]


def main(zpath, out):
    z = zipfile.ZipFile(zpath)
    name = [i.filename for i in z.infolist() if i.filename.endswith(".csv")][0]
    rows, bad = {}, 0
    with z.open(name) as f:
        hdr = None
        for row in csv.reader(io.TextIOWrapper(f, errors="replace")):
            if len(row) > 3 and row[0] == "coin" and row[1] == "Type":
                hdr = row; continue
            if not hdr or len(row) < 8 or row[2] not in NET: continue
            a = row[4].strip().lower()
            if not (a.startswith("0x") and len(a) == 42): continue
            key = (NET[row[2]], a)
            if key not in rows:
                sig = bytes.fromhex(row[7][2:]) if row[7].startswith("0x") else b""
                ok = recover(row[6].encode(), sig) == bytes.fromhex(a[2:])
                bad += not ok
                rows[key] = {"chain": key[0], "address": a, "network": row[2], "snapshot_height": int(row[3] or 0),
                             "message": row[6], "signature": row[7], "sig_verified": ok, "coins": []}
            rows[key]["coins"].append(f"{row[0]}:{row[5]}")
    con = duckdb.connect()
    con.execute("CREATE TABLE t(chain VARCHAR, address VARCHAR, network VARCHAR, snapshot_height BIGINT, message VARCHAR, "
                "signature VARCHAR, sig_verified BOOLEAN, coins VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?,?)",
                    [(r["chain"], r["address"], r["network"], r["snapshot_height"], r["message"], r["signature"],
                      r["sig_verified"], ";".join(r["coins"])[:2000]) for r in rows.values()])
    con.execute(f"COPY t TO '{out}' (FORMAT parquet)")
    print({"addresses": len(rows), "signature_failures": bad,
           "per_chain": con.execute("SELECT chain, count(*), sum(sig_verified::int) FROM t GROUP BY 1 ORDER BY 2 DESC").fetchall()})


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
