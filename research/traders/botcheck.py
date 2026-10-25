#!/usr/bin/env python3
"""Bot-filter effectiveness: of the raw top 100 senders (by swap count, and by computed PnL), how many were removed and why.
usage: botcheck.py <chain>"""
import sys

import duckdb

EM, CHARTS, GL = "/srv/skew/em-data", "/srv/skew/em-charts", "/srv/skew/em-graphloop"
ch = sys.argv[1]
c = duckdb.connect()
c.execute("SET threads=4")
c.execute("SET memory_limit='4GB'")
W3 = ("('0x' || substr(hex(address), 1, 16))::UBIGINT AS w1, ('0x' || substr(hex(address), 17, 16))::UBIGINT AS w2, "
      "('0x' || substr(hex(address), 33, 8))::UINTEGER AS w3")
c.execute(f"CREATE TABLE lab AS SELECT {W3}, any_value(category) AS category FROM read_parquet('{GL}/export/{ch}/labels.parquet') GROUP BY ALL")
c.execute(f"""CREATE TABLE top AS SELECT w1, w2, w3, count(*) AS swaps FROM read_parquet('{CHARTS}/data/{ch}/wallet/trades_b=*.parquet')
              GROUP BY ALL ORDER BY swaps DESC LIMIT 100""")
c.execute(f"""CREATE TABLE r AS
  SELECT t.*, lower(printf('0x%016x%016x%08x', t.w1, t.w2, t.w3)) AS wallet, l.category, p.bot_reason
  FROM top t LEFT JOIN lab l USING (w1, w2, w3)
  LEFT JOIN read_parquet('{EM}/traders/{ch}/wallet_pnl.parquet') p ON p.wallet = lower(printf('0x%016x%016x%08x', t.w1, t.w2, t.w3))""")
EXCL = ("mev_bot", "dex_router", "dex_pool", "cex_hot_wallet", "bridge", "lending", "token_contract", "nft_contract", "contract_other",
        "multisig_or_safe")
rows = c.execute(f"""SELECT CASE WHEN category IN {EXCL} THEN 'label:' || category WHEN bot_reason IS NOT NULL THEN 'behaviour:' || bot_reason
                                ELSE 'kept (' || coalesce(category, 'unlabelled') || ')' END AS fate, count(*), sum(swaps)
                     FROM r GROUP BY 1 ORDER BY 2 DESC""").fetchall()
print(f"{ch}: raw top 100 senders by swap count")
for f, n, s in rows:
    print(f"  {f:40s} {n:4d} wallets  {s:,} swaps")
# by computed PnL (label-excluded senders never reach the ledger, so this shows the behaviour filter)
rows = c.execute(f"""SELECT coalesce('behaviour:' || bot_reason, 'kept') AS fate, count(*) FROM (
                       SELECT bot_reason FROM read_parquet('{EM}/traders/{ch}/wallet_pnl.parquet') ORDER BY pnl_all DESC LIMIT 100) GROUP BY 1""").fetchall()
print(f"{ch}: raw top 100 by all-time PnL (after the label filter)")
for f, n in rows:
    print(f"  {f:40s} {n:4d}")
