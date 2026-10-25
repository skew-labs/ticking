#pragma once
// Deterministic, code-first classifier.  Every node gets a base label {category, confidence,
// source, subtype, tier, rule}.  Rules use only graph facts (emitted topic families, token/pool/
// router registries, CSR concentration, MEV scans, sweep fan-in).  Nodes whose confidence stays
// below `resolve` are "residual"; only residual nodes with importance >= the LLM threshold reach
// the agent frontier.  Same inputs -> byte-identical output (no clocks, no randomness, no races:
// atomic counters only commute).
#include "graph.hpp"
#include "util.hpp"

namespace machine::graphloop {

struct ClassifyParams {
    double resolve = 0.50;            // conf >= resolve => resolved without an LLM
    double deposit_share = 0.80;      // cex_deposit: top-2 outflow share (validator X)
    uint32_t deposit_max_sent = 500;  // deposit addresses send few txs
    uint32_t deposit_max_out = 2;     // ... to <= 2 distinct counterparties
    uint32_t sweep_min = 25;          // hub needs >= this many deposit-like senders
    uint32_t hot_min_outdeg = 50, hot_min_sent = 100;
    uint32_t mev_sandwich_min = 5, mev_arb_min = 50, mev_min_activity = 100;
    double mev_fail_ratio = 0.15;
    double whale_eth = 1000, whale_usd = 2.5e6;
    uint32_t hub_degree = 1000, hub_degree_any = 10000;   // fan-out >= hub_degree_any: hub regardless of tx count
    uint32_t dex_trader_min = 1000; double dex_trader_share = 0.5;
};

enum Rule : uint8_t {
    r_none, r_registry, r_system, r_pool, r_pool_v2_nosync, r_venue, r_lending, r_bridge, r_safe, r_nft, r_token,
    r_token_weak, r_aa, r_perp, r_lp_manager, r_oracle, r_factory, r_router, r_mev_contract, r_proxy_only,
    r_unknown_topics, r_known_other, r_contract_nologs, r_mev_sandwich, r_mev_arb, r_mev_operator, r_whale,
    r_hub_out, r_hub_in, r_eoa, r_eoa_busy, r_recv_only, r_recv_hub, r_cex_hot, r_cex_deposit, r_mev_link, r_distributor,
    r_mev_dex_trader, rule_count
};
inline constexpr std::string_view rule_names[rule_count] = {
    "none", "registry", "system", "pool_events", "pool_v2_no_sync", "dex_venue", "lending_events", "bridge_events",
    "safe_events", "nft_events", "token_events", "token_transfer_only", "erc4337", "perp_events", "lp_manager", "oracle",
    "factory", "router_trades", "mev_contract", "proxy_only", "unknown_topics", "known_other_topics", "contract_no_logs",
    "mev_sandwich", "mev_cyclic_arb", "mev_operator", "whale_volume", "hub_fan_out", "hub_fan_in", "eoa_default",
    "eoa_busy", "receive_only", "receive_only_hub", "cex_sweep_hub", "cex_deposit_sweep", "mev_link", "distributor", "mev_dex_trader"};

inline uint8_t usd_tier(double usd) {
    return usd < 1e4 ? 0 : usd < 1e5 ? 1 : usd < 1e6 ? 2 : usd < 1e7 ? 3 : usd < 1e8 ? 4 : 5;
}
inline float importance(const Graph& g, uint32_t v, uint32_t sweep) {
    double act = double(g.u32[d_tx_sent][v]) + g.u32[d_tx_recv][v] + g.u32[d_tt_out][v] + g.u32[d_tt_in][v];
    double imp = std::log1p(act) + std::log1p(double(g.u32[d_cp][v])) + 0.5 * std::log1p(double(g.f32[d_usd][v]) / 100.0) + 0.5 * std::log1p(double(sweep));
    return float(imp);
}
inline bool is_infra(const Graph& g, uint32_t v) {   // shared infrastructure: never a deposit sweep target / mev contract
    auto f = g.u32[d_flags][v];
    return f & (nf_pool | nf_venue | nf_router | nf_token20 | nf_token721 | nf_token1155 | nf_registry | nf_system);
}
// cex_deposit structural test (also the validator's rule): EOA-like, few sends, >= share of its
// outflow to <= 2 counterparties, receives from someone other than its sweep target.
inline bool deposit_like(const Graph& g, uint32_t u, const ClassifyParams& p) {
    if (g.is_contract(u)) return false;
    uint32_t sent = g.u32[d_tx_sent][u], recv = g.u32[d_tx_recv][u], h = g.u32[d_top1o][u];
    if (sent < 1 || sent > p.deposit_max_sent || h == none || h == u) return false;
    if (g.u32[d_outdeg][u] > p.deposit_max_out) return false;
    if (share_of(g.u16[d_top2o_sh][u]) < p.deposit_share) return false;
    if (double(sent) > std::max(500.0, 2.0 * recv)) return false;
    if (g.u32[d_in_n][u] == 0 || (g.u32[d_top1i][u] == h && share_of(g.u16[d_top1i_sh][u]) > 0.999)) return false;
    return !is_infra(g, h);
}

struct ClassifyResult {
    std::vector<BaseLabel> lab; std::vector<float> imp; std::vector<uint32_t> sweep; std::vector<SparseEntry> extra; json stats;
};

inline BaseLabel make(uint8_t cat, double conf, uint8_t src, uint8_t sub, Rule r) { return {cat, conf8(conf), src, sub, 0, uint8_t(r), 0, 0}; }

// Per-node rules that only look at the node itself (pass 1).
inline BaseLabel classify_node(const Graph& g, uint32_t v, const ClassifyParams& p) {
    const uint32_t f = g.u32[d_flags][v];
    if (f & nf_system) return make(contract_other, 0.99, s_classifier, st_system, r_system);
    const uint64_t T = g.topics_bits(v);
    const uint32_t sent = g.u32[d_tx_sent][v], recv = g.u32[d_tx_recv][v];
    const uint32_t sandwich = g.sparse(v, k_sandwich), arb = g.sparse(v, k_arb);
    if (f & nf_contract) {
        if ((T & tf_swap_any) || (f & nf_pool)) {
            if ((T & tf_swap_v2) && !(T & tf_sync_v2) && !(T & (tf_swap_v3 | tf_swap_v4 | tf_swap_curve | tf_swap_balancer)) && !(f & nf_pool))
                return make(contract_other, 0.40, s_classifier, st_pool_other, r_pool_v2_nosync);
            uint8_t sub = (T & tf_swap_v4) ? st_pool_v4 : (T & tf_swap_curve) ? st_pool_curve : (T & tf_swap_balancer) ? st_pool_balancer
                        : (T & tf_swap_v3) ? st_pool_v3 : (T & tf_swap_v2) ? st_pool_v2 : st_pool_other;
            return make(dex_pool, (T & tf_swap_any) ? 0.95 : 0.80, s_classifier, sub, r_pool);
        }
        if (f & nf_venue) return make(dex_pool, 0.85, s_classifier, st_pool_v4, r_venue);
        if (T & tf_lending) return make(lending, 0.90, s_classifier, st_lending_other, r_lending);
        if (T & tf_bridge) return make(bridge, 0.85, s_classifier, st_bridge_other, r_bridge);
        if (T & tf_safe) return make(multisig_or_safe, 0.95, s_classifier, st_safe, r_safe);
        if ((T & tf_erc1155) || ((f & nf_token721) && (T & tf_transfer))) return make(nft_contract, 0.95, s_classifier, (T & tf_erc1155) ? st_erc1155 : st_erc721, r_nft);
        if ((T & tf_transfer) && ((f & nf_token20) || (T & tf_approval))) {
            uint8_t sub = (f & nf_weth) || (T & tf_weth) ? st_weth : (f & nf_stable) ? st_stable : (T & tf_vault4626) ? st_vault4626
                        : (T & tf_lending_token) ? st_lending_token : (T & tf_stock) ? st_stock : st_erc20;
            return make(token_contract, 0.95, s_classifier, sub, r_token);
        }
        if (T & tf_transfer) return make(token_contract, 0.70, s_classifier, st_erc20, r_token_weak);
        if (T & tf_aa) return make(contract_other, 0.90, s_classifier, st_entrypoint, r_aa);
        if (T & (tf_perp | tf_gmx)) return make(contract_other, 0.80, s_classifier, st_perp, r_perp);
        if (T & tf_lp_manager) return make(contract_other, 0.85, s_classifier, st_lp_manager, r_lp_manager);
        if (T & tf_oracle) return make(contract_other, 0.85, s_classifier, st_oracle, r_oracle);
        if (T & tf_factory) return make(contract_other, 0.85, s_classifier, st_factory, r_factory);
        if (T & tf_distributor) return make(contract_other, 0.75, s_classifier, st_distributor, r_distributor);
        if (f & nf_router) return make(dex_router, 0.90, s_classifier, st_router, r_router);
        if ((sandwich >= p.mev_sandwich_min || arb >= p.mev_arb_min) && recv >= p.mev_min_activity && g.u32[d_indeg][v] <= 10)
            return make(mev_bot, 0.85, s_classifier, st_mev_contract, r_mev_contract);
        const uint64_t admin = tf_proxy | tf_owner | tf_admin;
        if (T && !(T & ~admin)) return make(contract_other, 0.45, s_classifier, st_proxy_only, r_proxy_only);
        if (T & tf_unknown) return make(contract_other, 0.35, s_classifier, st_unknown_topics, r_unknown_topics);
        if (T & tf_known_other) return make(contract_other, 0.50, s_classifier, st_unknown_topics, r_known_other);
        return make(contract_other, recv >= 100 ? 0.40 : 0.55, s_classifier, st_no_logs, r_contract_nologs);
    }
    const double eth = double(g.f32[d_eth_in][v]) + g.f32[d_eth_out][v], usd = g.f32[d_usd][v];
    if (f & nf_eoa) {
        if (sandwich >= p.mev_sandwich_min && sent >= p.mev_min_activity) return make(mev_bot, 0.90, s_classifier, st_mev_sandwich, r_mev_sandwich);
        if (arb >= p.mev_arb_min && sent >= p.mev_min_activity) return make(mev_bot, 0.85, s_classifier, st_mev_arb, r_mev_arb);
        uint32_t h = g.u32[d_top1o][v];
        if (sent >= 1000 && double(g.u32[d_tx_fail][v]) >= p.mev_fail_ratio * sent && share_of(g.u16[d_top1o_sh][v]) >= 0.7 && h != none
            && g.is_contract(h) && !is_infra(g, h))
            return make(mev_bot, 0.70, s_classifier, st_mev_operator, r_mev_operator);
        // high-frequency DEX trader: >= 1k swaps making up >= 50 % of its sends (LLM-confirmed pattern on Robinhood)
        uint32_t dex = g.sparse(v, k_dex);
        if (dex >= p.dex_trader_min && double(dex) >= p.dex_trader_share * sent && sent >= p.mev_min_activity)
            return make(mev_bot, 0.60, s_classifier, st_mev_arb, r_mev_dex_trader);
        bool hub_out = (g.u32[d_outdeg][v] >= p.hub_degree && sent >= 5 * p.hub_degree) || g.u32[d_outdeg][v] >= p.hub_degree_any;
        if (hub_out) return make(eoa_user, 0.35, s_classifier, st_hub_fan_out, r_hub_out);
        if (eth >= p.whale_eth || usd >= p.whale_usd) return make(eoa_whale, 0.75, s_classifier, st_eoa, r_whale);
        if (g.u32[d_indeg][v] >= p.hub_degree) return make(eoa_user, 0.40, s_classifier, st_hub_fan_in, r_hub_in);
        if (sent > 20000) return make(eoa_user, 0.45, s_classifier, st_eoa, r_eoa_busy);
        return make(eoa_user, sent <= 2000 ? 0.75 : 0.60, s_classifier, st_eoa, r_eoa);
    }
    if (g.u32[d_indeg][v] >= p.hub_degree) return make(unknown, 0.20, s_classifier, st_hub_fan_in, r_recv_hub);
    if (eth >= p.whale_eth || usd >= p.whale_usd) return make(eoa_whale, 0.45, s_classifier, st_eoa_inferred, r_whale);
    return make(eoa_user, 0.55, s_classifier, st_eoa_inferred, r_recv_only);
}

// preset: registry labels (source == s_registry) win over every rule.
inline ClassifyResult classify(const Graph& g, const ClassifyParams& p, const std::vector<BaseLabel>& preset, int threads, const std::vector<int>* cpus) {
    ClassifyResult R; const uint32_t n = g.n;
    R.lab.resize(n); R.imp.resize(n); R.sweep.assign(n, 0);
    std::vector<std::atomic<uint32_t>> sweep(n);
    // pass 1: node-local rules + deposit-like fan-in counting
    parallel_for(n, threads, [&](size_t b, size_t e, int) {
        for (size_t v = b; v < e; v++) {
            R.lab[v] = preset[v].source == s_registry ? preset[v] : classify_node(g, uint32_t(v), p);
            if (deposit_like(g, uint32_t(v), p)) sweep[g.u32[d_top1o][v]].fetch_add(1, std::memory_order_relaxed);
        }
    }, 1 << 15, cpus);
    for (uint32_t v = 0; v < n; v++) R.sweep[v] = sweep[v].load(std::memory_order_relaxed);
    // pass 2: sweep hubs -> cex_hot_wallet (EOA side; contracts keep their label)
    std::vector<uint8_t> hot(n, 0);
    parallel_for(n, threads, [&](size_t b, size_t e, int) {
        for (size_t v = b; v < e; v++) {
            uint32_t s = R.sweep[v];
            if (s < p.sweep_min || g.is_contract(uint32_t(v)) || R.lab[v].source == s_registry) continue;
            if (g.u32[d_outdeg][v] < p.hot_min_outdeg || g.u32[d_tx_sent][v] < p.hot_min_sent) continue;
            double conf = std::min(0.85, 0.45 + 0.1 * std::log10(double(s)));
            R.lab[v] = make(cex_hot_wallet, conf, s_classifier, st_cex_sweep_hub, r_cex_hot);
            hot[v] = 1;
        }
    }, 1 << 15, cpus);
    // pass 3: deposits sweeping into a hot wallet; MEV operator <-> contract links
    parallel_for(n, threads, [&](size_t b, size_t e, int) {
        for (size_t v = b; v < e; v++) {
            if (R.lab[v].source == s_registry) continue;
            uint32_t h = g.u32[d_top1o][v];
            if (h != none && hot[h] && deposit_like(g, uint32_t(v), p)) {
                double conf = conf_of(R.lab[h].conf) * 0.85 * std::min(1.0, share_of(g.u16[d_top2o_sh][v]) / 0.9);
                if (conf > conf_of(R.lab[v].conf) || R.lab[v].category == eoa_user) R.lab[v] = make(cex_deposit, std::max(conf, 0.5), s_classifier, st_cex_deposit_sweep, r_cex_deposit);
                continue;
            }
            // EOA whose outflow goes to a contract that the MEV scan flagged
            if (g.is_eoa(uint32_t(v)) && h != none && R.lab[v].category != mev_bot && g.u32[d_tx_sent][v] >= p.mev_min_activity
                && share_of(g.u16[d_top1o_sh][v]) >= 0.8 && R.lab[h].category == mev_bot && R.lab[h].rule == r_mev_contract)
                R.lab[v] = make(mev_bot, 0.80, s_classifier, st_mev_operator, r_mev_link);
        }
    }, 1 << 15, cpus);
    // pass 4 (sequential, writes other nodes): MEV operator EOA -> its private bot contract
    for (uint32_t v = 0; v < n; v++) {
        if (R.lab[v].category != mev_bot || g.is_contract(v)) continue;
        uint32_t h = g.u32[d_top1o][v];
        if (h == none || !g.is_contract(h) || is_infra(g, h) || R.lab[h].source == s_registry || R.lab[h].category == mev_bot) continue;
        if (g.u32[d_indeg][h] > 10 || share_of(g.u16[d_top1o_sh][v]) < 0.7 || R.lab[h].category == dex_pool || R.lab[h].category == token_contract) continue;
        R.lab[h] = make(mev_bot, 0.75, s_classifier, st_mev_contract, r_mev_link);
    }
    // importance, tier, extra sparse values
    parallel_for(n, threads, [&](size_t b, size_t e, int) {
        for (size_t v = b; v < e; v++) {
            R.imp[v] = importance(g, uint32_t(v), R.sweep[v]);
            R.lab[v].tier = usd_tier(g.f32[d_usd][v]);
        }
    }, 1 << 15, cpus);
    for (uint32_t v = 0; v < n; v++) if (R.sweep[v]) R.extra.push_back({v, R.sweep[v], k_sweep, {}});
    // stats
    std::array<uint64_t, category_count> by_cat{}; std::array<uint64_t, rule_count> by_rule{}; uint64_t resolved = 0;
    std::array<uint64_t, category_count> res_cat{};
    for (uint32_t v = 0; v < n; v++) {
        by_cat[R.lab[v].category]++; by_rule[R.lab[v].rule]++;
        if (conf_of(R.lab[v].conf) >= p.resolve) { resolved++; res_cat[R.lab[v].category]++; }
    }
    json cats = json::object(), rules = json::object();
    for (int c = 0; c < category_count; c++) cats[std::string(category_names[c])] = {{"nodes", by_cat[c]}, {"resolved", res_cat[c]}};
    for (int r = 0; r < rule_count; r++) if (by_rule[r]) rules[std::string(rule_names[r])] = by_rule[r];
    R.stats = {{"nodes", n}, {"resolved", resolved}, {"resolved_pct", n ? 100.0 * double(resolved) / n : 0.0}, {"resolve_threshold", p.resolve},
               {"by_category", cats}, {"by_rule", rules}};
    return R;
}
} // namespace machine::graphloop
