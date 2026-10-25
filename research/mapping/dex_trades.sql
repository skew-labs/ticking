-- =====================================================================================
-- dex_trades.sql  --  DEX trade layer from em-extract logs + transactions + token_transfers
-- =====================================================================================
-- Sections (run by build_map.sh when DEX=1):
--   dex_macros  helpers + signature tables (prepended to every DuckDB run)
--   dex_range   per 10M-block range -> stage/ranges/rNNN/dex/{dex_trades,dex_pools,dex_pairs_seen}.parquet
--   dex_merge   all ranges -> dex_trades/, dex_pools/, address_dex/, address_dex_summary (-> addresses)
--   dex_report  coverage report -> dex_coverage.json (+ printed tables)
--
-- Decoded events (topic0 + exact shape: topic_count, data length and address-shaped indexed
-- address topics must match, else the log is counted as shape_mismatch and not decoded; the
-- same signature with a different `indexed` layout is common):
--   uniswap_v2   Swap(address,uint256,uint256,uint256,uint256,address)        V2 + forks (Sushi, Camelot V2, ...)
--   solidly_v2   Swap(address,address,uint256,uint256,uint256,uint256)        Velodrome/Aerodrome/Solidly style
--   uniswap_v3   Swap(address,address,int256,int256,uint160,uint128,int24)    V3 + forks + Algebra V1/Camelot V3
--   pancake_v3   Swap(...,int24,uint128,uint128)    algebra_integral Swap(...,int24,uint24,uint24)
--   uniswap_v4   PoolManager Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)
--   curve*       TokenExchange / TokenExchangeUnderlying (int128 and uint256 index variants, tricrypto-ng)
--   balancer_v2  Vault Swap(bytes32,address,address,uint256,uint256);  balancer_v3 Vault Swap(address,address,address,...)
--                (balancer_v3 only from the canonical Vault 0xbA1333333333a1BA1108E8412f11850A5C319bA9)
--   gmx_v2       EventEmitter EventLog1/EventLog2 with eventName SwapInfo/PositionIncrease/PositionDecrease:
--                detected only (trader/tx/log); the nested EventLogData payload is NOT decoded.
-- Direction conventions: V2 uses amountXIn/amountXOut; V3-family amounts are pool deltas
-- (>0 = paid into pool); V4 amounts are swapper deltas (<0 = paid into pool); Curve uses
-- sold_id/bought_id; Balancer has tokenIn/tokenOut in topics.
-- Token resolution order (token_source): 'event' (Balancer) > 'pool_created_event' (PairCreated /
-- PoolCreated / Algebra Pool / V4 Initialize, any range) > 'tx_transfer_match' (ERC20 transfer
-- to/from the pool with the exact amount in the same tx) > 'pool_slot_learned' (most frequent
-- matched token for that pool slot / Curve coin index across all swaps).
-- Amounts: raw integers. amount_in/out DECIMAL(38,0) when < 10^38, *_big VARCHAR when larger;
-- word values >= 2^127 are not decoded (amount_overflow = true, raw log data kept in raw_data).
-- V4 / native: currency 0x000..0 = native ETH.  router = tx.to when it is a likely router
-- (>= 50 swap logs from >= 5 distinct tx senders, not a pool and not the venue itself).

-- @section dex_macros
CREATE OR REPLACE MACRO w(h, i) AS substr(h, 64 * i + 1, 64);
-- unsigned word < 2^127 / signed word in int128 range -> HUGEINT, else NULL
CREATE OR REPLACE MACRO w_u(x) AS CASE WHEN left(x, 32) = '00000000000000000000000000000000' AND substr(x, 33, 1) < '8'
    THEN from_hex(substr(x, 33, 32))::BIT::HUGEINT END;
CREATE OR REPLACE MACRO w_s(x) AS CASE WHEN (left(x, 32) = '00000000000000000000000000000000' AND substr(x, 33, 1) < '8')
    OR (left(x, 32) = 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF' AND substr(x, 33, 1) >= '8')
    THEN from_hex(substr(x, 33, 32))::BIT::HUGEINT END;
CREATE OR REPLACE MACRO w_addr(x) AS from_hex(substr(x, 25, 40));
CREATE OR REPLACE MACRO t_addr(t) AS from_hex(right(hex(t), 40));
CREATE OR REPLACE MACRO u_blob(y) AS from_hex(lpad(hex(y), 64, '0'));
CREATE OR REPLACE MACRO is_addr_topic(t) AS t IS NOT NULL AND left(hex(t), 24) = '000000000000000000000000';
CREATE OR REPLACE TEMP TABLE dex_sig(topic0 BLOB, sig VARCHAR, role VARCHAR, tc UTINYINT, dlen INTEGER);
INSERT INTO dex_sig VALUES
 (from_hex('d78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822'), 'uniswap_v2', 'swap', 3, 128),
 (from_hex('b3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b'), 'solidly_v2', 'swap', 3, 128),
 (from_hex('c42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'), 'uniswap_v3', 'swap', 3, 160),
 (from_hex('19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83'), 'pancake_v3', 'swap', 3, 224),
 (from_hex('121cb44ee54098b1a04743c487e7460d8dd429b27f88b1f4d4767396e1a59f79'), 'algebra_integral', 'swap', 3, 224),
 (from_hex('40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'), 'uniswap_v4', 'swap', 3, 192),
 (from_hex('8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140'), 'curve', 'swap', 2, 128),
 (from_hex('d013ca23e77a65003c2c659c5442c00c805371b7fc1ebd4c206c41d1536bd90b'), 'curve_underlying', 'swap', 2, 128),
 (from_hex('b2e76ae99761dc136e598d4a629bb347eccb9532a5f8bbd72e18467c3c34cc98'), 'curve_crypto', 'swap', 2, 128),
 (from_hex('143f1f8e861fbdeddd5b46e844b7d3ac7b86a122f36e8c463859ee6811b1f29c'), 'curve_tricrypto_ng', 'swap', 2, 192),
 (from_hex('2170c741c41531aec20e7c107c24eecfdd15e69c9bb0a8dd37b1840b9e0b207b'), 'balancer_v2', 'swap', 4, 64),
 (from_hex('0874b2d545cb271cdbda4e093020c452328b24af12382ed62c4d00f5c26709db'), 'balancer_v3', 'swap', 4, 128),
 (from_hex('137a44067c8961cd7e1d876f4754a5a3a75989b4552f1843fc69c3b372def160'), 'gmx_v2', 'gmx', 3, NULL),
 (from_hex('468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5'), 'gmx_v2', 'gmx', 4, NULL),
 (from_hex('0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9'), 'pair_created', 'pool', 3, 64),
 (from_hex('783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118'), 'v3_pool_created', 'pool', 4, 64),
 (from_hex('91ccaa7a278130b65168c3a0c8d3bcae84cf5e43704342bd3ec0b59e59c036db'), 'algebra_pool', 'pool', 3, 32),
 (from_hex('dd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438'), 'v4_initialize', 'pool', 4, 160),
 (from_hex('1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1'), 'v2_sync', 'aux', 1, 64);
CREATE OR REPLACE TEMP TABLE gmx_events(h BLOB, name VARCHAR);
INSERT INTO gmx_events VALUES
 (from_hex('93534d650a9b8eb67820f87038b8e8b36b741c6f7eb14d1a7ac5027e80fd4a82'), 'SwapInfo'),
 (from_hex('f94196ccb31f81a3e67df18f2a62cbfb50009c80a7d3c728a3f542e3abc5cb63'), 'PositionIncrease'),
 (from_hex('07d51b51b408d7c62dcc47cc558da5ce6a6e0fd129a427ebce150f52b0e5171a'), 'PositionDecrease');

-- @section dex_range
CREATE TEMP TABLE dl AS
SELECT l.block_number AS bn, l.tx_index, l.log_index, l.address AS emitter, l.topic1, l.topic2, l.topic3,
       l.data, s.sig, s.role,
       (l.topic_count = s.tc AND (s.dlen IS NULL OR octet_length(l.data) = s.dlen) AND CASE
            WHEN s.sig IN ('uniswap_v2', 'solidly_v2', 'uniswap_v3', 'pancake_v3', 'algebra_integral',
                           'pair_created', 'v3_pool_created', 'algebra_pool')
                 THEN is_addr_topic(l.topic1) AND is_addr_topic(l.topic2)
            WHEN s.sig = 'uniswap_v4' THEN is_addr_topic(l.topic2)
            WHEN s.sig LIKE 'curve%' THEN is_addr_topic(l.topic1)
            WHEN s.sig = 'balancer_v2' THEN is_addr_topic(l.topic2) AND is_addr_topic(l.topic3)
            WHEN s.sig = 'balancer_v3' THEN l.address = from_hex('ba1333333333a1ba1108e8412f11850a5c319ba9')
                 AND is_addr_topic(l.topic1) AND is_addr_topic(l.topic2) AND is_addr_topic(l.topic3)
            WHEN s.sig = 'v4_initialize' THEN is_addr_topic(l.topic2) AND is_addr_topic(l.topic3)
            ELSE true END) AS shape_ok
FROM (SELECT * FROM src_logs WHERE topic0 IN (
    from_hex('d78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822'), from_hex('b3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b'),
    from_hex('c42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'), from_hex('19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83'),
    from_hex('121cb44ee54098b1a04743c487e7460d8dd429b27f88b1f4d4767396e1a59f79'), from_hex('40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'),
    from_hex('8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140'), from_hex('d013ca23e77a65003c2c659c5442c00c805371b7fc1ebd4c206c41d1536bd90b'),
    from_hex('b2e76ae99761dc136e598d4a629bb347eccb9532a5f8bbd72e18467c3c34cc98'), from_hex('143f1f8e861fbdeddd5b46e844b7d3ac7b86a122f36e8c463859ee6811b1f29c'),
    from_hex('2170c741c41531aec20e7c107c24eecfdd15e69c9bb0a8dd37b1840b9e0b207b'), from_hex('0874b2d545cb271cdbda4e093020c452328b24af12382ed62c4d00f5c26709db'),
    from_hex('137a44067c8961cd7e1d876f4754a5a3a75989b4552f1843fc69c3b372def160'), from_hex('468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5'),
    from_hex('0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9'), from_hex('783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118'),
    from_hex('91ccaa7a278130b65168c3a0c8d3bcae84cf5e43704342bd3ec0b59e59c036db'), from_hex('dd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438'))) l
JOIN dex_sig s ON s.topic0 = l.topic0;

-- 1. decode swaps into a common shape (slot_in/slot_out: pool token index or Curve coin index)
CREATE TEMP TABLE sw0 AS
WITH v2 AS (
    SELECT *, w_u(w(h, 0)) AS a0i, w_u(w(h, 1)) AS a1i, w_u(w(h, 2)) AS a0o, w_u(w(h, 3)) AS a1o
    FROM (SELECT *, hex(data) AS h FROM dl WHERE role = 'swap' AND shape_ok AND sig IN ('uniswap_v2', 'solidly_v2'))),
v3 AS (
    SELECT *, w_s(w(h, 0)) AS a0, w_s(w(h, 1)) AS a1
    FROM (SELECT *, hex(data) AS h FROM dl WHERE role = 'swap' AND shape_ok
          AND sig IN ('uniswap_v3', 'pancake_v3', 'algebra_integral', 'uniswap_v4'))),
cv AS (
    SELECT *, CASE WHEN sig IN ('curve', 'curve_underlying') THEN w_s(w(h, 0)) ELSE w_u(w(h, 0)) END AS i_sold,
           w_u(w(h, 1)) AS a_sold,
           CASE WHEN sig IN ('curve', 'curve_underlying') THEN w_s(w(h, 2)) ELSE w_u(w(h, 2)) END AS i_bought,
           w_u(w(h, 3)) AS a_bought
    FROM (SELECT *, hex(data) AS h FROM dl WHERE role = 'swap' AND shape_ok AND sig LIKE 'curve%')),
bal AS (
    SELECT *, w_u(w(h, 0)) AS ai, w_u(w(h, 1)) AS ao
    FROM (SELECT *, hex(data) AS h FROM dl WHERE role = 'swap' AND shape_ok AND sig LIKE 'balancer%'))
SELECT bn, tx_index, log_index, emitter, sig AS protocol, emitter AS pool,
       t_addr(topic1) AS sender, t_addr(topic2) AS recipient,
       CASE WHEN a0i > 0 AND a1o > 0 THEN 0 WHEN a1i > 0 AND a0o > 0 THEN 1 END AS slot_in,
       CASE WHEN a0i > 0 AND a1o > 0 THEN 1 WHEN a1i > 0 AND a0o > 0 THEN 0 END AS slot_out,
       NULL::BLOB AS tok_in_evt, NULL::BLOB AS tok_out_evt,
       CASE WHEN a0i > 0 AND a1o > 0 THEN a0i WHEN a1i > 0 AND a0o > 0 THEN a1i END AS amt_in,
       CASE WHEN a0i > 0 AND a1o > 0 THEN a1o WHEN a1i > 0 AND a0o > 0 THEN a0o END AS amt_out,
       (a0i IS NULL OR a1i IS NULL OR a0o IS NULL OR a1o IS NULL) AS ovf,
       CASE WHEN a0i IS NULL OR a1i IS NULL OR a0o IS NULL OR a1o IS NULL THEN data END AS data
FROM v2
UNION ALL
SELECT bn, tx_index, log_index, emitter, sig,
       CASE WHEN sig = 'uniswap_v4' THEN topic1 ELSE emitter END,
       CASE WHEN sig = 'uniswap_v4' THEN t_addr(topic2) ELSE t_addr(topic1) END,
       CASE WHEN sig = 'uniswap_v4' THEN NULL ELSE t_addr(topic2) END,
       CASE WHEN sig = 'uniswap_v4' THEN (CASE WHEN a0 < 0 AND a1 > 0 THEN 0 WHEN a1 < 0 AND a0 > 0 THEN 1 END)
            ELSE (CASE WHEN a0 > 0 AND a1 < 0 THEN 0 WHEN a1 > 0 AND a0 < 0 THEN 1 END) END,
       CASE WHEN sig = 'uniswap_v4' THEN (CASE WHEN a0 < 0 AND a1 > 0 THEN 1 WHEN a1 < 0 AND a0 > 0 THEN 0 END)
            ELSE (CASE WHEN a0 > 0 AND a1 < 0 THEN 1 WHEN a1 > 0 AND a0 < 0 THEN 0 END) END,
       NULL, NULL,
       CASE WHEN sig = 'uniswap_v4' THEN (CASE WHEN a0 < 0 AND a1 > 0 THEN -a0 WHEN a1 < 0 AND a0 > 0 THEN -a1 END)
            ELSE (CASE WHEN a0 > 0 AND a1 < 0 THEN a0 WHEN a1 > 0 AND a0 < 0 THEN a1 END) END,
       CASE WHEN sig = 'uniswap_v4' THEN (CASE WHEN a0 < 0 AND a1 > 0 THEN a1 WHEN a1 < 0 AND a0 > 0 THEN a0 END)
            ELSE (CASE WHEN a0 > 0 AND a1 < 0 THEN -a1 WHEN a1 > 0 AND a0 < 0 THEN -a0 END) END,
       (a0 IS NULL OR a1 IS NULL), CASE WHEN a0 IS NULL OR a1 IS NULL THEN data END
FROM v3
UNION ALL
SELECT bn, tx_index, log_index, emitter, sig, emitter, t_addr(topic1), NULL,
       i_sold::INTEGER, i_bought::INTEGER, NULL, NULL, a_sold, a_bought,
       (a_sold IS NULL OR a_bought IS NULL OR i_sold IS NULL OR i_bought IS NULL),
       CASE WHEN a_sold IS NULL OR a_bought IS NULL OR i_sold IS NULL OR i_bought IS NULL THEN data END
FROM cv
UNION ALL
SELECT bn, tx_index, log_index, emitter, sig,
       CASE WHEN sig = 'balancer_v2' THEN from_hex(left(hex(topic1), 40)) ELSE t_addr(topic1) END,
       NULL, NULL, NULL, NULL, t_addr(topic2), t_addr(topic3), ai, ao, (ai IS NULL OR ao IS NULL),
       CASE WHEN ai IS NULL OR ao IS NULL THEN data END
FROM bal
UNION ALL
SELECT bn, tx_index, log_index, emitter, 'gmx_v2:' || g.name, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       NULL, NULL, NULL, NULL
FROM dl JOIN gmx_events g ON g.h = dl.topic1 WHERE dl.role = 'gmx' AND dl.shape_ok;

-- 2. exact-amount ERC20 transfer match to / from the pool inside the same tx
CREATE TEMP TABLE ttx AS
SELECT tt.block_number AS bn, tt.tx_index, tt.token, tt."from" AS f, tt."to" AS t, tt.amount
FROM src_tt tt
SEMI JOIN (SELECT DISTINCT bn, tx_index FROM sw0 WHERE tok_in_evt IS NULL AND protocol NOT LIKE 'uniswap_v4%' AND protocol NOT LIKE 'gmx%') k
     ON k.bn = tt.block_number AND k.tx_index = tt.tx_index
WHERE tt.standard IN ('ERC20', 'WETH_DEPOSIT', 'WETH_WITHDRAWAL');
CREATE TEMP TABLE m_in AS
SELECT s.bn, s.log_index, min(x.token) AS token
FROM sw0 s JOIN ttx x ON x.bn = s.bn AND x.tx_index = s.tx_index AND x.t = s.pool AND x.amount = u_blob(s.amt_in)
WHERE s.amt_in IS NOT NULL AND s.protocol NOT LIKE 'uniswap_v4%' GROUP BY s.bn, s.log_index;
CREATE TEMP TABLE m_out AS
SELECT s.bn, s.log_index, min(x.token) AS token
FROM sw0 s JOIN ttx x ON x.bn = s.bn AND x.tx_index = s.tx_index AND x.f = s.pool AND x.amount = u_blob(s.amt_out)
WHERE s.amt_out IS NOT NULL AND s.protocol NOT LIKE 'uniswap_v4%' GROUP BY s.bn, s.log_index;

-- 3. range partial outputs
COPY (
SELECT s.bn, s.tx_index, s.log_index, t.hash AS tx_hash, s.protocol, s.emitter AS venue, s.pool,
       t."from" AS trader, s.sender, s.recipient, t."to" AS tx_to, s.slot_in, s.slot_out,
       s.tok_in_evt, s.tok_out_evt, mi.token AS tok_in_tx, mo.token AS tok_out_tx,
       (s.amt_in & m64())::UBIGINT AS ain0, (s.amt_in >> 64)::UBIGINT AS ain1,
       (s.amt_out & m64())::UBIGINT AS aout0, (s.amt_out >> 64)::UBIGINT AS aout1,
       s.ovf, s.data AS raw_data
FROM sw0 s
JOIN src_tx t ON t.block_number = s.bn AND t.tx_index = s.tx_index
LEFT JOIN m_in mi ON mi.bn = s.bn AND mi.log_index = s.log_index
LEFT JOIN m_out mo ON mo.bn = s.bn AND mo.log_index = s.log_index
) TO '@RDIR@/dex_trades.parquet' (FORMAT parquet, COMPRESSION zstd);

COPY (
SELECT sig AS src, emitter AS factory, bn,
       CASE sig WHEN 'pair_created' THEN w_addr(w(h, 0)) WHEN 'v3_pool_created' THEN w_addr(w(h, 1))
                WHEN 'algebra_pool' THEN w_addr(w(h, 0)) WHEN 'v4_initialize' THEN topic1 END AS pool,
       t_addr(topic1) AS token0, t_addr(topic2) AS token1
FROM (SELECT *, hex(data) AS h FROM dl WHERE role = 'pool' AND shape_ok AND sig <> 'v4_initialize')
UNION ALL
SELECT sig, emitter, bn, topic1, t_addr(topic2), t_addr(topic3)
FROM dl WHERE role = 'pool' AND shape_ok AND sig = 'v4_initialize'
) TO '@RDIR@/dex_pools.parquet' (FORMAT parquet, COMPRESSION zstd);

COPY (
SELECT pool, slot, token, count(*)::UBIGINT AS n FROM (
    SELECT s.pool, s.slot_in AS slot, mi.token FROM sw0 s JOIN m_in mi ON mi.bn = s.bn AND mi.log_index = s.log_index
    UNION ALL
    SELECT s.pool, s.slot_out, mo.token FROM sw0 s JOIN m_out mo ON mo.bn = s.bn AND mo.log_index = s.log_index)
WHERE slot IS NOT NULL GROUP BY ALL
) TO '@RDIR@/dex_pairs_seen.parquet' (FORMAT parquet, COMPRESSION zstd);

COPY (
SELECT sig, role, count(*) AS n_logs, count(*) FILTER (WHERE NOT shape_ok) AS shape_mismatch,
       count(DISTINCT emitter) AS n_emitters
FROM dl GROUP BY sig, role
) TO '@RDIR@/dex_counts.parquet' (FORMAT parquet);

-- @section dex_merge
-- inputs: stg_dex (range trade partials), stg_dex_pools, stg_dex_pairs
CREATE TEMP TABLE reg AS
SELECT pool, arg_min(src, bn) AS src, arg_min(factory, bn) AS factory, arg_min(token0, bn) AS token0,
       arg_min(token1, bn) AS token1, min(bn) AS created_block
FROM stg_dex_pools WHERE pool IS NOT NULL GROUP BY pool;
CREATE TEMP TABLE learned AS
SELECT pool, slot, arg_max(token, n) AS token, sum(n) AS n
FROM (SELECT pool, slot, token, sum(n) AS n FROM stg_dex_pairs GROUP BY ALL) GROUP BY pool, slot;
CREATE TEMP TABLE routers AS
SELECT tx_to AS router FROM stg_dex
WHERE tx_to IS NOT NULL AND tx_to <> venue AND (pool IS NULL OR tx_to <> pool)
  AND NOT EXISTS (SELECT 1 FROM reg WHERE reg.pool = stg_dex.tx_to)
GROUP BY tx_to HAVING count(*) >= 50 AND approx_count_distinct(trader) >= 5;

COPY (
SELECT '@CHAIN@' AS chain, s.bn AS block_number, s.tx_hash, s.tx_index, s.log_index,
       CASE WHEN s.protocol = 'uniswap_v3' AND r.src = 'algebra_pool' THEN 'algebra_v3'
            WHEN s.protocol = 'uniswap_v3' AND r.src IS NULL THEN 'uniswap_v3_like'
            WHEN s.protocol = 'uniswap_v2' AND r.src IS NULL THEN 'uniswap_v2_like'
            ELSE s.protocol END AS protocol,
       s.venue, s.pool, r.factory, s.trader, s.sender, s.recipient,
       coalesce(s.tok_in_evt, CASE s.slot_in WHEN 0 THEN r.token0 WHEN 1 THEN r.token1 END, s.tok_in_tx, li.token) AS token_in,
       coalesce(s.tok_out_evt, CASE s.slot_out WHEN 0 THEN r.token0 WHEN 1 THEN r.token1 END, s.tok_out_tx, lo.token) AS token_out,
       CASE WHEN s.tok_in_evt IS NOT NULL THEN 'event'
            WHEN r.pool IS NOT NULL AND s.slot_in IN (0, 1) THEN 'pool_created_event'
            WHEN s.tok_in_tx IS NOT NULL AND s.tok_out_tx IS NOT NULL THEN 'tx_transfer_match'
            WHEN coalesce(s.tok_in_tx, li.token) IS NOT NULL AND coalesce(s.tok_out_tx, lo.token) IS NOT NULL THEN 'pool_slot_learned'
       END AS token_source,
       limbs_dec38(s.ain0::HUGEINT, s.ain1::HUGEINT, 0::HUGEINT, 0::HUGEINT) AS amount_in,
       limbs_dec38(s.aout0::HUGEINT, s.aout1::HUGEINT, 0::HUGEINT, 0::HUGEINT) AS amount_out,
       CASE WHEN s.ain0 IS NOT NULL AND limbs_dec38(s.ain0::HUGEINT, s.ain1::HUGEINT, 0::HUGEINT, 0::HUGEINT) IS NULL
            THEN limbs_str(s.ain0::HUGEINT, s.ain1::HUGEINT, 0::HUGEINT, 0::HUGEINT) END AS amount_in_big,
       CASE WHEN s.aout0 IS NOT NULL AND limbs_dec38(s.aout0::HUGEINT, s.aout1::HUGEINT, 0::HUGEINT, 0::HUGEINT) IS NULL
            THEN limbs_str(s.aout0::HUGEINT, s.aout1::HUGEINT, 0::HUGEINT, 0::HUGEINT) END AS amount_out_big,
       s.ovf AS amount_overflow, s.raw_data,
       CASE WHEN ro.router IS NOT NULL THEN s.tx_to END AS router, s.tx_to,
       (s.bn // 10000000)::INTEGER AS range_id
FROM stg_dex s
-- equi-join on an expression: a residual left-side predicate in ON makes DuckDB fall back to a nested-loop join
LEFT JOIN reg r ON r.pool = CASE WHEN s.protocol NOT LIKE 'curve%' AND s.protocol NOT LIKE 'balancer%' THEN s.pool END
LEFT JOIN learned li ON li.pool = s.pool AND li.slot = s.slot_in
LEFT JOIN learned lo ON lo.pool = s.pool AND lo.slot = s.slot_out
LEFT JOIN routers ro ON ro.router = s.tx_to
) TO '@NEXT@/dex_trades' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (range_id));

CREATE TEMP VIEW trades AS SELECT * FROM read_parquet('@NEXT@/dex_trades/*/*.parquet', hive_partitioning = false);

COPY (
WITH s AS (
    SELECT pool, venue, mode(protocol) AS protocol, min(block_number) AS first_swap_block, max(block_number) AS last_swap_block,
           count(*)::UBIGINT AS n_swaps, approx_count_distinct(trader)::UBIGINT AS n_traders_approx,
           mode(token_source) AS token_source
    FROM trades WHERE pool IS NOT NULL GROUP BY pool, venue),
c AS (SELECT pool, list(token ORDER BY slot) AS coins FROM learned GROUP BY pool)
SELECT coalesce(s.pool, r.pool) AS pool,
       coalesce(s.protocol, CASE r.src WHEN 'pair_created' THEN 'uniswap_v2' WHEN 'v3_pool_created' THEN 'uniswap_v3'
                                      WHEN 'algebra_pool' THEN 'algebra_v3' WHEN 'v4_initialize' THEN 'uniswap_v4' END) AS protocol,
       s.venue, r.factory,
       coalesce(r.token0, c.coins[1]) AS token0, coalesce(r.token1, c.coins[2]) AS token1,
       CASE WHEN len(c.coins) > 2 THEN c.coins END AS coins,
       r.created_block, least(r.created_block, s.first_swap_block) AS first_block, s.last_swap_block,
       coalesce(s.n_swaps, 0)::UBIGINT AS n_swaps, coalesce(s.n_traders_approx, 0)::UBIGINT AS n_traders_approx,
       CASE WHEN r.pool IS NOT NULL THEN 'pool_created_event' ELSE s.token_source END AS token_source
FROM s FULL OUTER JOIN reg r ON r.pool = s.pool
LEFT JOIN c ON c.pool = coalesce(s.pool, r.pool)
ORDER BY n_swaps DESC
) TO '@NEXT@/dex_pools/dex_pools.parquet' (FORMAT parquet, COMPRESSION zstd);

-- per (trader, token): trades / raw token volume sold (token_in) and bought (token_out)
COPY (
SELECT address, token, sum(n_sold)::UBIGINT AS n_sold, sum(n_bought)::UBIGINT AS n_bought,
       limbs_dec38(sum(s0)::HUGEINT, sum(s1)::HUGEINT, 0::HUGEINT, 0::HUGEINT) AS amount_sold,
       limbs_dec38(sum(b0)::HUGEINT, sum(b1)::HUGEINT, 0::HUGEINT, 0::HUGEINT) AS amount_bought,
       CASE WHEN limbs_dec38(sum(s0)::HUGEINT, sum(s1)::HUGEINT, 0::HUGEINT, 0::HUGEINT) IS NULL
            THEN limbs_str(sum(s0)::HUGEINT, sum(s1)::HUGEINT, 0::HUGEINT, 0::HUGEINT) END AS amount_sold_big,
       CASE WHEN limbs_dec38(sum(b0)::HUGEINT, sum(b1)::HUGEINT, 0::HUGEINT, 0::HUGEINT) IS NULL
            THEN limbs_str(sum(b0)::HUGEINT, sum(b1)::HUGEINT, 0::HUGEINT, 0::HUGEINT) END AS amount_bought_big,
       min(block_number) AS first_block, max(block_number) AS last_block
FROM (
    SELECT trader AS address, token_in AS token, 1 AS n_sold, 0 AS n_bought, block_number,
           coalesce(amount_in::HUGEINT & m64(), 0) AS s0, coalesce(amount_in::HUGEINT >> 64, 0) AS s1, 0::HUGEINT AS b0, 0::HUGEINT AS b1
    FROM trades WHERE token_in IS NOT NULL
    UNION ALL
    SELECT trader, token_out, 0, 1, block_number, 0::HUGEINT, 0::HUGEINT,
           coalesce(amount_out::HUGEINT & m64(), 0), coalesce(amount_out::HUGEINT >> 64, 0)
    FROM trades WHERE token_out IS NOT NULL)
GROUP BY address, token ORDER BY address
) TO '@NEXT@/address_dex/address_dex.parquet' (FORMAT parquet, COMPRESSION zstd);

COPY (
SELECT t.address, t.dex_trades, t.dex_pools, coalesce(k.dex_tokens, 0)::UBIGINT AS dex_tokens FROM (
    SELECT trader AS address, count(*)::UBIGINT AS dex_trades, count(DISTINCT pool)::UBIGINT AS dex_pools
    FROM trades GROUP BY trader) t
LEFT JOIN (SELECT address, count(*)::UBIGINT AS dex_tokens FROM read_parquet('@NEXT@/address_dex/address_dex.parquet') GROUP BY address) k
       ON k.address = t.address
) TO '@NEXT@/address_dex_summary.parquet' (FORMAT parquet, COMPRESSION zstd);

-- @section dex_report
-- inputs: published map (emitter_topics, dex_trades) + stg_dex_counts; prints and writes dex_coverage.json
CREATE TEMP TABLE known_nonswap(topic0 BLOB, name VARCHAR);
INSERT INTO known_nonswap VALUES
 (from_hex('ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'), 'Transfer'),
 (from_hex('8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925'), 'Approval'),
 (from_hex('17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31'), 'ApprovalForAll'),
 (from_hex('c3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62'), 'TransferSingle'),
 (from_hex('4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb'), 'TransferBatch'),
 (from_hex('e1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c'), 'Deposit(WETH)'),
 (from_hex('7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65'), 'Withdrawal(WETH)'),
 (from_hex('1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1'), 'Sync(UniV2)'),
 (from_hex('4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f'), 'Mint(UniV2)'),
 (from_hex('dccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496'), 'Burn(UniV2)'),
 (from_hex('7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde'), 'Mint(UniV3)'),
 (from_hex('0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c'), 'Burn(UniV3)'),
 (from_hex('70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0'), 'Collect(UniV3)'),
 (from_hex('98636036cb66a9c19a37435efc1e90142190214e8abeb821bdba3f2990dd4c95'), 'Initialize(UniV3)'),
 (from_hex('f208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec'), 'ModifyLiquidity(UniV4)'),
 (from_hex('8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e0'), 'OwnershipTransferred'),
 (from_hex('bc7cd75a20ee27fd9adebab32041f755214dbc6bffa90cc0225b39da2e5c2d3b'), 'Upgraded'),
 (from_hex('49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f'), 'UserOperationEvent(ERC4337)'),
 (from_hex('bb47ee3e183a558b1a2ff0874b079f3fc5478b7454eacf2bfc5af2ff5878f972'), 'BeforeExecution(ERC4337)'),
 (from_hex('3d0ce9bfc3ed7d6862dbb28b2dea94561fe714a1b4d019aa8af39730d1ad7c3d'), 'SafeReceived(Safe)'),
 (from_hex('442e715f626346e8c54381002da614f62bee8d27386535b2521ec8540898556e'), 'ExecutionSuccess(Safe)'),
 (from_hex('205442d60b70af1203d43cab62352c3b69b94f091be32fe683198057282b5c92'), 'FeeCollected(address,address,uint256,uint256)'),
 (from_hex('40d0efd1a53d60ecbf40971b9daf7dc90178c3aadc7aab1765632738fa8b8f01'), 'Collect(UniV3 NPM)'),
 (from_hex('3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f'), 'IncreaseLiquidity(UniV3 NPM)'),
 (from_hex('26f6a048ee9138f2c0ce266f322cb99228e8d619ae2bff30c67f8dcf9d2377b4'), 'DecreaseLiquidity(UniV3 NPM)'),
 (from_hex('0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f'), 'AnswerUpdated(Chainlink)');
CREATE TEMP TABLE et AS SELECT address, topic0, n FROM read_parquet('@MAP@/emitter_topics/*.parquet');
CREATE TEMP TABLE top50 AS SELECT address, sum(n) AS n FROM et GROUP BY address ORDER BY n DESC LIMIT 50;
CREATE TEMP TABLE cls AS
SELECT et.address, et.topic0, et.n,
       CASE WHEN s.role = 'swap' THEN 'swap_signature' WHEN s.role = 'gmx' THEN 'gmx_eventlog'
            WHEN s.role IN ('pool', 'aux') THEN 'dex_aux'
            WHEN k.name IS NOT NULL THEN 'known_non_swap' ELSE 'unknown' END AS cls,
       coalesce(s.sig, k.name) AS name
FROM et SEMI JOIN top50 USING (address)
LEFT JOIN dex_sig s ON s.topic0 = et.topic0 LEFT JOIN known_nonswap k ON k.topic0 = et.topic0;
CREATE TEMP TABLE dec AS
SELECT venue AS address, count(*) AS n_trades,
       count(*) FILTER (WHERE amount_in IS NOT NULL AND amount_out IS NOT NULL) AS n_amounts,
       count(*) FILTER (WHERE token_in IS NOT NULL AND token_out IS NOT NULL) AS n_tokens
FROM read_parquet('@MAP@/dex_trades/*/*.parquet', hive_partitioning = false) WHERE protocol NOT LIKE 'gmx%' GROUP BY venue;
CREATE TEMP TABLE rep AS SELECT
  (SELECT sum(n) FROM cls) AS top50_logs,
  (SELECT sum(n) FROM cls WHERE cls = 'swap_signature') AS top50_swap_logs,
  (SELECT sum(n_trades) FROM dec SEMI JOIN top50 USING (address)) AS top50_trades_rows,
  (SELECT sum(n_amounts) FROM dec SEMI JOIN top50 USING (address)) AS top50_trades_amounts_decoded,
  (SELECT sum(n_tokens) FROM dec SEMI JOIN top50 USING (address)) AS top50_trades_tokens_resolved,
  (SELECT sum(n) FROM cls WHERE cls = 'gmx_eventlog') AS top50_gmx_eventlog_logs,
  (SELECT count(*) FROM read_parquet('@MAP@/dex_trades/*/*.parquet', hive_partitioning = false) WHERE protocol LIKE 'gmx%') AS gmx_trade_events_detected,
  (SELECT sum(n) FROM cls WHERE cls = 'known_non_swap') AS top50_known_non_swap_logs,
  (SELECT sum(n) FROM cls WHERE cls = 'dex_aux') AS top50_dex_aux_logs,
  (SELECT sum(n) FROM cls WHERE cls = 'unknown') AS top50_unknown_logs,
  (SELECT count(DISTINCT topic0) FROM cls WHERE cls = 'unknown') AS top50_unknown_topics;
CREATE TEMP TABLE sigs AS
SELECT s.sig, s.role, sum(et.n) AS n_logs, count(DISTINCT et.address) AS n_emitters
FROM et JOIN dex_sig s ON s.topic0 = et.topic0 GROUP BY s.sig, s.role ORDER BY n_logs DESC;
CREATE TEMP TABLE unk AS
SELECT left(hx(topic0), 18) AS topic0_prefix, sum(n) AS n, count(DISTINCT address) AS n_emitters_top50,
       hx(arg_max(address, n)) AS main_emitter
FROM cls WHERE cls = 'unknown' GROUP BY topic0 ORDER BY n DESC LIMIT 20;
CREATE TEMP TABLE prot AS
SELECT protocol, count(*) AS n_trades, count(*) FILTER (WHERE token_in IS NOT NULL AND token_out IS NOT NULL) AS n_tokens_resolved,
       count(*) FILTER (WHERE amount_in IS NOT NULL AND amount_out IS NOT NULL) AS n_amounts, count(DISTINCT pool) AS n_pools,
       count(*) FILTER (WHERE router IS NOT NULL) AS n_with_router
FROM read_parquet('@MAP@/dex_trades/*/*.parquet', hive_partitioning = false) GROUP BY protocol ORDER BY n_trades DESC;
COPY (SELECT (SELECT to_json(r) FROM rep r) AS top50_summary,
             (SELECT to_json(list(s)) FROM sigs s) AS dex_signatures,
             (SELECT to_json(list(p)) FROM prot p) AS protocols,
             (SELECT to_json(list(u)) FROM unk u) AS top50_unknown_topics)
TO '@MAP@/dex_coverage.json' (FORMAT json);
SELECT * FROM rep;
SELECT * FROM sigs;
SELECT * FROM prot;
SELECT * FROM unk;
