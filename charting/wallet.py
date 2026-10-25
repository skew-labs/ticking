"""Wallet trade / position-action lookups for chart markers (index from build_wallet_index.py + live tape)."""
import glob
import json
import os
import re

import duckdb

from common import EM, ROOT

QK_KIND = "CASE t.qk WHEN 0 THEN 'usd' WHEN 1 THEN 'eth' WHEN 2 THEN 'btc' ELSE 'arb' END"
ADDR = re.compile(r"^0x[0-9a-f]{40}$")


def w3(addr):
    """0x address -> (8-byte, 8-byte, 4-byte) unsigned ints, the index's wallet key."""
    h = addr[2:].lower()
    return int(h[:16], 16), int(h[16:32], 16), int(h[32:], 16)


class WalletIndex:
    def __init__(self, chain):
        self.chain = chain
        self.dir = f"{ROOT}/data/{chain}/wallet"
        self.mtime = None
        self.info, self.mid = {}, {}
        self.load()

    def load(self):
        p = f"{self.dir}/info.json"
        if not os.path.exists(p):
            self.info, self.mid = {}, {}
            return
        m = os.path.getmtime(p)
        if m == self.mtime:
            return
        self.info = json.load(open(p))
        c = duckdb.connect()
        self.mid = {tok: mid for mid, tok in c.execute(f"SELECT mid, token FROM read_parquet('{self.dir}/markets.parquet')").fetchall()}
        self.mtime = m

    @property
    def ready(self):
        return bool(self.mid)

    def query(self, ch, address, market, frm=None, to=None, limit=1000):
        self.load()
        addr = address.lower()
        con = ch.con.cursor()
        bm = f"{ROOT}/data/block_minutes_{self.chain}.parquet"
        token = market["token"]
        mid = self.mid.get(token)
        last_idx = int(self.info.get("trades_last_block", 0))
        rows = []
        if mid is not None:
            tf = f"{self.dir}/trades_b={addr[2]}.parquet"
            rows = con.execute(f"""
              WITH t AS (SELECT bn, tx_index, log_index, buy, base_amt::DOUBLE AS base_amt, quote_amt::DOUBLE AS quote_amt, qk
                         FROM read_parquet('{tf}') WHERE w1 = ? AND w2 = ? AND w3 = ? AND mid = ?),
              b AS (SELECT minute, first_block FROM read_parquet('{bm}')
                    WHERE last_block >= (SELECT min(bn) FROM t) AND first_block <= (SELECT max(bn) FROM t)),
              tm AS (SELECT t.*, {QK_KIND} AS kind, b.minute FROM t ASOF JOIN b ON t.bn >= b.first_block),
              q AS (SELECT kind, minute, px FROM anchor_px UNION ALL SELECT 'usd', 0, 1.0)
              SELECT tm.bn, tm.tx_index, tm.log_index, tm.buy, tm.base_amt, tm.quote_amt * q.px AS usd, tm.minute, 'index' AS src
              FROM tm ASOF LEFT JOIN q ON q.kind = tm.kind AND tm.minute >= q.minute ORDER BY tm.bn, tm.log_index""",
                               list(w3(addr)) + [mid]).fetchall()
        seen = {(r[0], r[2]) for r in rows}
        if "tape" in ch.tables:
            for r in con.execute("""SELECT bn, tx_index, log_index, side = 'B', base_volume, usd, time, 'tape' FROM tape
                                    WHERE trader = from_hex(?) AND token = ? AND bn > ? ORDER BY bn, log_index""",
                                 [addr[2:], token, last_idx]).fetchall():
                if (r[0], r[2]) not in seen:
                    seen.add((r[0], r[2]))
                    rows.append(r)
        h = ch.hot() if hasattr(ch, "hot") else None
        if h is not None:  # follower trades newer than the deep build (second-precision block times)
            for r in h.execute("""SELECT f.slot, f.tx_order, f.event_order, m.side = 'B', f.base_volume, m.usd, f.time, 'hot'
                                  FROM fill_meta m JOIN fills f ON f.identity = m.identity
                                  WHERE m.trader = ? AND m.token = ? ORDER BY f.slot, f.event_order""", (addr, token)).fetchall():
                if (r[0], r[2]) not in seen:
                    seen.add((r[0], r[2]))
                    rows.append(r)
        rows.sort(key=lambda r: (r[0], r[2]))
        rows = [r for r in rows if r[6] is not None and (frm is None or r[6] >= frm) and (to is None or r[6] <= to)]
        truncated = len(rows) > limit
        rows = rows[-limit:]
        hashes = self.tx_hashes(con, [(r[0], r[2]) for r in rows])
        trades = [{"time": r[6], "side": "buy" if r[3] else "sell", "amount": r[4], "usd": r[5],
                   "price": (r[5] / r[4]) if r[5] is not None and r[4] else None, "block": r[0], "tx_index": r[1],
                   "log_index": r[2], "tx": hashes.get((r[0], r[2])), "source": "dex_swap"} for r in rows]
        actions = []
        if mid is not None and os.path.exists(f"{self.dir}/actions.parquet"):
            for bn, txi, li, act, proto, pk, amt, txh, minute, px in con.execute(f"""
              WITH a AS (SELECT bn, tx_index, log_index, action, protocol, pos_kind, amount, tx_hash
                         FROM read_parquet('{self.dir}/actions.parquet') WHERE w1 = ? AND w2 = ? AND w3 = ? AND mid = ?),
              b AS (SELECT minute, first_block FROM read_parquet('{bm}')
                    WHERE last_block >= (SELECT min(bn) FROM a) AND first_block <= (SELECT max(bn) FROM a)),
              am AS (SELECT a.*, b.minute FROM a ASOF JOIN b ON a.bn >= b.first_block),
              p AS (SELECT time // 1000 AS minute, close FROM token_bars WHERE token = ? AND period = 3600)
              SELECT am.bn, am.tx_index, am.log_index, am.action, am.protocol, am.pos_kind, am.amount, am.tx_hash, am.minute, p.close
              FROM am ASOF LEFT JOIN p ON am.minute >= p.minute ORDER BY am.bn, am.log_index""", list(w3(addr)) + [mid, token]).fetchall():
                if minute is None or (frm is not None and minute < frm) or (to is not None and minute > to):
                    continue
                actions.append({"time": minute, "action": act, "protocol": proto, "position": pk, "amount": amt,
                                "usd": amt * px if px is not None and amt is not None else None, "block": bn,
                                "tx_index": txi, "log_index": li, "tx": txh})
        return {"trades": trades, "actions": actions[-limit:], "truncated": truncated or len(actions) > limit,
                "index_until_block": last_idx}

    def tx_hashes(self, con, keys):
        """(block, log_index) -> 0x tx hash from the map dex_trades (snapshot) or the stage trades (gap + follower)."""
        if not keys:
            return {}
        out = {}
        blocks = sorted({k[0] for k in keys})
        snap = int(self.info.get("snapshot_last_block", 0))
        old = [b for b in blocks if b <= snap]
        new = [b for b in blocks if b > snap]
        if old:
            rids = sorted({b // 10_000_000 for b in old})
            files = [f for r in rids for f in glob.glob(f"{EM}/map/{self.chain}/dex_trades/range_id={r}/*.parquet")]
            if files:
                lst = ",".join("'%s'" % f for f in files)
                bl = ",".join(str(b) for b in old)
                for bn, li, h in con.execute(f"""SELECT block_number, log_index, tx_hash FROM read_parquet([{lst}])
                                                 WHERE block_number IN ({bl})""").fetchall():
                    out[(bn, li)] = "0x" + h.hex() if h else None
        if new:
            files = glob.glob(f"{ROOT}/data/{self.chain}/stage/trades/part=[tr]*.parquet")
            if files:
                lst = ",".join("'%s'" % f for f in files)
                bl = ",".join(str(b) for b in new)
                for bn, li, h in con.execute(f"""SELECT bn, log_index, tx_hash FROM read_parquet([{lst}], union_by_name=true)
                                                 WHERE bn IN ({bl})""").fetchall():
                    out[(bn, li)] = "0x" + h.hex() if h else None
            try:  # live rows newer than the last stage read
                bl = ",".join(str(b) for b in new)
                for bn, li, h in con.execute(f"SELECT bn, log_index, tx_hash FROM tape WHERE bn IN ({bl})").fetchall():
                    out.setdefault((bn, li), "0x" + h.hex() if h else None)
            except duckdb.Error:
                pass
        return out
