// A small chain from one JSON file, for serve tests and smoke runs before real builds exist. Same ChainStore contract as
// store_v2.cpp; balances are replayed exactly from the listed transfers (Raw256, no floating point).
//   {"kind":"synth","chain":"robinhood","chain_id":4663,"build_id":"synth-1","head_block":1000,
//    "blocks":[[first_block, first_time], [last_block, last_time]],             (linear block time)
//    "nodes":[{"addr":"0x..","category":"dex_pool","entity":"","class":"","is_contract":false,"tx_sent":0,"tx_received":0,
//              "first_block":0,"last_block":0,"in_degree":0,"out_degree":0,"conf":0.2}],
//    "tokens":[{"node":3,"symbol":"CAT","decimals":18,"creator":1}],           (node = index of the token contract in nodes)
//    "edges":[{"from":0,"to":1,"asset":-1,"n":5,"amount":"123","first_block":1,"last_block":20,"exact":true}],   (asset -1 = native)
//    "transfers":[{"token":0,"block":12,"from":0,"to":1,"amount":"1000"}]}      (from -1 = mint)
#include <machine/engine/serve_store.hpp>
#include <machine/graphloop/durable.hpp>
#include <nlohmann/json.hpp>
#include <algorithm>
#include <map>

namespace machine::engine::serve {
namespace {
using json = nlohmann::json;

Raw256 parse_dec(const std::string& s) {
    Raw256 r;
    for (char c : s) {
        graphloop::need(c >= '0' && c <= '9', "synth: amount digits");
        unsigned __int128 carry = uint64_t(c - '0');
        for (int i = 0; i < 4; i++) { unsigned __int128 v = (unsigned __int128)r.w[i] * 10 + carry; r.w[i] = uint64_t(v); carry = v >> 64; }
    }
    return r;
}
// a - b for a >= b
Raw256 sub(Raw256 a, const Raw256& b) {
    unsigned __int128 borrow = 0;
    for (int i = 0; i < 4; i++) {
        unsigned __int128 x = (unsigned __int128)a.w[i] - b.w[i] - borrow;
        a.w[i] = uint64_t(x); borrow = (x >> 64) ? 1 : 0;
    }
    return a;
}

class SynthStore final : public ChainStore {
    struct Tr { TokenId token; uint64_t block; uint32_t log; NodeId from, to; Raw256 amount; };
    std::string chain_, build_;
    uint64_t chain_id_ = 0, head_ = 0, b0_ = 0, b1_ = 1;
    uint32_t t0_ = 0, t1_ = 1;
    std::vector<NodeAttr> nodes_;
    std::map<std::string, NodeId> by_addr_;
    std::vector<TokenAttr> toks_;
    std::vector<NodeId> tok_node_;
    std::vector<std::pair<NodeId, EdgeAgg>> out_, in_;   // (owner, edge with peer = other side)
    std::vector<Tr> trs_;
public:
    explicit SynthStore(const std::string& path) {
        json m = json::parse(graphloop::load_text(path));
        chain_ = m.at("chain"); build_ = m.at("build_id"); chain_id_ = m.value("chain_id", 0ull); head_ = m.at("head_block");
        auto& bl = m.at("blocks");
        b0_ = bl[0][0]; t0_ = bl[0][1]; b1_ = bl[1][0]; t1_ = bl[1][1];
        for (auto& n : m.at("nodes")) {
            NodeAttr a; a.addr = graphloop::addr_from(n.at("addr").get<std::string>()); a.known = true;
            a.category = n.value("category", "unknown"); a.entity = n.value("entity", ""); a.cls = n.value("class", "");
            a.is_contract = n.value("is_contract", false); a.delegated = n.value("delegated", false);
            a.tx_sent = n.value("tx_sent", 0ull); a.tx_received = n.value("tx_received", 0ull);
            a.first_block = n.value("first_block", 0ull); a.last_block = n.value("last_block", 0ull);
            a.first_time = time_at(a.first_block); a.last_time = time_at(a.last_block);
            a.in_degree = n.value("in_degree", 0u); a.out_degree = n.value("out_degree", 0u); a.confidence = n.value("conf", 0.0f);
            a.contracts_created = n.value("contracts_created", 0u); a.dex_trades = n.value("dex_trades", 0ull);
            by_addr_[graphloop::hex0x(a.addr)] = NodeId(nodes_.size());
            nodes_.push_back(a);
        }
        for (auto& t : m.value("tokens", json::array())) {
            TokenAttr a; NodeId nd = t.at("node"); a.addr = nodes_.at(nd).addr; a.symbol = t.value("symbol", ""); a.decimals = t.value("decimals", 18);
            if (t.contains("creator")) { a.has_creator = true; a.creator = nodes_.at(t["creator"].get<NodeId>()).addr; }
            toks_.push_back(a); tok_node_.push_back(nd);
        }
        for (auto& e : m.value("edges", json::array())) {
            EdgeAgg x; x.n = e.at("n"); x.amount = parse_dec(e.value("amount", "0")); x.has_amount = e.value("exact", true);
            int asset = e.value("asset", -1); x.asset = asset < 0 ? kNative : TokenId(asset); x.kind = asset < 0 ? 0 : 1;
            x.first_block = e.at("first_block"); x.last_block = e.at("last_block");
            NodeId f = e.at("from"), t = e.at("to");
            EdgeAgg o = x; o.peer = t; out_.push_back({f, o});
            EdgeAgg i = x; i.peer = f; in_.push_back({t, i});
        }
        uint32_t li = 0;
        for (auto& t : m.value("transfers", json::array())) {
            int f = t.value("from", -1);
            trs_.push_back({t.at("token").get<TokenId>(), t.at("block").get<uint64_t>(), li++, f < 0 ? kNone : NodeId(f), t.at("to").get<NodeId>(),
                            parse_dec(t.at("amount").get<std::string>())});
        }
        std::stable_sort(trs_.begin(), trs_.end(), [](const Tr& a, const Tr& b) { return a.block < b.block; });
        // token stats from the transfers: supply = mints - burns (to the zero address), transfers, first/last block
        for (TokenId k = 0; k < toks_.size(); k++) {
            Raw256 minted, burned; uint64_t n = 0; bool any = false;
            for (auto& r : trs_) {
                if (r.token != k) continue;
                n++;
                if (!any) { toks_[k].first_block = r.block; any = true; }
                toks_[k].last_block = r.block;
                if (r.from == kNone) minted += r.amount;
                if (r.to != kNone && nodes_[r.to].addr == Addr{}) burned += r.amount;
            }
            toks_[k].transfers = n; toks_[k].has_supply = true; toks_[k].supply = sub(minted, burned);
            toks_[k].first_time = time_at(toks_[k].first_block); toks_[k].last_time = time_at(toks_[k].last_block);
        }
    }
    const std::string& chain() const override { return chain_; }
    uint64_t chain_id() const override { return chain_id_; }
    const std::string& build_id() const override { return build_; }
    uint64_t head_block() const override { return head_; }
    uint32_t head_time() const override { return time_at(head_); }
    NodeId node_of(const Addr& a) const override { auto it = by_addr_.find(graphloop::hex0x(a)); return it == by_addr_.end() ? kNone : it->second; }
    bool node_attr(NodeId n, NodeAttr& out) const override { if (n >= nodes_.size()) return false; out = nodes_[n]; return true; }
    TokenId token_of(const Addr& a) const override {
        for (TokenId k = 0; k < toks_.size(); k++) if (toks_[k].addr == a) return k;
        return kNone;
    }
    bool token_attr(TokenId t, TokenAttr& out) const override { if (t >= toks_.size()) return false; out = toks_[t]; return true; }
    uint64_t block_at(uint32_t t) const override {
        if (t < t0_) return 0;
        uint64_t b = b0_ + uint64_t(t - t0_) * (b1_ - b0_) / std::max<uint32_t>(1, t1_ - t0_);
        return std::min(b, head_);
    }
    uint32_t time_at(uint64_t b) const override {
        if (b <= b0_) return t0_;
        return uint32_t(t0_ + (std::min(b, std::max(b1_, head_)) - b0_) * (t1_ - t0_) / std::max<uint64_t>(1, b1_ - b0_));
    }
    bool edges(NodeId n, Dir d, uint64_t b0, uint64_t b1, const std::function<bool(const EdgeAgg&)>& cb, bool& truncated, Tier& tier) const override {
        truncated = false; tier = Tier::hot;
        for (auto& [owner, e] : d == Dir::out ? out_ : in_) {
            if (owner != n || e.last_block < b0 || e.first_block > b1) continue;
            if (!cb(e)) break;
        }
        return true;
    }
    Avail holders_at(TokenId t, uint64_t block, size_t limit, HoldersAt& out) const override {
        if (t >= toks_.size()) return Avail::missing;
        std::map<NodeId, Raw256> bal;   // exact replay; synth inputs never go negative
        for (auto& r : trs_) {
            if (r.token != t || r.block > block) continue;
            if (r.from != kNone) bal[r.from] = sub(bal[r.from], r.amount);
            bal[r.to] += r.amount;
        }
        out = HoldersAt{};
        std::vector<Holding> hs;
        for (auto& [n, v] : bal) {
            if (v.zero() || nodes_[n].addr == Addr{} || graphloop::hex0x(nodes_[n].addr) == "0x000000000000000000000000000000000000dead") continue;
            hs.push_back({n, v}); out.held += v; out.holders++;
        }
        std::sort(hs.begin(), hs.end(), [&](const Holding& a, const Holding& b) {
            if (!(a.amount == b.amount)) return b.amount < a.amount;
            return nodes_[a.holder].addr < nodes_[b.holder].addr;
        });
        if (hs.size() > limit) hs.resize(limit);
        out.top = std::move(hs);
        return Avail::ok;
    }
    Avail token_events(TokenId t, NodeId n, const std::function<bool(const TokenEvent&)>& cb, Tier& tier) const override {
        tier = Tier::hot;
        for (auto& r : trs_) {
            if ((t != kNone && r.token != t) || (n != kNone && r.from != n && r.to != n)) continue;
            TokenEvent e; e.block = r.block; e.time = time_at(r.block); e.log_index = r.log; e.from = r.from; e.to = r.to; e.amount = r.amount;
            if (!cb(e)) break;
        }
        return Avail::ok;
    }
    Avail balance_at(NodeId n, TokenId t, uint64_t block, Raw256& out) const override {
        out = Raw256{};
        for (auto& r : trs_) {
            if (r.token != t || r.block > block) continue;
            if (r.from == n) out = sub(out, r.amount);
            if (r.to == n) out += r.amount;
        }
        return Avail::ok;
    }
    uint32_t caps() const override { return kDict | kBlocks | kGraph | kHolders | kEvents | kLabels; }
};
} // namespace

StoreRef open_synth(const std::string& json_path) { return std::make_shared<SynthStore>(json_path); }
} // namespace machine::engine::serve
