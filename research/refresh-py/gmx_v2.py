#!/usr/bin/env python3
"""GMX v2 EventEmitter decoder (EventLog1 / EventLog2, eventName in PositionIncrease / PositionDecrease).

ABI of the non-indexed part: (address msgSender, string eventName, EventLogData eventData) where
EventLogData = 7 x XItems{ (string key, T value)[] items; (string key, T[] value)[] arrayItems }
for T in address, uint256, int256, bool, bytes32, bytes, string (gmx-synthetics EventUtils.sol).
topic1 = keccak(eventName) (string indexed), so we filter by name hash before decoding.
Writes table gmx_v2_events (see sql/25_metaload.sql for schema)."""
import sys

import pyarrow as pa

KINDS = ["address", "uint", "int", "bool", "bytes32", "bytes", "string"]
NAMES = {
    "f94196ccb31f81a3e67df18f2a62cbfb50009c80a7d3c728a3f542e3abc5cb63": "PositionIncrease",
    "07d51b51b408d7c62dcc47cc558da5ce6a6e0fd129a427ebce150f52b0e5171a": "PositionDecrease",
}


def u(b, o):
    return int.from_bytes(b[o:o + 32], "big")


def s256(b, o):
    v = u(b, o)
    return v - (1 << 256) if v >= 1 << 255 else v


def items(b, base, kind):
    out = {}
    io = base + u(b, base)          # offset of items[] relative to XItems start
    n = u(b, io)
    arr = io + 32
    for i in range(n):
        e = arr + u(b, arr + 32 * i)
        ko = e + u(b, e)
        kl = u(b, ko)
        key = b[ko + 32:ko + 32 + kl].decode("utf-8", "replace")
        if kind == "address":
            out[key] = "0x" + b[e + 44:e + 64].hex()
        elif kind == "uint":
            out[key] = u(b, e + 32)
        elif kind == "int":
            out[key] = s256(b, e + 32)
        elif kind == "bool":
            out[key] = u(b, e + 32) == 1
        elif kind == "bytes32":
            out[key] = "0x" + b[e + 32:e + 64].hex()
    return out


def decode(data):
    ed = u(data, 64)                 # head word 2 = offset of eventData
    res = {}
    for k, kind in enumerate(KINDS[:5]):
        res[kind] = items(data, ed + u(data, ed + 32 * k), kind)
    return res


def clamp(v):
    if v is None:
        return None
    return v if -(1 << 127) < v < (1 << 127) else None


def run_markets(con):
    """MarketCreated (GM market -> index/long/short token) straight from base logs (EventEmitter only)."""
    con.execute("CREATE OR REPLACE TABLE gmx_v2_markets (market VARCHAR, index_token VARCHAR, long_token VARCHAR, short_token VARCHAR)")
    rows = con.execute("""SELECT lower(hex(data)) FROM logs WHERE address = unhex('c8ee91a54287db53897056e12d9819156d3822fb')
        AND topic1 = unhex('ad5d762f1fc581b3e684cf095d93d3a2c10754f60124b09bec8bf3d76473baaf')""").fetchall()
    out = []
    for (h,) in rows:
        try:
            a = decode(bytes.fromhex(h))["address"]
            out.append((a.get("marketToken"), a.get("indexToken"), a.get("longToken"), a.get("shortToken")))
        except Exception:
            pass
    if out:
        con.executemany("INSERT INTO gmx_v2_markets VALUES (?, ?, ?, ?)", out)
    print(f"gmx_v2: {len(out)} markets", file=sys.stderr)


def run(con, out):
    run_markets(con)
    hashes = ",".join(f"unhex('{h}')" for h in NAMES)
    q = f"""SELECT block_number, tx_index, log_index, '0x' || lower(hex(address)), lower(hex(topic1)), h
            FROM read_parquet('{out}/stage/logs_sel/*/*.parquet', hive_partitioning=true)
            WHERE topic0 IN (unhex('137a44067c8961cd7e1d876f4754a5a3a75989b4552f1843fc69c3b372def160'),
                             unhex('468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5'))
              AND topic1 IN ({hashes})"""
    con.execute("DELETE FROM gmx_v2_events")
    cur = con.cursor()
    cur.execute(q)
    n = bad = 0
    while True:
        rows = cur.fetchmany(20000)
        if not rows:
            break
        batch = []
        for bn, ti, li, addr, t1, h in rows:
            try:
                d = decode(bytes.fromhex(h))
            except Exception:
                bad += 1
                continue
            a, ui, ii, bo, b32 = d["address"], d["uint"], d["int"], d["bool"], d["bytes32"]
            cda = ui.get("collateralDeltaAmount", ii.get("collateralDeltaAmount"))
            batch.append((bn, ti, li, addr, NAMES[t1], a.get("account"), a.get("market"), a.get("collateralToken"),
                          bo.get("isLong"), clamp(ui.get("sizeInUsd")), clamp(ui.get("sizeDeltaUsd")),
                          clamp(ui.get("collateralAmount")), clamp(cda), ui.get("orderType"),
                          b32.get("positionKey"), clamp(ui.get("executionPrice"))))
        if batch:
            cols = list(zip(*batch))
            names = ["block_number", "tx_index", "log_index", "contract", "event_name", "account", "market", "collateral_token",
                     "is_long", "size_in_usd", "size_delta_usd", "collateral_amount", "collateral_delta_amount", "order_type",
                     "position_key", "execution_price"]
            arrs = []
            for nm, c in zip(names, cols):
                if nm in ("size_in_usd", "size_delta_usd", "collateral_amount", "collateral_delta_amount", "execution_price"):
                    arrs.append(pa.array([None if v is None else str(v) for v in c], pa.string()))
                elif nm in ("block_number", "tx_index", "log_index", "order_type"):
                    arrs.append(pa.array(c, pa.int64()))
                elif nm == "is_long":
                    arrs.append(pa.array(c, pa.bool_()))
                else:
                    arrs.append(pa.array(c, pa.string()))
            tbl = pa.Table.from_arrays(arrs, names=names)
            con.register("gmx_batch", tbl)
            con.execute("""INSERT INTO gmx_v2_events SELECT block_number, tx_index, log_index, contract, event_name, account, market,
                collateral_token, is_long, size_in_usd::HUGEINT, size_delta_usd::HUGEINT, collateral_amount::HUGEINT,
                collateral_delta_amount::HUGEINT, order_type, position_key, execution_price::HUGEINT FROM gmx_batch""")
            con.unregister("gmx_batch")
        n += len(batch)
    print(f"gmx_v2: decoded {n} position events ({bad} undecodable)", file=sys.stderr)
