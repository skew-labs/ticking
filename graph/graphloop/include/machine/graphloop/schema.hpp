#pragma once
// Code tables shared by builder, server, exporter and agent_context.md.  Category codes are the
// Galaxy DATA_CONTRACT.md table (uint8, never renumber); everything else is append-only.
#include "core.hpp"
#include <string_view>

namespace machine::graphloop {

enum Category : uint8_t {
    cex_hot_wallet, cex_deposit, bridge, dex_router, dex_pool, lending, token_contract, nft_contract,
    mev_bot, multisig_or_safe, contract_other, eoa_user, eoa_whale, unknown, category_count
};
inline constexpr std::string_view category_names[category_count] = {
    "cex_hot_wallet", "cex_deposit", "bridge", "dex_router", "dex_pool", "lending", "token_contract", "nft_contract",
    "mev_bot", "multisig_or_safe", "contract_other", "eoa_user", "eoa_whale", "unknown"};
inline bool contract_only(uint8_t c) { return c == bridge || c == dex_router || c == dex_pool || c == lending || c == token_contract || c == nft_contract || c == multisig_or_safe || c == contract_other; }
inline bool eoa_only(uint8_t c) { return c == eoa_user || c == eoa_whale; }
inline int category_code(std::string_view s) { for (int i = 0; i < category_count; i++) if (category_names[i] == s) return i; return -1; }

// Typed edges: low 5 bits type, high bits flags.
enum EdgeType : uint8_t {
    e_native, e_erc20, e_erc721, e_erc1155, e_dex_swap, e_lending_supply, e_lending_borrow, e_lp, e_bridge,
    e_perp, e_stake, e_wrap, e_liquidation, e_other, edge_type_count
};
inline constexpr std::string_view edge_names[edge_type_count] = {
    "native", "erc20", "erc721", "erc1155", "dex_swap", "lending_supply", "lending_borrow", "lp", "bridge",
    "perp", "stake", "wrap", "liquidation", "other"};
inline constexpr uint8_t ef_multi_asset = 0x20, ef_econ = 0x40, et_mask = 0x1f;
inline int edge_code(std::string_view s) {
    if (s == "native_tx" || s == "native") return e_native;
    for (int i = 0; i < edge_type_count; i++) if (edge_names[i] == s) return i;
    return -1;
}
inline int econ_action_edge(std::string_view a) {
    if (a == "swap") return e_dex_swap;
    if (a == "supply" || a == "withdraw") return e_lending_supply;
    if (a == "borrow" || a == "repay") return e_lending_borrow;
    if (a == "liquidate" || a == "liquidated") return e_liquidation;
    if (a == "lp_add" || a == "lp_remove") return e_lp;
    if (a == "bridge_out" || a == "bridge_in") return e_bridge;
    if (a == "perp_increase" || a == "perp_decrease") return e_perp;
    if (a == "stake" || a == "unstake") return e_stake;
    if (a == "wrap" || a == "unwrap") return e_wrap;
    if (a == "flashloan" || a == "other") return e_other;
    return -1;
}

enum Source : uint8_t { s_none, s_classifier, s_registry, s_propagation, s_agent, s_manual, source_count };
inline constexpr std::string_view source_names[source_count] = {"none", "classifier", "registry", "propagation", "agent", "manual"};

// Fine-grained kind (append-only).  Exposed to agents as a hint word and to the UI.
enum Subtype : uint8_t {
    st_none, st_eoa, st_eoa_inferred, st_erc20, st_weth, st_stable, st_vault4626, st_stock, st_lending_token,
    st_erc721, st_erc1155, st_pool_v2, st_pool_v3, st_pool_v4, st_pool_curve, st_pool_balancer, st_pool_other,
    st_router, st_lending_aave, st_lending_compound, st_lending_comet, st_lending_morpho, st_lending_other,
    st_bridge_gateway, st_bridge_oft, st_bridge_cctp, st_bridge_other, st_safe, st_entrypoint, st_perp, st_oracle,
    st_lp_manager, st_proxy_only, st_unknown_topics, st_no_logs, st_system, st_mev_sandwich, st_mev_arb,
    st_mev_operator, st_cex_sweep_hub, st_cex_deposit_sweep, st_factory, st_distributor, st_deployer, st_hub_fan_in,
    st_hub_fan_out, st_mev_contract, subtype_count
};
inline constexpr std::string_view subtype_names[subtype_count] = {
    "", "eoa", "eoa_inferred", "erc20", "weth", "stablecoin", "vault4626", "stock_token", "lending_token",
    "erc721", "erc1155", "pool_v2", "pool_v3", "pool_v4", "pool_curve", "pool_balancer", "pool_other",
    "router", "lending_aave", "lending_compound", "lending_comet", "lending_morpho", "lending_other",
    "bridge_gateway", "bridge_oft", "bridge_cctp", "bridge_other", "safe", "aa_entrypoint", "perp", "oracle",
    "lp_manager", "proxy_only", "unknown_topics", "no_logs_contract", "system", "mev_sandwich", "mev_arb",
    "mev_operator", "cex_sweep_hub", "cex_deposit_sweep", "factory", "distributor", "deployer", "hub_fan_in",
    "hub_fan_out", "mev_contract"};

// Node flag bits (features.flags).
enum : uint32_t {
    nf_created_tx = 1u << 0, nf_emits = 1u << 1, nf_sends = 1u << 2, nf_token20 = 1u << 3, nf_token721 = 1u << 4,
    nf_token1155 = 1u << 5, nf_pool = 1u << 6, nf_venue = 1u << 7, nf_router = 1u << 8, nf_registry = 1u << 9,
    nf_econ_wallet = 1u << 10, nf_econ_core = 1u << 11, nf_native_ovf = 1u << 12, nf_system = 1u << 13,
    nf_contract = 1u << 14, nf_eoa = 1u << 15, nf_deployer = 1u << 16, nf_sandwich = 1u << 17, nf_cyc_arb = 1u << 18,
    nf_stable = 1u << 19, nf_weth = 1u << 20, nf_bridger = 1u << 21, nf_code_inferred = 1u << 22
};

// Emitted-event family bits (features.topics, u64).
enum : uint64_t {
    tf_transfer = 1ull << 0, tf_approval = 1ull << 1, tf_approval_all = 1ull << 2, tf_erc1155 = 1ull << 3,
    tf_swap_v2 = 1ull << 4, tf_sync_v2 = 1ull << 5, tf_mintburn_v2 = 1ull << 6, tf_swap_v3 = 1ull << 7,
    tf_mintburn_v3 = 1ull << 8, tf_swap_v4 = 1ull << 9, tf_swap_curve = 1ull << 10, tf_swap_balancer = 1ull << 11,
    tf_factory = 1ull << 12, tf_lending = 1ull << 13, tf_lending_token = 1ull << 14, tf_safe = 1ull << 15,
    tf_bridge = 1ull << 16, tf_weth = 1ull << 17, tf_vault4626 = 1ull << 18, tf_perp = 1ull << 19,
    tf_proxy = 1ull << 20, tf_owner = 1ull << 21, tf_aa = 1ull << 22, tf_lp_manager = 1ull << 23,
    tf_oracle = 1ull << 24, tf_admin = 1ull << 25, tf_distributor = 1ull << 26, tf_stock = 1ull << 27,
    tf_gmx = 1ull << 28, tf_known_other = 1ull << 30, tf_unknown = 1ull << 31
};
inline constexpr uint64_t tf_swap_any = tf_swap_v2 | tf_swap_v3 | tf_swap_v4 | tf_swap_curve | tf_swap_balancer;
inline constexpr uint64_t tf_pool_any = tf_swap_any | tf_sync_v2 | tf_mintburn_v2 | tf_mintburn_v3;

struct TopicFamily { uint64_t bit; std::string_view word; };
inline constexpr TopicFamily topic_families[] = {
    {tf_transfer, "Transfer"}, {tf_approval, "Approval"}, {tf_approval_all, "ApprovalForAll"}, {tf_erc1155, "ERC1155"},
    {tf_swap_v2, "SwapV2"}, {tf_sync_v2, "SyncV2"}, {tf_mintburn_v2, "LiqV2"}, {tf_swap_v3, "SwapV3"},
    {tf_mintburn_v3, "LiqV3"}, {tf_swap_v4, "SwapV4"}, {tf_swap_curve, "SwapCurve"}, {tf_swap_balancer, "SwapBalancer"},
    {tf_factory, "PoolCreated"}, {tf_lending, "Lending"}, {tf_lending_token, "LendToken"}, {tf_safe, "Safe"},
    {tf_bridge, "Bridge"}, {tf_weth, "WETH"}, {tf_vault4626, "ERC4626"}, {tf_perp, "Perp"}, {tf_proxy, "Proxy"},
    {tf_owner, "Ownable"}, {tf_aa, "ERC4337"}, {tf_lp_manager, "LPManager"}, {tf_oracle, "Oracle"}, {tf_admin, "Admin"},
    {tf_distributor, "Distributor"}, {tf_stock, "StockToken"}, {tf_gmx, "GMXEvent"}, {tf_known_other, "KnownOther"},
    {tf_unknown, "Unknown"}};
inline std::string topic_family_words(uint64_t bits) {
    std::string o;
    for (auto& f : topic_families) if (bits & f.bit) { if (!o.empty()) o += ','; o += f.word; }
    return o;
}

// Built-in event signatures (topic0 = keccak256(signature), computed with CryptoPP Keccak_256).
struct KnownEvent { std::string_view sig; std::string_view topic0_hex; std::string_view name; uint64_t bit; };
inline constexpr KnownEvent known_events[] = {
    {"Transfer(address,address,uint256)", "", "Transfer", tf_transfer},
    {"Approval(address,address,uint256)", "", "Approval", tf_approval},
    {"ApprovalForAll(address,address,bool)", "", "ApprovalForAll", tf_approval_all},
    {"TransferSingle(address,address,address,uint256,uint256)", "", "TransferSingle", tf_erc1155},
    {"TransferBatch(address,address,address,uint256[],uint256[])", "", "TransferBatch", tf_erc1155},
    {"Swap(address,uint256,uint256,uint256,uint256,address)", "", "Swap(V2)", tf_swap_v2},
    {"Swap(address,address,uint256,uint256,uint256,uint256)", "", "Swap(Solidly)", tf_swap_v2},
    {"Sync(uint112,uint112)", "", "Sync(V2)", tf_sync_v2},
    {"Sync(uint256,uint256)", "", "Sync(Solidly)", tf_sync_v2},
    {"Mint(address,uint256,uint256)", "", "Mint(V2)", tf_mintburn_v2},
    {"Burn(address,uint256,uint256,address)", "", "Burn(V2)", tf_mintburn_v2},
    {"Swap(address,address,int256,int256,uint160,uint128,int24)", "", "Swap(V3)", tf_swap_v3},
    {"", "19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83", "Swap(PancakeV3)", tf_swap_v3},
    {"", "121cb44ee54098b1a04743c487e7460d8dd429b27f88b1f4d4767396e1a59f79", "Swap(AlgebraIntegral)", tf_swap_v3},
    {"Mint(address,address,int24,int24,uint128,uint256,uint256)", "", "Mint(V3)", tf_mintburn_v3},
    {"Burn(address,int24,int24,uint128,uint256,uint256)", "", "Burn(V3)", tf_mintburn_v3},
    {"Collect(address,address,int24,int24,uint128,uint128)", "", "Collect(V3)", tf_mintburn_v3},
    {"Initialize(uint160,int24)", "", "Initialize(V3)", tf_mintburn_v3},
    {"Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)", "", "Swap(V4)", tf_swap_v4},
    {"Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)", "", "Initialize(V4)", tf_swap_v4},
    {"ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)", "", "ModifyLiquidity(V4)", tf_swap_v4},
    {"TokenExchange(address,int128,uint256,int128,uint256)", "", "TokenExchange(Curve)", tf_swap_curve},
    {"TokenExchangeUnderlying(address,int128,uint256,int128,uint256)", "", "TokenExchangeUnderlying(Curve)", tf_swap_curve},
    {"TokenExchange(address,uint256,uint256,uint256,uint256)", "", "TokenExchange(CurveCrypto)", tf_swap_curve},
    {"", "143f1f8e861fbdeddd5b46e844b7d3ac7b86a122f36e8c463859ee6811b1f29c", "TokenExchange(TricryptoNG)", tf_swap_curve},
    {"Swap(bytes32,address,address,uint256,uint256)", "", "Swap(BalancerV2)", tf_swap_balancer},
    {"", "0874b2d545cb271cdbda4e093020c452328b24af12382ed62c4d00f5c26709db", "Swap(BalancerV3)", tf_swap_balancer},
    {"PairCreated(address,address,address,uint256)", "", "PairCreated", tf_factory},
    {"PoolCreated(address,address,uint24,int24,address)", "", "PoolCreated(V3)", tf_factory},
    {"Pool(address,address,address)", "", "Pool(AlgebraFactory)", tf_factory},
    {"Supply(address,address,address,uint256,uint16)", "", "Supply(AaveV3)", tf_lending},
    {"Borrow(address,address,address,uint256,uint8,uint256,uint16)", "", "Borrow(AaveV3)", tf_lending},
    {"Repay(address,address,address,uint256,bool)", "", "Repay(AaveV3)", tf_lending},
    {"Withdraw(address,address,address,uint256)", "", "Withdraw(AaveV3)", tf_lending},
    {"LiquidationCall(address,address,address,uint256,uint256,address,bool)", "", "LiquidationCall(Aave)", tf_lending},
    {"FlashLoan(address,address,address,uint256,uint8,uint256,uint16)", "", "FlashLoan(AaveV3)", tf_lending},
    {"ReserveDataUpdated(address,uint256,uint256,uint256,uint256,uint256)", "", "ReserveDataUpdated(Aave)", tf_lending},
    {"Deposit(address,address,address,uint256,uint16)", "", "Deposit(AaveV2)", tf_lending},
    {"Borrow(address,uint256,uint256,uint256)", "", "Borrow(CompoundV2)", tf_lending},
    {"RepayBorrow(address,address,uint256,uint256,uint256)", "", "RepayBorrow(CompoundV2)", tf_lending},
    {"LiquidateBorrow(address,address,uint256,address,uint256)", "", "LiquidateBorrow(CompoundV2)", tf_lending},
    {"Mint(address,uint256,uint256)", "", "Mint(cToken)", tf_mintburn_v2},
    {"Redeem(address,uint256,uint256)", "", "Redeem(cToken)", tf_lending},
    {"Supply(address,address,uint256)", "", "Supply(Comet)", tf_lending},
    {"Withdraw(address,address,uint256)", "", "Withdraw(Comet)", tf_lending},
    {"SupplyCollateral(address,address,address,uint256)", "", "SupplyCollateral(Comet)", tf_lending},
    {"WithdrawCollateral(address,address,address,uint256)", "", "WithdrawCollateral(Comet)", tf_lending},
    {"AbsorbDebt(address,address,uint256,uint256)", "", "AbsorbDebt(Comet)", tf_lending},
    {"Supply(bytes32,address,address,uint256,uint256)", "", "Supply(MorphoBlue)", tf_lending},
    {"Borrow(bytes32,address,address,address,uint256,uint256)", "", "Borrow(MorphoBlue)", tf_lending},
    {"Mint(address,address,uint256,uint256,uint256)", "", "Mint(aToken)", tf_lending_token},
    {"Burn(address,address,address,uint256,uint256,uint256)", "", "Burn(aToken)", tf_lending_token},
    {"BalanceTransfer(address,address,uint256,uint256)", "", "BalanceTransfer(aToken)", tf_lending_token},
    {"ExecutionSuccess(bytes32,uint256)", "", "ExecutionSuccess(Safe)", tf_safe},
    {"ExecutionFailure(bytes32,uint256)", "", "ExecutionFailure(Safe)", tf_safe},
    {"SafeSetup(address,address[],uint256,address,address)", "", "SafeSetup", tf_safe},
    {"SafeReceived(address,uint256)", "", "SafeReceived", tf_safe},
    {"AddedOwner(address)", "", "AddedOwner(Safe)", tf_safe},
    {"ChangedThreshold(uint256)", "", "ChangedThreshold(Safe)", tf_safe},
    {"ExecutionFromModuleSuccess(address)", "", "ExecutionFromModuleSuccess(Safe)", tf_safe},
    {"DepositInitiated(address,address,address,uint256,uint256)", "", "DepositInitiated(ArbGateway)", tf_bridge},
    {"WithdrawalInitiated(address,address,address,uint256,uint256,uint256)", "", "WithdrawalInitiated(ArbGateway)", tf_bridge},
    {"DepositFinalized(address,address,address,uint256)", "", "DepositFinalized(ArbGateway)", tf_bridge},
    {"WithdrawalFinalized(address,address,address,uint256,uint256)", "", "WithdrawalFinalized(ArbGateway)", tf_bridge},
    {"L2ToL1Tx(address,address,uint256,uint256,uint256,uint256,uint256,uint256,bytes)", "", "L2ToL1Tx(ArbSys)", tf_bridge},
    {"OFTSent(bytes32,uint32,address,uint256,uint256)", "", "OFTSent", tf_bridge},
    {"OFTReceived(bytes32,uint32,address,uint256)", "", "OFTReceived", tf_bridge},
    {"DepositForBurn(uint64,address,uint256,address,bytes32,uint32,bytes32,bytes32)", "", "DepositForBurn(CCTP)", tf_bridge},
    {"MintAndWithdraw(address,uint256,address)", "", "MintAndWithdraw(CCTP)", tf_bridge},
    {"MessageDelivered(uint256,bytes32,address,uint8,address,bytes32,uint256,uint64)", "", "MessageDelivered(ArbBridge)", tf_bridge},
    {"Deposit(address,uint256)", "", "Deposit(WETH)", tf_weth},
    {"Withdrawal(address,uint256)", "", "Withdrawal(WETH)", tf_weth},
    {"Deposit(address,address,uint256,uint256)", "", "Deposit(ERC4626)", tf_vault4626},
    {"Withdraw(address,address,address,uint256,uint256)", "", "Withdraw(ERC4626)", tf_vault4626},
    {"Upgraded(address)", "", "Upgraded(proxy)", tf_proxy},
    {"AdminChanged(address,address)", "", "AdminChanged(proxy)", tf_proxy},
    {"BeaconUpgraded(address)", "", "BeaconUpgraded(proxy)", tf_proxy},
    {"OwnershipTransferred(address,address)", "", "OwnershipTransferred", tf_owner},
    {"Initialized(uint8)", "", "Initialized(uint8)", tf_admin},
    {"Initialized(uint64)", "", "Initialized(uint64)", tf_admin},
    {"Paused(address)", "", "Paused", tf_admin},
    {"Unpaused(address)", "", "Unpaused", tf_admin},
    {"RoleGranted(bytes32,address,address)", "", "RoleGranted", tf_admin},
    {"RoleRevoked(bytes32,address,address)", "", "RoleRevoked", tf_admin},
    {"UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)", "", "UserOperationEvent(ERC4337)", tf_aa},
    {"AccountDeployed(bytes32,address,address,address)", "", "AccountDeployed(ERC4337)", tf_aa},
    {"IncreaseLiquidity(uint256,uint128,uint256,uint256)", "", "IncreaseLiquidity(NPM)", tf_lp_manager},
    {"DecreaseLiquidity(uint256,uint128,uint256,uint256)", "", "DecreaseLiquidity(NPM)", tf_lp_manager},
    {"Collect(uint256,address,uint256,uint256)", "", "Collect(NPM)", tf_lp_manager},
    {"AnswerUpdated(int256,uint256,uint256)", "", "AnswerUpdated(Chainlink)", tf_oracle},
    {"NewTransmission(uint32,int192,address,uint32,int192[],bytes,int192,bytes32,uint40)", "", "NewTransmission(OCR)", tf_oracle},
    {"", "137a44067c8961cd7e1d876f4754a5a3a75989b4552f1843fc69c3b372def160", "EventLog1(GMXv2)", tf_gmx},
    {"", "468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5", "EventLog2(GMXv2)", tf_gmx},
    {"", "7e3bde2ba7aca4a8499608ca57f3b0c1c1c93ace63ffd3741a9fab204146fc9a", "EventLog(GMXv2)", tf_gmx},
    {"Claimed(address,address,uint256)", "", "Claimed(Merkl)", tf_distributor},
};

// econ/protocols events.csv / contracts.csv family -> topic bit, category and subtype.
inline uint64_t family_topic_bit(std::string_view f) {
    if (f == "erc20") return tf_transfer;
    if (f == "weth") return tf_weth;
    if (f == "univ2") return tf_swap_v2;
    if (f == "univ3" || f == "pancake_v3" || f == "algebra") return tf_swap_v3;
    if (f == "univ4") return tf_swap_v4;
    if (f == "univ3_npm" || f == "algebra_npm") return tf_lp_manager;
    if (f == "aave_v3" || f == "aave_v2" || f == "comet" || f == "compound_v2" || f == "morpho_blue" || f == "silo_v1" || f == "silo_v2" || f == "dolomite" || f == "radiant") return tf_lending;
    if (f == "aave_v3_token") return tf_lending_token;
    if (f == "gmx_v1" || f == "gmx_v2") return tf_perp;
    if (f == "across" || f == "relay" || f == "hop" || f == "oft" || f == "cctp_v1" || f == "cctp_v2" || f == "arb_gateway" || f == "arbsys" || f == "stargate") return tf_bridge;
    if (f == "erc4626" || f == "metamorpho") return tf_vault4626;
    if (f == "chainlink") return tf_oracle;
    if (f == "merkl") return tf_distributor;
    if (f == "robinhood_stock") return tf_stock;
    return tf_known_other;
}
struct FamilyClass { uint8_t category; uint8_t subtype; };
inline FamilyClass family_class(std::string_view family, std::string_view role) {
    auto has = [&](std::string_view s) { return role.find(s) != std::string_view::npos; };
    if (family == "aave_v3" || family == "aave_v2" || family == "radiant") return {lending, st_lending_aave};
    if (family == "comet") return {lending, st_lending_comet};
    if (family == "compound_v2") return {lending, st_lending_compound};
    if (family == "morpho_blue" || family == "metamorpho") return {lending, st_lending_morpho};
    if (family == "silo_v1" || family == "silo_v2" || family == "dolomite") return {lending, st_lending_other};
    if (family == "univ2" || family == "univ3" || family == "univ4" || family == "pancake_v3" || family == "algebra" || family == "camelot" || family == "sushi") {
        if (has("router") || has("universal")) return {dex_router, st_router};
        if (has("factory")) return {contract_other, st_factory};
        if (has("pool_manager") || has("poolmanager")) return {dex_pool, st_pool_v4};
        if (has("npm") || has("position")) return {contract_other, st_lp_manager};
        if (has("quoter")) return {contract_other, st_none};
        return {dex_pool, family == "univ2" ? st_pool_v2 : st_pool_v3};
    }
    if (family == "univ3_npm" || family == "algebra_npm") return {contract_other, st_lp_manager};
    if (family == "gmx_v1" || family == "gmx_v2") return {contract_other, st_perp};
    if (family == "arb_gateway" || family == "arbsys") return {bridge, st_bridge_gateway};
    if (family == "oft" || family == "stargate") return {bridge, st_bridge_oft};
    if (family == "cctp_v1" || family == "cctp_v2") return {bridge, st_bridge_cctp};
    if (family == "across" || family == "relay" || family == "hop") return {bridge, st_bridge_other};
    if (family == "weth") return {token_contract, st_weth};
    if (family == "stable") return has("token") ? FamilyClass{token_contract, st_stable} : FamilyClass{contract_other, st_none};
    if (family == "erc4626") return {token_contract, st_vault4626};
    if (family == "robinhood_stock") return {token_contract, st_stock};
    if (family == "chainlink") return {contract_other, st_oracle};
    if (family == "merkl") return {contract_other, st_distributor};
    if (family == "aggregator" || has("router")) return {dex_router, st_router};
    return {contract_other, st_none};
}

// Protocol-discovery families/actions (dispatcher protocol.py FAMILIES / ACTIONS, same order).
inline constexpr std::string_view protocol_families[] = {
    "lending", "dex_amm", "dex_lp_manager", "perp", "bridge", "staking_lsd", "vault_yield", "token_wrapper",
    "oracle", "nft", "governance", "other", "unknown"};
inline constexpr size_t protocol_family_count = std::size(protocol_families);
inline constexpr std::string_view protocol_actions[] = {
    "supply", "withdraw", "borrow", "repay", "liquidate", "lp_add", "lp_remove", "swap", "bridge_out", "bridge_in",
    "perp_increase", "perp_decrease", "stake", "unstake", "wrap", "unwrap", "other"};
inline constexpr size_t protocol_action_count = std::size(protocol_actions);
inline FamilyClass protocol_family_class(size_t f) {
    switch (f) {
    case 0: return {lending, st_lending_other};
    case 1: return {dex_pool, st_pool_other};
    case 2: return {contract_other, st_lp_manager};
    case 3: return {contract_other, st_perp};
    case 4: return {bridge, st_bridge_other};
    case 6: return {token_contract, st_vault4626};
    case 7: return {token_contract, st_weth};
    case 8: return {contract_other, st_oracle};
    case 9: return {nft_contract, st_erc721};
    default: return {contract_other, st_none};
    }
}
} // namespace machine::graphloop
