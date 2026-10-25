-- Stage 2: discovery dimensions (protocol instances found from data, merged with the official registry).
-- Requires: view logs_sel (+ ev_* views from gen_events.sql) over ${OUT}/stage/logs_sel.parquet.
CREATE OR REPLACE TABLE reg_contracts AS
  SELECT lower(address) AS address, protocol, family, role, source FROM read_csv('${SRC}/contracts.csv', header=true, all_varchar=true)
  WHERE chain = '${CHAIN}';

-- DEX pools (v2 pairs, v3/algebra pools) from factory creation events
CREATE OR REPLACE TABLE dim_pools AS
WITH p AS (
  SELECT pair AS pool, token0, token1, NULL::INTEGER AS fee, contract AS factory, 'v2' AS kind, block_number FROM ev_univ2__PairCreated
  UNION ALL SELECT pool, token0, token1, fee::INTEGER, contract, 'v3', block_number FROM ev_univ3__PoolCreated
  UNION ALL SELECT pool, token0, token1, NULL, contract, 'algebra', block_number FROM ev_algebra__Pool
)
SELECT p.*, coalesce(r.protocol, CASE kind WHEN 'v2' THEN 'univ2_fork' WHEN 'v3' THEN 'univ3_fork' ELSE 'algebra_fork' END || ':' || substr(factory, 1, 10)) AS protocol
FROM p LEFT JOIN reg_contracts r ON r.address = p.factory
QUALIFY row_number() OVER (PARTITION BY pool ORDER BY block_number) = 1;

CREATE OR REPLACE TABLE dim_v4_pools AS
SELECT e.id AS pool_id, e.currency0 AS token0, e.currency1 AS token1, e.fee::INTEGER AS fee, e.tickSpacing::INTEGER AS tick_spacing,
       e.hooks, e.contract AS manager, coalesce(r.protocol, 'univ4_fork:' || substr(e.contract, 1, 10)) AS protocol, e.block_number
FROM ev_univ4__Initialize e LEFT JOIN reg_contracts r ON r.address = e.contract
QUALIFY row_number() OVER (PARTITION BY e.contract, e.id ORDER BY e.block_number) = 1;

-- Aave v3 / v2 style lending pools: emitters of Supply (v3) / Deposit (v2)
CREATE OR REPLACE TABLE dim_aave_pools AS
SELECT contract AS pool, fam, coalesce(any_value(r.protocol), fam || '_fork:' || substr(contract, 1, 10)) AS protocol, count(*) n
FROM (SELECT contract, 'aave_v3' fam FROM ev_aave_v3__Supply UNION ALL SELECT contract, 'aave_v2' FROM ev_aave_v2__Deposit
      UNION ALL SELECT address, family FROM reg_contracts WHERE family IN ('aave_v3','aave_v2') AND role IN ('pool','lending_pool','lending_pool_alt')) x
LEFT JOIN reg_contracts r ON r.address = x.contract
GROUP BY contract, fam;

-- Aave aTokens / debt tokens: (token -> underlying, pool, kind) from token Initialized + configurator ReserveInitialized
CREATE OR REPLACE TABLE dim_aave_tokens AS
WITH t AS (
  SELECT contract AS token, underlyingAsset AS underlying, pool, 'atoken' AS kind, block_number FROM ev_aave_v3_token__ATokenInitialized
  UNION ALL SELECT contract, underlyingAsset, pool, 'vdebt', block_number FROM ev_aave_v3_token__DebtTokenInitialized
  UNION ALL SELECT aToken, asset, NULL, 'atoken', block_number FROM ev_aave_v3__ReserveInitialized
  UNION ALL SELECT variableDebtToken, asset, NULL, 'vdebt', block_number FROM ev_aave_v3__ReserveInitialized
)
SELECT token, any_value(underlying) AS underlying, max(pool) AS pool, any_value(kind) AS kind, min(block_number) AS block_number
FROM t WHERE token <> '0x0000000000000000000000000000000000000000' GROUP BY token;

-- Compound v3 Comets: registry + emitters of Comet-only events
CREATE OR REPLACE TABLE dim_comets AS
SELECT c AS comet, coalesce(any_value(r.protocol), 'comet_fork:' || substr(c, 1, 10)) AS protocol,
       any_value(CASE WHEN r.source LIKE '%' THEN NULL END) AS dummy
FROM (SELECT contract c FROM ev_comet__SupplyCollateral UNION SELECT contract FROM ev_comet__AbsorbDebt
      UNION SELECT contract FROM ev_comet__WithdrawCollateral UNION SELECT address FROM reg_contracts WHERE family='comet' AND role LIKE 'comet:%') x
LEFT JOIN reg_contracts r ON r.address = x.c GROUP BY c;

-- Compound v2 cTokens: emitters of AccrueInterest(4 x uint) that also emit 1-topic Mint/Redeem/Borrow
CREATE OR REPLACE TABLE dim_ctokens AS
SELECT contract AS ctoken, 'compound_v2_fork:' || substr(contract, 1, 10) AS protocol, count(*) n
FROM (SELECT contract FROM ev_compound_v2__AccrueInterest) a
WHERE contract IN (SELECT contract FROM ev_compound_v2__Mint UNION SELECT contract FROM ev_compound_v2__Borrow UNION SELECT contract FROM ev_compound_v2__Redeem)
GROUP BY contract;

-- Morpho Blue markets
CREATE OR REPLACE TABLE dim_morpho_markets AS
SELECT e.contract AS morpho, e.id, e.loanToken AS loan_token, e.collateralToken AS collateral_token, e.oracle, e.irm, e.lltv,
       coalesce(r.protocol, 'morpho_blue_fork:' || substr(e.contract, 1, 10)) AS protocol, e.block_number
FROM ev_morpho_blue__CreateMarket e LEFT JOIN reg_contracts r ON r.address = e.contract;

-- position managers (NFT LP): emitters of IncreaseLiquidity
CREATE OR REPLACE TABLE dim_npm AS
SELECT contract AS npm, kind, coalesce(any_value(r.protocol), kind || '_npm:' || substr(contract, 1, 10)) AS protocol, count(*) n FROM (
  SELECT contract, 'univ3' kind FROM ev_univ3_npm__IncreaseLiquidity UNION ALL SELECT contract, 'algebra' FROM ev_algebra_npm__IncreaseLiquidity) x
LEFT JOIN reg_contracts r ON r.address = x.contract GROUP BY contract, kind;

-- NPM tokenId -> pool/ticks: pool Mint (owner = npm) in the same tx with identical amounts (univ3); algebra NPM event carries pool
CREATE OR REPLACE TABLE dim_npm_tokens AS
WITH inc AS (SELECT i.*, n.kind FROM ev_univ3_npm__IncreaseLiquidity i JOIN dim_npm n ON n.npm = i.contract),
m AS (SELECT * FROM ev_univ3__Mint WHERE owner IN (SELECT npm FROM dim_npm)),
u AS (
  SELECT inc.contract AS npm, inc.tokenId AS token_id, m.contract AS pool, m.tickLower::INTEGER AS tick_lower, m.tickUpper::INTEGER AS tick_upper, inc.block_number
  FROM inc JOIN m ON m.block_number = inc.block_number AND m.tx_index = inc.tx_index AND m.owner = inc.contract
       AND m.amount0 = inc.amount0 AND m.amount1 = inc.amount1 AND m.log_index < inc.log_index
  UNION ALL
  SELECT a.contract, a.tokenId, a.pool, NULL, NULL, a.block_number FROM ev_algebra_npm__IncreaseLiquidity a
)
SELECT u.npm, u.token_id, u.pool, u.tick_lower, u.tick_upper, p.token0, p.token1, p.protocol
FROM u LEFT JOIN dim_pools p ON p.pool = u.pool
QUALIFY row_number() OVER (PARTITION BY u.npm, u.token_id ORDER BY u.block_number) = 1;

-- ERC-4626 vaults (staking / LSD / yield wrappers)
CREATE OR REPLACE TABLE dim_vaults AS
SELECT contract AS vault, count(*) n FROM (SELECT contract FROM ev_erc4626__Deposit UNION ALL SELECT contract FROM ev_erc4626__Withdraw) GROUP BY 1;

-- Silo v1 silos: emitters of silo Deposit
CREATE OR REPLACE TABLE dim_silos AS SELECT DISTINCT contract AS silo FROM ev_silo_v1__Deposit;

-- OFT contracts (token resolved via token() eth_call in meta step; native OFTs are the token)
CREATE OR REPLACE TABLE dim_oft AS
SELECT contract AS oft, coalesce(any_value(r.protocol), 'oft') AS protocol, count(*) n
FROM (SELECT contract FROM ev_oft__OFTSent UNION ALL SELECT contract FROM ev_oft__OFTReceived) x
LEFT JOIN reg_contracts r ON r.address = x.contract GROUP BY contract;

-- WETH-like tokens (wrap/unwrap): registry + emitters of BOTH 2-topic Deposit & Withdrawal
CREATE OR REPLACE TABLE dim_weth AS
SELECT address AS token, 'registry' src FROM reg_contracts WHERE family = 'weth'
UNION
SELECT contract, 'events' FROM ev_weth__Deposit WHERE contract IN (SELECT contract FROM ev_weth__Withdrawal) GROUP BY contract HAVING count(*) > 0;

-- Chainlink-style aggregators (description resolved via eth_call)
CREATE OR REPLACE TABLE dim_aggregators AS
SELECT contract AS aggregator, count(*) n, max(block_number) last_block FROM ev_chainlink__AnswerUpdated GROUP BY 1;

COPY (SELECT 'pools' dim, count(*) n FROM dim_pools UNION ALL SELECT 'v4_pools', count(*) FROM dim_v4_pools
  UNION ALL SELECT 'aave_pools', count(*) FROM dim_aave_pools UNION ALL SELECT 'aave_tokens', count(*) FROM dim_aave_tokens
  UNION ALL SELECT 'comets', count(*) FROM dim_comets UNION ALL SELECT 'ctokens', count(*) FROM dim_ctokens
  UNION ALL SELECT 'morpho_markets', count(*) FROM dim_morpho_markets UNION ALL SELECT 'npm', count(*) FROM dim_npm
  UNION ALL SELECT 'npm_tokens', count(*) FROM dim_npm_tokens UNION ALL SELECT 'vaults', count(*) FROM dim_vaults
  UNION ALL SELECT 'oft', count(*) FROM dim_oft UNION ALL SELECT 'weth', count(*) FROM dim_weth UNION ALL SELECT 'aggregators', count(*) FROM dim_aggregators
) TO '${OUT}/dims_summary.csv' (HEADER);
