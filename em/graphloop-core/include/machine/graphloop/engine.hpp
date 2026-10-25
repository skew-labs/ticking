#pragma once
// Graph-loop engine: immutable graph + mutable label overlay + frontier + batches + journal.
//   label(v) = overlay[v] if present else base[v]          (overlay: lazily-backed 8 B/node)
//   agent commits -> overlay (src=agent) -> WAL -> local propagation -> neighbours re-enqueued
// Packs shown to models are aliased per batch: S<k> subjects, A<i> EOAs/receive-only, X<i>
// contracts, T<i> tokens, E<i> event topics.  The alias map stays here; proposals are de-aliased,
// validated against the exact pack facts and committed (or rejected with reasons).
#include "classify.hpp"
#include "frontier.hpp"
#include "validate.hpp"
#include "wal.hpp"
#include <condition_variable>
#include <deque>
#include <map>
#include <memory>
#include <thread>
#include <shared_mutex>
#include <unordered_set>

namespace machine::graphloop {

inline constexpr std::string_view prompt_version = "em-graphloop-v1";
enum TaskType : uint8_t { t_address = 0, t_protocol = 1 };
inline constexpr std::string_view task_names[2] = {"address_label", "protocol_discovery"};
enum LabelState : uint8_t { ls_base = 0, ls_candidate = 1, ls_committed = 2, ls_disputed = 3 };
inline constexpr std::string_view state_names[4] = {"base", "candidate", "committed", "disputed"};

struct Label { uint8_t cat = unknown, conf = 0, src = s_none, sub = 0, tier = 0, state = 0, agree = 0, present = 0; };
static_assert(sizeof(Label) == 8);
inline uint64_t pack_label(const Label& l) { uint64_t x; std::memcpy(&x, static_cast<const void*>(&l), 8); return x; }
inline Label unpack_label(uint64_t x) { Label l; std::memcpy(static_cast<void*>(&l), &x, 8); return l; }

struct Detail {
    std::vector<std::pair<std::string, std::string>> evidence; std::string entity, provider, model, warnings;
    uint64_t batch = 0, ts = 0, seq = 0; uint8_t task_type = 0;
};
struct ProtoMap { std::string signature; uint8_t action = 0, family = 0, conf = 0; std::string wallet, asset, amount; Addr contract{}; std::string provider, model; };

struct EngineParams {
    double resolve = 0.50, llm_quantile = 0.99, confirm_quantile = 0.999, proto_quantile = 0.995;
    float llm_imp_min = -1, confirm_imp_min = -1, proto_imp_min = -1;   // < 0: derived from quantiles
    uint32_t ttl_s = 600, max_batch = 32, max_attempts = 3, neighbor_requeue = 32, hub_limit = 20000, path_budget = 20000, path_fanout = 128, path_fanout_end = 1024;
    double decay = 0.9, prop_min = 0.30;
    uint32_t snapshot_every = 200000, snapshot_secs = 600;
};

struct Fact { std::string key, val, real; };
struct TaskPack {
    uint32_t node = none; uint64_t task_id = 0; std::vector<Fact> facts; SubjectFacts sf; Hash hash{}; uint64_t hash64 = 0;
    std::vector<uint32_t> refs;            // alias keys referenced (cls<<32|id packed into 64 -> stored as indices)
    std::vector<uint64_t> ref_keys;
    std::vector<std::pair<std::string, uint32_t>> topic_aliases;   // E<i> -> topic id (protocol tasks)
};
struct Alias {
    static constexpr char cls_char[4] = {'A', 'X', 'T', 'E'};
    std::vector<uint32_t> list[4]; std::unordered_map<uint64_t, uint32_t> idx;
    std::string name(int c, uint32_t id) {
        uint64_t k = (uint64_t(c) << 32) | id; auto it = idx.find(k);
        uint32_t i = it != idx.end() ? it->second : (idx[k] = uint32_t(list[c].size()), list[c].push_back(id), uint32_t(list[c].size() - 1));
        return std::string(1, cls_char[c]) + std::to_string(i + 1);
    }
    bool resolve(std::string_view s, int& c, uint32_t& id) const {
        if (s.size() < 2) return false;
        for (c = 0; c < 4; c++) if (s[0] == cls_char[c]) break;
        if (c == 4) return false;
        uint32_t i = 0; for (size_t k = 1; k < s.size(); k++) { if (s[k] < '0' || s[k] > '9') return false; i = i * 10 + uint32_t(s[k] - '0'); if (i > 1000000) return false; }
        if (i == 0 || i > list[c].size()) return false;
        id = list[c][i - 1]; return true;
    }
};
struct Batch {
    uint64_t id = 0; uint8_t type = 0; uint32_t lease_until = 0; uint64_t created_ms = 0; std::string agent;
    std::vector<TaskPack> tasks; Alias alias; std::string user; std::mutex mu; bool closed = false;
    int subject_of(uint32_t v) const { for (size_t i = 0; i < tasks.size(); i++) if (tasks[i].node == v) return int(i); return -1; }
};

struct ProviderStats { uint64_t proposals = 0, tasks = 0, accepted = 0, rejected = 0, applied = 0, missing = 0, tok_in = 0, tok_out = 0, tok_cached = 0; };

class Engine {
public:
    const Graph& g; ClassifyParams cp; ValidateParams vp; EngineParams ep;
    std::string context_sha;
    Frontier fr[2];
    Wal* wal = nullptr;
    fs::path state_dir;
private:
    LazyArray<uint64_t> ov_;
    mutable std::mutex commit_mu_, stats_mu_, proto_mu_; mutable std::shared_mutex det_mu_;
    std::unordered_map<uint32_t, Detail> det_;
    std::unordered_map<uint32_t, std::pair<std::string, uint8_t>> ent_ov_;   // propagated entity candidates
    std::unordered_map<uint32_t, uint8_t> attempts_;
    std::map<Hash, ProtoMap> proto_;                                         // learned topic0 mappings
    std::map<std::string, ProviderStats> prov_;
    std::map<std::string, uint64_t> reasons_;
    static constexpr size_t batch_shards = 16;
    struct BShard { std::mutex mu; std::unordered_map<uint64_t, std::shared_ptr<Batch>> m; };
    std::array<BShard, batch_shards> bs_;
    std::atomic<uint64_t> next_batch_{1};
    std::array<std::atomic<uint32_t>, 4096> commit_ring_{};   // commits per second (ring by unix second)
    std::array<std::atomic<uint32_t>, 4096> commit_ring_sec_{};
    std::atomic<uint64_t> commits_{0}, proposals_{0}, leased_tasks_{0}, applied_since_snap_{0};
    std::atomic<uint64_t> tool_calls_{0}, prop_changes_{0};
    std::thread prop_th_; std::mutex pq_mu_; std::condition_variable pq_cv_; std::deque<uint32_t> pq_; bool pq_stop_ = false;
public:
    bool async_propagation = false, replaying = false;
    std::atomic<uint64_t> last_snapshot_seq{0}, last_snapshot_ms{0};

    Engine(const Graph& graph, ClassifyParams c, EngineParams e) : g(graph), cp(c), ep(e), ov_(graph.n) {
        vp.deposit_share = cp.deposit_share;
        if (ep.llm_imp_min < 0 || ep.confirm_imp_min < 0 || ep.proto_imp_min < 0) {
            std::vector<float> s; s.reserve(std::min<size_t>(g.n, 1000000));
            uint64_t step = std::max<uint64_t>(1, g.n / 1000000);
            for (uint64_t v = 0; v < g.n; v += step) s.push_back(g.f32[d_imp][v]);
            std::sort(s.begin(), s.end());
            auto q = [&](double p) { return s.empty() ? 0.f : s[std::min(s.size() - 1, size_t(p * double(s.size())))]; };
            if (ep.llm_imp_min < 0) ep.llm_imp_min = q(ep.llm_quantile);
            if (ep.confirm_imp_min < 0) ep.confirm_imp_min = q(ep.confirm_quantile);
            if (ep.proto_imp_min < 0) ep.proto_imp_min = q(ep.proto_quantile);
        }
    }

    // ------------------------------------------------------------ labels
    Label label(uint32_t v) const {
        uint64_t x = __atomic_load_n(&ov_[v], __ATOMIC_ACQUIRE);
        if (x) { Label l = unpack_label(x); if (l.present) return l; }
        const auto& b = g.base[v];
        return Label{b.category, b.conf, b.source, b.subtype, b.tier, ls_base, 0, 0};
    }
    void set_label(uint32_t v, Label l) { l.present = 1; __atomic_store_n(&ov_[v], pack_label(l), __ATOMIC_RELEASE); }
    uint64_t raw(uint32_t v) const { return __atomic_load_n(&ov_[v], __ATOMIC_ACQUIRE); }
    // Propagation writes only if nobody (an agent commit) changed the word since it was read.
    bool cas_label(uint32_t v, uint64_t expect, Label l) { l.present = 1; uint64_t want = pack_label(l); return __atomic_compare_exchange_n(&ov_[v], &expect, want, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE); }
    void clear_label(uint32_t v) { __atomic_store_n(&ov_[v], uint64_t(0), __ATOMIC_RELEASE); }
    std::string entity(uint32_t v) const {
        { std::shared_lock<std::shared_mutex> l(det_mu_); auto it = det_.find(v); if (it != det_.end() && !it->second.entity.empty()) return it->second.entity; auto e = ent_ov_.find(v); if (e != ent_ov_.end()) return e->second.first; }
        return std::string(g.str(g.sparse(v, k_entity, none)));
    }
    bool resolved(uint32_t v) const { auto l = label(v); return conf_of(l.conf) >= ep.resolve && l.state != ls_disputed; }
    static bool confirm_rule(uint8_t r) { return r == r_cex_hot || r == r_hub_out || r == r_hub_in || r == r_mev_operator || r == r_mev_link || r == r_whale || r == r_recv_hub || r == r_eoa_busy || r == r_mev_contract || r == r_mev_dex_trader; }
    bool needs_llm(uint32_t v) const {
        auto l = label(v);
        if (l.src == s_registry || l.src == s_manual || (l.src == s_agent && l.state != ls_disputed)) return false;
        float imp = g.f32[d_imp][v];
        if (l.state == ls_disputed) return true;
        if (conf_of(l.conf) < ep.resolve && imp >= ep.llm_imp_min) return true;
        bool candidate = l.state == ls_candidate || (l.state == ls_base && confirm_rule(g.base[v].rule));
        return candidate && imp >= ep.confirm_imp_min;
    }
    float priority(uint32_t v) const {
        auto l = label(v); double u = 1.0 - conf_of(l.conf);
        if (l.state == ls_disputed) u += 0.5;
        if (l.state == ls_candidate || confirm_rule(g.base[v].rule)) u += 0.25;
        uint8_t att = 0; { std::shared_lock<std::shared_mutex> k(det_mu_); auto it = attempts_.find(v); if (it != attempts_.end()) att = it->second; }
        return float(u * g.f32[d_imp][v] * std::pow(0.7, att));
    }
    bool needs_protocol(uint32_t v) const {
        if (!g.is_contract(v) || g.base[v].source == s_registry) return false;
        auto st = g.base[v].subtype;
        if (st != st_unknown_topics || g.topic_ids(v).empty()) return false;   // only contracts with unmapped events
        if (g.f32[d_imp][v] < ep.proto_imp_min) return false;
        std::shared_lock<std::shared_mutex> l(det_mu_); auto it = det_.find(v); return it == det_.end() || it->second.task_type != t_protocol;
    }
    void fill_frontier(int threads) {
        std::atomic<uint64_t> a{0}, p{0};
        parallel_for(g.n, threads, [&](size_t b, size_t e, int) {
            for (size_t v = b; v < e; v++) {
                if (needs_llm(uint32_t(v))) { fr[t_address].push(uint32_t(v), priority(uint32_t(v))); a++; }
                if (needs_protocol(uint32_t(v))) { fr[t_protocol].push(uint32_t(v), g.f32[d_imp][v]); p++; }
            }
        }, 1 << 16);
        log_event("frontier_filled", "\"address_label\":" + std::to_string(a.load()) + ",\"protocol_discovery\":" + std::to_string(p.load()) +
                  ",\"llm_importance_min\":" + std::to_string(ep.llm_imp_min) + ",\"confirm_importance_min\":" + std::to_string(ep.confirm_imp_min));
    }

    // ------------------------------------------------------------ pack rendering
    static std::string fx(double x, int prec = 2) {   // ".92", "1.0", "0"
        if (x <= 0) return "0";
        if (x >= 1) { char b[16]; std::snprintf(b, sizeof b, "%.2g", x); return b; }
        char b[16]; std::snprintf(b, sizeof b, "%.*f", prec, x); std::string s(b); if (s[0] == '0') s.erase(0, 1); return s;
    }
    static std::string yymm(uint32_t t) {
        if (!t) return "?";
        time_t tt = time_t(t); tm u{}; gmtime_r(&tt, &u); char b[32]; std::snprintf(b, sizeof b, "%02d%02d", u.tm_year % 100, u.tm_mon + 1); return b;
    }
    int alias_class(uint32_t u) const { auto f = g.u32[d_flags][u]; return (f & (nf_token20 | nf_token721 | nf_token1155)) ? 2 : (f & (nf_contract | nf_system)) ? 1 : 0; }
    // Alias for a node inside a batch (subjects first), or the real address without a batch.
    std::string ref(Batch* b, uint32_t u, TaskPack* t) const {
        if (!b) return hex0x(g.addr[u]);
        int s = b->subject_of(u); if (s >= 0) return "S" + std::to_string(s + 1);
        int c = alias_class(u); std::string nm = b->alias.name(c, u);
        if (t) t->ref_keys.push_back((uint64_t(c) << 32) | u);
        return nm;
    }
    std::string legend_entry(uint32_t u, int c) const {
        auto l = label(u); std::string o;
        if (c == 2) {
            std::string sym(g.str(g.sparse(u, k_symbol, none))); auto ent = entity(u);
            o = sym.empty() ? std::string(g.u32[d_flags][u] & nf_token721 ? "nft" : g.u32[d_flags][u] & nf_token1155 ? "erc1155" : "erc20") : sym;
            if (sym.empty() && !ent.empty()) o += " \"" + ent + "\"";
            uint32_t h = g.sparse(u, k_tok_holders); if (h) o += " h=" + human(h);
            return o;
        }
        o = std::string(category_names[l.cat]) + " " + fx(conf_of(l.conf));
        if (l.sub && l.sub < subtype_count && !subtype_names[l.sub].empty()) o += " " + std::string(subtype_names[l.sub]);
        auto ent = entity(u); if (!ent.empty()) o += " \"" + ent + "\"";
        if (l.src == s_agent) o += " confirmed";
        uint32_t sw = g.sparse(u, k_sweep); if (sw) o += " sw=" + human(sw);
        return o;
    }
    static void add(TaskPack& t, std::string key, std::string val, std::string real = {}) { if (real.empty()) real = val; t.facts.push_back({std::move(key), std::move(val), std::move(real)}); }
    std::string topic_label(uint32_t tid) const {
        auto nm = g.str(g.topic_name[tid]);
        if (!nm.empty()) return std::string(nm);
        std::lock_guard<std::mutex> l(proto_mu_); auto it = proto_.find(g.topic[tid]);
        return it != proto_.end() ? it->second.signature : std::string();
    }

    TaskPack render(Batch* b, uint32_t v, uint8_t type) const {
        TaskPack t; t.node = v;
        const auto L = label(v); const auto& B = g.base[v]; const uint32_t f = g.u32[d_flags][v];
        auto& sf = t.sf; sf.node = v; sf.type = node_type(g, v); sf.topics = g.topics_bits(v);
        sf.sent = g.u32[d_tx_sent][v]; sf.recv = g.u32[d_tx_recv][v]; sf.cp = g.u32[d_cp][v]; sf.outdeg = g.u32[d_outdeg][v];
        sf.top2_out = share_of(g.u16[d_top2o_sh][v]); sf.eth_in = g.f32[d_eth_in][v]; sf.eth_out = g.f32[d_eth_out][v]; sf.usd = g.f32[d_usd][v];
        sf.registry = B.source == s_registry; sf.base_cat = B.category; sf.base_conf = conf_of(B.conf);
        auto R = [&](uint32_t u) { std::string a = ref(b, u, &t); return std::pair<std::string, std::string>{a, hex0x(g.addr[u])}; };
        add(t, "ty", std::string(1, sf.type));
        if (f & nf_code_inferred) add(t, "code", "inferred");
        add(t, "hint", std::string(category_names[L.cat]) + "/" + fx(conf_of(L.conf)) + "/" + std::string(rule_names[B.rule]));
        auto ent = entity(v); if (!ent.empty()) { add(t, "ent", "\"" + ent + "\""); sf.names.push_back(ent); }
        std::string sym(g.str(g.sparse(v, k_symbol, none))); if (!sym.empty()) { add(t, "sym", sym); sf.names.push_back(sym); }
        if (sf.sent || sf.recv) add(t, "tx", human(sf.sent) + "/" + human(sf.recv));
        if (sf.sent >= 10 && g.u32[d_tx_fail][v]) add(t, "fl", fx(double(g.u32[d_tx_fail][v]) / sf.sent));
        if (g.u32[d_tt_out][v] || g.u32[d_tt_in][v]) add(t, "tt", human(g.u32[d_tt_out][v]) + "/" + human(g.u32[d_tt_in][v]));
        add(t, "cp", human(sf.cp));
        add(t, "dg", human(sf.outdeg) + "/" + human(g.u32[d_indeg][v]));
        if (sf.eth_in + sf.eth_out >= 0.001) add(t, "eth", human(sf.eth_out) + "/" + human(sf.eth_in));
        if (sf.usd >= 1000) add(t, "usd", "$" + human(sf.usd));
        add(t, "seen", yymm(g.u32[d_first][v]) + ".." + yymm(g.u32[d_last][v]));
        if (uint32_t lg = g.sparse(v, k_logs)) add(t, "logs", human(lg));
        // emitted events (top 5 by count; names when known, E-aliases otherwise)
        std::span<const uint32_t> cnts; auto tids = g.topic_ids(v, &cnts);
        if (!tids.empty()) {
            std::string ev, evr; size_t lim = type == t_protocol ? 12 : 5;
            for (size_t i = 0; i < tids.size() && i < lim; i++) {
                std::string nm = topic_label(tids[i]), a, r;
                if (type == t_protocol || nm.empty()) {
                    std::string al = b ? b->alias.name(3, tids[i]) : hex0x(View(g.topic[tids[i]].data(), 32));
                    if (b) { t.ref_keys.push_back((uint64_t(3) << 32) | tids[i]); t.topic_aliases.push_back({al, tids[i]}); }
                    a = al + (nm.empty() ? "" : "=" + nm); r = hex0x(View(g.topic[tids[i]].data(), 32)) + (nm.empty() ? "" : "=" + nm);
                } else { a = nm; r = nm; }
                if (!ev.empty()) { ev += ","; evr += ","; }
                ev += a + ":" + human(cnts[i]); evr += r + ":" + std::to_string(cnts[i]);
            }
            if (tids.size() > lim) { ev += ",+" + std::to_string(tids.size() - lim); evr += ",+" + std::to_string(tids.size() - lim); }
            add(t, "ev", ev, evr);
            std::string fam = topic_family_words(sf.topics & ~(tf_known_other | tf_unknown)); if (!fam.empty()) add(t, "fam", fam);
        }
        if (f & (nf_token20 | nf_token721 | nf_token1155)) {
            uint32_t sb = g.sparse(v, k_tok_std); std::string s = (sb & 1 ? "erc20" : sb & 2 ? "erc721" : "erc1155");
            add(t, "tok", s + " h=" + human(g.sparse(v, k_tok_holders)) + " x=" + human(g.sparse(v, k_tok_transfers)));
        }
        if (f & (nf_pool | nf_venue)) add(t, "pool", std::string(g.str(g.sparse(v, k_pool_proto, none))) + " sw=" + human(g.sparse(v, k_pool_trades)));
        if (uint32_t rt = g.sparse(v, k_router)) add(t, "rt", human(rt));
        if (uint32_t dx = g.sparse(v, k_dex)) add(t, "dex", human(dx));
        uint32_t sw = g.sparse(v, k_sandwich), ar = g.sparse(v, k_arb);
        if (sw || ar) add(t, "mev", "s" + human(sw) + "/a" + human(ar));
        if (uint32_t s = g.sparse(v, k_sweep)) add(t, "sw", human(s));
        if (uint32_t mk = g.sparse(v, k_created)) add(t, "mk", human(mk));
        if (uint32_t c = g.sparse(v, k_creator, none); c != none) { auto [a, r] = R(c); add(t, "by", a, r); }
        if (g.u32[d_out_n][v]) add(t, "sh2", fx(sf.top2_out));
        // top counterparties (CSR order = weight desc): o1..o3 / i1..i3  alias:type:n:share
        auto side = [&](const Graph::Adj& adj, uint32_t total, const char* pre, int k) {
            uint64_t lim = std::min<uint64_t>(adj.len, uint64_t(k));
            for (uint64_t i = 0; i < lim; i++) {
                auto [a, r] = R(adj.nb[i]);
                std::string tail = ":" + std::string(edge_names[adj.t[i] & et_mask]) + ":" + human(double(wcount(adj.w[i]))) + ":" + fx(total ? double(wcount(adj.w[i])) / total : 0);
                add(t, pre + std::to_string(i + 1), a + tail, r + tail);
            }
        };
        side(g.out(v), g.u32[d_out_n][v], "o", 3);
        side(g.in(v), g.u32[d_in_n][v], "i", 3);
        // bridge legs (counterparties currently labelled bridge, among the top edges)
        {
            uint32_t bo = 0, bi = 0; auto o = g.out(v), in = g.in(v);
            for (uint64_t i = 0; i < std::min<uint64_t>(o.len, 64); i++) if (label(o.nb[i]).cat == bridge) bo++;
            for (uint64_t i = 0; i < std::min<uint64_t>(in.len, 64); i++) if (label(in.nb[i]).cat == bridge) bi++;
            if (bo || bi) add(t, "br", std::to_string(bo) + "/" + std::to_string(bi));
        }
        for (auto& fct : t.facts) sf.keys.push_back(fct.key);
        // aliases referenced by this subject are citable too (their legend line is part of the pack)
        for (auto k : t.ref_keys) { int c = int(k >> 32); uint32_t u = uint32_t(k); if (b) { auto nm = b->alias.name(c, u); sf.keys.push_back(nm); }
            if (c < 3) { auto e = entity(u); if (!e.empty()) sf.names.push_back(e); std::string s(g.str(g.sparse(u, k_symbol, none))); if (!s.empty()) sf.names.push_back(s); } }
        // input hash over the de-aliased facts + counterparty labels (alias numbering independent)
        std::string canon = std::string(prompt_version) + "|" + std::string(task_names[type]) + "|" + hex0x(g.addr[v]);
        for (auto& fct : t.facts) { canon += "|" + fct.key + "=" + fct.real; }
        std::vector<uint64_t> rk(t.ref_keys); std::sort(rk.begin(), rk.end()); rk.erase(std::unique(rk.begin(), rk.end()), rk.end());
        for (auto k : rk) { int c = int(k >> 32); if (c < 3) canon += "|L" + hex0x(g.addr[uint32_t(k)]) + "=" + legend_entry(uint32_t(k), c); }
        t.hash = sha256(canon); std::memcpy(&t.hash64, t.hash.data(), 8);
        if (t.hash64 == 0) t.hash64 = 1;
        return t;
    }
    std::string subject_line(const TaskPack& t, size_t k) const {
        std::string o = "S" + std::to_string(k + 1);
        for (auto& f : t.facts) { o += ' '; o += f.key; o += '='; o += f.val; }
        return o;
    }
    std::string build_user(Batch& b) const {
        std::string o = "batch " + std::to_string(b.id) + " task=" + std::string(task_names[b.type]) + " n=" + std::to_string(b.tasks.size()) + "\n";
        for (size_t k = 0; k < b.tasks.size(); k++) o += subject_line(b.tasks[k], k) + "\n";
        std::unordered_set<uint64_t> used; for (auto& t : b.tasks) for (auto k : t.ref_keys) used.insert(k);
        std::string leg;
        for (int c = 0; c < 4; c++) for (size_t i = 0; i < b.alias.list[c].size(); i++) {
            uint32_t u = b.alias.list[c][i]; if (!used.count((uint64_t(c) << 32) | u)) continue;
            std::string nm = std::string(1, Alias::cls_char[c]) + std::to_string(i + 1);
            if (c == 3) { if (b.type == t_protocol) leg += nm + "=" + hex0x(View(g.topic[u].data(), 32)) + "\n"; continue; }
            leg += nm + " " + legend_entry(u, c) + "\n";
        }
        if (!leg.empty()) o += "legend\n" + leg;
        return o;
    }

    // ------------------------------------------------------------ batches
    std::shared_ptr<Batch> find_batch(uint64_t id) { auto& s = bs_[id % batch_shards]; std::lock_guard<std::mutex> l(s.mu); auto it = s.m.find(id); return it == s.m.end() ? nullptr : it->second; }
    void drop_batch(uint64_t id) { auto& s = bs_[id % batch_shards]; std::lock_guard<std::mutex> l(s.mu); s.m.erase(id); }
    size_t batches_open() { size_t n = 0; for (auto& s : bs_) { std::lock_guard<std::mutex> l(s.mu); n += s.m.size(); } return n; }
    void expire_batches(uint32_t now_s) {
        for (auto& s : bs_) { std::lock_guard<std::mutex> l(s.mu); for (auto it = s.m.begin(); it != s.m.end();) { if (it->second->lease_until + 60 < now_s) it = s.m.erase(it); else ++it; } }
    }

    json lease(size_t n, uint8_t type, size_t batch_size, uint32_t ttl, const std::string& agent) {
        need(type <= t_protocol, "task_type");
        batch_size = std::clamp<size_t>(batch_size, 1, ep.max_batch); n = std::min<size_t>(n, 4096);
        uint32_t now_s = uint32_t(now_ms() / 1000); if (!ttl) ttl = ep.ttl_s;
        json out = json::array(); size_t remaining = n;
        while (remaining) {
            auto b = std::make_shared<Batch>(); b->id = next_batch_.fetch_add(1) * 1000003ull % 0xffffffffffull ^ (uint64_t(now_s) << 40);
            b->type = type; b->created_ms = now_ms(); b->lease_until = now_s + ttl; b->agent = agent;
            std::vector<uint32_t> got;
            size_t want = std::min(remaining, batch_size);
            fr[type].lease(want, now_s, ttl, b->id, got, [&](uint32_t v) {
                // subjects must be known before rendering so cross-references use S<k>
                TaskPack tp; tp.node = v; b->tasks.push_back(std::move(tp));
                return true;
            });
            if (got.empty()) break;
            // render (subjects known now), dedupe identical inputs already answered
            std::vector<TaskPack> kept;
            for (size_t k = 0; k < b->tasks.size(); k++) {
                uint32_t v = b->tasks[k].node;
                TaskPack t = render(b.get(), v, type);
                t.task_id = b->id;
                if (fr[type].last_hash(v) == t.hash64 || (type == t_address && !needs_llm(v))) { fr[type].release(v, b->id, true, t.hash64); continue; }
                kept.push_back(std::move(t));
            }
            bool dropped = kept.size() != b->tasks.size();
            b->tasks = std::move(kept);
            if (b->tasks.empty()) { remaining -= std::min(remaining, got.size()); continue; }
            if (dropped) {   // aliases were assigned for subjects that were later dropped: rebuild cleanly
                Alias fresh; b->alias = std::move(fresh);
                for (auto& t : b->tasks) { uint32_t v = t.node; uint64_t id = t.task_id; t = render(b.get(), v, type); t.task_id = id; }
            }
            b->user = build_user(*b);
            leased_tasks_ += b->tasks.size();
            json tasks = json::array();
            for (size_t k = 0; k < b->tasks.size(); k++) {
                auto& t = b->tasks[k];
                tasks.push_back({{"task_id", std::to_string(b->id) + "." + std::to_string(k + 1)}, {"subject", "S" + std::to_string(k + 1)},
                                 {"input_hash", hex(View(t.hash.data(), 32))}, {"keys", t.sf.keys}});
            }
            out.push_back({{"batch_id", std::to_string(b->id)}, {"task_type", task_names[type]}, {"prompt_version", prompt_version}, {"context_sha256", context_sha},
                           {"lease_until_ms", uint64_t(b->lease_until) * 1000}, {"user", b->user}, {"tasks", tasks},
                           {"est_input_tokens", uint64_t(double(b->user.size()) / 3.2)}});
            { auto& s = bs_[b->id % batch_shards]; std::lock_guard<std::mutex> l(s.mu); s.m[b->id] = b; }
            remaining -= std::min(remaining, got.size());
        }
        return {{"batches", out}};
    }
    void release_batch(uint64_t id) {
        auto b = find_batch(id); if (!b) return;
        std::lock_guard<std::mutex> l(b->mu); if (b->closed) return; b->closed = true;
        for (auto& t : b->tasks) fr[b->type].release(t.node, b->id, false, 0);
        drop_batch(id);
    }

    // ------------------------------------------------------------ proposals
    struct Parsed { int s = -1; int c = -1; double conf = -1; std::vector<std::string> ev; std::string name; int fam = -1; std::vector<Mapping> maps; std::vector<std::string> shape; };
    static int to_index(const json& x, std::string_view prefix) {
        if (x.is_number_integer()) return int(x.get<int64_t>());
        if (x.is_string()) { std::string s = x; if (!prefix.empty() && s.rfind(std::string(prefix), 0) == 0) s = s.substr(prefix.size()); try { return std::stoi(s); } catch (...) { return -1; } }
        return -1;
    }
    static double to_conf(const json& q) {
        if (q.is_number_integer()) { auto v = q.get<int64_t>(); return v >= 0 && v <= 9 ? double(v) / 10.0 + 0.05 : -1; }
        if (q.is_number_float()) { double d = q.get<double>(); return d >= 0 && d <= 1 ? d : -1; }
        return -1;
    }
    static std::vector<Parsed> parse_output(const json& out, uint8_t type) {
        std::vector<Parsed> r;
        json j = out;
        if (j.is_string()) {
            std::string s = j.get<std::string>();
            auto a = s.find('{'), z = s.rfind('}');
            need(a != std::string::npos && z != std::string::npos && z > a, "OUTPUT_NOT_JSON");
            j = json::parse(s.substr(a, z - a + 1), nullptr, false);
            need(!j.is_discarded(), "OUTPUT_NOT_JSON");
        }
        const json* arr = nullptr;
        if (j.is_object()) { for (auto k : {"r", "p", "results", "labels"}) if (j.contains(k) && j[k].is_array()) { arr = &j[k]; break; } }
        else if (j.is_array()) arr = &j;
        need(arr != nullptr, "OUTPUT_SHAPE");
        for (auto& e : *arr) {
            Parsed p;
            auto evs = [&](const json& x) { if (x.is_array()) for (auto& k : x) if (k.is_string() && k.get<std::string>().size() <= 64) p.ev.push_back(k.get<std::string>()); };
            if (type == t_address) {
                if (e.is_array() && e.size() >= 4) {
                    p.s = to_index(e[0], "S"); p.c = e[1].is_string() ? category_code(e[1].get<std::string>()) : to_index(e[1], ""); p.conf = to_conf(e[2]); evs(e[3]);
                    if (e.size() >= 5 && e[4].is_string()) p.name = e[4].get<std::string>();
                } else if (e.is_object()) {
                    if (e.contains("s")) p.s = to_index(e["s"], "S");
                    if (e.contains("c")) p.c = e["c"].is_string() ? category_code(e["c"].get<std::string>()) : to_index(e["c"], "");
                    if (e.contains("q")) p.conf = to_conf(e["q"]);
                    if (e.contains("e")) evs(e["e"]);
                    if (e.contains("n") && e["n"].is_string()) p.name = e["n"].get<std::string>();
                } else p.shape.push_back("ITEM_SHAPE");
            } else {
                // [s, fam, q, [[E, sig, action, wallet, asset, amount], ...], [evidence]]
                const json* ms = nullptr;
                if (e.is_array() && e.size() >= 4) {
                    p.s = to_index(e[0], "S"); p.fam = e[1].is_string() ? -2 : to_index(e[1], ""); if (e[1].is_string()) { for (size_t i = 0; i < protocol_family_count; i++) if (protocol_families[i] == e[1].get<std::string>()) p.fam = int(i); }
                    p.conf = to_conf(e[2]); ms = &e[3]; if (e.size() >= 5) evs(e[4]);
                } else if (e.is_object()) {
                    if (e.contains("s")) p.s = to_index(e["s"], "S");
                    if (e.contains("f")) { p.fam = to_index(e["f"], ""); if (e["f"].is_string()) for (size_t i = 0; i < protocol_family_count; i++) if (protocol_families[i] == e["f"].get<std::string>()) p.fam = int(i); }
                    if (e.contains("q")) p.conf = to_conf(e["q"]);
                    if (e.contains("m")) ms = &e["m"];
                    if (e.contains("e")) evs(e["e"]);
                }
                if (ms && ms->is_array()) for (auto& m : *ms) {
                    Mapping mp;
                    if (m.is_array() && m.size() >= 3) {
                        mp.topic_ref = m[0].is_string() ? m[0].get<std::string>() : ""; mp.signature = m[1].is_string() ? m[1].get<std::string>() : "";
                        mp.action = m[2].is_string() ? -2 : to_index(m[2], ""); if (m[2].is_string()) for (size_t i = 0; i < protocol_action_count; i++) if (protocol_actions[i] == m[2].get<std::string>()) mp.action = int(i);
                        if (m.size() > 3 && m[3].is_string()) mp.wallet = m[3];
                        if (m.size() > 4 && m[4].is_string()) mp.asset = m[4];
                        if (m.size() > 5 && m[5].is_string()) mp.amount = m[5];
                    } else { p.shape.push_back("MAPPING_SHAPE"); continue; }
                    p.maps.push_back(std::move(mp));
                }
            }
            r.push_back(std::move(p));
        }
        return r;
    }

    // Apply one accepted agent label (caller holds commit_mu_).  Returns true when it changed state.
    bool apply_label(uint32_t v, uint8_t cat, double conf, Detail d) {
        std::unique_lock<std::shared_mutex> dl(det_mu_);   // label word + detail change together (snapshot consistency)
        Label cur = label(v); bool changed = false;
        Label nl{cat, conf8(conf), s_agent, cur.sub, cur.tier, ls_committed, 1, 1};
        if (cur.src == s_agent || cur.src == s_manual) {
            double a = conf_of(cur.conf);
            if (cur.cat == cat) { nl.conf = conf8(std::min(0.99, std::max(a, conf) + 0.5 * (1 - std::max(a, conf)) * std::min(a, conf))); nl.agree = uint8_t(std::min(255, cur.agree + 1)); nl.state = ls_committed; set_label(v, nl); changed = true; }
            else if (conf > a + 0.1) { nl.state = ls_disputed; set_label(v, nl); changed = true; }
            else { cur.state = ls_disputed; set_label(v, cur); changed = true; d.warnings += (d.warnings.empty() ? "" : "|") + std::string("DISPUTED_KEPT_EXISTING"); }
        } else { set_label(v, nl); changed = true; }
        det_[v] = std::move(d); attempts_.erase(v);
        dl.unlock();
        commits_++; applied_since_snap_++;
        uint32_t sec = uint32_t(now_ms() / 1000); auto& slot = commit_ring_[sec % commit_ring_.size()];
        if (commit_ring_sec_[sec % commit_ring_.size()].exchange(sec) != sec) slot.store(0);
        slot.fetch_add(1);
        return changed;
    }

    // Deterministic local propagation from a node whose label changed (caller holds commit_mu_).
    void propagate_from(uint32_t v, int depth = 0) {
        if (depth > 2) return;
        Label L = label(v); double c = conf_of(L.conf);
        bool confirmed = L.src == s_agent || L.src == s_registry || L.src == s_manual;
        std::vector<uint32_t> changed;
        if (L.cat == cex_hot_wallet && confirmed && c >= 0.6) {
            auto in = g.in(v); uint64_t lim = std::min<uint64_t>(in.len, 200000);
            for (uint64_t i = 0; i < lim; i++) {
                uint32_t u = in.nb[i]; uint64_t w0 = raw(u); auto lu = label(u);
                if (lu.src == s_agent || lu.src == s_manual || lu.src == s_registry) continue;
                if (g.u32[d_top1o][u] != v || !deposit_like(g, u, cp)) continue;
                double nc = std::max(ep.prop_min, c * ep.decay * std::min(1.0, share_of(g.u16[d_top2o_sh][u]) / 0.9));
                if (lu.cat == cex_deposit && conf_of(lu.conf) >= nc) continue;
                if (cas_label(u, w0, Label{cex_deposit, conf8(nc), s_propagation, st_cex_deposit_sweep, lu.tier, ls_candidate, 0, 1})) changed.push_back(u);
            }
        }
        if (L.src == s_agent && L.cat != cex_hot_wallet && g.base[v].rule == r_cex_hot) {
            // agent rejected a classifier hub: demote the deposits that only the hub rule produced
            auto in = g.in(v); uint64_t lim = std::min<uint64_t>(in.len, 200000);
            for (uint64_t i = 0; i < lim; i++) {
                uint32_t u = in.nb[i]; uint64_t w0 = raw(u); auto lu = label(u);
                if (lu.cat != cex_deposit || lu.src == s_agent || lu.src == s_manual || g.u32[d_top1o][u] != v) continue;
                auto base = classify_node(g, u, cp);
                if (cas_label(u, w0, Label{base.category, base.conf, s_propagation, base.subtype, lu.tier, ls_candidate, 0, 1})) changed.push_back(u);
            }
        }
        if (L.cat == cex_deposit && L.src == s_agent && c >= 0.6) {
            uint32_t h = g.u32[d_top1o][v];
            if (h != none && !g.is_contract(h)) {
                uint64_t w0 = raw(h); auto lh = label(h);
                if (lh.src != s_agent && lh.src != s_manual && lh.src != s_registry) {
                    uint32_t k = 0; auto in = g.in(h); uint64_t lim = std::min<uint64_t>(in.len, 200000);
                    for (uint64_t i = 0; i < lim; i++) { auto lu = label(in.nb[i]); if (lu.cat == cex_deposit && lu.src == s_agent) k++; }
                    double nc = std::min(0.9, 0.55 + 0.1 * k);
                    if (k >= 2 && (lh.cat != cex_hot_wallet || conf_of(lh.conf) < nc) && cas_label(h, w0, Label{cex_hot_wallet, conf8(nc), s_propagation, st_cex_sweep_hub, lh.tier, ls_candidate, 0, 1})) changed.push_back(h);
                }
            }
        }
        if (L.cat == mev_bot && (confirmed || c >= 0.8)) {
            uint32_t u = g.is_contract(v) ? g.u32[d_top1i][v] : g.u32[d_top1o][v];
            double sh = share_of(g.is_contract(v) ? g.u16[d_top1i_sh][v] : g.u16[d_top1o_sh][v]);
            if (u != none && sh >= 0.8 && !is_infra(g, u) && (g.is_contract(v) ? g.is_eoa(u) : (g.is_contract(u) && g.u32[d_indeg][u] <= 10))) {
                uint64_t w0 = raw(u); auto lu = label(u); double nc = std::max(ep.prop_min, c * ep.decay);
                if (lu.src != s_agent && lu.src != s_registry && lu.src != s_manual && !(lu.cat == mev_bot && conf_of(lu.conf) >= nc)
                    && cas_label(u, w0, Label{mev_bot, conf8(nc), s_propagation, g.is_contract(u) ? uint8_t(st_mev_contract) : uint8_t(st_mev_operator), lu.tier, ls_candidate, 0, 1})) changed.push_back(u);
            }
        }
        if (g.is_contract(v) && confirmed) {   // entity propagation to siblings of the same deployer
            auto e = entity(v); uint32_t cr = g.sparse(v, k_creator, none);
            if (!e.empty() && cr != none) {
                auto kids = g.children(cr); size_t lim = std::min<size_t>(kids.size(), 2000);
                std::unique_lock<std::shared_mutex> l(det_mu_);
                for (size_t i = 0; i < lim; i++) { uint32_t w = kids[i]; if (w == v || g.sparse(w, k_entity, none) != none || det_.count(w)) continue; ent_ov_[w] = {e + " (sibling)", conf8(c * 0.8)}; }
            }
        }
        prop_changes_ += changed.size();
        for (auto u : changed) {
            if (needs_llm(u)) fr[t_address].push(u, priority(u));
            if (changed.size() <= 64) requeue_neighbors(u);   // bulk (hub) changes: the hub itself was re-queued already
            if (depth < 2 && changed.size() <= 64) propagate_from(u, depth + 1);
        }
    }
    // Ordered background propagation (commit order); commits never wait for it.
    void start_propagation() { prop_th_ = std::thread([this] { prop_loop(); }); }
    void stop_propagation() { { std::lock_guard<std::mutex> l(pq_mu_); pq_stop_ = true; } pq_cv_.notify_all(); if (prop_th_.joinable()) prop_th_.join(); }
    void enqueue_propagation(uint32_t v) { { std::lock_guard<std::mutex> l(pq_mu_); pq_.push_back(v); } pq_cv_.notify_one(); }
    size_t propagation_backlog() { std::lock_guard<std::mutex> l(pq_mu_); return pq_.size(); }
    void prop_loop() {
        for (;;) {
            uint32_t v;
            { std::unique_lock<std::mutex> l(pq_mu_); pq_cv_.wait(l, [&] { return pq_stop_ || !pq_.empty(); }); if (pq_.empty()) return; v = pq_.front(); pq_.pop_front(); }
            propagate_from(v); requeue_neighbors(v);
        }
    }
    void requeue_neighbors(uint32_t v) {
        auto o = g.out(v), in = g.in(v);
        for (uint64_t i = 0; i < std::min<uint64_t>(o.len, ep.neighbor_requeue); i++) { uint32_t u = o.nb[i]; if (needs_llm(u)) fr[t_address].push(u, priority(u)); }
        for (uint64_t i = 0; i < std::min<uint64_t>(in.len, ep.neighbor_requeue); i++) { uint32_t u = in.nb[i]; if (needs_llm(u)) fr[t_address].push(u, priority(u)); }
    }

    // Encodes one proposal record (WAL kind 6); also used for replay.
    struct TaskOutcome { uint32_t node; uint8_t verdict; uint8_t cat; double conf; std::string entity; std::vector<std::pair<std::string, std::string>> ev; std::string reasons, warnings; Hash input{}; int fam = -1; std::vector<Mapping> maps; };
    static std::string encode_proposal(const Graph& g, const std::string& prov, const std::string& model, uint64_t batch, uint8_t type, uint32_t tin, uint32_t tout, uint32_t tcache, const std::vector<TaskOutcome>& ts) {
        Enc e; e.str(prov).str(model).u64(batch).u8(type).u32(tin).u32(tout).u32(tcache).u16(uint16_t(ts.size()));
        for (auto& t : ts) {
            e.raw(g.addr[t.node].b, 20).u8(t.verdict).u8(t.cat).u8(conf8(t.conf)).str(t.entity).u8(uint8_t(std::min<size_t>(t.ev.size(), 255)));
            for (size_t i = 0; i < std::min<size_t>(t.ev.size(), 255); i++) e.str(t.ev[i].first).str(t.ev[i].second.substr(0, 4000));
            e.str(t.reasons.substr(0, 4000)).str(t.warnings.substr(0, 4000)).raw(t.input.data(), 32).u8(uint8_t(t.fam < 0 ? 255 : t.fam)).u8(uint8_t(std::min<size_t>(t.maps.size(), 255)));
            for (auto& m : t.maps) e.raw(m.topic0.data(), 32).str(m.signature).u8(uint8_t(m.action < 0 ? 255 : m.action)).str(m.wallet).str(m.asset).str(m.amount);
        }
        return e.b;
    }
    void count_stats(const std::string& prov, const std::string& model, uint32_t tin, uint32_t tout, uint32_t tc, const std::vector<TaskOutcome>& ts) {
        std::lock_guard<std::mutex> l(stats_mu_);
        auto& s = prov_[prov + "/" + model]; s.proposals++; s.tok_in += tin; s.tok_out += tout; s.tok_cached += tc;
        for (auto& t : ts) {
            s.tasks++;
            if (t.verdict == 0) { s.rejected++; size_t a = 0; while (a < t.reasons.size()) { size_t z = t.reasons.find('|', a); if (z == std::string::npos) z = t.reasons.size(); auto r = t.reasons.substr(a, z - a); reasons_[r.substr(0, r.find(':'))]++; a = z + 1; } }
            else if (t.verdict == 3) s.missing++;
            else { s.accepted++; if (t.verdict == 1) s.applied++; }
        }
    }
    void apply_outcomes(const std::string& prov, const std::string& model, uint64_t batch, uint8_t type, const std::vector<TaskOutcome>& ts, uint64_t seq) {
        for (auto& t : ts) {
            if (t.verdict != 1 && t.verdict != 2) continue;
            if (replaying && seq) { std::shared_lock<std::shared_mutex> l(det_mu_); auto it = det_.find(t.node); if (it != det_.end() && it->second.seq >= seq) continue; }
            Detail d; d.evidence = t.ev; d.entity = t.entity; d.provider = prov; d.model = model; d.warnings = t.warnings; d.batch = batch; d.ts = now_ms(); d.seq = seq; d.task_type = type;
            if (type == t_protocol) {
                std::lock_guard<std::mutex> l(proto_mu_);
                for (auto& m : t.maps) { ProtoMap pm; pm.signature = m.signature; pm.action = uint8_t(m.action); pm.family = uint8_t(std::max(0, t.fam)); pm.conf = conf8(t.conf);
                    pm.wallet = m.wallet; pm.asset = m.asset; pm.amount = m.amount; pm.contract = g.addr[t.node]; pm.provider = prov; pm.model = model; proto_.emplace(m.topic0, pm); }
            }
            apply_label(t.node, t.cat, t.conf, std::move(d));
            if (async_propagation) enqueue_propagation(t.node);
            else if (!replaying) { propagate_from(t.node); requeue_neighbors(t.node); }
            if (type == t_protocol) {   // graph-loop over the learned topics: re-enqueue other emitters
                for (auto& m : t.maps) { uint32_t tid = g.topic_find(m.topic0); if (tid == none) continue; auto em = g.topic_emitters(tid);
                    for (size_t i = 0; i < std::min<size_t>(em.size(), 5000); i++) if (em[i] != t.node && needs_llm(em[i])) fr[t_address].push(em[i], priority(em[i])); }
            }
        }
    }

    // Validate + commit a proposal.  done(json) is called once the WAL record is durable.
    void propose(uint64_t batch_id, const std::string& prov, const std::string& model, const json& usage, const json& output, std::function<void(int, json)> done) {
        auto b = find_batch(batch_id);
        if (!b) { done(404, {{"error", "BATCH_NOT_FOUND_OR_EXPIRED"}}); return; }
        std::unique_lock<std::mutex> bl(b->mu);
        if (b->closed) { done(409, {{"error", "BATCH_ALREADY_ANSWERED"}}); return; }
        proposals_++;
        std::vector<Parsed> ps; std::string parse_err;
        try { ps = parse_output(output, b->type); } catch (const std::exception& e) { parse_err = e.what(); }
        std::vector<TaskOutcome> outs(b->tasks.size());
        std::vector<int> taken(b->tasks.size(), -1);
        for (size_t i = 0; i < ps.size(); i++) if (ps[i].s >= 1 && size_t(ps[i].s) <= b->tasks.size() && taken[size_t(ps[i].s - 1)] < 0) taken[size_t(ps[i].s - 1)] = int(i);
        for (size_t k = 0; k < b->tasks.size(); k++) {
            auto& t = b->tasks[k]; auto& o = outs[k]; o.node = t.node; o.input = t.hash;
            if (taken[k] < 0) { o.verdict = 3; o.cat = unknown; o.reasons = parse_err.empty() ? "NO_RESULT_FOR_SUBJECT" : parse_err; continue; }
            auto& p = ps[size_t(taken[k])];
            std::vector<std::string> hard(p.shape.begin(), p.shape.end());
            if (!fr[b->type].holds(t.node, b->id)) hard.push_back("LEASE_EXPIRED");
            if (p.conf < 0) hard.push_back("CONFIDENCE_RANGE");
            Verdict v;
            if (b->type == t_address) {
                if (p.c < 0 || p.c >= category_count) hard.push_back("CATEGORY_NOT_IN_ENUM");
                else v = validate_address(uint8_t(p.c), std::max(0.0, p.conf), p.ev, p.name, t.sf, vp);
                o.cat = uint8_t(std::clamp(p.c, 0, int(category_count) - 1));
            } else {
                if (p.fam < 0 || size_t(p.fam) >= protocol_family_count) hard.push_back("FAMILY_NOT_IN_ENUM");
                for (auto& k2 : p.ev) if (std::find(t.sf.keys.begin(), t.sf.keys.end(), k2) == t.sf.keys.end()) hard.push_back("EVIDENCE_KEY_NOT_IN_PACK:" + k2.substr(0, 40));
                std::unordered_set<uint32_t> seen;
                for (auto& m : p.maps) {
                    int c = -1; uint32_t tid = none;
                    if (!b->alias.resolve(m.topic_ref, c, tid) || c != 3 || std::none_of(t.topic_aliases.begin(), t.topic_aliases.end(), [&](auto& x) { return x.second == tid; })) { hard.push_back("TOPIC0_NOT_IN_PACK:" + m.topic_ref.substr(0, 12)); continue; }
                    if (!seen.insert(tid).second) { hard.push_back("DUPLICATE_TOPIC0_MAPPING:" + m.topic_ref); continue; }
                    m.topic0 = g.topic[tid];
                    for (auto& r : check_mapping(m, nullptr)) hard.push_back(r);
                }
                if (p.maps.empty() && p.fam != int(protocol_family_count) - 1) v.soft.push_back("NO_EVENT_MAPPINGS");
                auto fc = protocol_family_class(size_t(std::max(0, p.fam)));
                o.cat = fc.category; o.fam = p.fam; o.maps = p.maps;
                if (contract_only(o.cat) && t.sf.type != 'C') hard.push_back("CONTRACT_CATEGORY_BUT_NOT_CONTRACT");
            }
            hard.insert(hard.end(), v.hard.begin(), v.hard.end());
            o.conf = std::clamp(p.conf, 0.0, 1.0); o.entity = p.name;
            for (auto& key : p.ev) {
                auto it = std::find_if(t.facts.begin(), t.facts.end(), [&](const Fact& f) { return f.key == key; });
                if (it != t.facts.end()) { o.ev.push_back({key, it->real}); continue; }
                int c = -1; uint32_t id = none;
                if (b->alias.resolve(key, c, id) && c < 3) o.ev.push_back({key, hex0x(g.addr[id]) + " " + legend_entry(id, c)});
            }
            for (size_t i = 0; i < hard.size(); i++) o.reasons += (i ? "|" : "") + hard[i];
            for (size_t i = 0; i < v.soft.size(); i++) o.warnings += (i ? "|" : "") + v.soft[i];
            o.verdict = hard.empty() ? 1 : 0;
        }
        b->closed = true;
        uint32_t tin = uint32_t(usage.value("prompt_tokens", 0)), tout = uint32_t(usage.value("completion_tokens", 0)), tc = uint32_t(usage.value("cached_tokens", 0));
        uint64_t seq = 0;
        {
            std::lock_guard<std::mutex> l(commit_mu_);
            // registry / existing-agent conflicts are resolved inside apply_label (accepted_not_applied = 2)
            for (auto& o : outs) if (o.verdict == 1) { auto cur = label(o.node); if ((cur.src == s_agent || cur.src == s_manual) && cur.cat != o.cat && o.conf <= conf_of(cur.conf) + 0.1) o.verdict = 2; }
            std::string rec = encode_proposal(g, prov, model, batch_id, b->type, tin, tout, tc, outs);
            if (wal) {
                json resp = verdict_json(*b, outs);
                auto cb = [done, resp]() mutable { done(200, resp); };
                seq = wal->append(6, rec, cb);
                apply_outcomes(prov, model, batch_id, b->type, outs, seq);
            } else apply_outcomes(prov, model, batch_id, b->type, outs, 0);
        }
        count_stats(prov, model, tin, tout, tc, outs);
        for (size_t k = 0; k < b->tasks.size(); k++) {
            auto& o = outs[k]; auto& t = b->tasks[k];
            bool final_ = o.verdict == 1 || o.verdict == 2;
            if (!final_ && o.verdict == 0) { std::unique_lock<std::shared_mutex> l(det_mu_); auto& a = attempts_[t.node]; a++; if (a >= ep.max_attempts) final_ = true; }
            fr[b->type].release(t.node, b->id, final_, t.hash64);
        }
        bl.unlock();
        drop_batch(batch_id);
        if (!wal) done(200, verdict_json(*b, outs));
    }
    json verdict_json(const Batch& b, const std::vector<TaskOutcome>& outs) const {
        static const char* vn[4] = {"rejected", "accepted", "accepted_not_applied", "missing"};
        json r = json::array(); size_t acc = 0, rej = 0, miss = 0;
        for (size_t k = 0; k < outs.size(); k++) {
            auto& o = outs[k]; (o.verdict == 1 || o.verdict == 2 ? acc : o.verdict == 3 ? miss : rej)++;
            json x = {{"task_id", std::to_string(b.id) + "." + std::to_string(k + 1)}, {"subject", "S" + std::to_string(k + 1)}, {"address", hex0x(g.addr[o.node])},
                      {"verdict", vn[o.verdict]}, {"category", category_names[o.cat]}, {"confidence", o.conf}};
            auto split = [](const std::string& s) { json a = json::array(); size_t i = 0; while (i < s.size()) { size_t z = s.find('|', i); if (z == std::string::npos) z = s.size(); a.push_back(s.substr(i, z - i)); i = z + 1; } return a; };
            if (!o.reasons.empty()) x["reasons"] = split(o.reasons);
            if (!o.warnings.empty()) x["warnings"] = split(o.warnings);
            r.push_back(x);
        }
        return {{"batch_id", std::to_string(b.id)}, {"accepted", acc}, {"rejected", rej}, {"missing", miss}, {"results", r}};
    }

    // Import externally produced LLM labels (dispatcher labels.jsonl) as journaled agent commits.
    // Graph hard rules are re-validated on our facts; their evidence keys refer to their own pack
    // format and are stored verbatim (prefixed "import:").  Reports code-vs-LLM agreement.
    void import_labels(const std::string& body, std::function<void(int, json)> done, bool dry_run = false) {
        struct Agg { uint64_t n = 0, agree = 0, n_conf = 0, agree_conf = 0, accepted = 0; std::map<std::string, uint64_t> code; };
        std::map<std::string, Agg> by_llm; std::map<std::string, uint64_t> reasons;
        uint64_t total = 0, missing = 0, badcat = 0, flagged = 0, acc = 0, rej = 0;
        std::map<std::pair<std::string, std::string>, std::vector<TaskOutcome>> groups;
        size_t pos = 0;
        while (pos < body.size()) {
            size_t e = body.find('\n', pos); if (e == std::string::npos) e = body.size();
            std::string line = body.substr(pos, e - pos); pos = e + 1;
            if (trim(line).empty()) continue;
            auto j = json::parse(line, nullptr, false);
            if (j.is_discarded() || !j.is_object()) { reasons["LINE_NOT_JSON"]++; continue; }
            total++;
            std::string addr = j.contains("address") && j["address"].is_string() ? j["address"].get<std::string>() : "";
            uint32_t v = g.find(addr); if (v == none) { missing++; continue; }
            std::string cs = j.contains("category") && j["category"].is_string() ? j["category"].get<std::string>() : "";
            int c = category_code(cs); if (c < 0) { badcat++; continue; }
            double conf = j.contains("confidence") && j["confidence"].is_number() ? std::clamp(j["confidence"].get<double>(), 0.0, 1.0) : 0.5;
            auto sget = [&](const char* k, const char* d) { return j.contains(k) && j[k].is_string() ? j[k].get<std::string>() : std::string(d); };
            std::string prov = sget("provider", "import"), model = sget("model", "unknown");
            bool flag = sget("status", "ok") == "flagged"; if (flag) flagged++;
            const auto& B = g.base[v];
            auto& a = by_llm[cs]; a.n++; a.code[std::string(category_names[B.category])]++;
            if (B.category == c) a.agree++;
            if (conf_of(B.conf) >= ep.resolve) { a.n_conf++; if (B.category == c) a.agree_conf++; }
            TaskPack t = render(nullptr, v, t_address);
            Verdict vd = validate_address(uint8_t(c), conf, {"ty"}, "", t.sf, vp);
            TaskOutcome o; o.node = v; o.cat = uint8_t(c); o.conf = conf; o.input = t.hash;
            if (j.contains("entity_name") && j["entity_name"].is_string()) o.entity = j["entity_name"].get<std::string>().substr(0, 80);
            if (j.contains("evidence") && j["evidence"].is_array())
                for (auto& ev : j["evidence"]) if (ev.is_object()) {
                    std::string k = ev.contains("key") && ev["key"].is_string() ? ev["key"].get<std::string>() : "";
                    std::string val = ev.contains("value") ? (ev["value"].is_string() ? ev["value"].get<std::string>() : ev["value"].dump()) : "";
                    o.ev.push_back({"import:" + k.substr(0, 120), val.substr(0, 400)});
                }
            std::vector<std::string> hard = vd.hard; if (flag) hard.push_back("DISPATCHER_FLAGGED");
            for (size_t i = 0; i < hard.size(); i++) { o.reasons += (i ? "|" : "") + hard[i]; reasons[hard[i].substr(0, hard[i].find(':'))]++; }
            o.warnings = "IMPORTED"; for (auto& w : vd.soft) o.warnings += "|" + w;
            o.verdict = hard.empty() ? 1 : 0; (o.verdict ? acc : rej)++; if (o.verdict) a.accepted++;
            groups[{prov, model}].push_back(std::move(o));
        }
        json cats = json::object(); uint64_t agree = 0, agree_conf = 0, nconf = 0;
        for (auto& [k, a] : by_llm) {
            agree += a.agree; agree_conf += a.agree_conf; nconf += a.n_conf;
            json code = json::object(); for (auto& [ck, cn] : a.code) code[ck] = cn;
            cats[k] = {{"llm_labels", a.n}, {"code_agrees", a.agree}, {"agree_rate", a.n ? double(a.agree) / a.n : 0.0},
                       {"code_resolved", a.n_conf}, {"agree_rate_when_code_resolved", a.n_conf ? double(a.agree_conf) / a.n_conf : 0.0}, {"accepted_by_validators", a.accepted}, {"code_labels", code}};
        }
        uint64_t matched = total - missing - badcat;
        json rj = json::object(); for (auto& [k, n] : reasons) rj[k] = n;
        json summary = {{"lines", total}, {"not_in_graph", missing}, {"bad_category", badcat}, {"flagged_by_dispatcher", flagged}, {"accepted_committed", acc}, {"rejected", rej},
                        {"reject_reasons", rj}, {"agreement_overall", matched ? double(agree) / matched : 0.0},
                        {"agreement_when_code_resolved", nconf ? double(agree_conf) / nconf : 0.0}, {"by_llm_category", cats}};
        if (dry_run) { summary["dry_run"] = true; done(200, summary); return; }
        std::lock_guard<std::mutex> l(commit_mu_);
        size_t chunks = 0; for (auto& [k, v] : groups) chunks += (v.size() + 999) / 1000;
        if (!chunks || !wal) { for (auto& [k, v] : groups) { apply_outcomes(k.first, k.second, 0, t_address, v, 0); count_stats(k.first, k.second, 0, 0, 0, v); } done(200, summary); return; }
        size_t left = chunks;
        for (auto& [k, v] : groups) {
            for (size_t i = 0; i < v.size(); i += 1000) {
                std::vector<TaskOutcome> part(v.begin() + long(i), v.begin() + long(std::min(v.size(), i + 1000)));
                std::string rec = encode_proposal(g, k.first, k.second, 0, t_address, 0, 0, 0, part);
                bool last = --left == 0;
                uint64_t seq = wal->append(6, rec, last ? std::function<void()>([done, summary]() mutable { done(200, summary); }) : std::function<void()>());
                apply_outcomes(k.first, k.second, 0, t_address, part, seq);
                count_stats(k.first, k.second, 0, 0, 0, part);
            }
        }
    }

    // Replay one WAL/snapshot proposal record (recovery; no WAL write, no frontier side effects).
    void replay_proposal(Dec& d, uint64_t seq) {
        std::string prov = d.str(), model = d.str(); uint64_t batch = d.u64(); uint8_t type = d.u8();
        uint32_t tin = d.u32(), tout = d.u32(), tc = d.u32(); uint16_t n = d.u16();
        std::vector<TaskOutcome> ts;
        for (uint16_t i = 0; i < n; i++) {
            TaskOutcome t; Addr a; d.take(a.b, 20); t.verdict = d.u8(); t.cat = d.u8(); t.conf = conf_of(d.u8()); t.entity = d.str();
            uint8_t ne = d.u8(); for (uint8_t k = 0; k < ne; k++) { auto key = d.str(); auto val = d.str(); t.ev.push_back({key, val}); }
            t.reasons = d.str(); t.warnings = d.str(); d.take(t.input.data(), 32); uint8_t fam = d.u8(); t.fam = fam == 255 ? -1 : fam;
            uint8_t nm = d.u8(); for (uint8_t k = 0; k < nm; k++) { Mapping m; d.take(m.topic0.data(), 32); m.signature = d.str(); uint8_t ac = d.u8(); m.action = ac == 255 ? -1 : ac; m.wallet = d.str(); m.asset = d.str(); m.amount = d.str(); t.maps.push_back(m); }
            t.node = g.find(a);
            if (t.node == none) continue;   // address not in this graph image (rebuilt map): skipped, still counted
            ts.push_back(std::move(t));
        }
        std::lock_guard<std::mutex> l(commit_mu_);
        apply_outcomes(prov, model, batch, type, ts, seq);
        for (auto& t : ts) if (t.verdict == 1 || t.verdict == 2) fr[type].set_last_hash(t.node, [&] { uint64_t h; std::memcpy(&h, t.input.data(), 8); return h ? h : 1; }());
        count_stats(prov, model, tin, tout, tc, ts);
    }

    // Snapshot records: compacted agent state (kind 7 per node) + protocol mappings + stats.
    std::vector<std::pair<uint8_t, std::string>> snapshot_records() {
        std::vector<std::pair<uint8_t, std::string>> recs;
        std::vector<uint32_t> nodes;
        { std::shared_lock<std::shared_mutex> l(det_mu_); nodes.reserve(det_.size()); for (auto& [v, dd] : det_) nodes.push_back(v); }
        std::sort(nodes.begin(), nodes.end());
        recs.reserve(nodes.size() + 64);
        for (size_t c = 0; c < nodes.size(); c += 4096) {
            std::shared_lock<std::shared_mutex> l(det_mu_);   // writers wait at most one chunk
            for (size_t i = c; i < std::min(nodes.size(), c + 4096); i++) {
                uint32_t v = nodes[i]; auto it = det_.find(v); if (it == det_.end()) continue; const Detail& dd = it->second;
                Label L = label(v); Enc e; e.raw(g.addr[v].b, 20).u64(pack_label(L)).str(dd.entity).str(dd.provider).str(dd.model).str(dd.warnings).u64(dd.batch).u64(dd.ts).u64(dd.seq).u8(dd.task_type);
                e.u8(uint8_t(std::min<size_t>(dd.evidence.size(), 255))); for (size_t k = 0; k < std::min<size_t>(dd.evidence.size(), 255); k++) e.str(dd.evidence[k].first).str(dd.evidence[k].second.substr(0, 4000));
                recs.push_back({7, std::move(e.b)});
            }
        }
        { std::lock_guard<std::mutex> l(proto_mu_);
          for (auto& [t, m] : proto_) { Enc e; e.raw(t.data(), 32).str(m.signature).u8(m.action).u8(m.family).u8(m.conf).str(m.wallet).str(m.asset).str(m.amount).raw(m.contract.b, 20).str(m.provider).str(m.model); recs.push_back({8, e.b}); } }
        { std::lock_guard<std::mutex> l(stats_mu_);
          json s = json::object(); for (auto& [k, p] : prov_) s[k] = {p.proposals, p.tasks, p.accepted, p.rejected, p.applied, p.missing, p.tok_in, p.tok_out, p.tok_cached};
          json r = json::object(); for (auto& [k, c] : reasons_) r[k] = c;
          Enc e; e.str(json({{"providers", s}, {"reasons", r}, {"commits", commits_.load()}}).dump().substr(0, 65000)); recs.push_back({9, e.b}); }
        return recs;
    }
    void load_snapshot_record(uint8_t kind, Dec& d) {
        if (kind == 7) {
            Addr a; d.take(a.b, 20); Label L = unpack_label(d.u64()); Detail dd; dd.entity = d.str(); dd.provider = d.str(); dd.model = d.str(); dd.warnings = d.str();
            dd.batch = d.u64(); dd.ts = d.u64(); dd.seq = d.u64(); dd.task_type = d.u8();
            uint8_t ne = d.u8(); for (uint8_t i = 0; i < ne; i++) { auto k = d.str(); auto v = d.str(); dd.evidence.push_back({k, v}); }
            uint32_t v = g.find(a); if (v == none) return;
            set_label(v, L); std::unique_lock<std::shared_mutex> l(det_mu_); det_[v] = std::move(dd);
        } else if (kind == 8) {
            Hash t; d.take(t.data(), 32); ProtoMap m; m.signature = d.str(); m.action = d.u8(); m.family = d.u8(); m.conf = d.u8(); m.wallet = d.str(); m.asset = d.str(); m.amount = d.str(); d.take(m.contract.b, 20); m.provider = d.str(); m.model = d.str();
            std::lock_guard<std::mutex> l(proto_mu_); proto_.emplace(t, m);
        } else if (kind == 9) {
            auto j = json::parse(d.str(), nullptr, false); if (j.is_discarded()) return;
            std::lock_guard<std::mutex> l(stats_mu_);
            for (auto& [k, a] : j["providers"].items()) { auto& p = prov_[k]; p.proposals = a[0]; p.tasks = a[1]; p.accepted = a[2]; p.rejected = a[3]; p.applied = a[4]; p.missing = a[5]; p.tok_in = a[6]; p.tok_out = a[7]; p.tok_cached = a[8]; }
            for (auto& [k, c] : j["reasons"].items()) reasons_[k] = c;
            commits_ = j.value("commits", uint64_t(0));
        }
    }
    // Deterministic global propagation pass (after recovery): ascending node id over agent labels.
    void propagate_all() {
        std::vector<uint32_t> nodes; { std::unique_lock<std::shared_mutex> l(det_mu_); for (auto& [v, dd] : det_) nodes.push_back(v); }
        std::sort(nodes.begin(), nodes.end());
        std::lock_guard<std::mutex> l(commit_mu_);
        for (auto v : nodes) propagate_from(v);
        for (uint32_t v = 0; v < g.n; v++) if (g.base[v].source == s_registry && (g.base[v].category == cex_hot_wallet || g.base[v].category == bridge || g.base[v].category == mev_bot)) propagate_from(v);
    }
    uint64_t applied_since_snapshot() const { return applied_since_snap_.load(); }
    void snapshot_taken() { applied_since_snap_ = 0; }
    std::mutex& commit_mutex() { return commit_mu_; }

    // ------------------------------------------------------------ tools
    uint32_t resolve_ref(const std::string& ref, Batch* b) const {
        if (ref.size() == 42 || ref.size() == 40) return g.find(ref);
        if (!b) return none;
        if (ref.size() >= 2 && ref[0] == 'S') { int k = to_index(json(ref), "S"); return k >= 1 && size_t(k) <= b->tasks.size() ? b->tasks[size_t(k - 1)].node : none; }
        int c; uint32_t id; return b->alias.resolve(ref, c, id) && c < 3 ? id : none;
    }
    static uint32_t types_mask(const std::string& s) {
        if (s.empty()) return 0xffffffffu;
        uint32_t m = 0; size_t i = 0;
        while (i <= s.size()) { size_t z = s.find(',', i); if (z == std::string::npos) z = s.size(); int c = edge_code(s.substr(i, z - i)); if (c >= 0) m |= 1u << c; i = z + 1; }
        return m ? m : 0xffffffffu;
    }
    json node_brief(Batch* b, uint32_t u) const {
        auto l = label(u);
        json j = {{"ref", ref(b, u, nullptr)}, {"type", std::string(1, node_type(g, u))}, {"category", category_names[l.cat]}, {"confidence", std::round(conf_of(l.conf) * 100) / 100}};
        auto e = entity(u); if (!e.empty()) j["entity"] = e;
        return j;
    }
    json neighbors(Batch* b, uint32_t v, const std::string& dir, uint32_t tmask, size_t limit) {
        tool_calls_++;
        std::unique_lock<std::mutex> lk; if (b) lk = std::unique_lock<std::mutex>(b->mu);   // alias map grows
        json out = json::array(); limit = std::clamp<size_t>(limit, 1, 200);
        auto scan = [&](const Graph::Adj& a, const char* d, uint32_t total) {
            size_t got = 0; uint64_t lim = std::min<uint64_t>(a.len, 1000000);
            for (uint64_t i = 0; i < lim && got < limit; i++) {
                if (!(tmask & (1u << (a.t[i] & et_mask)))) continue;
                auto j = node_brief(b, a.nb[i]); j["dir"] = d; j["edge"] = edge_names[a.t[i] & et_mask]; j["n"] = wcount(a.w[i]);
                j["share"] = total ? std::round(double(wcount(a.w[i])) / total * 1000) / 1000 : 0.0;
                if (a.t[i] & ef_multi_asset) j["multi_asset"] = true;
                out.push_back(j); got++;
            }
        };
        if (dir == "out" || dir == "both" || dir.empty()) scan(g.out(v), "out", g.u32[d_out_n][v]);
        if (dir == "in" || dir == "both" || dir.empty()) scan(g.in(v), "in", g.u32[d_in_n][v]);
        return {{"node", node_brief(b, v)}, {"out_degree", g.out_degree(v)}, {"in_degree", g.in_degree(v)}, {"neighbors", out}};
    }
    // Bounded bidirectional BFS over typed edges; hubs (degree > hub_limit) are not expanded.
    json path(Batch* b, uint32_t s, uint32_t t, int max_depth, uint32_t tmask, bool undirected) {
        tool_calls_++;
        std::unique_lock<std::mutex> lk; if (b) lk = std::unique_lock<std::mutex>(b->mu);
        max_depth = std::clamp(max_depth, 1, 8);
        if (s == t) return {{"found", true}, {"hops", 0}, {"path", json::array({node_brief(b, s)})}};
        std::unordered_map<uint32_t, std::pair<uint32_t, uint8_t>> pf, pb;   // node -> (parent, edge type)
        pf.reserve(4096); pb.reserve(4096);
        pf[s] = {none, 0}; pb[t] = {none, 0};
        std::vector<uint32_t> ff{s}, fb{t}; int df = 0, dbk = 0; uint32_t meet = none; size_t visited = 0;
        auto expand = [&](std::vector<uint32_t>& front, auto& mine, auto& other, bool forward) {
            std::vector<uint32_t> next;
            for (size_t qi = 0; qi < front.size(); qi++) {
                uint32_t u = front[qi];
                if (qi + 4 < front.size()) { __builtin_prefetch(&g.out_off[front[qi + 4]]); __builtin_prefetch(&g.in_off[front[qi + 4]]); }
                if (u != s && u != t && g.out_degree(u) + g.in_degree(u) > ep.hub_limit) continue;
                // adjacency is weight-sorted: the fan-out cap keeps the strongest edges
                const uint64_t cap = (u == s || u == t) ? ep.path_fanout_end : ep.path_fanout;
                auto visit = [&](const Graph::Adj& a) {
                    for (uint64_t i = 0; i < std::min<uint64_t>(a.len, cap); i++) {
                        if (!(tmask & (1u << (a.t[i] & et_mask)))) continue;
                        uint32_t w = a.nb[i];
                        if (mine.count(w)) continue;
                        mine[w] = {u, a.t[i]}; visited++;
                        if (other.count(w)) { meet = w; return; }
                        next.push_back(w);
                        if (visited > ep.path_budget) return;
                    }
                };
                visit(forward ? g.out(u) : g.in(u)); if (meet != none || visited > ep.path_budget) return next;
                if (undirected) { visit(forward ? g.in(u) : g.out(u)); if (meet != none || visited > ep.path_budget) return next; }
            }
            return next;
        };
        while (meet == none && df + dbk < max_depth && visited <= ep.path_budget && !ff.empty() && !fb.empty()) {
            if (ff.size() <= fb.size()) { ff = expand(ff, pf, pb, true); df++; } else { fb = expand(fb, pb, pf, false); dbk++; }
        }
        if (meet == none) return {{"found", false}, {"visited", visited}, {"max_depth", max_depth}, {"budget_exhausted", visited > ep.path_budget}};
        std::vector<uint32_t> nodes; std::vector<uint8_t> edges;     // edges[i] = type of the edge into nodes[i]
        for (uint32_t x = meet; x != none; x = pf[x].first) { nodes.push_back(x); edges.push_back(pf[x].second); }
        std::reverse(nodes.begin(), nodes.end()); std::reverse(edges.begin(), edges.end());
        for (uint32_t x = meet; pb.count(x) && pb[x].first != none; x = pb[x].first) { nodes.push_back(pb[x].first); edges.push_back(pb[x].second); }
        json p = json::array();
        for (size_t i = 0; i < nodes.size(); i++) { auto j = node_brief(b, nodes[i]); if (i) j["edge_in"] = edge_names[edges[i] & et_mask]; p.push_back(j); }
        return {{"found", true}, {"hops", p.size() - 1}, {"visited", visited}, {"path", p}};
    }
    json feature_pack(Batch* b, uint32_t v, uint8_t type) {
        tool_calls_++;
        TaskPack t;
        if (b) { std::lock_guard<std::mutex> l(b->mu); t = render(b, v, type); } else t = render(nullptr, v, type);
        json facts = json::object(); for (auto& f : t.facts) facts[f.key] = f.val;
        std::string line; for (auto& f : t.facts) line += f.key + "=" + f.val + " ";
        json leg = json::object();
        if (b) for (auto k : t.ref_keys) { int c = int(k >> 32); if (c < 3) leg[b->alias.name(c, uint32_t(k))] = legend_entry(uint32_t(k), c); }
        return {{"node", node_brief(b, v)}, {"text", line}, {"facts", facts}, {"legend", leg}, {"input_hash", hex(View(t.hash.data(), 32))}};
    }
    json label_json(uint32_t v) {
        auto l = label(v); auto& bl = g.base[v];
        json j = {{"address", hex0x(g.addr[v])}, {"node_id", v}, {"type", std::string(1, node_type(g, v))}, {"category", category_names[l.cat]},
                  {"confidence", conf_of(l.conf)}, {"source", source_names[l.src]}, {"state", state_names[l.state]}, {"subtype", subtype_names[l.sub < subtype_count ? l.sub : 0]},
                  {"tier", l.tier}, {"importance", g.f32[d_imp][v]}, {"base", {{"category", category_names[bl.category]}, {"confidence", conf_of(bl.conf)}, {"rule", rule_names[bl.rule]}}},
                  {"needs_llm", needs_llm(v)}};
        auto e = entity(v); if (!e.empty()) j["entity"] = e;
        std::shared_lock<std::shared_mutex> lk(det_mu_); auto it = det_.find(v);
        if (it != det_.end()) { json ev = json::array(); for (auto& [k, val] : it->second.evidence) ev.push_back({{"key", k}, {"value", val}});
            j["agent"] = {{"provider", it->second.provider}, {"model", it->second.model}, {"evidence", ev}, {"warnings", it->second.warnings}, {"wal_seq", it->second.seq}, {"ts_ms", it->second.ts}}; }
        return j;
    }
    template <class F> void for_each_detail(F&& f) { std::unique_lock<std::shared_mutex> l(det_mu_); for (auto& [v, d] : det_) f(v, d); }
    template <class F> void for_each_proto(F&& f) { std::lock_guard<std::mutex> l(proto_mu_); for (auto& [t, m] : proto_) f(t, m); }

    json status() {
        json fj = json::object();
        for (int i = 0; i < 2; i++) { auto s = fr[i].stats(); fj[std::string(task_names[i])] = {{"queued", s.queued}, {"leased", s.leased}, {"members", s.members}, {"leases_total", s.leases}, {"expired", s.expired}, {"dedup_skips", s.dedup_skips}, {"done", s.done}}; }
        uint32_t now = uint32_t(now_ms() / 1000); uint64_t c10 = 0, c60 = 0;
        for (uint32_t k = 1; k <= 60; k++) { uint32_t sec = now - k; if (commit_ring_sec_[sec % commit_ring_.size()].load() == sec) { auto c = commit_ring_[sec % commit_ring_.size()].load(); c60 += c; if (k <= 10) c10 += c; } }
        json pj = json::object(), rj = json::object();
        { std::lock_guard<std::mutex> l(stats_mu_);
          for (auto& [k, p] : prov_) pj[k] = {{"proposals", p.proposals}, {"tasks", p.tasks}, {"accepted", p.accepted}, {"rejected", p.rejected}, {"applied", p.applied}, {"missing", p.missing},
                                              {"accept_rate", p.accepted + p.rejected ? double(p.accepted) / double(p.accepted + p.rejected) : 0.0},
                                              {"tokens_in", p.tok_in}, {"tokens_out", p.tok_out}, {"tokens_cached", p.tok_cached}, {"tokens_in_per_task", p.tasks ? double(p.tok_in) / p.tasks : 0.0}};
          for (auto& [k, c] : reasons_) rj[k] = c; }
        json w = nullptr;
        if (wal) { auto s = wal->stats(); w = {{"next_seq", s.next_seq}, {"durable_seq", s.durable_seq}, {"fsyncs", s.fsyncs}, {"frames", s.frames}, {"bytes", s.bytes}, {"avg_records_per_fsync", s.avg_batch}, {"max_batch", s.max_batch}}; }
        size_t agent_labels; { std::unique_lock<std::shared_mutex> l(det_mu_); agent_labels = det_.size(); }
        return {{"graph", {{"chain", g.chain}, {"chain_id", g.chain_id}, {"graph_id", g.graph_id}, {"nodes", g.n}, {"edges", g.m}, {"mapped_bytes", g.mapped_bytes()}}},
                {"thresholds", {{"resolve", ep.resolve}, {"llm_importance_min", ep.llm_imp_min}, {"confirm_importance_min", ep.confirm_imp_min}, {"protocol_importance_min", ep.proto_imp_min}, {"ttl_s", ep.ttl_s}}},
                {"frontier", fj}, {"batches_open", batches_open()}, {"leased_tasks_total", leased_tasks_.load()}, {"proposals_total", proposals_.load()},
                {"commits", {{"total", commits_.load()}, {"per_sec_10s", double(c10) / 10.0}, {"per_sec_60s", double(c60) / 60.0}}},
                {"agent_labels", agent_labels}, {"propagation", {{"backlog", propagation_backlog()}, {"labels_changed", prop_changes_.load()}}}, {"providers", pj}, {"reject_reasons", rj}, {"wal", w}, {"tool_calls", tool_calls_.load()},
                {"snapshot", {{"seq", last_snapshot_seq.load()}, {"age_s", last_snapshot_ms.load() ? double(now_ms() - last_snapshot_ms.load()) / 1000 : -1.0}}},
                {"rss_bytes", rss_bytes()}, {"context_sha256", context_sha}};
    }
    json coverage(int threads) {
        std::array<std::atomic<uint64_t>, category_count> cat{}, res{}; std::array<std::atomic<uint64_t>, source_count> src{}; std::atomic<uint64_t> resolved{0}, llm{0};
        parallel_for(g.n, threads, [&](size_t b, size_t e, int) {
            for (size_t v = b; v < e; v++) { auto l = label(uint32_t(v)); cat[l.cat]++; src[l.src]++; if (conf_of(l.conf) >= ep.resolve && l.state != ls_disputed) { resolved++; res[l.cat]++; } if (needs_llm(uint32_t(v))) llm++; }
        }, 1 << 16);
        json c = json::object(); for (int i = 0; i < category_count; i++) c[std::string(category_names[i])] = {{"nodes", cat[i].load()}, {"resolved", res[i].load()}};
        json s = json::object(); for (int i = 0; i < source_count; i++) s[std::string(source_names[i])] = src[i].load();
        return {{"nodes", g.n}, {"resolved", resolved.load()}, {"resolved_pct", g.n ? 100.0 * double(resolved.load()) / g.n : 0.0}, {"needs_llm", llm.load()}, {"by_category", c}, {"by_source", s}};
    }
};
} // namespace machine::graphloop
