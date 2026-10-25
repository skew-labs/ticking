// ChainStore over one sealed EMV2 build (api.hpp readers): dict + blk from core, graph::* and ledger::* when their libraries are
// linked (HAVE_EMV2_GRAPH / HAVE_EMV2_LEDGER, defined by CMake for every reader library present). A build without a module's
// segments simply lacks that capability; the routes answer 501 for it.
#include <machine/engine/api.hpp>
#include <machine/engine/dict.hpp>
#include <machine/engine/seg.hpp>
#include <machine/engine/serve_store.hpp>
#include <machine/graphloop/schema.hpp>
#include <cstring>
#include <map>

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
        if (b_.has(eng::Kind::dict_addr_sfx) && b_.has(eng::Kind::dict_addr_pfx)) { dict_ = eng::AddrDict(b_); caps_ |= kDict; }
        if (b_.has(eng::Kind::dict_tok) && b_.has(eng::Kind::dict_tok_str)) { toks_ = eng::TokenDict(b_); has_toks_ = true; }
        if (b_.has(eng::Kind::blk_time)) { bt_ = eng::BlockTime(b_); caps_ |= kBlocks; }
#ifdef HAVE_EMV2_GRAPH
        if (b_.has(eng::Kind::g_out_off) && b_.has(eng::Kind::g_in_off) && linked(&eng::graph::open) && linked(&eng::graph::node) && linked(&eng::graph::edges)) {
            try {
                g_ = eng::graph::open(b_); caps_ |= kGraph;
                if (b_.has(eng::Kind::g_node_label)) caps_ |= kLabels;
            } catch (const std::exception& e) { graph_err_ = e.what(); g_.reset(); }   // e.g. layout guard: serve the rest
        }
#endif
#ifdef HAVE_EMV2_LEDGER
        if (b_.has(eng::Kind::tok_stats) && (b_.has(eng::Kind::bal_top) || b_.has(eng::Kind::bal_ckpt_index)) && linked(&eng::ledger::open) &&
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
            out.is_contract = a.is_contract;
            out.first_block = a.first_block; out.last_block = a.last_block;
            out.first_time = time_at(a.first_block); out.last_time = time_at(a.last_block);
            out.tx_sent = a.tx_sent; out.tx_received = a.tx_received; out.contracts_created = uint32_t(a.contracts_created);
            out.in_degree = uint32_t(std::min<uint64_t>(a.in_degree, 0xffffffffu)); out.out_degree = uint32_t(std::min<uint64_t>(a.out_degree, 0xffffffffu));
            out.label_source = "emv2";
        }
#endif
        return true;
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
        truncated = false; tier = Tier::hot;
#ifdef HAVE_EMV2_GRAPH
        if (!g_ || n == kNone) return false;
        eng::graph::EdgeQuery q;
        q.dir = d == Dir::in ? eng::graph::Dir::in : eng::graph::Dir::out;
        // one record per (peer, kind, asset) with exact n / amount / blocks, filtered on the exact blocks (invscreen's in_top/out_top rows)
        q.block0 = b0; q.block1 = b1; q.limit = 20000; q.want_amount = true; q.split_assets = true;
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
            a.kind = ty == graphloop::e_native ? 0 : ty == graphloop::e_erc20 ? 1 : 2;
            a.n = e.n; a.amount = raw(e.amount); a.has_amount = (e.flags & eng::graph::ef_amount) != 0;
            a.first_block = e.first_block; a.last_block = e.last_block;
            if (!cb(a)) break;
        }
        return true;
#else
        (void)n; (void)d; (void)b0; (void)b1; (void)cb;
        return false;
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
    eng::BuildDir b_;
    eng::AddrDict dict_;
    eng::TokenDict toks_;
    bool has_toks_ = false;
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
