-- Stage 4: econ_actions for one block partition [${PLO}, ${PHI}].
-- logs_sel / swaps_sel views are (re)bound to this partition by run.py before this file runs.
-- Each family SELECT yields: block_number, tx_index, log_index, sub, contract, family, action, wallet_ev, asset,
--   amount_raw (HUGEINT, token base units), counterparty, extra (JSON text),
--   position effect: pos_kind, pos_sign (+1/-1), pos_abs (absolute post-event amount when the event carries it), pos_key, pos_shares, pos_liq
CREATE OR REPLACE MACRO b2a(x) AS ('0x' || substr(x, 27, 40));
CREATE OR REPLACE MACRO hs(v) AS CAST(v AS VARCHAR);
CREATE OR REPLACE TEMP TABLE npm_set AS SELECT npm FROM dim_npm;

-- ======================= Aave v4 dims (persistent; filled per partition, AddReserve/AddAsset precede use) =======================
CREATE TABLE IF NOT EXISTS aave_v4_reserves (spoke VARCHAR, reserve_id HUGEINT, asset_id HUGEINT, hub VARCHAR, block_number UBIGINT);
CREATE TABLE IF NOT EXISTS aave_v4_assets (hub VARCHAR, asset_id HUGEINT, underlying VARCHAR, decimals INTEGER, block_number UBIGINT);
INSERT INTO aave_v4_reserves SELECT e.contract, e.reserveId, e.assetId, e.hub, e.block_number FROM ev_aave_v4__AddReserve e
  WHERE NOT EXISTS (SELECT 1 FROM aave_v4_reserves r WHERE r.spoke = e.contract AND r.reserve_id = e.reserveId);
INSERT INTO aave_v4_assets SELECT e.contract, e.assetId, e.underlying, e.decimals::INTEGER, e.block_number FROM ev_aave_v4_hub__AddAsset e
  WHERE NOT EXISTS (SELECT 1 FROM aave_v4_assets a WHERE a.hub = e.contract AND a.asset_id = e.assetId);

CREATE OR REPLACE TEMP TABLE acts AS
-- ======================= Aave v3 / v2 pools =======================
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, p.fam AS family, 'supply' AS action, e.onBehalfOf AS wallet_ev, e.reserve AS asset,
       e.amount AS amount_raw, e.contract AS counterparty, json_object('caller', e."user", 'referral', e.referralCode)::VARCHAR AS extra,
       'supplied' AS pos_kind, 1 AS pos_sign, NULL::HUGEINT AS pos_abs, NULL AS pos_key, NULL::HUGEINT AS pos_shares, NULL::HUGEINT AS pos_liq
FROM ev_aave_v3__Supply e JOIN dim_aave_pools p ON p.pool = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, p.fam AS family, 'supply' AS action, e.onBehalfOf AS wallet_ev, e.reserve AS asset,
       e.amount AS amount_raw, e.contract AS counterparty, json_object('caller', e."user")::VARCHAR AS extra, 'supplied' AS pos_kind, 1 AS pos_sign
FROM ev_aave_v2__Deposit e JOIN dim_aave_pools p ON p.pool = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, p.fam AS family, 'withdraw' AS action, e."user" AS wallet_ev, e.reserve AS asset,
       e.amount AS amount_raw, e.contract AS counterparty, json_object('to', e."to")::VARCHAR AS extra, 'supplied' AS pos_kind, -1 AS pos_sign
FROM ev_aave_v3__Withdraw e JOIN dim_aave_pools p ON p.pool = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, p.fam AS family, 'borrow' AS action, e.onBehalfOf AS wallet_ev, e.reserve AS asset, e.amount AS amount_raw, e.contract AS counterparty,
       json_object('caller', e."user", 'rate_mode', e.interestRateMode, 'borrow_rate_ray', hs(e.borrowRate))::VARCHAR AS extra, 'debt' AS pos_kind, 1 AS pos_sign
FROM ev_aave_v3__Borrow e JOIN dim_aave_pools p ON p.pool = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, p.fam AS family, 'borrow' AS action, e.onBehalfOf AS wallet_ev, e.reserve AS asset, e.amount AS amount_raw, e.contract AS counterparty,
       json_object('caller', e."user", 'rate_mode', hs(e.borrowRateMode))::VARCHAR AS extra, 'debt' AS pos_kind, 1 AS pos_sign
FROM ev_aave_v2__Borrow e JOIN dim_aave_pools p ON p.pool = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, p.fam AS family, 'repay' AS action, e."user" AS wallet_ev, e.reserve AS asset, e.amount AS amount_raw, e.contract AS counterparty,
       json_object('repayer', e.repayer, 'use_atokens', e.useATokens)::VARCHAR AS extra, 'debt' AS pos_kind, -1 AS pos_sign
FROM ev_aave_v3__Repay e JOIN dim_aave_pools p ON p.pool = e.contract
UNION ALL BY NAME   -- repayWithATokens also burns the user's aTokens
SELECT e.block_number, e.tx_index, e.log_index, 1 AS sub, e.contract, p.fam AS family, 'repay' AS action, e."user" AS wallet_ev, e.reserve AS asset, e.amount AS amount_raw, e.contract AS counterparty,
       json_object('use_atokens', true)::VARCHAR AS extra, 'supplied' AS pos_kind, -1 AS pos_sign
FROM ev_aave_v3__Repay e JOIN dim_aave_pools p ON p.pool = e.contract WHERE e.useATokens
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, p.fam AS family, 'repay' AS action, e."user" AS wallet_ev, e.reserve AS asset, e.amount AS amount_raw, e.contract AS counterparty,
       json_object('repayer', e.repayer)::VARCHAR AS extra, 'debt' AS pos_kind, -1 AS pos_sign
FROM ev_aave_v2__Repay e JOIN dim_aave_pools p ON p.pool = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, p.fam AS family, 'liquidated' AS action, e."user" AS wallet_ev, e.collateralAsset AS asset,
       e.liquidatedCollateralAmount AS amount_raw, e.contract AS counterparty,
       json_object('liquidator', e.liquidator, 'debt_asset', e.debtAsset, 'debt_to_cover', hs(e.debtToCover), 'receive_atoken', e.receiveAToken)::VARCHAR AS extra,
       'supplied' AS pos_kind, -1 AS pos_sign
FROM ev_aave_v3__LiquidationCall e JOIN dim_aave_pools p ON p.pool = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 1 AS sub, e.contract, p.fam AS family, 'liquidated' AS action, e."user" AS wallet_ev, e.debtAsset AS asset,
       e.debtToCover AS amount_raw, e.contract AS counterparty, json_object('liquidator', e.liquidator, 'leg', 'debt')::VARCHAR AS extra, 'debt' AS pos_kind, -1 AS pos_sign
FROM ev_aave_v3__LiquidationCall e JOIN dim_aave_pools p ON p.pool = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 2 AS sub, e.contract, p.fam AS family, 'liquidate' AS action, e.liquidator AS wallet_ev, e.debtAsset AS asset,
       e.debtToCover AS amount_raw, e.contract AS counterparty,
       json_object('borrower', e."user", 'collateral_asset', e.collateralAsset, 'collateral_seized', hs(e.liquidatedCollateralAmount))::VARCHAR AS extra
FROM ev_aave_v3__LiquidationCall e JOIN dim_aave_pools p ON p.pool = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, p.fam AS family, 'flashloan' AS action, e.initiator AS wallet_ev, e.asset, e.amount AS amount_raw, e.contract AS counterparty,
       json_object('target', e.target, 'premium', hs(e.premium), 'rate_mode', e.interestRateMode)::VARCHAR AS extra
FROM ev_aave_v3__FlashLoan e JOIN dim_aave_pools p ON p.pool = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, p.fam AS family, 'flashloan' AS action, e.initiator AS wallet_ev, e.asset, e.amount AS amount_raw, e.contract AS counterparty,
       json_object('target', e.target, 'premium', hs(e.premium))::VARCHAR AS extra
FROM ev_aave_v2__FlashLoan e JOIN dim_aave_pools p ON p.pool = e.contract
-- ======================= Aave v4 (spokes; asset = hub underlying of the reserve) =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'aave_v4' AS family, 'supply' AS action, e."user" AS wallet_ev, a.underlying AS asset,
       e.suppliedAmount AS amount_raw, e.contract AS counterparty, json_object('caller', e.caller, 'reserve_id', hs(e.reserveId), 'shares', hs(e.suppliedShares))::VARCHAR AS extra,
       'supplied' AS pos_kind, 1 AS pos_sign, e.contract || ':' || hs(e.reserveId) AS pos_key, e.suppliedShares AS pos_shares
FROM ev_aave_v4__Supply e JOIN aave_v4_reserves r ON r.spoke = e.contract AND r.reserve_id = e.reserveId JOIN aave_v4_assets a ON a.hub = r.hub AND a.asset_id = r.asset_id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'aave_v4' AS family, 'withdraw' AS action, e."user" AS wallet_ev, a.underlying AS asset,
       e.withdrawnAmount AS amount_raw, e.contract AS counterparty, json_object('caller', e.caller, 'reserve_id', hs(e.reserveId), 'shares', hs(e.withdrawnShares))::VARCHAR AS extra,
       'supplied' AS pos_kind, -1 AS pos_sign, e.contract || ':' || hs(e.reserveId) AS pos_key, -e.withdrawnShares AS pos_shares
FROM ev_aave_v4__Withdraw e JOIN aave_v4_reserves r ON r.spoke = e.contract AND r.reserve_id = e.reserveId JOIN aave_v4_assets a ON a.hub = r.hub AND a.asset_id = r.asset_id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'aave_v4' AS family, 'borrow' AS action, e."user" AS wallet_ev, a.underlying AS asset,
       e.drawnAmount AS amount_raw, e.contract AS counterparty, json_object('caller', e.caller, 'reserve_id', hs(e.reserveId), 'drawn_shares', hs(e.drawnShares))::VARCHAR AS extra,
       'debt' AS pos_kind, 1 AS pos_sign, e.contract || ':' || hs(e.reserveId) AS pos_key, e.drawnShares AS pos_shares
FROM ev_aave_v4__Borrow e JOIN aave_v4_reserves r ON r.spoke = e.contract AND r.reserve_id = e.reserveId JOIN aave_v4_assets a ON a.hub = r.hub AND a.asset_id = r.asset_id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'aave_v4' AS family, 'repay' AS action, e."user" AS wallet_ev, a.underlying AS asset,
       e.totalAmountRepaid AS amount_raw, e.contract AS counterparty, json_object('caller', e.caller, 'reserve_id', hs(e.reserveId), 'drawn_shares', hs(e.drawnShares))::VARCHAR AS extra,
       'debt' AS pos_kind, -1 AS pos_sign, e.contract || ':' || hs(e.reserveId) AS pos_key, -e.drawnShares AS pos_shares
FROM ev_aave_v4__Repay e JOIN aave_v4_reserves r ON r.spoke = e.contract AND r.reserve_id = e.reserveId JOIN aave_v4_assets a ON a.hub = r.hub AND a.asset_id = r.asset_id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'aave_v4' AS family, 'liquidated' AS action, e."user" AS wallet_ev, a.underlying AS asset,
       e.collateralAmountRemoved AS amount_raw, e.contract AS counterparty,
       json_object('liquidator', e.liquidator, 'leg', 'collateral', 'reserve_id', hs(e.collateralReserveId))::VARCHAR AS extra,
       'supplied' AS pos_kind, -1 AS pos_sign, e.contract || ':' || hs(e.collateralReserveId) AS pos_key, -e.collateralSharesLiquidated AS pos_shares
FROM ev_aave_v4__LiquidationCall e JOIN aave_v4_reserves r ON r.spoke = e.contract AND r.reserve_id = e.collateralReserveId JOIN aave_v4_assets a ON a.hub = r.hub AND a.asset_id = r.asset_id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 1 AS sub, e.contract, 'aave_v4' AS family, 'liquidated' AS action, e."user" AS wallet_ev, a.underlying AS asset,
       e.debtAmountRestored AS amount_raw, e.contract AS counterparty,
       json_object('liquidator', e.liquidator, 'leg', 'debt', 'reserve_id', hs(e.debtReserveId))::VARCHAR AS extra,
       'debt' AS pos_kind, -1 AS pos_sign, e.contract || ':' || hs(e.debtReserveId) AS pos_key, -e.drawnSharesLiquidated AS pos_shares
FROM ev_aave_v4__LiquidationCall e JOIN aave_v4_reserves r ON r.spoke = e.contract AND r.reserve_id = e.debtReserveId JOIN aave_v4_assets a ON a.hub = r.hub AND a.asset_id = r.asset_id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 2 AS sub, e.contract, 'aave_v4' AS family, 'liquidate' AS action, e.liquidator AS wallet_ev, a.underlying AS asset,
       e.debtAmountRestored AS amount_raw, e.contract AS counterparty,
       json_object('borrower', e."user", 'collateral_reserve_id', hs(e.collateralReserveId), 'collateral_removed', hs(e.collateralAmountRemoved))::VARCHAR AS extra
FROM ev_aave_v4__LiquidationCall e JOIN aave_v4_reserves r ON r.spoke = e.contract AND r.reserve_id = e.debtReserveId JOIN aave_v4_assets a ON a.hub = r.hub AND a.asset_id = r.asset_id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 3 AS sub, e.contract, 'aave_v4' AS family, 'liquidation_shares' AS action, e.liquidator AS wallet_ev, a.underlying AS asset,
       NULL::HUGEINT AS amount_raw, e.contract AS counterparty, json_object('borrower', e."user", 'shares', hs(e.collateralSharesToLiquidator))::VARCHAR AS extra,
       'supplied' AS pos_kind, 1 AS pos_sign, e.contract || ':' || hs(e.collateralReserveId) AS pos_key, e.collateralSharesToLiquidator AS pos_shares
FROM ev_aave_v4__LiquidationCall e JOIN aave_v4_reserves r ON r.spoke = e.contract AND r.reserve_id = e.collateralReserveId JOIN aave_v4_assets a ON a.hub = r.hub AND a.asset_id = r.asset_id WHERE e.receiveShares
-- ======================= Compound v3 (Comet) =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'comet' AS family, 'supply' AS action, e.dst AS wallet_ev, c.base_token AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('from', e."from")::VARCHAR AS extra, 'comet_base' AS pos_kind, 1 AS pos_sign
FROM ev_comet__Supply e JOIN dim_comets c ON c.comet = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'comet' AS family, 'withdraw' AS action, e.src AS wallet_ev, c.base_token AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('to', e."to")::VARCHAR AS extra, 'comet_base' AS pos_kind, -1 AS pos_sign
FROM ev_comet__Withdraw e JOIN dim_comets c ON c.comet = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'comet' AS family, 'supply' AS action, e.dst AS wallet_ev, e.asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('from', e."from", 'collateral', true)::VARCHAR AS extra, 'collateral' AS pos_kind, 1 AS pos_sign
FROM ev_comet__SupplyCollateral e JOIN dim_comets c ON c.comet = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'comet' AS family, 'withdraw' AS action, e.src AS wallet_ev, e.asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('to', e."to", 'collateral', true)::VARCHAR AS extra, 'collateral' AS pos_kind, -1 AS pos_sign
FROM ev_comet__WithdrawCollateral e JOIN dim_comets c ON c.comet = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, s.sub, e.contract, 'comet' AS family, CASE s.sub WHEN 0 THEN 'withdraw' ELSE 'supply' END AS action,
       CASE s.sub WHEN 0 THEN e."from" ELSE e."to" END AS wallet_ev, e.asset, e.amount AS amount_raw, e.contract AS counterparty,
       json_object('transfer_collateral', true)::VARCHAR AS extra, 'collateral' AS pos_kind, CASE s.sub WHEN 0 THEN -1 ELSE 1 END AS pos_sign
FROM ev_comet__TransferCollateral e JOIN dim_comets c ON c.comet = e.contract CROSS JOIN (VALUES (0), (1)) s(sub)
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'comet' AS family, 'liquidated' AS action, e.borrower AS wallet_ev, c.base_token AS asset, e.basePaidOut AS amount_raw,
       e.contract AS counterparty, json_object('absorber', e.absorber, 'usd_value_e8', hs(e.usdValue))::VARCHAR AS extra, 'comet_base' AS pos_kind, 1 AS pos_sign
FROM ev_comet__AbsorbDebt e JOIN dim_comets c ON c.comet = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'comet' AS family, 'liquidated' AS action, e.borrower AS wallet_ev, e.asset, e.collateralAbsorbed AS amount_raw,
       e.contract AS counterparty, json_object('absorber', e.absorber, 'usd_value_e8', hs(e.usdValue))::VARCHAR AS extra, 'collateral' AS pos_kind, -1 AS pos_sign
FROM ev_comet__AbsorbCollateral e JOIN dim_comets c ON c.comet = e.contract
-- ======================= Compound v2 forks =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'compound_v2' AS family, 'supply' AS action, e.minter AS wallet_ev, c.underlying AS asset, e.mintAmount AS amount_raw,
       e.contract AS counterparty, json_object('ctokens', hs(e.mintTokens))::VARCHAR AS extra, 'supplied' AS pos_kind, 1 AS pos_sign, e.mintTokens AS pos_shares
FROM ev_compound_v2__Mint e JOIN dim_ctokens c ON c.ctoken = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'compound_v2' AS family, 'withdraw' AS action, e.redeemer AS wallet_ev, c.underlying AS asset, e.redeemAmount AS amount_raw,
       e.contract AS counterparty, json_object('ctokens', hs(e.redeemTokens))::VARCHAR AS extra, 'supplied' AS pos_kind, -1 AS pos_sign, -e.redeemTokens AS pos_shares
FROM ev_compound_v2__Redeem e JOIN dim_ctokens c ON c.ctoken = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'compound_v2' AS family, 'borrow' AS action, e.borrower AS wallet_ev, c.underlying AS asset, e.borrowAmount AS amount_raw,
       e.contract AS counterparty, json_object('account_borrows', hs(e.accountBorrows))::VARCHAR AS extra, 'debt' AS pos_kind, 1 AS pos_sign, e.accountBorrows AS pos_abs
FROM ev_compound_v2__Borrow e JOIN dim_ctokens c ON c.ctoken = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'compound_v2' AS family, 'repay' AS action, e.borrower AS wallet_ev, c.underlying AS asset, e.repayAmount AS amount_raw,
       e.contract AS counterparty, json_object('payer', e.payer, 'account_borrows', hs(e.accountBorrows))::VARCHAR AS extra, 'debt' AS pos_kind, -1 AS pos_sign, e.accountBorrows AS pos_abs
FROM ev_compound_v2__RepayBorrow e JOIN dim_ctokens c ON c.ctoken = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 1 AS sub, e.contract, 'compound_v2' AS family, 'liquidated' AS action, e.borrower AS wallet_ev, c.underlying AS asset, e.repayAmount AS amount_raw,
       e.contract AS counterparty, json_object('liquidator', e.liquidator, 'ctoken_collateral', e.cTokenCollateral, 'seize_ctokens', hs(e.seizeTokens))::VARCHAR AS extra
FROM ev_compound_v2__LiquidateBorrow e JOIN dim_ctokens c ON c.ctoken = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 2 AS sub, e.contract, 'compound_v2' AS family, 'liquidate' AS action, e.liquidator AS wallet_ev, c.underlying AS asset, e.repayAmount AS amount_raw,
       e.contract AS counterparty, json_object('borrower', e.borrower)::VARCHAR AS extra
FROM ev_compound_v2__LiquidateBorrow e JOIN dim_ctokens c ON c.ctoken = e.contract
-- ======================= Morpho Blue =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'morpho_blue' AS family, 'supply' AS action, e.onBehalf AS wallet_ev, m.loan_token AS asset, e.assets AS amount_raw,
       e.contract AS counterparty, json_object('market', e.id, 'caller', e.caller, 'shares', hs(e.shares))::VARCHAR AS extra, 'supplied' AS pos_kind, 1 AS pos_sign, e.id AS pos_key, e.shares AS pos_shares
FROM ev_morpho_blue__Supply e JOIN dim_morpho_markets m ON m.morpho = e.contract AND m.id = e.id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'morpho_blue' AS family, 'withdraw' AS action, e.onBehalf AS wallet_ev, m.loan_token AS asset, e.assets AS amount_raw,
       e.contract AS counterparty, json_object('market', e.id, 'receiver', e.receiver, 'shares', hs(e.shares))::VARCHAR AS extra, 'supplied' AS pos_kind, -1 AS pos_sign, e.id AS pos_key, -e.shares AS pos_shares
FROM ev_morpho_blue__Withdraw e JOIN dim_morpho_markets m ON m.morpho = e.contract AND m.id = e.id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'morpho_blue' AS family, 'borrow' AS action, e.onBehalf AS wallet_ev, m.loan_token AS asset, e.assets AS amount_raw,
       e.contract AS counterparty, json_object('market', e.id, 'receiver', e.receiver, 'shares', hs(e.shares))::VARCHAR AS extra, 'debt' AS pos_kind, 1 AS pos_sign, e.id AS pos_key, e.shares AS pos_shares
FROM ev_morpho_blue__Borrow e JOIN dim_morpho_markets m ON m.morpho = e.contract AND m.id = e.id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'morpho_blue' AS family, 'repay' AS action, e.onBehalf AS wallet_ev, m.loan_token AS asset, e.assets AS amount_raw,
       e.contract AS counterparty, json_object('market', e.id, 'caller', e.caller, 'shares', hs(e.shares))::VARCHAR AS extra, 'debt' AS pos_kind, -1 AS pos_sign, e.id AS pos_key, -e.shares AS pos_shares
FROM ev_morpho_blue__Repay e JOIN dim_morpho_markets m ON m.morpho = e.contract AND m.id = e.id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'morpho_blue' AS family, 'supply' AS action, e.onBehalf AS wallet_ev, m.collateral_token AS asset, e.assets AS amount_raw,
       e.contract AS counterparty, json_object('market', e.id, 'collateral', true)::VARCHAR AS extra, 'collateral' AS pos_kind, 1 AS pos_sign, e.id AS pos_key
FROM ev_morpho_blue__SupplyCollateral e JOIN dim_morpho_markets m ON m.morpho = e.contract AND m.id = e.id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'morpho_blue' AS family, 'withdraw' AS action, e.onBehalf AS wallet_ev, m.collateral_token AS asset, e.assets AS amount_raw,
       e.contract AS counterparty, json_object('market', e.id, 'collateral', true, 'receiver', e.receiver)::VARCHAR AS extra, 'collateral' AS pos_kind, -1 AS pos_sign, e.id AS pos_key
FROM ev_morpho_blue__WithdrawCollateral e JOIN dim_morpho_markets m ON m.morpho = e.contract AND m.id = e.id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'morpho_blue' AS family, 'liquidated' AS action, e.borrower AS wallet_ev, m.collateral_token AS asset, e.seizedAssets AS amount_raw,
       e.contract AS counterparty, json_object('market', e.id, 'liquidator', e.caller)::VARCHAR AS extra, 'collateral' AS pos_kind, -1 AS pos_sign, e.id AS pos_key
FROM ev_morpho_blue__Liquidate e JOIN dim_morpho_markets m ON m.morpho = e.contract AND m.id = e.id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 1 AS sub, e.contract, 'morpho_blue' AS family, 'liquidated' AS action, e.borrower AS wallet_ev, m.loan_token AS asset, e.repaidAssets + e.badDebtAssets AS amount_raw,
       e.contract AS counterparty, json_object('market', e.id, 'bad_debt_assets', hs(e.badDebtAssets))::VARCHAR AS extra, 'debt' AS pos_kind, -1 AS pos_sign, e.id AS pos_key,
       -(e.repaidShares + e.badDebtShares) AS pos_shares
FROM ev_morpho_blue__Liquidate e JOIN dim_morpho_markets m ON m.morpho = e.contract AND m.id = e.id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 2 AS sub, e.contract, 'morpho_blue' AS family, 'liquidate' AS action, e.caller AS wallet_ev, m.loan_token AS asset, e.repaidAssets AS amount_raw,
       e.contract AS counterparty, json_object('market', e.id, 'borrower', e.borrower, 'seized', hs(e.seizedAssets))::VARCHAR AS extra
FROM ev_morpho_blue__Liquidate e JOIN dim_morpho_markets m ON m.morpho = e.contract AND m.id = e.id
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'morpho_blue' AS family, 'flashloan' AS action, e.caller AS wallet_ev, e.token AS asset, e.assets AS amount_raw, e.contract AS counterparty
FROM ev_morpho_blue__FlashLoan e WHERE e.contract IN (SELECT morpho FROM dim_morpho_markets)
-- ======================= Silo v1 =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'silo_v1' AS family, 'supply' AS action, e.depositor AS wallet_ev, e.asset, e.amount AS amount_raw, e.contract AS counterparty,
       json_object('collateral_only', e.collateralOnly)::VARCHAR AS extra, CASE WHEN e.collateralOnly THEN 'collateral' ELSE 'supplied' END AS pos_kind, 1 AS pos_sign
FROM ev_silo_v1__Deposit e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'silo_v1' AS family, 'withdraw' AS action, e.depositor AS wallet_ev, e.asset, e.amount AS amount_raw, e.contract AS counterparty,
       json_object('receiver', e.receiver, 'priority', e.priority)::VARCHAR AS extra, 'supplied' AS pos_kind, -1 AS pos_sign
FROM ev_silo_v1__Withdraw e WHERE e.contract IN (SELECT silo FROM dim_silos)
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'silo_v1' AS family, 'borrow' AS action, e."user" AS wallet_ev, e.asset, e.amount AS amount_raw, e.contract AS counterparty, 'debt' AS pos_kind, 1 AS pos_sign
FROM ev_silo_v1__Borrow e WHERE e.contract IN (SELECT silo FROM dim_silos)
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'silo_v1' AS family, 'repay' AS action, e."user" AS wallet_ev, e.asset, e.amount AS amount_raw, e.contract AS counterparty, 'debt' AS pos_kind, -1 AS pos_sign
FROM ev_silo_v1__Repay e WHERE e.contract IN (SELECT silo FROM dim_silos)
-- ======================= Dolomite =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'dolomite' AS family, 'supply' AS action, e.accountOwner AS wallet_ev, 'dolomite_market:' || hs(e.market) AS asset,
       e.deltaWei AS amount_raw, e.contract AS counterparty, json_object('account_number', hs(e.accountNumber), 'market', hs(e.market), 'new_par', hs(e.newPar), 'new_par_positive', e.newParSign, 'from', e."from")::VARCHAR AS extra
FROM ev_dolomite__LogDeposit e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'dolomite' AS family, CASE WHEN e.newParSign THEN 'withdraw' ELSE 'borrow' END AS action, e.accountOwner AS wallet_ev,
       'dolomite_market:' || hs(e.market) AS asset, e.deltaWei AS amount_raw, e.contract AS counterparty,
       json_object('account_number', hs(e.accountNumber), 'market', hs(e.market), 'new_par', hs(e.newPar), 'new_par_positive', e.newParSign, 'to', e."to")::VARCHAR AS extra
FROM ev_dolomite__LogWithdraw e
-- ======================= ERC-4626 vaults (stake/unstake) =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'erc4626' AS family, 'stake' AS action, e.owner AS wallet_ev, coalesce(v.asset, e.contract) AS asset, e.assets AS amount_raw,
       e.contract AS counterparty, json_object('shares', hs(e.shares), 'sender', e.sender)::VARCHAR AS extra
FROM ev_erc4626__Deposit e LEFT JOIN meta_vault v ON v.vault = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'erc4626' AS family, 'unstake' AS action, e.owner AS wallet_ev, coalesce(v.asset, e.contract) AS asset, e.assets AS amount_raw,
       e.contract AS counterparty, json_object('shares', hs(e.shares), 'receiver', e.receiver)::VARCHAR AS extra
FROM ev_erc4626__Withdraw e LEFT JOIN meta_vault v ON v.vault = e.contract
-- ======================= DEX liquidity =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, s.sub, e.contract, 'univ2' AS family, 'lp_add' AS action, NULL AS wallet_ev,
       CASE s.sub WHEN 0 THEN p.token0 ELSE p.token1 END AS asset, CASE s.sub WHEN 0 THEN e.amount0 ELSE e.amount1 END AS amount_raw, e.contract AS counterparty,
       json_object('router', e.sender)::VARCHAR AS extra
FROM ev_univ2__Mint e JOIN dim_pools p ON p.pool = e.contract AND p.kind = 'v2' CROSS JOIN (VALUES (0), (1)) s(sub)
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, s.sub, e.contract, 'univ2' AS family, 'lp_remove' AS action, NULL AS wallet_ev,
       CASE s.sub WHEN 0 THEN p.token0 ELSE p.token1 END AS asset, CASE s.sub WHEN 0 THEN e.amount0 ELSE e.amount1 END AS amount_raw, e.contract AS counterparty,
       json_object('to', e."to")::VARCHAR AS extra
FROM ev_univ2__Burn e JOIN dim_pools p ON p.pool = e.contract AND p.kind = 'v2' CROSS JOIN (VALUES (0), (1)) s(sub)
UNION ALL BY NAME   -- v3 pool-level positions not owned by a position manager
SELECT e.block_number, e.tx_index, e.log_index, s.sub, e.contract, 'univ3' AS family, 'lp_add' AS action, e.owner AS wallet_ev,
       CASE s.sub WHEN 0 THEN p.token0 ELSE p.token1 END AS asset, CASE s.sub WHEN 0 THEN e.amount0 ELSE e.amount1 END AS amount_raw, e.contract AS counterparty,
       json_object('tick_lower', e.tickLower, 'tick_upper', e.tickUpper, 'liquidity', hs(e.amount))::VARCHAR AS extra,
       CASE s.sub WHEN 0 THEN 'lp' END AS pos_kind, 1 AS pos_sign, e.contract || ':' || e.owner || ':' || hs(e.tickLower) || ':' || hs(e.tickUpper) AS pos_key,
       CASE s.sub WHEN 0 THEN e.amount END AS pos_liq
FROM ev_univ3__Mint e JOIN dim_pools p ON p.pool = e.contract CROSS JOIN (VALUES (0), (1)) s(sub) WHERE e.owner NOT IN (SELECT npm FROM npm_set)
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, s.sub, e.contract, 'univ3' AS family, 'lp_remove' AS action, e.owner AS wallet_ev,
       CASE s.sub WHEN 0 THEN p.token0 ELSE p.token1 END AS asset, CASE s.sub WHEN 0 THEN e.amount0 ELSE e.amount1 END AS amount_raw, e.contract AS counterparty,
       json_object('tick_lower', e.tickLower, 'tick_upper', e.tickUpper, 'liquidity', hs(e.amount))::VARCHAR AS extra,
       CASE s.sub WHEN 0 THEN 'lp' END AS pos_kind, -1 AS pos_sign, e.contract || ':' || e.owner || ':' || hs(e.tickLower) || ':' || hs(e.tickUpper) AS pos_key,
       CASE s.sub WHEN 0 THEN -e.amount END AS pos_liq
FROM ev_univ3__Burn e JOIN dim_pools p ON p.pool = e.contract CROSS JOIN (VALUES (0), (1)) s(sub) WHERE e.owner NOT IN (SELECT npm FROM npm_set) AND e.amount > 0
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, s.sub, e.contract, 'univ3' AS family, 'lp_collect' AS action, e.recipient AS wallet_ev,
       CASE s.sub WHEN 0 THEN p.token0 ELSE p.token1 END AS asset, CASE s.sub WHEN 0 THEN e.amount0 ELSE e.amount1 END AS amount_raw, e.contract AS counterparty,
       json_object('owner', e.owner)::VARCHAR AS extra
FROM ev_univ3__Collect e JOIN dim_pools p ON p.pool = e.contract CROSS JOIN (VALUES (0), (1)) s(sub) WHERE e.owner NOT IN (SELECT npm FROM npm_set)
UNION ALL BY NAME   -- NFT position managers (Uniswap V3 / forks)
SELECT e.block_number, e.tx_index, e.log_index, s.sub, e.contract, 'univ3_npm' AS family, 'lp_add' AS action, NULL AS wallet_ev,
       CASE s.sub WHEN 0 THEN t.token0 ELSE t.token1 END AS asset, CASE s.sub WHEN 0 THEN e.amount0 ELSE e.amount1 END AS amount_raw, coalesce(t.pool, e.contract) AS counterparty,
       json_object('token_id', hs(e.tokenId), 'liquidity', hs(e.liquidity), 'npm', e.contract)::VARCHAR AS extra,
       CASE s.sub WHEN 0 THEN 'lp' END AS pos_kind, 1 AS pos_sign, e.contract || ':' || hs(e.tokenId) AS pos_key, CASE s.sub WHEN 0 THEN e.liquidity END AS pos_liq
FROM ev_univ3_npm__IncreaseLiquidity e JOIN dim_npm n ON n.npm = e.contract LEFT JOIN dim_npm_tokens t ON t.npm = e.contract AND t.token_id = e.tokenId CROSS JOIN (VALUES (0), (1)) s(sub)
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, s.sub, e.contract, 'univ3_npm' AS family, 'lp_add' AS action, NULL AS wallet_ev,
       CASE s.sub WHEN 0 THEN t.token0 ELSE t.token1 END AS asset, CASE s.sub WHEN 0 THEN e.amount0 ELSE e.amount1 END AS amount_raw, e.pool AS counterparty,
       json_object('token_id', hs(e.tokenId), 'liquidity', hs(e.actualLiquidity), 'npm', e.contract)::VARCHAR AS extra,
       CASE s.sub WHEN 0 THEN 'lp' END AS pos_kind, 1 AS pos_sign, e.contract || ':' || hs(e.tokenId) AS pos_key, CASE s.sub WHEN 0 THEN e.actualLiquidity END AS pos_liq
FROM ev_algebra_npm__IncreaseLiquidity e LEFT JOIN dim_npm_tokens t ON t.npm = e.contract AND t.token_id = e.tokenId CROSS JOIN (VALUES (0), (1)) s(sub)
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, s.sub, e.contract, 'univ3_npm' AS family, 'lp_remove' AS action, NULL AS wallet_ev,
       CASE s.sub WHEN 0 THEN t.token0 ELSE t.token1 END AS asset, CASE s.sub WHEN 0 THEN e.amount0 ELSE e.amount1 END AS amount_raw, coalesce(t.pool, e.contract) AS counterparty,
       json_object('token_id', hs(e.tokenId), 'liquidity', hs(e.liquidity), 'npm', e.contract)::VARCHAR AS extra,
       CASE s.sub WHEN 0 THEN 'lp' END AS pos_kind, -1 AS pos_sign, e.contract || ':' || hs(e.tokenId) AS pos_key, CASE s.sub WHEN 0 THEN -e.liquidity END AS pos_liq
FROM ev_univ3_npm__DecreaseLiquidity e JOIN dim_npm n ON n.npm = e.contract LEFT JOIN dim_npm_tokens t ON t.npm = e.contract AND t.token_id = e.tokenId CROSS JOIN (VALUES (0), (1)) s(sub)
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, s.sub, e.contract, 'univ3_npm' AS family, 'lp_collect' AS action, e.recipient AS wallet_ev,
       CASE s.sub WHEN 0 THEN t.token0 ELSE t.token1 END AS asset, CASE s.sub WHEN 0 THEN e.amount0 ELSE e.amount1 END AS amount_raw, coalesce(t.pool, e.contract) AS counterparty,
       json_object('token_id', hs(e.tokenId), 'npm', e.contract)::VARCHAR AS extra
FROM ev_univ3_npm__Collect e JOIN dim_npm n ON n.npm = e.contract LEFT JOIN dim_npm_tokens t ON t.npm = e.contract AND t.token_id = e.tokenId CROSS JOIN (VALUES (0), (1)) s(sub)
UNION ALL BY NAME   -- Uniswap V4 (liquidity units; token amounts settled via PoolManager deltas)
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'univ4' AS family, CASE WHEN e.liquidityDelta >= 0 THEN 'lp_add' ELSE 'lp_remove' END AS action, NULL AS wallet_ev,
       p.token0 AS asset, NULL::HUGEINT AS amount_raw, e.contract AS counterparty,
       json_object('pool_id', e.id, 'token1', p.token1, 'tick_lower', e.tickLower, 'tick_upper', e.tickUpper, 'liquidity_delta', hs(e.liquidityDelta), 'salt', e.salt, 'sender', e.sender)::VARCHAR AS extra,
       'lp' AS pos_kind, sign(e.liquidityDelta)::INTEGER AS pos_sign, 'v4:' || e.id || ':' || e.sender || ':' || e.salt || ':' || hs(e.tickLower) || ':' || hs(e.tickUpper) AS pos_key, e.liquidityDelta AS pos_liq
FROM ev_univ4__ModifyLiquidity e LEFT JOIN dim_v4_pools p ON p.manager = e.contract AND p.pool_id = e.id WHERE e.liquidityDelta <> 0
-- ======================= GMX v1 =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'gmx_v1' AS family, 'perp_increase' AS action, e.account AS wallet_ev, e.indexToken AS asset, e.sizeDelta AS amount_raw,
       e.contract AS counterparty, json_object('key', e.key, 'collateral_token', e.collateralToken, 'collateral_delta_usd30', hs(e.collateralDelta), 'is_long', e.isLong, 'price30', hs(e.price), 'fee_usd30', hs(e.fee))::VARCHAR AS extra,
       'perp_size' AS pos_kind, 1 AS pos_sign, e.key AS pos_key
FROM ev_gmx_v1__IncreasePosition e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'gmx_v1' AS family, 'perp_decrease' AS action, e.account AS wallet_ev, e.indexToken AS asset, e.sizeDelta AS amount_raw,
       e.contract AS counterparty, json_object('key', e.key, 'collateral_token', e.collateralToken, 'collateral_delta_usd30', hs(e.collateralDelta), 'is_long', e.isLong, 'price30', hs(e.price), 'fee_usd30', hs(e.fee))::VARCHAR AS extra,
       'perp_size' AS pos_kind, -1 AS pos_sign, e.key AS pos_key
FROM ev_gmx_v1__DecreasePosition e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'gmx_v1' AS family, 'perp_liquidated' AS action, e.account AS wallet_ev, e.indexToken AS asset, e.size AS amount_raw,
       e.contract AS counterparty, json_object('key', e.key, 'collateral_token', e.collateralToken, 'collateral_usd30', hs(e.collateral), 'is_long', e.isLong, 'realised_pnl_usd30', hs(e.realisedPnl), 'mark_price30', hs(e.markPrice))::VARCHAR AS extra,
       'perp_size' AS pos_kind, -1 AS pos_sign, 0::HUGEINT AS pos_abs, e.key AS pos_key
FROM ev_gmx_v1__LiquidatePosition e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'gmx_v1' AS family, 'swap' AS action, e.account AS wallet_ev, e.tokenIn AS asset, e.amountIn AS amount_raw,
       e.contract AS counterparty, json_object('token_out', e.tokenOut, 'amount_out', hs(e.amountOutAfterFees))::VARCHAR AS extra
FROM ev_gmx_v1__Swap e WHERE e.contract IN (SELECT address FROM reg_contracts WHERE protocol = 'gmx_v1')
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'gmx_v1' AS family, 'lp_add' AS action, e.account AS wallet_ev, e.token AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('glp_minted', hs(e.mintAmount), 'usdg', hs(e.usdgAmount))::VARCHAR AS extra
FROM ev_gmx_v1__AddLiquidity e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'gmx_v1' AS family, 'lp_remove' AS action, e.account AS wallet_ev, e.token AS asset, e.amountOut AS amount_raw,
       e.contract AS counterparty, json_object('glp_burned', hs(e.glpAmount))::VARCHAR AS extra
FROM ev_gmx_v1__RemoveLiquidity e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'gmx_v1' AS family, 'stake' AS action, e.account AS wallet_ev, e.token AS asset, e.amount AS amount_raw, e.contract AS counterparty FROM ev_gmx_v1__StakeGmx e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'gmx_v1' AS family, 'unstake' AS action, e.account AS wallet_ev, e.token AS asset, e.amount AS amount_raw, e.contract AS counterparty FROM ev_gmx_v1__UnstakeGmx e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'gmx_v1' AS family, 'stake' AS action, e.account AS wallet_ev, 'GLP' AS asset, e.amount AS amount_raw, e.contract AS counterparty FROM ev_gmx_v1__StakeGlp e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'gmx_v1' AS family, 'unstake' AS action, e.account AS wallet_ev, 'GLP' AS asset, e.amount AS amount_raw, e.contract AS counterparty FROM ev_gmx_v1__UnstakeGlp e
-- ======================= GMX v2 (decoded payload from py/gmx_v2.py) =======================
UNION ALL BY NAME
SELECT g.block_number, g.tx_index, g.log_index, s.sub, g.contract, 'gmx_v2' AS family,
       CASE WHEN g.event_name = 'PositionIncrease' THEN 'perp_increase' WHEN g.order_type = 7 THEN 'perp_liquidated' ELSE 'perp_decrease' END AS action,
       g.account AS wallet_ev, CASE s.sub WHEN 0 THEN coalesce(mk.index_token, g.market) ELSE g.collateral_token END AS asset,
       CASE s.sub WHEN 0 THEN g.size_delta_usd ELSE abs(g.collateral_delta_amount) END AS amount_raw, g.contract AS counterparty,
       json_object('market', g.market, 'is_long', g.is_long, 'size_in_usd30', hs(g.size_in_usd), 'collateral_token', g.collateral_token,
                   'collateral_amount', hs(g.collateral_amount), 'order_type', g.order_type, 'position_key', g.position_key, 'leg', CASE s.sub WHEN 0 THEN 'size_usd30' ELSE 'collateral' END)::VARCHAR AS extra,
       CASE s.sub WHEN 0 THEN 'perp_size' ELSE 'perp_collateral' END AS pos_kind,
       CASE WHEN g.event_name = 'PositionIncrease' THEN 1 ELSE -1 END AS pos_sign,
       CASE s.sub WHEN 0 THEN g.size_in_usd ELSE g.collateral_amount END AS pos_abs, g.position_key AS pos_key
FROM gmx_v2_events g LEFT JOIN gmx_v2_markets mk ON mk.market = g.market CROSS JOIN (VALUES (0), (1)) s(sub)
WHERE g.event_name IN ('PositionIncrease', 'PositionDecrease') AND g.block_number BETWEEN ${PLO} AND ${PHI}
-- ======================= Bridges =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'arbsys' AS family, 'bridge_out' AS action, e.caller AS wallet_ev, '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' AS asset,
       e.callvalue AS amount_raw, e.contract AS counterparty, json_object('l1_destination', e.destination, 'position', hs(e.position))::VARCHAR AS extra
FROM ev_arbsys__L2ToL1Tx e WHERE e.contract = '0x0000000000000000000000000000000000000064' AND e.callvalue > 0
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'arb_gateway' AS family, 'bridge_out' AS action, e._from AS wallet_ev, NULL AS asset,
       e._amount AS amount_raw, e.contract AS counterparty, json_object('l1_token', e.l1Token, 'l1_to', e._to)::VARCHAR AS extra
FROM ev_arb_gateway__WithdrawalInitiated e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'arb_gateway' AS family, 'bridge_in' AS action, e._to AS wallet_ev, NULL AS asset,
       e._amount AS amount_raw, e.contract AS counterparty, json_object('l1_token', e.l1Token, 'l1_from', e._from)::VARCHAR AS extra
FROM ev_arb_gateway__DepositFinalized e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'cctp' AS family, 'bridge_out' AS action, e.depositor AS wallet_ev, e.burnToken AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('dest_domain', e.destinationDomain, 'mint_recipient', b2a(e.mintRecipient), 'version', 1)::VARCHAR AS extra
FROM ev_cctp_v1__DepositForBurn e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'cctp' AS family, 'bridge_out' AS action, e.depositor AS wallet_ev, e.burnToken AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('dest_domain', e.destinationDomain, 'mint_recipient', b2a(e.mintRecipient), 'max_fee', hs(e.maxFee), 'version', 2)::VARCHAR AS extra
FROM ev_cctp_v2__DepositForBurn e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'cctp' AS family, 'bridge_in' AS action, e.mintRecipient AS wallet_ev, e.mintToken AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('version', 1)::VARCHAR AS extra
FROM ev_cctp_v1__MintAndWithdraw e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'cctp' AS family, 'bridge_in' AS action, e.mintRecipient AS wallet_ev, e.mintToken AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('version', 2, 'fee', hs(e.feeCollected))::VARCHAR AS extra
FROM ev_cctp_v2__MintAndWithdraw e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'across' AS family, 'bridge_out' AS action, e.depositor AS wallet_ev, e.inputToken AS asset, e.inputAmount AS amount_raw,
       e.contract AS counterparty, json_object('dest_chain', hs(e.destinationChainId), 'recipient', e.recipient, 'output_token', e.outputToken, 'output_amount', hs(e.outputAmount), 'deposit_id', hs(e.depositId))::VARCHAR AS extra
FROM ev_across__V3FundsDeposited e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'across' AS family, 'bridge_out' AS action, b2a(e.depositor) AS wallet_ev, b2a(e.inputToken) AS asset, e.inputAmount AS amount_raw,
       e.contract AS counterparty, json_object('dest_chain', hs(e.destinationChainId), 'recipient', b2a(e.recipient), 'output_token', e.outputToken, 'output_amount', hs(e.outputAmount), 'deposit_id', hs(e.depositId))::VARCHAR AS extra
FROM ev_across__FundsDeposited e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'across' AS family, 'bridge_in' AS action, e.recipient AS wallet_ev, e.outputToken AS asset, e.outputAmount AS amount_raw,
       e.contract AS counterparty, json_object('origin_chain', hs(e.originChainId), 'relayer', e.relayer, 'depositor', e.depositor, 'deposit_id', hs(e.depositId))::VARCHAR AS extra
FROM ev_across__FilledV3Relay e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'across' AS family, 'bridge_in' AS action, b2a(e.updatedRecipient) AS wallet_ev, b2a(e.outputToken) AS asset,
       e.updatedOutputAmount AS amount_raw, e.contract AS counterparty,
       json_object('origin_chain', hs(e.originChainId), 'relayer', b2a(e.relayer), 'depositor', b2a(e.depositor), 'deposit_id', hs(e.depositId), 'fill_type', e.fillType)::VARCHAR AS extra
FROM ev_across__FilledRelay e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'oft' AS family, 'bridge_out' AS action, e.fromAddress AS wallet_ev, coalesce(o.token, e.contract) AS asset, e.amountSentLD AS amount_raw,
       e.contract AS counterparty, json_object('dst_eid', e.dstEid, 'amount_received', hs(e.amountReceivedLD), 'guid', e.guid)::VARCHAR AS extra
FROM ev_oft__OFTSent e LEFT JOIN meta_oft o ON o.oft = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'oft' AS family, 'bridge_in' AS action, e.toAddress AS wallet_ev, coalesce(o.token, e.contract) AS asset, e.amountReceivedLD AS amount_raw,
       e.contract AS counterparty, json_object('src_eid', e.srcEid, 'guid', e.guid)::VARCHAR AS extra
FROM ev_oft__OFTReceived e LEFT JOIN meta_oft o ON o.oft = e.contract
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'hop' AS family, 'bridge_out' AS action, NULL AS wallet_ev, NULL AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('dest_chain', hs(e.chainId), 'recipient', e.recipient, 'bonder_fee', hs(e.bonderFee))::VARCHAR AS extra
FROM ev_hop__TransferSent e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'hop' AS family, 'bridge_in' AS action, e.recipient AS wallet_ev, NULL AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('relayer', e.relayer, 'relayer_fee', hs(e.relayerFee))::VARCHAR AS extra
FROM ev_hop__TransferFromL1Completed e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'relay' AS family, 'bridge_out' AS action, e."from" AS wallet_ev, e.token AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('request_id', e.id)::VARCHAR AS extra
FROM ev_relay__RelayErc20Deposit e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'relay' AS family, 'bridge_out' AS action, e."from" AS wallet_ev, '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('request_id', e.id)::VARCHAR AS extra
FROM ev_relay__RelayNativeDeposit e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'relay' AS family, 'bridge_in' AS action, e."to" AS wallet_ev, '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('solver_fill', true)::VARCHAR AS extra
FROM ev_relay__SolverNativeTransfer e
-- ======================= incentives =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'merkl' AS family, 'incentive_claim' AS action, e."user" AS wallet_ev, e.token AS asset, e.amount AS amount_raw,
       e.contract AS counterparty, json_object('distributor', e.contract)::VARCHAR AS extra
FROM ev_merkl__Claimed e
-- ======================= Robinhood stock tokens: corporate actions =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'robinhood_stock' AS family, 'corporate_action' AS action, e.contract AS wallet_ev, e.contract AS asset,
       e.newMultiplier AS amount_raw, e.contract AS counterparty,
       json_object('old_multiplier_e18', hs(e.oldMultiplier), 'new_multiplier_e18', hs(e.newMultiplier), 'effective_at', hs(e.effectiveAtTimestamp))::VARCHAR AS extra
FROM ev_robinhood_stock__UIMultiplierUpdated e
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'robinhood_stock' AS family, 'corporate_action' AS action, e.contract AS wallet_ev, e.contract AS asset,
       NULL::HUGEINT AS amount_raw, e.contract AS counterparty, json_object('kind', 'rename', 'name', abi_string(e_h.h, 0), 'symbol', abi_string(e_h.h, 1))::VARCHAR AS extra
FROM ev_robinhood_stock__MetaDataUpdated e JOIN logs_sel e_h ON e_h.block_number = e.block_number AND e_h.log_index = e.log_index
-- ======================= WETH-style wrap/unwrap events =======================
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'weth' AS family, 'wrap' AS action, e.dst AS wallet_ev, e.contract AS asset, e.wad AS amount_raw, e.contract AS counterparty
FROM ev_weth__Deposit e WHERE e.contract IN (SELECT token FROM dim_weth)
UNION ALL BY NAME
SELECT e.block_number, e.tx_index, e.log_index, 0 AS sub, e.contract, 'weth' AS family, 'unwrap' AS action, e.src AS wallet_ev, e.contract AS asset, e.wad AS amount_raw, e.contract AS counterparty
FROM ev_weth__Withdrawal e WHERE e.contract IN (SELECT token FROM dim_weth)
;

-- ======================= token-transfer derived actions =======================
-- issuer mint/burn for tracked issuer tokens (Robinhood stock tokens, registry stablecoins) and aeWETH-style wrap/unwrap
-- (mint/burn Transfer on WETH tokens that emit no Deposit events). Gateway/CCTP/OFT mints in the same tx are bridge legs, not issuance.
CREATE OR REPLACE TEMP TABLE tt_part AS
SELECT t.block_number, t.tx_index, t.log_index, a2s(t.token) AS token, a2s(t."from") AS f, a2s(t."to") AS tt, blob_uint(t.amount) AS amt
FROM token_transfers t
WHERE t.block_number BETWEEN ${PLO} AND ${PHI} AND t.standard = 'ERC20'
  AND (t."from" = unhex('0000000000000000000000000000000000000000') OR t."to" = unhex('0000000000000000000000000000000000000000'))
  AND a2s(t.token) IN (SELECT token FROM issuer_tokens UNION SELECT token FROM dim_weth);

INSERT INTO acts BY NAME
SELECT block_number, tx_index, log_index, 0 AS sub, token AS contract, i.kind AS family,
       CASE WHEN f = '0x0000000000000000000000000000000000000000' THEN 'issuer_mint' ELSE 'issuer_burn' END AS action,
       CASE WHEN f = '0x0000000000000000000000000000000000000000' THEN tt ELSE f END AS wallet_ev, token AS asset, amt AS amount_raw, token AS counterparty,
       json_object('issuer_token', i.label)::VARCHAR AS extra
FROM tt_part JOIN issuer_tokens i USING (token)
WHERE NOT (f = '0x0000000000000000000000000000000000000000' AND tt = '0x0000000000000000000000000000000000000000');

INSERT INTO acts BY NAME
SELECT block_number, tx_index, log_index, 0 AS sub, token AS contract, 'weth' AS family,
       CASE WHEN f = '0x0000000000000000000000000000000000000000' THEN 'wrap' ELSE 'unwrap' END AS action,
       CASE WHEN f = '0x0000000000000000000000000000000000000000' THEN tt ELSE f END AS wallet_ev, token AS asset, amt AS amount_raw, token AS counterparty
FROM tt_part WHERE token IN (SELECT token FROM dim_weth WHERE token NOT IN (SELECT contract FROM ev_weth__Deposit))
  AND token NOT IN (SELECT token FROM issuer_tokens);

-- ======================= swaps =======================
CREATE OR REPLACE TEMP TABLE sw_part AS
SELECT block_number, tx_index, log_index, kind, a0, a1, a2s(emitter) AS emitter_s,
       CASE WHEN kind = 'v4' THEN a2s(emitter) || '|' || a2s(v4_id) ELSE a2s(emitter) END AS pool_key,
       CASE WHEN kind = 'v4' THEN a2s(v4_id) ELSE a2s(emitter) END AS pool_ref
FROM swaps_sel WHERE block_number BETWEEN ${PLO} AND ${PHI} AND (a0 > 0 OR a1 > 0);
CREATE OR REPLACE TEMP TABLE pool_tokens AS
SELECT pool AS pool_key, token0, token1 FROM dim_pools
UNION ALL SELECT manager || '|' || pool_id, token0, token1 FROM dim_v4_pools;
INSERT INTO acts BY NAME
SELECT s.block_number, s.tx_index, s.log_index, 0 AS sub, s.emitter_s AS contract, 'dex_' || s.kind AS family, 'swap' AS action, NULL AS wallet_ev,
       CASE WHEN s.a0 > 0 THEN p.token0 ELSE p.token1 END AS asset,
       CASE WHEN s.a0 > 0 THEN s.a0 ELSE s.a1 END AS amount_raw, s.emitter_s AS counterparty,
       json_object('token_out', CASE WHEN s.a0 > 0 THEN p.token1 ELSE p.token0 END,
                   'amount_out', hs(CASE WHEN s.a0 > 0 THEN -s.a1 ELSE -s.a0 END), 'pool', s.pool_ref)::VARCHAR AS extra
FROM sw_part s LEFT JOIN pool_tokens p ON p.pool_key = s.pool_key;

-- ======================= Arbitrum-native deposit transactions (types 100 / 105) =======================
INSERT INTO acts BY NAME
SELECT t.block_number, t.tx_index, NULL::UINTEGER AS log_index, 0 AS sub, '0x0000000000000000000000000000000000000064' AS contract, 'arb_deposit_tx' AS family, 'bridge_in' AS action,
       CASE WHEN t.type = 100 THEN a2s(t."to") ELSE a2s(t."from") END AS wallet_ev, '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' AS asset,
       CASE WHEN t.type = 100 THEN blob_uint(t.value) ELSE blob_uint(t.deposit_value) END AS amount_raw, NULL AS counterparty,
       json_object('tx_type', t.type, 'retryable_to', CASE WHEN t.type = 105 THEN a2s(t."to") END)::VARCHAR AS extra
FROM transactions t
WHERE t.block_number BETWEEN ${PLO} AND ${PHI} AND t.type IN (100, 105) AND t.status = 1
  AND CASE WHEN t.type = 100 THEN blob_uint(t.value) ELSE blob_uint(t.deposit_value) END > 0;

-- ======================= finalize: tx context, wallet fallback, protocol labels =======================
CREATE OR REPLACE TEMP TABLE akeys AS SELECT DISTINCT block_number, tx_index FROM acts;
CREATE OR REPLACE TEMP TABLE txp AS
SELECT t.block_number, t.tx_index, a2s(t.hash) AS tx_hash, a2s(t."from") AS tx_from, a2s(t."to") AS tx_to
FROM transactions t SEMI JOIN akeys k ON k.block_number = t.block_number AND k.tx_index = t.tx_index
WHERE t.block_number BETWEEN ${PLO} AND ${PHI};
CREATE OR REPLACE TEMP TABLE blk AS SELECT number AS block_number, to_timestamp(timestamp::BIGINT) AS block_time FROM blocks WHERE number BETWEEN ${PLO} AND ${PHI};

-- gateway legs: resolve L2 token from the mint/burn in the same tx
CREATE OR REPLACE TEMP TABLE gw_keys AS SELECT DISTINCT block_number, tx_index FROM acts WHERE family = 'arb_gateway';
CREATE OR REPLACE TEMP TABLE tt_bridge AS
SELECT t.block_number, t.tx_index, a2s(t.token) AS token, a2s(t."from") AS f, a2s(t."to") AS tt, blob_uint(t.amount) AS amt
FROM token_transfers t SEMI JOIN gw_keys k ON k.block_number = t.block_number AND k.tx_index = t.tx_index
WHERE t.block_number BETWEEN ${PLO} AND ${PHI}
  AND (t."from" = unhex('0000000000000000000000000000000000000000') OR t."to" = unhex('0000000000000000000000000000000000000000'));
CREATE OR REPLACE TEMP TABLE gw_tok AS
SELECT a.block_number, a.tx_index, a.log_index, any_value(m.token) AS token
FROM acts a JOIN tt_bridge m ON m.block_number = a.block_number AND m.tx_index = a.tx_index AND m.amt = a.amount_raw
  AND ((a.action = 'bridge_in' AND m.f = '0x0000000000000000000000000000000000000000' AND m.tt = a.wallet_ev)
    OR (a.action = 'bridge_out' AND m.tt = '0x0000000000000000000000000000000000000000' AND m.f = a.wallet_ev))
WHERE a.family IN ('arb_gateway') AND a.asset IS NULL GROUP BY ALL;

COPY (
SELECT ${CHAIN}::INTEGER AS chain, a.block_number, b.block_time, x.tx_hash, a.tx_index, a.log_index, a.sub,
       coalesce(a.wallet_ev, x.tx_from) AS wallet,
       coalesce(rc.protocol, ap.protocol, cm.protocol, ct.protocol, mm.protocol, np.protocol, dp.protocol, v4.protocol, a.family) AS protocol,
       -- Comet base leg: supply while net-negative = repay; withdraw into negative = borrow (relabelled in positions step)
       a.action, coalesce(a.asset, g.token) AS asset, a.amount_raw, a.counterparty AS counterparty_contract, a.extra AS extra_json,
       a.family, x.tx_from, x.tx_to, a.pos_kind, a.pos_sign, a.pos_abs, a.pos_key, a.pos_shares, a.pos_liq, a.contract AS emitter
FROM acts a
LEFT JOIN txp x ON x.block_number = a.block_number AND x.tx_index = a.tx_index
LEFT JOIN blk b ON b.block_number = a.block_number
LEFT JOIN gw_tok g ON g.block_number = a.block_number AND g.tx_index = a.tx_index AND g.log_index IS NOT DISTINCT FROM a.log_index
LEFT JOIN (SELECT address, any_value(protocol) protocol FROM reg_contracts GROUP BY 1) rc ON rc.address = a.contract
LEFT JOIN dim_aave_pools ap ON ap.pool = a.contract
LEFT JOIN dim_comets cm ON cm.comet = a.contract
LEFT JOIN dim_ctokens ct ON ct.ctoken = a.contract
LEFT JOIN (SELECT DISTINCT morpho, protocol FROM dim_morpho_markets) mm ON mm.morpho = a.contract
LEFT JOIN dim_npm np ON np.npm = a.contract
LEFT JOIN dim_pools dp ON dp.pool = a.contract
LEFT JOIN (SELECT DISTINCT manager, protocol FROM dim_v4_pools) v4 ON v4.manager = a.contract
) TO '${OUT}/econ_actions/part_${PLO}.parquet' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 1000000);
