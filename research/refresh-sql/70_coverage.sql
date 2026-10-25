-- Stage 9: decode coverage. A (contract, topic0) pair counts as decoded when the topic0 is a registry event
-- AND the emitter belongs to the discovered instance set that the decoder accepts (e.g. comet.Supply only on Comets).
CREATE OR REPLACE TABLE topic_dict AS SELECT lower(t0) AS t0, any_value(signature) AS signature FROM read_csv('${SRC}/topic_dict.csv', header=true, all_varchar=true) GROUP BY 1;
CREATE OR REPLACE TABLE event_defs2 AS SELECT key, family, event, lower(topic0) AS t0, topic_count::INTEGER AS tc FROM read_csv('${SRC}/events.csv', header=true, all_varchar=true);
CREATE OR REPLACE TABLE hist AS
SELECT '0x' || lower(hex(address)) AS address, '0x' || lower(hex(topic0)) AS t0, topic_count::INTEGER AS tc, n, first_block, last_block
FROM read_parquet('${OUT}/contract_topic_hist.parquet');

CREATE OR REPLACE TABLE emitter_sets AS
SELECT 'comet' AS fam, comet AS address FROM dim_comets
UNION ALL SELECT 'compound_v2', ctoken FROM dim_ctokens
UNION ALL SELECT 'aave_pool', pool FROM dim_aave_pools
UNION ALL SELECT 'aave_token', token FROM dim_aave_tokens
UNION ALL SELECT 'v2pair', pool FROM dim_pools WHERE kind = 'v2'
UNION ALL SELECT 'v3pool', pool FROM dim_pools WHERE kind <> 'v2'
UNION ALL SELECT 'npm', npm FROM dim_npm
UNION ALL SELECT 'morpho', DISTINCT_m FROM (SELECT DISTINCT morpho AS DISTINCT_m FROM dim_morpho_markets)
UNION ALL SELECT 'silo', silo FROM dim_silos
UNION ALL SELECT 'weth', token FROM dim_weth
UNION ALL SELECT 'gmx', address FROM reg_contracts WHERE protocol LIKE 'gmx%';

CREATE OR REPLACE TABLE hist_class AS
SELECT h.*, td.signature, e.key AS event_key,
  CASE
    WHEN h.t0 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef' AND h.tc IN (3, 4) THEN 'decoded_transfer'
    WHEN h.t0 IN ('0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62', '0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb') THEN 'decoded_transfer'
    WHEN e.key IS NULL THEN NULL
    WHEN e.family = 'comet' THEN CASE WHEN h.address IN (SELECT address FROM emitter_sets WHERE fam = 'comet') THEN 'decoded_econ' END
    WHEN e.family = 'compound_v2' AND e.event <> 'MarketListed' THEN CASE WHEN h.address IN (SELECT address FROM emitter_sets WHERE fam = 'compound_v2') THEN 'decoded_econ' END
    WHEN e.family IN ('aave_v3', 'aave_v2') AND e.event NOT IN ('ReserveInitialized') THEN CASE WHEN h.address IN (SELECT address FROM emitter_sets WHERE fam = 'aave_pool') THEN 'decoded_econ' END
    WHEN e.family = 'aave_v3_token' AND e.event NOT LIKE '%Initialized' THEN CASE WHEN h.address IN (SELECT address FROM emitter_sets WHERE fam = 'aave_token') THEN 'decoded_econ' END
    WHEN e.family = 'univ2' AND e.event <> 'PairCreated' THEN CASE WHEN h.address IN (SELECT address FROM emitter_sets WHERE fam = 'v2pair') THEN 'decoded_econ' END
    WHEN e.family IN ('univ3', 'pancake_v3') AND e.event <> 'PoolCreated' THEN CASE WHEN h.address IN (SELECT address FROM emitter_sets WHERE fam IN ('v3pool')) THEN 'decoded_econ' END
    WHEN e.family = 'univ3_npm' THEN CASE WHEN h.address IN (SELECT address FROM emitter_sets WHERE fam = 'npm') THEN 'decoded_econ' END
    WHEN e.family = 'morpho_blue' AND e.event <> 'CreateMarket' THEN CASE WHEN h.address IN (SELECT address FROM emitter_sets WHERE fam = 'morpho') THEN 'decoded_econ' END
    WHEN e.family = 'silo_v1' THEN CASE WHEN h.address IN (SELECT address FROM emitter_sets WHERE fam = 'silo') THEN 'decoded_econ' END
    WHEN e.family = 'weth' THEN CASE WHEN h.address IN (SELECT address FROM emitter_sets WHERE fam = 'weth') THEN 'decoded_econ' END
    WHEN e.family = 'gmx_v1' AND e.event = 'Swap' THEN CASE WHEN h.address IN (SELECT address FROM emitter_sets WHERE fam = 'gmx') THEN 'decoded_econ' END
    WHEN e.family = 'robinhood_stock' AND e.event = 'TransferWithScaledUI' THEN 'decoded_transfer'   -- mirrors the paired Transfer
    WHEN e.family = 'relay' AND e.event = 'SolverCallExecuted' THEN NULL
    ELSE 'decoded_econ'
  END AS cls0,
  CASE WHEN h.t0 IN ('0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925', '0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31',
                     '0x8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e0', '0x7f26b83ff96e1f2b6a682f133852f6798a09c465da95921460cefb3847402498',
                     '0xc7f505b2f371ae2175ee4913f4499e1f2633a7b5936321eed1cdaeb6115181d2', '0xbc7cd75a20ee27fd9adebab32041f755214dbc6bffa90cc0225b39da2e5c2d3b',
                     '0x1cf3b03a6cf19fa2baba4df148e9dcabedea7f8a5c07840e207e5c089be95d3e', '0x7e644d79422f17c01e4894b5f4f588d331ebfa28653d42ae832dc59e38c9798f')
       THEN true ELSE false END AS non_economic   -- Approval, ApprovalForAll, OwnershipTransferred, Initialized, Upgraded, BeaconUpgraded, AdminChanged
FROM hist h LEFT JOIN topic_dict td ON td.t0 = h.t0
LEFT JOIN event_defs2 e ON e.t0 = h.t0 AND e.tc = h.tc;

CREATE OR REPLACE TABLE hist_class2 AS
SELECT *, coalesce(cls0, CASE WHEN non_economic THEN 'non_economic' WHEN signature IS NOT NULL THEN 'known_sig_undecoded' ELSE 'unknown_sig' END) AS cls
FROM hist_class;

CREATE OR REPLACE TABLE coverage_chain AS
SELECT ${CHAIN} AS chain, sum(n) AS logs,
  sum(n) FILTER (WHERE cls = 'decoded_econ') AS decoded_econ, sum(n) FILTER (WHERE cls = 'decoded_transfer') AS decoded_transfer,
  sum(n) FILTER (WHERE cls = 'non_economic') AS non_economic, sum(n) FILTER (WHERE cls = 'known_sig_undecoded') AS known_sig_undecoded,
  sum(n) FILTER (WHERE cls = 'unknown_sig') AS unknown_sig,
  round(100.0 * sum(n) FILTER (WHERE cls IN ('decoded_econ', 'decoded_transfer')) / sum(n), 2) AS pct_decoded,
  round(100.0 * sum(n) FILTER (WHERE cls IN ('decoded_econ', 'decoded_transfer', 'non_economic')) / sum(n), 2) AS pct_decoded_or_non_economic
FROM hist_class2;

CREATE OR REPLACE TABLE coverage_top_contracts AS
WITH c AS (
  SELECT address, sum(n) AS logs, sum(n) FILTER (WHERE cls IN ('decoded_econ', 'decoded_transfer')) AS decoded,
         sum(n) FILTER (WHERE cls = 'non_economic') AS non_econ, count(*) AS n_topics, min(first_block) AS first_block, max(last_block) AS last_block
  FROM hist_class2 GROUP BY 1
)
SELECT row_number() OVER (ORDER BY logs DESC) AS rank, c.*, round(100.0 * coalesce(decoded, 0) / logs, 1) AS pct_decoded,
       round(100.0 * (coalesce(decoded, 0) + coalesce(non_econ, 0)) / logs, 1) AS pct_decoded_or_non_econ,
       tm.symbol, tm.name, rc.protocol AS registry_protocol
FROM c LEFT JOIN token_meta tm ON tm.token = c.address
LEFT JOIN (SELECT address, any_value(protocol) protocol FROM reg_contracts GROUP BY 1) rc ON rc.address = c.address
ORDER BY logs DESC LIMIT 200;

-- undecoded high-volume contracts with topic0 histograms (input for LLM protocol-discovery agents)
CREATE OR REPLACE TABLE undecoded_contracts AS
WITH cand AS (
  SELECT address, sum(n) AS logs, sum(n) FILTER (WHERE cls IN ('known_sig_undecoded', 'unknown_sig')) AS undecoded_logs
  FROM hist_class2 GROUP BY 1 HAVING sum(n) FILTER (WHERE cls IN ('known_sig_undecoded', 'unknown_sig')) > 0
  ORDER BY undecoded_logs DESC LIMIT 300
), topics AS (
  SELECT h.address, list({'topic0': h.t0, 'topic_count': h.tc, 'signature': h.signature, 'n': h.n, 'class': h.cls} ORDER BY h.n DESC)[1:12] AS topic_hist
  FROM hist_class2 h JOIN cand USING (address) GROUP BY 1
), cr AS (
  SELECT a2s(address) AS address, any_value(a2s(creator)) AS creator, min(block_number) AS created_block FROM contract_creations
  WHERE a2s(address) IN (SELECT address FROM cand) GROUP BY 1
)
SELECT ${CHAIN} AS chain, cand.address, cand.logs, cand.undecoded_logs, round(100.0 * cand.undecoded_logs / cand.logs, 1) AS pct_undecoded,
       tm.symbol, tm.name, cr.creator, cr.created_block, t.topic_hist
FROM cand JOIN topics t USING (address) LEFT JOIN token_meta tm ON tm.token = cand.address LEFT JOIN cr USING (address)
ORDER BY cand.undecoded_logs DESC;

-- econ_actions inventory
CREATE OR REPLACE VIEW econ_actions AS SELECT * FROM read_parquet('${OUT}/econ_actions/*.parquet');
CREATE OR REPLACE TABLE action_inventory AS
SELECT chain, protocol, action, count(*) AS n, count(DISTINCT wallet) AS wallets, min(block_time) AS first_time, max(block_time) AS last_time
FROM econ_actions GROUP BY ALL ORDER BY n DESC;

COPY coverage_chain TO '${OUT}/coverage_chain.csv' (HEADER);
COPY coverage_top_contracts TO '${OUT}/coverage_top200.csv' (HEADER);
COPY undecoded_contracts TO '${OUT}/undecoded_contracts.json' (FORMAT json, ARRAY true);
COPY action_inventory TO '${OUT}/action_inventory.csv' (HEADER);
