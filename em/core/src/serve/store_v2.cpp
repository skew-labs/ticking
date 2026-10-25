// ChainStore over one sealed EMV2 build (api.hpp readers): dict + blk from core, graph::* and ledger::* when their libraries are
// linked (HAVE_EMV2_GRAPH / HAVE_EMV2_LEDGER, defined by CMake for every reader library present). A build without a module's
// segments simply lacks that capability; the routes answer 501 for it.
#include <cctype>
#include <mutex>
#include <machine/engine/api.hpp>
#include <machine/engine/dict.hpp>
#include <machine/engine/overlay.hpp>
#include <machine/engine/seg.hpp>
#include <machine/engine/serve_store.hpp>
#include <machine/graphloop/schema.hpp>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <map>
#include <unordered_map>

// The graph and ledger readers are weak references: a reader library that is linked but does not implement the api.hpp entry
// points yet (or is not linked at all) leaves them null, and the build simply lacks that capability instead of failing to link.
namespace machine::engine::graph {
[[gnu::weak]] std::shared_ptr<const Image> open(const BuildDir& b);
[[gnu::weak]] NodeAttr node(const Image& g, uint32_t id);
[[gnu::weak]] EdgeResult edges(const Image& g, uint32_t id, const EdgeQuery& q);
} // namespace machine::engine::graph
namespace machine::engine::ledger {
[[gnu::weak]] std::shared_ptr<const Store> open(const BuildDir& b);
[[gnu::weak]] HoldersAt holders_at(const Store& s, uint32_t tid, uint64_t block, uint32_t limit);
[[gnu::weak]] BalanceAt balance_at(const Store& s, uint32_t addr, uint32_t tid, uint64_t block);
[[gnu::weak]] bool events(const Store& s, const EventQuery& q, const std::function<void(const Event&)>& f);
[[gnu::weak]] std::optional<TokenStats> token_stats(const Store& s, uint32_t tid);
} // namespace machine::engine::ledger

namespace machine::engine::serve {
namespace {
namespace eng = machine::engine;
namespace fs = std::filesystem;
template <class F> bool linked(F* f) { return reinterpret_cast<void*>(f) != nullptr; }

const std::map<std::string, uint64_t> kChainId = {
    {"ethereum", 1}, {"optimism", 10}, {"bsc", 56}, {"unichain", 130}, {"polygon", 137}, {"hyperevm", 999}, {"robinhood", 4663}, {"arc", 5042},
    {"base", 8453}, {"arbitrum", 42161}, {"avalanche", 43114}, {"linea", 59144}, {"xlayer", 196}};

Raw256 raw(const eng::U256& u) { Raw256 r; std::memcpy(r.w, u.w, sizeof r.w); return r; }

class V2Store final : public ChainStore {
public:
    explicit V2Store(const std::string& dir) : b_(dir) {
        chain_ = b_.chain(); build_ = b_.build_id();
        graphloop::need(!chain_.empty() && !build_.empty(), "manifest: chain/build_id");
        auto ci = kChainId.find(chain_); chain_id_ = ci == kChainId.end() ? 0 : ci->second;
        if (b_.has(eng::Kind::dict_addr_sfx) && b_.has(eng::Kind::dict_addr_pfx)) {
            dict_ = eng::AddrDict(b_); caps_ |= kDict;
            eng::Addr z{}, d = graphloop::addr_from("0x000000000000000000000000000000000000dead");
            if (auto i = dict_.find(z)) zero_id_ = *i;
            if (auto i = dict_.find(d)) dead_id_ = *i;
        }
        if (b_.has(eng::Kind::dict_tok) && b_.has(eng::Kind::dict_tok_str)) { toks_ = eng::TokenDict(b_); has_toks_ = true; }
        if (b_.has(eng::Kind::blk_time)) { bt_ = eng::BlockTime(b_); caps_ |= kBlocks; }
        {   // address-keyed labels and token metadata beside the build (<root>/<chain>/overlay); optional
            std::error_code ec;
            fs::path od = fs::path(dir).parent_path() / "overlay";
            if (fs::exists(od / "overlay.json", ec)) {
                try { ovl_ = eng::Overlay(od); has_ovl_ = true; caps_ |= kLabels;
                      ovl_stamp_ = std::chrono::duration_cast<std::chrono::nanoseconds>(fs::last_write_time(od / "overlay.json").time_since_epoch()).count(); }
                catch (const std::exception& e) { ovl_err_ = e.what(); }
            }
        }
#ifdef HAVE_EMV2_GRAPH
        if (b_.has(eng::Kind::g_out_off) && b_.has(eng::Kind::g_in_off) && linked(&eng::graph::open) && linked(&eng::graph::node) && linked(&eng::graph::edges)) {
            try {
                g_ = eng::graph::open(b_); caps_ |= kGraph;
                if (b_.has(eng::Kind::g_node_label)) caps_ |= kLabels;
            } catch (const std::exception& e) { graph_err_ = e.what(); g_.reset(); }   // e.g. layout guard: serve the rest
        }
#endif
#ifdef HAVE_EMV2_LEDGER
        if (b_.has_any(eng::Kind::tok_stats) && (b_.has_any(eng::Kind::bal_top) || b_.has_any(eng::Kind::bal_ckpt_index)) && linked(&eng::ledger::open) &&
            linked(&eng::ledger::holders_at) && linked(&eng::ledger::token_stats)) {
            try {
                l_ = eng::ledger::open(b_); caps_ |= kHolders;
                if (linked(&eng::ledger::events) && linked(&eng::ledger::balance_at)) caps_ |= kEvents;
            } catch (const std::exception& e) { ledger_err_ = e.what(); l_.reset(); }   // e.g. ledger_format mismatch
        }
#endif
        auto& m = b_.manifest();
        head_ = m.contains("head_block") && m["head_block"].is_number_unsigned() ? m["head_block"].get<uint64_t>() : (caps_ & kBlocks ? bt_.last_block() : 0);
        head_t_ = time_at(head_);
    }
    const std::string& chain() const override { return chain_; }
    uint64_t chain_id() const override { return chain_id_; }
    const std::string& build_id() const override { return build_; }
    uint64_t head_block() const override { return head_; }
    uint32_t head_time() const override { return head_t_; }

    NodeId node_of(const Addr& a) const override {
        if (!(caps_ & kDict)) return kNone;
        auto id = dict_.find(a);
        return id ? *id : kNone;
    }
    bool node_attr(NodeId n, NodeAttr& out) const override {
        if (!(caps_ & kDict) || n == kNone || n >= dict_.size()) return false;
        out = NodeAttr{};
        out.addr = dict_.get(n); out.known = true;
#ifdef HAVE_EMV2_GRAPH
        if (g_) {
            eng::graph::NodeAttr a = eng::graph::node(*g_, n);
            if (a.category < graphloop::category_count) out.category = std::string(graphloop::category_names[a.category]);
            out.entity = std::string(a.entity);
            out.confidence = float(a.conf) / 255.0f;
            out.is_contract = a.is_contract; out.hub = a.is_hub;
            out.first_block = a.first_block; out.last_block = a.last_block;
            out.first_time = time_at(a.first_block); out.last_time = time_at(a.last_block);
            out.tx_sent = a.tx_sent; out.tx_received = a.tx_received; out.contracts_created = uint32_t(a.contracts_created);
            out.in_degree = uint32_t(std::min<uint64_t>(a.in_degree, 0xffffffffu)); out.out_degree = uint32_t(std::min<uint64_t>(a.out_degree, 0xffffffffu));
            out.label_source = "emv2";
        }
#endif
        if (has_ovl_) {   // labels: the overlay (graphloop / Muse labels) wins over the build's own base label
            if (const eng::OvlLabel* l = ovl_.label(out.addr.b)) {
                if (l->category < graphloop::category_count) out.category = std::string(graphloop::category_names[l->category]);
                out.entity = std::string(ovl_.str(l->entity_off, l->entity_len));
                out.confidence = float(l->conf) / 255.0f;
                out.label_source = l->source < graphloop::source_count ? std::string(graphloop::source_names[l->source]) : "overlay";
                if (graphloop::contract_only(l->category)) out.is_contract = true;
            }
            if (ovl_.token(out.addr.b)) out.is_contract = true;   // a token contract is a contract even when the map missed its creation
        }
        return true;
    }
    int64_t overlay_stamp() const override { return ovl_stamp_; }
    bool window_flows(NodeId n, uint64_t b0, uint64_t b1, size_t max_events, std::vector<EdgeAgg>& out_, std::vector<EdgeAgg>& in_,
                      bool& truncated) const override {
        out_.clear(); in_.clear(); truncated = false;
#ifdef HAVE_EMV2_LEDGER
        if (!l_ || !(caps_ & kEvents) || n == kNone) return false;
        eng::ledger::EventQuery q; q.addr = n; q.tid = eng::none32; q.block0 = b0 + 1; q.block1 = b1;
        q.limit = uint32_t(std::min<size_t>(max_events, 0xfffffffeu)); q.newest_first = false;
        std::map<std::tuple<NodeId, TokenId, int>, size_t> at;
        bool complete = eng::ledger::events(*l_, q, [&](const eng::ledger::Event& e) {
            if (e.from == e.to) return;
            bool out = e.from == n;
            NodeId peer = out ? e.to : e.from;
            auto& v = out ? out_ : in_;
            auto [it, fresh] = at.try_emplace({peer, e.tid, out ? 0 : 1}, v.size());
            if (fresh) {
                EdgeAgg a; a.peer = peer; a.asset = e.tid; a.kind = e.kind == eng::TokenKind::erc20 ? 1 : 2; a.has_amount = a.kind == 1;
                a.first_block = e.block; v.push_back(a);
            }
            EdgeAgg& a = v[it->second];
            a.n++; a.last_block = e.block;
            if (a.kind == 1 && a.has_amount) {
                eng::U256 x, sum; std::memcpy(x.w, a.amount.w, sizeof x.w);
                if (!eng::add(x, e.amount, sum)) a.has_amount = false; else a.amount = raw(sum);   // > 2^256: amount unknown
            }
        });
        truncated = !complete;
        return true;
#else
        (void)n; (void)b0; (void)b1; (void)max_events;
        return false;
#endif
    }
    bool token_meta(const Addr& a, std::string& symbol, int& decimals) const override {
        if (!has_ovl_) return false;
        const eng::OvlToken* m = ovl_.token(a.b);
        if (!m) return false;
        symbol = std::string(ovl_.str(m->sym_off, m->sym_len)); decimals = m->decimals == 255 ? -1 : m->decimals;
        return true;
    }
    uint64_t hot_degree(NodeId n) const override {
#ifdef HAVE_EMV2_GRAPH
        if (g_) return eng::graph::hot_degree(*g_, n);
#endif
        (void)n; return ~0ull;
    }
    void prefetch_nodes(const std::vector<NodeId>& nodes) const override {
#ifdef HAVE_EMV2_GRAPH
        if (g_) eng::graph::prefetch_nodes(*g_, nodes);
#else
        (void)nodes;
#endif
    }
    // Symbol index (lowercase symbol hash -> token ids), built on first use; the registry warms it at load, off the request path.
    static uint64_t sym_hash(std::string_view v) {
        uint64_t h = 1469598103934665603ull;
        for (char c : v) { h ^= uint8_t(std::tolower(uint8_t(c))); h *= 1099511628211ull; }
        return h;
    }
    std::string_view sym_of(uint32_t t) const {
        std::string_view sym = toks_.symbol(t);
        if (sym.empty() && has_ovl_) {
            Addr a = dict_.get(toks_.rec(t).addr_id);
            if (const eng::OvlToken* m = ovl_.token(a.b)) sym = ovl_.str(m->sym_off, m->sym_len);
        }
        return sym;
    }
    void find_tokens(std::string_view symbol, size_t cap, std::vector<TokenId>& out) const override {
        out.clear();
        if (!has_toks_ || !(caps_ & kDict)) return;
        std::call_once(sym_once_, [&] {
            sym_idx_.reserve(toks_.size());
            for (uint32_t t = 0; t < toks_.size(); t++) { std::string_view v = sym_of(t); if (!v.empty()) sym_idx_.push_back({sym_hash(v), t}); }
            std::sort(sym_idx_.begin(), sym_idx_.end());
        });
        if (symbol.empty() || !cap) return;
        auto same = [&](std::string_view a) {
            if (a.size() != symbol.size()) return false;
            for (size_t i = 0; i < a.size(); i++) if (std::tolower(uint8_t(a[i])) != std::tolower(uint8_t(symbol[i]))) return false;
            return true;
        };
        uint64_t h = sym_hash(symbol);
        auto it = std::lower_bound(sym_idx_.begin(), sym_idx_.end(), std::pair<uint64_t, uint32_t>{h, 0});
        for (; it != sym_idx_.end() && it->first == h && out.size() < cap; ++it) if (same(sym_of(it->second))) out.push_back(it->second);
    }
    std::vector<NodeId> entity_nodes(std::string_view name, size_t cap) const override {
        std::vector<NodeId> out;
        if (!has_ovl_ || !(caps_ & kDict)) return out;
        for (auto& a : ovl_.entity_addrs(name, cap)) if (auto id = dict_.find(a)) out.push_back(*id);
        return out;
    }
    void peers(NodeId n, Dir d, size_t limit, const std::function<bool(NodeId, uint64_t, uint8_t)>& cb) const override {
#ifdef HAVE_EMV2_GRAPH
        if (!g_ || n == kNone) return;
        eng::graph::EdgeQuery q;
        q.dir = d == Dir::in ? eng::graph::Dir::in : eng::graph::Dir::out;
        q.limit = uint32_t(std::min<size_t>(limit, 100000)); q.want_amount = false; q.split_assets = false;
        auto res = eng::graph::edges(*g_, n, q);
        for (auto& e : res.edges) {
            uint8_t ty = uint8_t(e.type) & graphloop::et_mask;
            if (!cb(e.peer, e.n, ty == graphloop::e_native ? 0 : ty == graphloop::e_erc20 ? 1 : 2)) break;
        }
#else
        (void)n; (void)d; (void)limit; (void)cb;
#endif
    }
    TokenId token_of(const Addr& a) const override {
        if (!has_toks_ || !(caps_ & kDict)) return kNone;
        auto id = dict_.find(a); if (!id) return kNone;
        if (auto t = toks_.find(*id, eng::TokenKind::erc20)) return *t;
        uint32_t first = 0; auto span = toks_.by_addr(*id, first);
        return span.empty() ? kNone : first;
    }
    bool token_attr(TokenId t, TokenAttr& out) const override {
        if (!has_toks_ || t == kNone || t >= toks_.size()) return false;
        const auto& r = toks_.rec(t);
        out = TokenAttr{};
        out.addr = dict_.get(r.addr_id);
        out.symbol = std::string(toks_.symbol(t));
        out.decimals = r.decimals == 255 ? 18 : r.decimals;     // invscreen token_meta: 18 when decimals are unknown
        if (has_ovl_)
            if (const eng::OvlToken* m = ovl_.token(out.addr.b)) {   // econ token_meta: symbol and decimals
                if (out.symbol.empty()) out.symbol = std::string(ovl_.str(m->sym_off, m->sym_len));
                if (r.decimals == 255 && m->decimals != 255) out.decimals = m->decimals;
            }
        out.first_block = r.first_block; out.first_time = time_at(r.first_block);
#ifdef HAVE_EMV2_LEDGER
        if (l_) {
            if (auto st = eng::ledger::token_stats(*l_, t)) {
                if (!st->supply.negative()) { out.has_supply = true; out.supply = raw(st->supply.v); }
                out.transfers = st->n_transfers; out.holders_approx = st->n_holders;
                out.last_block = st->last_block; out.last_time = time_at(st->last_block);
            }
        }
#endif
#ifdef HAVE_EMV2_GRAPH
        if (g_) {   // the token contract's creator (contract creation tx.from in the map)
            eng::graph::NodeAttr a = eng::graph::node(*g_, r.addr_id);
            if (a.creator != eng::none32 && a.creator < dict_.size()) { out.has_creator = true; out.creator = dict_.get(a.creator); }
        }
#endif
        return true;
    }
    uint64_t block_at(uint32_t t) const override {
        if (!(caps_ & kBlocks)) return 0;
        auto b = bt_.block_at(t);
        return b ? std::min<uint64_t>(*b, head_) : 0;
    }
    uint32_t time_at(uint64_t block) const override {
        if (!(caps_ & kBlocks) || bt_.count() == 0) return 0;
        uint64_t b = std::clamp<uint64_t>(block, bt_.first_block(), bt_.last_block());
        return bt_.time_at(b).value_or(0);
    }
    bool edges(NodeId n, Dir d, uint64_t b0, uint64_t b1, const std::function<bool(const EdgeAgg&)>& cb, bool& truncated, Tier& tier) const override {
        return edges_q(n, d, b0, b1, 20000, cb, truncated, tier);
    }
    bool edges_top(NodeId n, Dir d, uint64_t b0, uint64_t b1, size_t limit, const std::function<bool(const EdgeAgg&)>& cb, bool& truncated,
                   Tier& tier) const override {
        return edges_q(n, d, b0, b1, uint32_t(std::clamp<size_t>(limit, 1, 20000)), cb, truncated, tier);
    }
    // graph::edges orders by count and cuts at q.limit before it reads exact detail rows, so a small limit reads few peers
    bool edges_q(NodeId n, Dir d, uint64_t b0, uint64_t b1, uint32_t limit, const std::function<bool(const EdgeAgg&)>& cb, bool& truncated,
                 Tier& tier) const {
        truncated = false; tier = Tier::hot;
#ifdef HAVE_EMV2_GRAPH
        if (!g_ || n == kNone) return false;
        eng::graph::EdgeQuery q;
        q.dir = d == Dir::in ? eng::graph::Dir::in : eng::graph::Dir::out;
        // one record per (peer, kind, asset) with exact n / amount / blocks, filtered on the exact blocks (invscreen's in_top/out_top rows)
        q.block0 = b0; q.block1 = b1; q.limit = limit; q.want_amount = true; q.split_assets = true;
        auto res = eng::graph::edges(*g_, n, q);
        truncated = res.truncated;
        if (res.cold_missing) tier = Tier::cold;   // part of the answer lives in the cold tier and was not read here
        for (auto& e : res.edges) {
            if (e.flags & eng::graph::ef_from_cold) tier = Tier::cold;
            // the hot lists filter by day on one side only: re-check the asked block window on the exact blocks
            if ((e.flags & eng::graph::ef_exact_n) && (e.last_block < b0 || e.first_block > b1)) continue;
            EdgeAgg a;
            a.peer = e.peer;
            uint8_t ty = uint8_t(e.type) & graphloop::et_mask;
            // asset: the token when the reader names it; native for native edges; otherwise unknown (kNone: symbol/amount null)
            a.asset = e.asset_tid != eng::none32 ? e.asset_tid : ty == graphloop::e_native ? kNative : kNone;
            if (a.asset == kNone && e.asset_node != eng::none32) a.asset_node = e.asset_node;
            a.kind = ty == graphloop::e_native ? 0 : ty == graphloop::e_erc20 ? 1 : 2;
            // erc721 / erc1155 totals are not comparable amounts (map edges: total_amount NULL): answer null, never "0"
            a.n = e.n; a.amount = raw(e.amount); a.has_amount = (e.flags & eng::graph::ef_amount) != 0 && a.kind != 2;
            a.first_block = e.first_block; a.last_block = e.last_block;
            if (!cb(a)) break;
        }
        return true;
#else
        (void)n; (void)d; (void)b0; (void)b1; (void)cb;
        return false;
#endif
    }
    Avail net_changes(TokenId t, uint64_t b0, uint64_t b1, size_t max_events, std::vector<NetChange>& out, bool& truncated,
                      uint64_t& scanned) const override {
        out.clear(); truncated = false; scanned = 0;
#ifdef HAVE_EMV2_LEDGER
        if (!l_ || !(caps_ & kEvents) || t == kNone) return Avail::missing;
        if (b1 <= b0) return Avail::ok;
        eng::ledger::EventQuery q; q.tid = t; q.addr = eng::none32; q.block0 = b0 + 1; q.block1 = b1;
        q.limit = uint32_t(std::min<size_t>(max_events, 0xfffffffeu)); q.newest_first = false;
        std::unordered_map<NodeId, size_t> at;
        auto bump = [&](NodeId n, const eng::I256& d, bool in) {
            if (n == eng::none32 || sink(n)) return;
            auto [it, fresh] = at.try_emplace(n, out.size());
            if (fresh) { NetChange c; c.node = n; out.push_back(c); }
            NetChange& c = out[it->second];
            (void)eng::add(c.delta, d, c.delta);   // windows never overflow I256 in practice; a wrap would show as a sign error in verify
            if (in) c.n_in++; else c.n_out++;
        };
        bool complete = eng::ledger::events(*l_, q, [&](const eng::ledger::Event& e) {
            scanned++;
            if (e.from == e.to) return;
            auto amt = eng::I256::from_u256(e.amount);
            eng::I256 v = amt ? *amt : eng::I256{};
            eng::I256 neg; (void)eng::neg(v, neg);
            bump(e.to, v, true); bump(e.from, neg, false);
        });
        truncated = !complete;
        return Avail::ok;
#else
        (void)t; (void)b0; (void)b1; (void)max_events;
        return Avail::missing;
#endif
    }
    Avail holders_at(TokenId t, uint64_t block, size_t limit, HoldersAt& out) const override {
#ifdef HAVE_EMV2_LEDGER
        if (!l_ || t == kNone) return Avail::missing;
        auto h = eng::ledger::holders_at(*l_, t, block, uint32_t(limit));
        if (h.overflow) return Avail::preparing;
        out = HoldersAt{};
        // cold_missing: ranks past the checkpoint's top-1000 were not proven; the returned balances are still exact
        if (h.cold_missing) out.tier = Tier::cold;
        out.holders = h.holders;
        for (auto& x : h.top) {
            if (x.balance.sign() <= 0) continue;
            Holding hd; hd.holder = x.addr; hd.amount = raw(x.balance.v);
            out.held += hd.amount;
            out.top.push_back(hd);
        }
        return Avail::ok;
#else
        (void)t; (void)block; (void)limit; (void)out;
        return Avail::missing;
#endif
    }
    Avail token_events(TokenId t, NodeId n, const std::function<bool(const TokenEvent&)>& cb, Tier& tier) const override {
        tier = Tier::hot;
#ifdef HAVE_EMV2_LEDGER
        if (!l_ || !(caps_ & kEvents)) return Avail::missing;
        eng::ledger::EventQuery q; q.tid = t == kNone ? eng::none32 : t; q.addr = n == kNone ? eng::none32 : n; q.limit = 5000; q.newest_first = false;
        bool stop = false;
        eng::ledger::events(*l_, q, [&](const eng::ledger::Event& e) {
            if (stop) return;
            TokenEvent te; te.block = e.block; te.time = e.time; te.log_index = e.log_index;
            if (e.has_hash) std::memcpy(te.tx.data(), e.tx_hash, 32);
            te.from = e.from; te.to = e.to; te.amount = raw(e.amount);
            if (!cb(te)) stop = true;
        });
        return Avail::ok;
#else
        (void)t; (void)n; (void)cb;
        return Avail::missing;
#endif
    }
    Avail balance_at(NodeId n, TokenId t, uint64_t block, Raw256& out) const override {
#ifdef HAVE_EMV2_LEDGER
        if (!l_ || !(caps_ & kEvents)) return Avail::missing;
        auto b = eng::ledger::balance_at(*l_, n, t, block);
        if (b.overflow) return Avail::preparing;
        out = b.balance.negative() ? Raw256{} : raw(b.balance.v);
        return Avail::ok;
#else
        (void)n; (void)t; (void)block; (void)out;
        return Avail::missing;
#endif
    }
    uint32_t caps() const override { return caps_; }
    std::string missing_reason(uint32_t cap) const override {
        if ((cap & (kHolders | kEvents)) && !ledger_err_.empty()) return "ledger: " + ledger_err_;
        if ((cap & (kGraph | kLabels)) && !graph_err_.empty()) return "graph: " + graph_err_;
        return {};
    }

private:
    std::string graph_err_, ledger_err_;
    bool sink(NodeId n) const noexcept { return n == zero_id_ || n == dead_id_; }
    eng::Overlay ovl_; bool has_ovl_ = false; int64_t ovl_stamp_ = 0; std::string ovl_err_;
    NodeId zero_id_ = kNone, dead_id_ = kNone;
    eng::BuildDir b_;
    eng::AddrDict dict_;
    eng::TokenDict toks_;
    bool has_toks_ = false;
    mutable std::once_flag sym_once_;
    mutable std::vector<std::pair<uint64_t, uint32_t>> sym_idx_;
    eng::BlockTime bt_;
#ifdef HAVE_EMV2_GRAPH
    std::shared_ptr<const eng::graph::Image> g_;
#endif
#ifdef HAVE_EMV2_LEDGER
    std::shared_ptr<const eng::ledger::Store> l_;
#endif
    std::string chain_, build_;
    uint64_t chain_id_ = 0, head_ = 0;
    uint32_t head_t_ = 0, caps_ = 0;
};
} // namespace

StoreRef open_v2(const std::string& dir) { return std::make_shared<V2Store>(dir); }
} // namespace machine::engine::serve
