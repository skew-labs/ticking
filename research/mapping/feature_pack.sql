-- =====================================================================================
-- feature_pack.sql  --  compact JSON feature packs for LLM labeling (run by feature_pack.sh)
-- =====================================================================================
-- Inputs: the published map (@MAP@/addresses, edges, address_tokens, emitter_topics,
-- token_stats, dex_pools, dex_trades) and the view src_tx (raw transactions of the ranges in
-- the map: sample tx hashes and 4-byte selectors from transactions.method_id; the extractor
-- keeps method_id + input_size, not the full calldata).
-- Selection: top @N@ addresses by distinct_counterparties + tx_sent + tx_received (0x0 excluded).
-- Output: @MAP@/feature_packs/packs_top@N@.parquet (address, score, pack, bytes) and, written by
-- feature_pack.sh, packs_top@N@.jsonl: one JSON object per line, first keys "chain", "address".
-- Each pack is < 3000 bytes (lists are trimmed when the full pack is larger).
-- Pack fields (flat layout read by em-labeling/dispatch.py: chain, address, tx_sent, tx_received,
-- topic0_hist[{topic0,n}], top_*_counterparties[{addr,n}], top_tokens, native_in/out, sample_txs):
--   is_contract (tx-level creation seen), emits_logs, creator, created_block, creates_contracts,
--   first_time/last_time (unix s), first_block/last_block, tx_sent(_failed), tx_received,
--   token_transfers_out/in, logs_emitted, dex_trades (as tx sender), distinct_counterparties,
--   out_degree/in_degree, fan_in_out_ratio = in/out, fanin_ratio/fanout_ratio = in/out degree over
--   distinct counterparties, native_in/native_out (wei / 1e18),
--   outflow_concentration: share of outflow going to the top-2 out counterparties,
--   top_out_counterparties / top_in_counterparties: [{addr, n, share, is_contract, cat}]
--       n = txs + token transfers on that edge direction, share = n / all outflow (inflow) of the
--       address (self-edges excluded); cat = heuristic category of the counterparty:
--       zero | system (ArbOS/precompile) | pool | dex_venue (V4 PoolManager, Balancer Vault) | token |
--       router | hub_fan_in | hub_fan_out | contract | eoa
--   unique_function_selectors_called: [[selector(:name), n_txs], ...] (from transactions.method_id;
--       the full calldata is not extracted, "(no calldata)" = plain transfer)
--   topic0_hist: [{topic0, n, event?}] events emitted by the address (event = local name if known)
--   top_tokens: [{token, std, n_in, n_out, holds}], sample_txs: latest sent / received tx hashes
-- Size: the largest list tier whose JSON is < 3000 bytes is used (tiers 5/10/8/6/3 -> 2/3/2/2/0).

-- @section feature_pack
CREATE TEMP TABLE known_topics(topic0 BLOB, name VARCHAR);
INSERT INTO known_topics VALUES
 (from_hex('ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'), 'Transfer'),
 (from_hex('8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925'), 'Approval'),
 (from_hex('17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31'), 'ApprovalForAll'),
 (from_hex('c3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62'), 'TransferSingle'),
 (from_hex('4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb'), 'TransferBatch'),
 (from_hex('e1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c'), 'Deposit(WETH)'),
 (from_hex('7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65'), 'Withdrawal(WETH)'),
 (from_hex('8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e0'), 'OwnershipTransferred'),
 (from_hex('bc7cd75a20ee27fd9adebab32041f755214dbc6bffa90cc0225b39da2e5c2d3b'), 'Upgraded(proxy)'),
 (from_hex('7e644d79422f17c01e4894b5f4f588d331ebfa28653d42ae832dc59e38c9798f'), 'AdminChanged(proxy)'),
 (from_hex('7f26b83ff96e1f2b6a682f133852f6798a09c465da95921460cefb3847402498'), 'Initialized(uint8)'),
 (from_hex('c7f505b2f371ae2175ee4913f4499e1f2633a7b5936321eed1cdaeb6115181d2'), 'Initialized(uint64)'),
 (from_hex('2f8788117e7eff1d82e926ec794901d17c78024a50270940304540a733656f0d'), 'RoleGranted'),
 (from_hex('62e78cea01bee320cd4e420270b5ea74000d11b0c9f74754ebdbfc544b05a258'), 'Paused'),
 (from_hex('5db9ee0a495bf2e6ff9c91a7834c1ba4fdd244a5e8aa4e537bd38aeae4b073aa'), 'Unpaused'),
 (from_hex('1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1'), 'Sync(UniV2)'),
 (from_hex('d78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822'), 'Swap(UniV2)'),
 (from_hex('4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f'), 'Mint(UniV2)'),
 (from_hex('dccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496'), 'Burn(UniV2)'),
 (from_hex('0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9'), 'PairCreated(UniV2)'),
 (from_hex('b3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b'), 'Swap(Solidly)'),
 (from_hex('c42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'), 'Swap(UniV3/Algebra)'),
 (from_hex('19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83'), 'Swap(PancakeV3)'),
 (from_hex('7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde'), 'Mint(UniV3)'),
 (from_hex('0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c'), 'Burn(UniV3)'),
 (from_hex('70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0'), 'Collect(UniV3)'),
 (from_hex('98636036cb66a9c19a37435efc1e90142190214e8abeb821bdba3f2990dd4c95'), 'Initialize(UniV3)'),
 (from_hex('783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118'), 'PoolCreated(UniV3)'),
 (from_hex('91ccaa7a278130b65168c3a0c8d3bcae84cf5e43704342bd3ec0b59e59c036db'), 'Pool(AlgebraFactory)'),
 (from_hex('40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'), 'Swap(UniV4)'),
 (from_hex('dd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438'), 'Initialize(UniV4)'),
 (from_hex('f208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec'), 'ModifyLiquidity(UniV4)'),
 (from_hex('8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140'), 'TokenExchange(Curve)'),
 (from_hex('d013ca23e77a65003c2c659c5442c00c805371b7fc1ebd4c206c41d1536bd90b'), 'TokenExchangeUnderlying(Curve)'),
 (from_hex('b2e76ae99761dc136e598d4a629bb347eccb9532a5f8bbd72e18467c3c34cc98'), 'TokenExchange(CurveCrypto)'),
 (from_hex('143f1f8e861fbdeddd5b46e844b7d3ac7b86a122f36e8c463859ee6811b1f29c'), 'TokenExchange(CurveTricryptoNG)'),
 (from_hex('2170c741c41531aec20e7c107c24eecfdd15e69c9bb0a8dd37b1840b9e0b207b'), 'Swap(BalancerV2Vault)'),
 (from_hex('0874b2d545cb271cdbda4e093020c452328b24af12382ed62c4d00f5c26709db'), 'Swap(BalancerV3Vault)'),
 (from_hex('137a44067c8961cd7e1d876f4754a5a3a75989b4552f1843fc69c3b372def160'), 'EventLog1(GMXv2)'),
 (from_hex('468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5'), 'EventLog2(GMXv2)'),
 (from_hex('7e3bde2ba7aca4a8499608ca57f3b0c1c1c93ace63ffd3741a9fab204146fc9a'), 'EventLog(GMXv2)'),
 (from_hex('49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f'), 'UserOperationEvent(ERC4337)'),
 (from_hex('5e3c1311ea442664e8b1611bfabef659120ea7a0a2cfc0667700bebc69cbffe1'), 'MessageDelivered(ArbBridge)'),
 (from_hex('bb47ee3e183a558b1a2ff0874b079f3fc5478b7454eacf2bfc5af2ff5878f972'), 'BeforeExecution(ERC4337)'),
 (from_hex('3d0ce9bfc3ed7d6862dbb28b2dea94561fe714a1b4d019aa8af39730d1ad7c3d'), 'SafeReceived(Safe)'),
 (from_hex('442e715f626346e8c54381002da614f62bee8d27386535b2521ec8540898556e'), 'ExecutionSuccess(Safe)'),
 (from_hex('205442d60b70af1203d43cab62352c3b69b94f091be32fe683198057282b5c92'), 'FeeCollected(addr,addr,uint,uint)'),
 (from_hex('40d0efd1a53d60ecbf40971b9daf7dc90178c3aadc7aab1765632738fa8b8f01'), 'Collect(UniV3 NPM)'),
 (from_hex('3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f'), 'IncreaseLiquidity(UniV3 NPM)'),
 (from_hex('26f6a048ee9138f2c0ce266f322cb99228e8d619ae2bff30c67f8dcf9d2377b4'), 'DecreaseLiquidity(UniV3 NPM)'),
 (from_hex('0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f'), 'AnswerUpdated(Chainlink)');
CREATE TEMP TABLE known_selectors(sel BLOB, name VARCHAR);
INSERT INTO known_selectors VALUES
 (from_hex('a9059cbb'), 'transfer'), (from_hex('095ea7b3'), 'approve'), (from_hex('23b872dd'), 'transferFrom'),
 (from_hex('3593564c'), 'execute(UniversalRouter)'), (from_hex('24856bc3'), 'execute(UniversalRouter,nodeadline)'),
 (from_hex('5ae401dc'), 'multicall(deadline)'), (from_hex('ac9650d8'), 'multicall'),
 (from_hex('1fad948c'), 'handleOps(4337v0.6)'), (from_hex('765e827f'), 'handleOps(4337v0.7)'),
 (from_hex('6a761202'), 'execTransaction(Safe)'), (from_hex('d0e30db0'), 'deposit'), (from_hex('2e1a7d4d'), 'withdraw'),
 (from_hex('38ed1739'), 'swapExactTokensForTokens'), (from_hex('7ff36ab5'), 'swapExactETHForTokens'),
 (from_hex('18cbafe5'), 'swapExactTokensForETH'), (from_hex('414bf389'), 'exactInputSingle(V3)'),
 (from_hex('c04b8d59'), 'exactInput(V3)'), (from_hex('04e45aaf'), 'exactInputSingle(SR02)'), (from_hex('b858183f'), 'exactInput(SR02)'),
 (from_hex('40c10f19'), 'mint'), (from_hex('a22cb465'), 'setApprovalForAll'), (from_hex('42842e0e'), 'safeTransferFrom(721)'),
 (from_hex('f242432a'), 'safeTransferFrom(1155)'), (from_hex('4e71d92d'), 'claim'), (from_hex('b61d27f6'), 'execute(acct)'),
 (from_hex('47e1da2a'), 'executeBatch'), (from_hex('34fcd5be'), 'executeBatch(calls)'), (from_hex('82ad56cb'), 'aggregate3'),
 (from_hex('48c89491'), 'unlock(V4)'), (from_hex('dd46508f'), 'modifyLiquidities(V4)'), (from_hex('d505accf'), 'permit'),
 (from_hex('128acb08'), 'swap(V3pool)'), (from_hex('ef9e5e28'), 'updatePriceFeeds'), (from_hex('b1dc65a4'), 'transmit(OCR)'),
 (from_hex('6bf6a42d'), 'startBlock(ArbOS)'), (from_hex('b6693771'), 'batchPostingReport(ArbOS)'),
 (from_hex('c9f95d32'), 'submitRetryable'), (from_hex('25e16063'), 'withdrawEth(ArbSys)'), (from_hex('928c169a'), 'sendTxToL1(ArbSys)');

CREATE TEMP TABLE top AS
SELECT *, (distinct_counterparties + tx_sent + tx_received)::UBIGINT AS score
FROM read_parquet('@MAP@/addresses/*.parquet')
WHERE address <> z20()
ORDER BY score DESC, address LIMIT @N@;

-- directed counterparty totals of the selected addresses (self-edges excluded)
CREATE TEMP TABLE cpd AS
WITH e AS (SELECT "from", "to", n FROM read_parquet('@MAP@/edges/*.parquet') WHERE "from" <> "to")
SELECT e."from" AS a, e."to" AS c, 'o' AS dir, sum(n)::UBIGINT AS n FROM e SEMI JOIN top ON top.address = e."from" GROUP BY 1, 2
UNION ALL
SELECT e."to", e."from", 'i', sum(n)::UBIGINT FROM e SEMI JOIN top ON top.address = e."to" GROUP BY 1, 2;
CREATE TEMP TABLE cp_top AS
SELECT a, dir, sum(n) AS total, max_by({'c': c, 'n': n}, n, 5) AS l FROM cpd GROUP BY a, dir;

-- heuristic categories of the counterparties that made it into a top list
CREATE TEMP TABLE cp_ids AS SELECT DISTINCT u.e.c AS c FROM (SELECT unnest(l) AS e FROM cp_top) u;
CREATE TEMP TABLE cp_cat AS
SELECT i.c,
       (ad.is_contract OR ad.logs_emitted > 0) AS is_contract,
       CASE WHEN i.c = z20() THEN 'zero'
            WHEN left(hex(i.c), 34) = '0000000000000000000000000000000000' THEN 'system'
            WHEN p.pool IS NOT NULL THEN 'pool'
            WHEN dv.venue IS NOT NULL THEN 'dex_venue'
            WHEN ts.token IS NOT NULL THEN 'token'
            WHEN r.router IS NOT NULL THEN 'router'
            WHEN ad.in_degree >= 10000 AND ad.in_degree > 10 * ad.out_degree THEN 'hub_fan_in'
            WHEN ad.out_degree >= 10000 AND ad.out_degree > 10 * ad.in_degree THEN 'hub_fan_out'
            WHEN ad.is_contract OR ad.logs_emitted > 0 THEN 'contract'
            WHEN ad.tx_sent > 0 THEN 'eoa' END AS cat
FROM cp_ids i
LEFT JOIN read_parquet('@MAP@/addresses/*.parquet') ad ON ad.address = i.c
LEFT JOIN (SELECT DISTINCT pool FROM dex_pools_v WHERE octet_length(pool) = 20) p ON p.pool = i.c
LEFT JOIN (SELECT token FROM read_parquet('@MAP@/token_stats/*.parquet')) ts ON ts.token = i.c
LEFT JOIN (SELECT DISTINCT router FROM dex_routers_v) r ON r.router = i.c
LEFT JOIN (SELECT DISTINCT venue FROM dex_pools_v WHERE venue IS NOT NULL AND (octet_length(pool) <> 20 OR pool <> venue)) dv ON dv.venue = i.c;
CREATE TEMP TABLE cp_l AS
SELECT u.a, u.dir, any_value(u.total) AS total,
       list({'addr': hx(u.e.c), 'n': u.e.n, 'share': round(u.e.n / greatest(u.total, 1), 4),
             'is_contract': coalesce(k.is_contract, false), 'cat': k.cat} ORDER BY u.pos) AS l,
       sum(u.e.n) FILTER (WHERE u.pos <= 2) AS top2
FROM (SELECT a, dir, total, unnest(l) AS e, generate_subscripts(l, 1) AS pos FROM cp_top) u
LEFT JOIN cp_cat k ON k.c = u.e.c
GROUP BY u.a, u.dir;

-- tokens touched by the selected addresses
CREATE TEMP TABLE tk AS
SELECT address AS a,
       max_by({'token': hx(token), 'std': kind, 'n_in': n_in, 'n_out': n_out, 'holds': holds}, n_in + n_out, 6) AS l
FROM read_parquet('@MAP@/address_tokens/*.parquet') t SEMI JOIN top ON top.address = t.address
GROUP BY address;

-- emitted events (address = log emitter)
CREATE TEMP TABLE ev AS
SELECT address AS a, max_by(CASE WHEN k.name IS NULL THEN json_object('topic0', hx(et.topic0), 'n', et.n)
                                 ELSE json_object('topic0', hx(et.topic0), 'n', et.n, 'event', k.name) END, et.n, 8) AS l
FROM read_parquet('@MAP@/emitter_topics/*.parquet') et
LEFT JOIN known_topics k ON k.topic0 = et.topic0
SEMI JOIN top ON top.address = et.address
GROUP BY address;

-- sent txs: 4-byte selectors (transactions.method_id) + latest sample hashes
CREATE TEMP TABLE sent AS
SELECT t."from" AS a, t.method_id, count(*)::UBIGINT AS n,
       max_by(hx(t.hash), t.block_number * 1000000 + t.tx_index) AS last_hash,
       max(t.block_number * 1000000 + t.tx_index) AS last_ord
FROM src_tx t SEMI JOIN top ON top.address = t."from" GROUP BY 1, 2;
CREATE TEMP TABLE sel AS
SELECT s.a, max_by([to_json(CASE WHEN s.method_id IS NULL THEN '(no calldata)'
                               ELSE hx(s.method_id) || coalesce(':' || k.name, '') END), to_json(s.n)], s.n, 10) AS l,
       max_by(s.last_hash, s.last_ord, 2) AS hashes
FROM sent s LEFT JOIN known_selectors k ON k.sel = s.method_id GROUP BY s.a;
CREATE TEMP TABLE rx AS
SELECT t."to" AS a, max_by(hx(t.hash), t.block_number * 1000000 + t.tx_index, 2) AS hashes
FROM src_tx t SEMI JOIN top ON top.address = t."to" WHERE t."from" <> t."to" GROUP BY 1;

CREATE TEMP TABLE packs AS
WITH base AS (
SELECT t.address, t.score,
  {'chain': '@CHAIN@', 'address': hx(t.address),
   'is_contract': t.is_contract, 'emits_logs': t.logs_emitted > 0,
   'creator': CASE WHEN t.creator IS NOT NULL THEN hx(t.creator) END, 'created_block': t.contract_creation_block,
   'creates_contracts': t.contracts_created,
   'first_time': epoch(t.first_time)::BIGINT, 'last_time': epoch(t.last_time)::BIGINT,
   'first_block': t.first_block, 'last_block': t.last_block,
   'tx_sent': t.tx_sent, 'tx_sent_failed': t.tx_sent_failed, 'tx_received': t.tx_received,
   'token_transfers_out': t.token_transfers_out, 'token_transfers_in': t.token_transfers_in,
   'logs_emitted': t.logs_emitted, 'dex_trades': t.dex_trades,
   'distinct_counterparties': t.distinct_counterparties, 'out_degree': t.out_degree, 'in_degree': t.in_degree,
   'fan_in_out_ratio': round(t.in_degree::DOUBLE / greatest(t.out_degree, 1), 3),
   'fanin_ratio': round(t.in_degree::DOUBLE / greatest(t.distinct_counterparties, 1), 4),
   'fanout_ratio': round(t.out_degree::DOUBLE / greatest(t.distinct_counterparties, 1), 4),
   'native_in': round(t.native_value_received::DOUBLE / 1e18, 6), 'native_out': round(t.native_value_sent::DOUBLE / 1e18, 6),
   'outflow_concentration': round(co.top2 / greatest(co.total, 1), 4)
  } AS head,
  coalesce(co.l, []) AS outl, coalesce(ci.l, []) AS inl, coalesce(k.l, []) AS tkl, coalesce(v.l, []) AS evl,
  coalesce(se.l, []) AS sell, list_distinct(coalesce(se.hashes, []) || coalesce(r.hashes, [])) AS txl
FROM top t
LEFT JOIN cp_l co ON co.a = t.address AND co.dir = 'o' LEFT JOIN cp_l ci ON ci.a = t.address AND ci.dir = 'i'
LEFT JOIN tk k ON k.a = t.address LEFT JOIN ev v ON v.a = t.address
LEFT JOIN sel se ON se.a = t.address LEFT JOIN rx r ON r.a = t.address),
tiers AS (SELECT * FROM (VALUES (1, 5, 10, 8, 6, 3), (2, 4, 6, 5, 4, 2), (3, 3, 4, 3, 3, 1), (4, 2, 3, 2, 2, 0))
          t(tier, ncp, nsel, ntop, ntok, ntx)),
p AS (
SELECT b.address, b.score, x.tier,
  json_merge_patch(to_json(b.head), json_object(
     'top_out_counterparties', b.outl[1:x.ncp], 'top_in_counterparties', b.inl[1:x.ncp],
     'unique_function_selectors_called', b.sell[1:x.nsel],
     'topic0_hist', b.evl[1:x.ntop], 'top_tokens', b.tkl[1:x.ntok], 'sample_txs', b.txl[1:x.ntx])) AS pk
FROM base b CROSS JOIN tiers x)
SELECT address, score, arg_min(pk, tier) FILTER (WHERE strlen(pk::VARCHAR) < 3000) AS pack,
       min(tier) FILTER (WHERE strlen(pk::VARCHAR) < 3000) AS tier
FROM p GROUP BY address, score;

COPY (SELECT address, score, pack::VARCHAR AS pack, strlen(pack::VARCHAR) AS bytes FROM packs ORDER BY score DESC)
TO '@MAP@/feature_packs/packs_top@N@.parquet' (FORMAT parquet, COMPRESSION zstd);
SELECT count(*) AS packs, max(strlen(pack::VARCHAR)) AS max_bytes, avg(strlen(pack::VARCHAR))::INT AS avg_bytes,
       count(*) FILTER (WHERE pack IS NULL OR strlen(pack::VARCHAR) >= 3000) AS over_3k,
       count(*) FILTER (WHERE tier = 1) AS full_tier, count(*) FILTER (WHERE tier > 1) AS trimmed FROM packs;
