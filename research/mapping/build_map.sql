-- =====================================================================================
-- build_map.sql  --  wallet-mapping layer over em-extract Parquet (DuckDB >= 1.5)
-- =====================================================================================
-- This file is a set of SQL *sections* ("-- @section <name>") that build_map.sh runs as
-- separate DuckDB processes, after substituting @PLACEHOLDERS@ and prepending a preamble
-- that defines the input views (src_* = explicit lists of finished chunk files; *.tmp-*
-- files are never listed because the list comes from OK manifests).
--
-- Pipeline (all steps restartable, outputs written to temp dirs and renamed):
--   range    (per 10M-block range, incremental)  -> <map>/stage/ranges/r<NNN>/
--              edges/bucket=b/   partial edge aggregates  (from,to,asset,kind) + limb sums
--              addr/bucket=b/    partial per-address stats (+ first/last time via blocks)
--              flows/bucket=b/   partial per-(token,holder) in/out flows
--              tokens.parquet, emitter_topics.parquet, counts.parquet
--   merge_edges     (per bucket of hash(from))   -> edges/part-b, stage rev pairs by bucket(to)
--   merge_flows     (per bucket of holder)       -> address_tokens/part-b, holder counts
--   merge_addresses (per bucket of address)      -> addresses/part-b (exact degrees)
--   finalize                                     -> token_stats, emitter_topics
--   checks                                       -> checks.json
-- Buckets use the last address byte (deterministic, DuckDB-version independent), so a
-- given address is always in the same bucket in every range partial.
--
-- uint256 handling (amounts are FIXED_LEN_BYTE_ARRAY(32) big-endian):
--   * every amount is split into four 64-bit limbs (UBIGINT); limb sums are exact in
--     HUGEINT for < 2^63 rows, stored in partial Parquet as DECIMAL(38,0) (DuckDB writes
--     HUGEINT/UHUGEINT to Parquet as DOUBLE, so they are never written as such).
--   * merge sums limbs again, then carry-normalises them.  Output:
--       total_amount       DECIMAL(38,0)  exact when the total < 10^38, else NULL
--       total_amount_big   VARCHAR        exact base-10 string, only set when total >= 10^38
--     (exact string = coalesce(total_amount::VARCHAR, total_amount_big); built with
--      BIGNUM additions of 18-digit partial products, no precision loss up to ~2^320).
--   * addresses.native_value_*: DECIMAL(38,0) (10^38 wei = 1e20 ETH, cannot overflow in
--     practice); on overflow the value is NULL and native_value_overflow = true.
--   * address_tokens.holds is an exact sign test on the 256-bit net balance;
--     net_approx is the same value as DOUBLE.
-- Semantics:
--   native_tx edges/values: top-level txs with non-NULL "to".  n counts all txs (also
--   failed ones); total_amount / native_value_* only count status = 1 (value actually
--   moved).  Creation txs (to = NULL) count in tx_sent / native_value_sent, not in edges.
--   deposit_value (type 105) is a mint, not a transfer -> not counted.
--   Token kinds: ERC20 (+WETH_DEPOSIT/WITHDRAWAL) -> erc20, ERC721 -> erc721,
--   ERC1155_SINGLE/BATCH -> erc1155 (batch = one row per element). Unknown standards are
--   counted in counts.parquet (n_tt_unknown) and skipped.
-- =====================================================================================

-- @section settings
SET threads = @THREADS@;
SET memory_limit = '@MEM@';
SET temp_directory = '@TMP@';
SET max_temp_directory_size = '@TMPMAX@';
SET preserve_insertion_order = false;
SET enable_progress_bar = false;

-- @section macros
CREATE OR REPLACE MACRO z32() AS from_hex('0000000000000000000000000000000000000000000000000000000000000000');
CREATE OR REPLACE MACRO max128() AS from_hex('00000000000000000000000000000000ffffffffffffffffffffffffffffffff');
CREATE OR REPLACE MACRO z20() AS from_hex('0000000000000000000000000000000000000000');
CREATE OR REPLACE MACRO m64() AS 18446744073709551615::HUGEINT;
CREATE OR REPLACE MACRO um64() AS 18446744073709551615::UHUGEINT;
-- low / high 128 bits of a 32-byte big-endian uint256
CREATE OR REPLACE MACRO u256_lo(b) AS from_hex(substr(hex(b), 33, 32))::BIT::UHUGEINT;
CREATE OR REPLACE MACRO u256_hi(b) AS from_hex(substr(hex(b), 1, 32))::BIT::UHUGEINT;
-- deterministic bucket: last address byte mod @BUCKETS@ (power of two <= 256)
CREATE OR REPLACE MACRO bkt(a) AS (from_hex(right(hex(a), 2))::BIT::UTINYINT % @BUCKETS@)::UTINYINT;
CREATE OR REPLACE MACRO kind_code(std) AS (CASE std WHEN 'ERC20' THEN 1 WHEN 'WETH_DEPOSIT' THEN 1
    WHEN 'WETH_WITHDRAWAL' THEN 1 WHEN 'ERC721' THEN 2 WHEN 'ERC1155_SINGLE' THEN 3
    WHEN 'ERC1155_BATCH' THEN 3 END)::UTINYINT;
CREATE OR REPLACE MACRO kind_name(k) AS CASE k WHEN 0 THEN 'native_tx' WHEN 1 THEN 'erc20'
    WHEN 2 THEN 'erc721' WHEN 3 THEN 'erc1155' END;
-- limb carry chain (HUGEINT, works for signed limbs: >> is an arithmetic shift, & m64 = mod 2^64)
CREATE OR REPLACE MACRO c1(s0, s1) AS s1 + (s0 >> 64);
CREATE OR REPLACE MACRO c2(s0, s1, s2) AS s2 + (c1(s0, s1) >> 64);
CREATE OR REPLACE MACRO c3(s0, s1, s2, s3) AS s3 + (c2(s0, s1, s2) >> 64);
CREATE OR REPLACE MACRO lo128(s0, s1) AS ((c1(s0, s1) & m64())::UHUGEINT << 64) | (s0 & m64())::UHUGEINT;
-- exact DECIMAL(38,0) of sum(limb_i * 2^(64 i)) when < 10^38, else NULL   (limbs >= 0)
CREATE OR REPLACE MACRO limbs_dec38(s0, s1, s2, s3) AS
    CASE WHEN c2(s0, s1, s2) = 0 AND s3 = 0
          AND lo128(s0, s1) < 100000000000000000000000000000000000000::UHUGEINT
         THEN lo128(s0, s1)::DECIMAL(38, 0) END;
-- exact base-10 string for any value (limbs >= 0); 2^128/2^192/2^256 split in 10^18 digits
CREATE OR REPLACE MACRO bn(x, z) AS (CAST(x AS VARCHAR) || repeat('0', z))::BIGNUM;
CREATE OR REPLACE MACRO limbs_str(s0, s1, s2, s3) AS (
      bn(s0 & m64(), 0)
    + bn((c1(s0, s1) & m64())::UHUGEINT << 64, 0)
    + bn((c2(s0, s1, s2) & m64()) * 374607431768211456, 0)
    + bn((c2(s0, s1, s2) & m64()) * 282366920938463463, 18)
    + bn((c2(s0, s1, s2) & m64()) * 340, 36)
    + bn((c3(s0, s1, s2, s3) & m64()) * 355444464034512896, 0)
    + bn((c3(s0, s1, s2, s3) & m64()) * 789423207666416102, 18)
    + bn((c3(s0, s1, s2, s3) & m64()) * 101735386680763835, 36)
    + bn((c3(s0, s1, s2, s3) & m64()) * 6277, 54)
    + bn((c3(s0, s1, s2, s3) >> 64) * 584007913129639936, 0)
    + bn((c3(s0, s1, s2, s3) >> 64) * 984665640564039457, 18)
    + bn((c3(s0, s1, s2, s3) >> 64) * 985008687907853269, 36)
    + bn((c3(s0, s1, s2, s3) >> 64) * 89237316195423570, 54)
    + bn((c3(s0, s1, s2, s3) >> 64) * 115792, 72))::VARCHAR;
-- sign test of a signed 4-limb value (exact) and DOUBLE approximation
CREATE OR REPLACE MACRO limbs_pos(d0, d1, d2, d3) AS
    c3(d0, d1, d2, d3) > 0 OR (c3(d0, d1, d2, d3) = 0 AND ((d0 & m64()) > 0
        OR (c1(d0, d1) & m64()) > 0 OR (c2(d0, d1, d2) & m64()) > 0));
CREATE OR REPLACE MACRO limbs_f64(d0, d1, d2, d3) AS d0::DOUBLE + d1::DOUBLE * 18446744073709551616.0
    + d2::DOUBLE * 3.402823669209385e38 + d3::DOUBLE * 6.277101735386681e57;
CREATE OR REPLACE MACRO hx(b) AS '0x' || lower(hex(b));

-- @section range
-- inputs: views src_blocks, src_tx, src_logs, src_tt, src_cc (one 10M-block range)
CREATE TEMP TABLE blk AS SELECT number AS bn, make_timestamp(timestamp::BIGINT * 1000000) AS ts FROM src_blocks;

-- 1. range-level edge aggregate (tx rows with "to" NULL kept here for sender stats only)
CREATE TEMP TABLE e AS
WITH tx AS (
    SELECT "from" AS f, "to" AS t, status, block_number AS bn,
           CASE WHEN status = 1 AND value <> z32() THEN u256_lo(value) ELSE 0::UHUGEINT END AS lo,
           CASE WHEN status = 1 AND value > max128() THEN u256_hi(value) ELSE 0::UHUGEINT END AS hi
    FROM src_tx),
tt AS (
    SELECT "from" AS f, "to" AS t, token AS a, kind_code(standard) AS k, block_number AS bn,
           CASE WHEN standard <> 'ERC721' AND amount <> z32() THEN u256_lo(amount) ELSE 0::UHUGEINT END AS lo,
           CASE WHEN standard <> 'ERC721' AND amount > max128() THEN u256_hi(amount) ELSE 0::UHUGEINT END AS hi
    FROM src_tt WHERE kind_code(standard) IS NOT NULL)
SELECT f, t, 'native'::BLOB AS a, 0::UTINYINT AS k, count(*)::UBIGINT AS n,
       count(*) FILTER (WHERE status <> 1)::UBIGINT AS n_fail,
       sum((lo & um64())::UBIGINT)::HUGEINT AS s0, sum((lo >> 64)::UBIGINT)::HUGEINT AS s1,
       sum((hi & um64())::UBIGINT)::HUGEINT AS s2, sum((hi >> 64)::UBIGINT)::HUGEINT AS s3,
       min(bn) AS fb, max(bn) AS lb
FROM tx GROUP BY f, t
UNION ALL
SELECT f, t, a, k, count(*)::UBIGINT, 0::UBIGINT,
       sum((lo & um64())::UBIGINT)::HUGEINT, sum((lo >> 64)::UBIGINT)::HUGEINT,
       sum((hi & um64())::UBIGINT)::HUGEINT, sum((hi >> 64)::UBIGINT)::HUGEINT,
       min(bn), max(bn)
FROM tt GROUP BY f, t, a, k;

COPY (SELECT f AS "from", t AS "to", a AS asset, k AS kind, n,
             s0::DECIMAL(38,0) AS s0, s1::DECIMAL(38,0) AS s1, s2::DECIMAL(38,0) AS s2, s3::DECIMAL(38,0) AS s3,
             fb, lb, bkt(f) AS bucket
      FROM e WHERE t IS NOT NULL)
TO '@RDIR@/edges' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (bucket));

-- 2. emitted-event histogram per contract
CREATE TEMP TABLE et AS
SELECT address, topic0, count(*)::UBIGINT AS n, min(block_number) AS fb, max(block_number) AS lb
FROM src_logs GROUP BY address, topic0;
COPY et TO '@RDIR@/emitter_topics.parquet' (FORMAT parquet, COMPRESSION zstd);

-- 3. per-address partial stats
COPY (
WITH r AS (
    SELECT f AS address,
           sum(n) FILTER (WHERE k = 0) AS tx_sent, sum(n_fail) FILTER (WHERE k = 0) AS tx_sent_failed,
           sum(s0) FILTER (WHERE k = 0) AS vs0, sum(s1) FILTER (WHERE k = 0) AS vs1,
           sum(s2) FILTER (WHERE k = 0) AS vs2, sum(s3) FILTER (WHERE k = 0) AS vs3,
           sum(n) FILTER (WHERE k > 0) AS tt_out, min(fb) AS fb, max(lb) AS lb
    FROM e GROUP BY f
    UNION ALL BY NAME
    SELECT t AS address,
           sum(n) FILTER (WHERE k = 0) AS tx_received, sum(n_fail) FILTER (WHERE k = 0) AS tx_received_failed,
           sum(s0) FILTER (WHERE k = 0) AS vr0, sum(s1) FILTER (WHERE k = 0) AS vr1,
           sum(s2) FILTER (WHERE k = 0) AS vr2, sum(s3) FILTER (WHERE k = 0) AS vr3,
           sum(n) FILTER (WHERE k > 0) AS tt_in, min(fb) AS fb, max(lb) AS lb
    FROM e WHERE t IS NOT NULL GROUP BY t
    UNION ALL BY NAME
    SELECT address, sum(n) AS logs_emitted, min(fb) AS fb, max(lb) AS lb FROM et GROUP BY address
    UNION ALL BY NAME
    SELECT address, min(block_number) AS cc_block, arg_min(creator, block_number) AS creator,
           min(block_number) AS fb, max(block_number) AS lb
    FROM src_cc GROUP BY address
    UNION ALL BY NAME
    SELECT creator AS address, count(*) AS contracts_created, min(block_number) AS fb, max(block_number) AS lb
    FROM src_cc GROUP BY creator),
g AS (
    SELECT address,
           coalesce(sum(tx_sent), 0)::UBIGINT AS tx_sent, coalesce(sum(tx_sent_failed), 0)::UBIGINT AS tx_sent_failed,
           coalesce(sum(tx_received), 0)::UBIGINT AS tx_received,
           coalesce(sum(tx_received_failed), 0)::UBIGINT AS tx_received_failed,
           coalesce(sum(vs0), 0)::DECIMAL(38,0) AS vs0, coalesce(sum(vs1), 0)::DECIMAL(38,0) AS vs1,
           coalesce(sum(vs2), 0)::DECIMAL(38,0) AS vs2, coalesce(sum(vs3), 0)::DECIMAL(38,0) AS vs3,
           coalesce(sum(vr0), 0)::DECIMAL(38,0) AS vr0, coalesce(sum(vr1), 0)::DECIMAL(38,0) AS vr1,
           coalesce(sum(vr2), 0)::DECIMAL(38,0) AS vr2, coalesce(sum(vr3), 0)::DECIMAL(38,0) AS vr3,
           coalesce(sum(tt_out), 0)::UBIGINT AS tt_out, coalesce(sum(tt_in), 0)::UBIGINT AS tt_in,
           coalesce(sum(logs_emitted), 0)::UBIGINT AS logs_emitted,
           coalesce(sum(contracts_created), 0)::UBIGINT AS contracts_created,
           min(cc_block) AS cc_block, arg_min(creator, cc_block) AS creator,
           min(fb) AS fb, max(lb) AS lb
    FROM r GROUP BY address)
SELECT g.*, b1.ts AS ft, b2.ts AS lt, bkt(g.address) AS bucket
FROM g LEFT JOIN blk b1 ON b1.bn = g.fb LEFT JOIN blk b2 ON b2.bn = g.lb
) TO '@RDIR@/addr' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (bucket));

-- 4. per-(token, holder) flows (for holders / address_tokens)
COPY (
SELECT a AS token, h AS holder, k AS kind, sum(n_in)::UBIGINT AS n_in, sum(n_out)::UBIGINT AS n_out,
       sum(i0)::DECIMAL(38,0) AS i0, sum(i1)::DECIMAL(38,0) AS i1, sum(i2)::DECIMAL(38,0) AS i2, sum(i3)::DECIMAL(38,0) AS i3,
       sum(o0)::DECIMAL(38,0) AS o0, sum(o1)::DECIMAL(38,0) AS o1, sum(o2)::DECIMAL(38,0) AS o2, sum(o3)::DECIMAL(38,0) AS o3,
       min(fb) AS fb, max(lb) AS lb, bkt(h) AS bucket
FROM (
    SELECT a, t AS h, k, n AS n_in, 0::UBIGINT AS n_out, s0 AS i0, s1 AS i1, s2 AS i2, s3 AS i3,
           0::HUGEINT AS o0, 0::HUGEINT AS o1, 0::HUGEINT AS o2, 0::HUGEINT AS o3, fb, lb
    FROM e WHERE k > 0
    UNION ALL
    SELECT a, f AS h, k, 0::UBIGINT, n, 0::HUGEINT, 0::HUGEINT, 0::HUGEINT, 0::HUGEINT, s0, s1, s2, s3, fb, lb
    FROM e WHERE k > 0)
GROUP BY a, h, k
) TO '@RDIR@/flows' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (bucket));

-- 5. token partial stats
COPY (
SELECT t.*, b1.ts AS ft, b2.ts AS lt FROM (
    SELECT a AS token, k AS kind, sum(n)::UBIGINT AS n_transfers, min(fb) AS fb, max(lb) AS lb
    FROM e WHERE k > 0 GROUP BY a, k) t
LEFT JOIN blk b1 ON b1.bn = t.fb LEFT JOIN blk b2 ON b2.bn = t.lb
) TO '@RDIR@/tokens.parquet' (FORMAT parquet, COMPRESSION zstd);

-- 6. source counts (for consistency checks)
COPY (SELECT
    (SELECT count(*) FROM src_blocks) AS n_blocks,
    (SELECT min(number) FROM src_blocks) AS min_block, (SELECT max(number) FROM src_blocks) AS max_block,
    (SELECT count(*) FROM src_tx) AS n_tx, (SELECT count("to") FROM src_tx) AS n_tx_to,
    (SELECT count(*) FROM src_tx WHERE status = 1) AS n_tx_ok,
    (SELECT count(*) FROM src_tt WHERE kind_code(standard) = 1) AS n_tt_erc20,
    (SELECT count(*) FROM src_tt WHERE kind_code(standard) = 2) AS n_tt_erc721,
    (SELECT count(*) FROM src_tt WHERE kind_code(standard) = 3) AS n_tt_erc1155,
    (SELECT count(*) FROM src_tt WHERE kind_code(standard) IS NULL) AS n_tt_unknown,
    (SELECT count(*) FROM src_logs) AS n_logs, (SELECT count(*) FROM src_cc) AS n_cc,
    (SELECT count(*) FROM e) AS n_edge_partials
) TO '@RDIR@/counts.parquet' (FORMAT parquet);

-- @section merge_edges
-- inputs: view stg_edges (this bucket, all ranges)
CREATE TEMP TABLE m AS
SELECT "from", "to", asset, kind, sum(n)::UBIGINT AS n,
       sum(s0)::HUGEINT AS s0, sum(s1)::HUGEINT AS s1, sum(s2)::HUGEINT AS s2, sum(s3)::HUGEINT AS s3,
       min(fb) AS fb, max(lb) AS lb
FROM stg_edges GROUP BY "from", "to", asset, kind;

COPY (
SELECT "from", "to", asset, kind_name(kind) AS kind, n,
       CASE WHEN kind <> 2 THEN limbs_dec38(s0, s1, s2, s3) END AS total_amount,
       CASE WHEN kind <> 2 AND limbs_dec38(s0, s1, s2, s3) IS NULL THEN limbs_str(s0, s1, s2, s3) END AS total_amount_big,
       fb AS first_block, lb AS last_block
FROM m ORDER BY "from", "to"
) TO '@NEXT@/edges/part-@B@.parquet' (FORMAT parquet, COMPRESSION zstd);

-- reverse adjacency (distinct (to, from) pairs), re-bucketed by "to" for exact in-degree
COPY (SELECT "to" AS a, "from" AS x, bkt("to") AS bucket FROM m WHERE "to" <> "from" GROUP BY "to", "from")
TO '@MAP@/stage/merge/rev/src=@B@' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (bucket), OVERWRITE_OR_IGNORE);

-- @section merge_flows
-- inputs: view stg_flows (this bucket, all ranges)
CREATE TEMP TABLE fl AS
SELECT token, holder, kind, n_in, n_out, d0, d1, d2, d3, fb, lb,
       CASE WHEN kind = 2 THEN n_in > n_out ELSE limbs_pos(d0, d1, d2, d3) END AS holds,
       CASE WHEN kind = 2 THEN n_in::DOUBLE - n_out::DOUBLE ELSE limbs_f64(d0, d1, d2, d3) END AS net_approx
FROM (
    SELECT token, holder, kind, sum(n_in)::UBIGINT AS n_in, sum(n_out)::UBIGINT AS n_out,
           (sum(i0) - sum(o0))::HUGEINT AS d0, (sum(i1) - sum(o1))::HUGEINT AS d1,
           (sum(i2) - sum(o2))::HUGEINT AS d2, (sum(i3) - sum(o3))::HUGEINT AS d3,
           min(fb) AS fb, max(lb) AS lb
    FROM stg_flows GROUP BY token, holder, kind);

COPY (SELECT holder AS address, token, kind_name(kind) AS kind, n_in, n_out, holds, net_approx,
             fb AS first_block, lb AS last_block
      FROM fl ORDER BY holder, token)
TO '@NEXT@/address_tokens/part-@B@.parquet' (FORMAT parquet, COMPRESSION zstd);

COPY (SELECT token, kind,
             count(*) FILTER (WHERE holds AND holder <> z20())::UBIGINT AS n_holders,
             count(*) FILTER (WHERE n_in > 0 AND holder <> z20())::UBIGINT AS n_receivers,
             count(*) FILTER (WHERE n_out > 0 AND holder <> z20())::UBIGINT AS n_senders
      FROM fl GROUP BY token, kind)
TO '@MAP@/stage/merge/holders/part-@B@.parquet' (FORMAT parquet, COMPRESSION zstd);

-- @section merge_addresses
-- inputs: views stg_addr (this bucket, all ranges), out_edges (final edges of this bucket),
--         rev_pairs (reverse pairs whose "to" is in this bucket), dex_addr (optional)
CREATE TEMP TABLE deg AS
SELECT a, count(*)::UBIGINT AS cp, count(*) FILTER (WHERE o)::UBIGINT AS out_deg,
       count(*) FILTER (WHERE i)::UBIGINT AS in_deg
FROM (
    SELECT a, x, bool_or(o) AS o, bool_or(i) AS i FROM (
        SELECT "from" AS a, "to" AS x, true AS o, false AS i FROM out_edges WHERE "from" <> "to"
        UNION ALL
        SELECT a, x, false, true FROM rev_pairs)
    GROUP BY a, x)
GROUP BY a;

COPY (
WITH g AS (
    SELECT address,
           sum(tx_sent)::UBIGINT AS tx_sent, sum(tx_sent_failed)::UBIGINT AS tx_sent_failed,
           sum(tx_received)::UBIGINT AS tx_received, sum(tx_received_failed)::UBIGINT AS tx_received_failed,
           sum(vs0)::HUGEINT AS vs0, sum(vs1)::HUGEINT AS vs1, sum(vs2)::HUGEINT AS vs2, sum(vs3)::HUGEINT AS vs3,
           sum(vr0)::HUGEINT AS vr0, sum(vr1)::HUGEINT AS vr1, sum(vr2)::HUGEINT AS vr2, sum(vr3)::HUGEINT AS vr3,
           sum(tt_out)::UBIGINT AS tt_out, sum(tt_in)::UBIGINT AS tt_in,
           sum(logs_emitted)::UBIGINT AS logs_emitted, sum(contracts_created)::UBIGINT AS contracts_created,
           min(cc_block) AS cc_block, arg_min(creator, cc_block) AS creator,
           min(fb) AS fb, max(lb) AS lb, min(ft) AS ft, max(lt) AS lt
    FROM stg_addr GROUP BY address)
SELECT g.address,
       g.fb AS first_block, g.ft AS first_time, g.lb AS last_block, g.lt AS last_time,
       g.tx_sent, g.tx_sent_failed, g.tx_received, g.tx_received_failed,
       limbs_dec38(vs0, vs1, vs2, vs3) AS native_value_sent,
       limbs_dec38(vr0, vr1, vr2, vr3) AS native_value_received,
       (limbs_dec38(vs0, vs1, vs2, vs3) IS NULL OR limbs_dec38(vr0, vr1, vr2, vr3) IS NULL) AS native_value_overflow,
       g.tt_out AS token_transfers_out, g.tt_in AS token_transfers_in,
       coalesce(d.cp, 0)::UBIGINT AS distinct_counterparties,
       coalesce(d.out_deg, 0)::UBIGINT AS out_degree, coalesce(d.in_deg, 0)::UBIGINT AS in_degree,
       (g.cc_block IS NOT NULL) AS is_contract, g.creator, g.cc_block AS contract_creation_block,
       g.contracts_created, g.logs_emitted,
       coalesce(x.dex_trades, 0)::UBIGINT AS dex_trades, coalesce(x.dex_pools, 0)::UBIGINT AS dex_pools,
       coalesce(x.dex_tokens, 0)::UBIGINT AS dex_tokens
FROM g LEFT JOIN deg d ON d.a = g.address
       LEFT JOIN dex_addr x ON x.address = g.address
ORDER BY g.address
) TO '@NEXT@/addresses/part-@B@.parquet' (FORMAT parquet, COMPRESSION zstd);

-- @section finalize
-- inputs: views stg_tokens, stg_holders, stg_topics
COPY (
WITH t AS (
    SELECT token, kind, sum(n_transfers) AS n, min(fb) AS fb, max(lb) AS lb, min(ft) AS ft, max(lt) AS lt
    FROM stg_tokens GROUP BY token, kind),
h AS (
    SELECT token, kind, sum(n_holders) AS n_holders, sum(n_receivers) AS n_receivers, sum(n_senders) AS n_senders
    FROM stg_holders GROUP BY token, kind)
SELECT t.token, string_agg(kind_name(t.kind), ',' ORDER BY t.kind) AS standard,
       sum(t.n)::UBIGINT AS n_transfers, coalesce(sum(h.n_holders), 0)::UBIGINT AS n_holders_approx,
       coalesce(sum(h.n_receivers), 0)::UBIGINT AS n_receivers, coalesce(sum(h.n_senders), 0)::UBIGINT AS n_senders,
       min(t.fb) AS first_block, max(t.lb) AS last_block, min(t.ft) AS first_time, max(t.lt) AS last_time
FROM t LEFT JOIN h ON h.token = t.token AND h.kind = t.kind
GROUP BY t.token ORDER BY n_transfers DESC
) TO '@NEXT@/token_stats/token_stats.parquet' (FORMAT parquet, COMPRESSION zstd);

COPY (SELECT address, topic0, sum(n)::UBIGINT AS n, min(fb) AS first_block, max(lb) AS last_block
      FROM stg_topics GROUP BY address, topic0 ORDER BY address, n DESC)
TO '@NEXT@/emitter_topics/emitter_topics.parquet' (FORMAT parquet, COMPRESSION zstd);

-- @section checks
-- inputs: views stg_counts, map_edges, map_addresses, map_tokens, map_address_tokens, src_tx (all ranges)
CREATE TEMP TABLE src AS SELECT sum(n_blocks) n_blocks, min(min_block) min_block, max(max_block) max_block,
    sum(n_tx) n_tx, sum(n_tx_to) n_tx_to, sum(n_tx_ok) n_tx_ok, sum(n_tt_erc20) n_tt_erc20,
    sum(n_tt_erc721) n_tt_erc721, sum(n_tt_erc1155) n_tt_erc1155, sum(n_tt_unknown) n_tt_unknown,
    sum(n_logs) n_logs, sum(n_cc) n_cc FROM stg_counts;
CREATE TEMP TABLE ek AS SELECT kind, sum(n) n, count(*) edges, sum(total_amount) FILTER (WHERE kind = 'native_tx') amt,
    count(*) FILTER (WHERE total_amount_big IS NOT NULL) n_big FROM map_edges GROUP BY kind;
CREATE TEMP TABLE dup AS SELECT count(*) AS dup_keys FROM (
    SELECT "from", "to", asset, kind FROM map_edges GROUP BY ALL HAVING count(*) > 1);
CREATE TEMP TABLE ad AS SELECT count(*) n_addr, count(DISTINCT address) n_addr_distinct,
    sum(tx_sent) tx_sent, sum(tx_received) tx_received, sum(token_transfers_out) tt_out,
    sum(token_transfers_in) tt_in, sum(native_value_sent) v_sent, sum(native_value_received) v_recv,
    count(*) FILTER (WHERE is_contract) n_contract, sum(contracts_created) cc_created, sum(logs_emitted) logs
    FROM map_addresses;
-- independent native-value recomputation from raw tx with a different decoding path
CREATE TEMP TABLE rawv AS SELECT
    sum(('0x' || substr(hex(value), 49, 16))::UBIGINT) FILTER (WHERE status = 1 AND "to" IS NOT NULL) AS l0,
    sum(('0x' || substr(hex(value), 33, 16))::UBIGINT) FILTER (WHERE status = 1 AND "to" IS NOT NULL) AS l1,
    sum(('0x' || substr(hex(value), 17, 16))::UBIGINT) FILTER (WHERE status = 1 AND "to" IS NOT NULL) AS l2,
    sum(('0x' || substr(hex(value), 1, 16))::UBIGINT) FILTER (WHERE status = 1 AND "to" IS NOT NULL) AS l3
    FROM src_tx WHERE value <> z32();
CREATE TEMP TABLE res AS
SELECT 'native_tx edges.n == count(tx with to)' AS check_name, (SELECT n FROM ek WHERE kind = 'native_tx')::VARCHAR AS got, n_tx_to::VARCHAR AS expected FROM src
UNION ALL SELECT 'erc20 edges.n == token_transfers ERC20(+WETH)', (SELECT n FROM ek WHERE kind = 'erc20')::VARCHAR, n_tt_erc20::VARCHAR FROM src
UNION ALL SELECT 'erc721 edges.n == token_transfers ERC721', (SELECT n FROM ek WHERE kind = 'erc721')::VARCHAR, n_tt_erc721::VARCHAR FROM src
UNION ALL SELECT 'erc1155 edges.n == token_transfers ERC1155', coalesce((SELECT n FROM ek WHERE kind = 'erc1155'), 0)::VARCHAR, n_tt_erc1155::VARCHAR FROM src
UNION ALL SELECT 'duplicate (from,to,asset,kind) keys', (SELECT dup_keys FROM dup)::VARCHAR, '0'
UNION ALL SELECT 'addresses unique', (SELECT n_addr_distinct FROM ad)::VARCHAR, (SELECT n_addr FROM ad)::VARCHAR
UNION ALL SELECT 'sum(addresses.tx_sent) == count(tx)', (SELECT tx_sent FROM ad)::VARCHAR, n_tx::VARCHAR FROM src
UNION ALL SELECT 'sum(addresses.tx_received) == count(tx with to)', (SELECT tx_received FROM ad)::VARCHAR, n_tx_to::VARCHAR FROM src
UNION ALL SELECT 'sum(token_transfers_out) == all token transfers', (SELECT tt_out FROM ad)::VARCHAR, (n_tt_erc20 + n_tt_erc721 + n_tt_erc1155)::VARCHAR FROM src
UNION ALL SELECT 'sum(token_transfers_in) == all token transfers', (SELECT tt_in FROM ad)::VARCHAR, (n_tt_erc20 + n_tt_erc721 + n_tt_erc1155)::VARCHAR FROM src
UNION ALL SELECT 'sum(token_stats.n_transfers) == all token transfers', (SELECT sum(n_transfers) FROM map_tokens)::VARCHAR, (n_tt_erc20 + n_tt_erc721 + n_tt_erc1155)::VARCHAR FROM src
UNION ALL SELECT 'sum(address_tokens.n_in) == all token transfers', (SELECT sum(n_in) FROM map_address_tokens)::VARCHAR, (n_tt_erc20 + n_tt_erc721 + n_tt_erc1155)::VARCHAR FROM src
UNION ALL SELECT 'sum(address_tokens.n_out) == all token transfers', (SELECT sum(n_out) FROM map_address_tokens)::VARCHAR, (n_tt_erc20 + n_tt_erc721 + n_tt_erc1155)::VARCHAR FROM src
UNION ALL SELECT 'native edges total_amount == addresses.native_value_received', (SELECT amt FROM ek WHERE kind = 'native_tx')::VARCHAR, (SELECT v_recv FROM ad)::VARCHAR
UNION ALL SELECT 'native edges total_amount == raw recompute (hex path)', (SELECT amt FROM ek WHERE kind = 'native_tx')::VARCHAR,
       (SELECT limbs_dec38(coalesce(l0,0), coalesce(l1,0), coalesce(l2,0), coalesce(l3,0)) FROM rawv)::VARCHAR
UNION ALL SELECT 'is_contract count == contract_creations rows (distinct)', (SELECT n_contract FROM ad)::VARCHAR, (SELECT count(DISTINCT address) FROM src_cc)::VARCHAR
UNION ALL SELECT 'sum(contracts_created) == contract_creations rows', (SELECT cc_created FROM ad)::VARCHAR, n_cc::VARCHAR FROM src
UNION ALL SELECT 'sum(logs_emitted) == logs rows', (SELECT logs FROM ad)::VARCHAR, n_logs::VARCHAR FROM src
UNION ALL SELECT 'unknown token standards (skipped)', n_tt_unknown::VARCHAR, '0' FROM src;
COPY (SELECT check_name, got, expected, got IS NOT DISTINCT FROM expected AS ok FROM res) TO '@MAP@/checks.json' (FORMAT json, ARRAY true);
SELECT check_name, got, expected, got IS NOT DISTINCT FROM expected AS ok FROM res;
SELECT * FROM src;
SELECT kind, n, edges, n_big FROM ek ORDER BY kind;
