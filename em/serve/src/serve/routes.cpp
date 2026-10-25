// emv2-serve routes. Each answer mirrors the Python service the bridge reads today (invscreen.py for holders-at, address-chains and
// trace-graph; glserve /tools/path for trace with `to`), key for key and in the same order, so the bridge's shadow mode compares
// like with like. Differences that come from the data itself (EMV2 reads exact balances from its own ledger, the build head instead
// of eth_getCode at the chain head) are stated in the answer's strings, not hidden.
#include <machine/engine/serve_json.hpp>
#include <machine/engine/serve_routes.hpp>
#include <cryptopp/sha.h>
#include <algorithm>
#include <deque>
#include <map>
#include <optional>
#include <set>
#include <unordered_map>
#include <unordered_set>

namespace machine::engine::serve {
namespace {
using graphloop::HttpRequest;
using graphloop::HttpResponse;
using graphloop::qparam;

constexpr const char* kPrefix = "/api/public/em";
const std::set<std::string> kVenueCats = {"dex_pool", "dex_router", "token_contract", "bridge", "lending", "nft_contract"};
const std::set<std::string> kExchCats = {"cex_hot_wallet", "cex_deposit"};
const std::set<std::string> kHubCats = {"dex_pool", "dex_router", "token_contract", "bridge", "lending", "nft_contract", "cex_hot_wallet", "cex_deposit"};
constexpr uint32_t kFunderMaxFanout = 60;                      // investigate.FUNDER_MAX_FANOUT
const std::map<int, size_t> kFanout = {{1, 10}, {2, 6}, {3, 4}}; // invscreen FANOUT
constexpr size_t kMaxNodes = 120;                               // invscreen MAX_NODES
const std::map<std::string, uint32_t> kWindows = {{"7d", 7 * 86400}, {"30d", 30 * 86400}, {"90d", 90 * 86400}, {"1y", 365 * 86400}};
const std::pair<const char*, uint32_t> kSnapOffsets[] = {{"launch", 0}, {"+10m", 600}, {"+1h", 3600}, {"+6h", 21600}, {"+1d", 86400}};

const char* native_symbol(const std::string& chain) {
    if (chain == "polygon") return "POL";
    if (chain == "arc") return "USDC";
    if (chain == "xlayer") return "OKB";
    if (chain == "hyperevm") return "HYPE";
    return "ETH";   // invscreen sym_dec: "ETH" for native on every chain it serves
}

std::string lower(std::string s) { for (auto& c : s) c = char(std::tolower(uint8_t(c))); return s; }
bool is_addr(const std::string& s) {
    if (s.size() != 42 || s[0] != '0' || s[1] != 'x') return false;
    for (size_t i = 2; i < 42; i++) if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) return false;
    return true;
}
bool is_digits(const std::string& s) { return !s.empty() && std::all_of(s.begin(), s.end(), [](char c) { return c >= '0' && c <= '9'; }); }
bool burn(const Addr& a) {
    static const Addr dead = graphloop::addr_from("0x000000000000000000000000000000000000dead");
    static const Addr zero{};
    return a == zero || a == dead;
}

std::string build_header(const ChainMap& chains, const std::vector<std::string>& used) {
    std::string h;
    for (auto& c : used) {
        auto it = chains.find(c);
        if (it == chains.end()) continue;
        if (!h.empty()) h += ',';
        h += c + ":" + it->second->build_id();
    }
    return h.empty() ? "none" : h;
}
// graphloop's HttpServer writes "Content-Type: <ctype>\r\n": the build id travels as one more header line after it.
HttpResponse reply(int status, std::string body, const std::string& build) {
    return {status, std::move(body), "application/json\r\nX-EMV2-Build: " + build};
}
// An answer computed at a moment before the build head says so: with_as_of() reports this block/time instead of the head.
HttpResponse reply_at(int status, std::string body, const std::string& build, uint64_t block, uint64_t time) {
    return {status, std::move(body), "application/json\r\nX-EMV2-Build: " + build + "\r\nX-EMV2-As-Of: " + std::to_string(block) + ":" + std::to_string(time)};
}
HttpResponse err(int status, const char* error, const std::string& detail, const std::string& build = "none") {
    Json j; j.ob().ks("error", error).ks("detail", detail).oe();
    return reply(status, j.s, build);
}
HttpResponse unsupported(const std::string& detail, const std::string& build = "none") { return err(501, "unsupported", detail, build); }
HttpResponse bad(const std::string& detail) { Json j; j.ob().ks("detail", detail).oe(); return reply(400, j.s, "none"); }

// Only the bridge's parameters reach the services; anything else is refused the same way the bridge does.
bool only(const std::string& query, std::initializer_list<const char*> keys) {
    size_t i = 0;
    while (i < query.size()) {
        size_t z = query.find('&', i); if (z == std::string::npos) z = query.size();
        auto kv = std::string_view(query).substr(i, z - i);
        auto key = kv.substr(0, kv.find('='));
        if (!key.empty() && std::none_of(keys.begin(), keys.end(), [&](const char* k) { return key == k; })) return false;
        i = z + 1;
    }
    return true;
}

// sha1(json.dumps(x, sort_keys=True)).hexdigest()[:16] for invscreen's inputHash; the caller hands the already sorted text.
std::string sha1_16(const std::string& text) {
    uint8_t d[20];
    CryptoPP::SHA1().CalculateDigest(d, reinterpret_cast<const uint8_t*>(text.data()), text.size());
    return graphloop::hex(graphloop::View(d, 20)).substr(0, 16);
}

struct Label { std::string category, entity, cls; bool known = false; };
Label label_of(const ChainStore& s, NodeId n) {
    Label l;
    NodeAttr a;
    if (n != kNone && s.node_attr(n, a)) { l.category = a.category; l.entity = a.entity; l.cls = a.cls; l.known = true; }
    return l;
}
} // namespace

// invscreen kind_of(lab, cls)
std::string kind_of(const std::string& cat, const std::string& c) {
    if (cat == "dex_pool" || c == "dex") return "pool";
    if (kExchCats.count(cat) || c == "cex" || c == "exchange") return "exchange";
    if (cat == "bridge" || c == "bridge") return "bridge";
    if (cat == "dex_router") return "router";
    if (cat.find("mev") != std::string::npos || c == "mev") return "mev";
    if (cat.find("whale") != std::string::npos || c == "whale") return "whale";
    if (cat.rfind("contract", 0) == 0 || cat == "token_contract" || cat == "lending" || cat == "nft_contract" || cat == "multisig_or_safe")
        return cat != "multisig_or_safe" ? "contract" : "safe";
    return "wallet";
}

namespace {
// ------------------------------------------------------------------------------------------------ holders-at (invscreen holders_at)
struct Moment { std::string key; uint64_t block; uint64_t time; };

void who(Json& j, const ChainStore& s, NodeId n, const Addr& a) {
    Label l = label_of(s, n);
    j.ks("address", addr_hex(a)).ks("kind", kind_of(l.category, l.cls)).ksn("category", l.category).ksn("entity", l.entity).ksn("class", l.cls);
}

HttpResponse holders_at(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    if (!only(rq.query, {"market", "snaps", "limit", "t", "block", "to"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");
    std::string m = lower(qparam(rq.query, "market"));
    auto colon = m.find(':');
    if (colon == std::string::npos || !is_addr(m.substr(colon + 1))) return bad("market must be <chain>:<0x token address>.");
    std::string chain = m.substr(0, colon), token = m.substr(colon + 1);
    auto it = chains.find(chain);
    if (it == chains.end()) return unsupported("chain " + chain + " is not served by emv2 yet");
    const ChainStore& s = *it->second;
    const std::string build = chain + ":" + s.build_id();
    if (!(s.caps() & ChainStore::kHolders) || !(s.caps() & ChainStore::kBlocks)) {
        std::string why = s.missing_reason(ChainStore::kHolders);
        if (why.empty()) return unsupported("this build carries no holder balances", build);
        Json p; p.ob().ks("schema", "holders-at/1").kb("available", false).ks("reason", "The balances of this build are being prepared.")
                .k("status").ob().ks("state", "preparing").ks("detail", why).oe().oe();
        return reply(200, p.s, build);
    }
    Json j;
    TokenId tid = s.token_of(graphloop::addr_from(token));
    TokenAttr ta;
    if (tid == kNone || !s.token_attr(tid, ta)) {
        j.ob().ks("schema", "holders-at/1").kb("available", false).ks("reason", "This token is not in the map's token table yet.").oe();
        return reply(200, j.s, build);   // elapsed_ms is appended by the caller
    }
    std::string limit_s = qparam(rq.query, "limit");
    size_t lim = is_digits(limit_s) ? std::clamp<size_t>(size_t(std::min<uint64_t>(std::stoull(limit_s.substr(0, 9)), 100000)), 1, 100) : 30;
    std::optional<double> supply;
    if (ta.has_supply) supply = ta.supply.scaled(ta.decimals);
    std::optional<Addr> creator;
    if (ta.has_creator) creator = ta.creator;
    uint64_t t0 = ta.first_time, b0 = ta.first_block, head = s.head_block();
    if (std::string pin = qparam(rq.query, "to"); is_digits(pin)) head = std::min(head, s.block_at(uint32_t(std::min<uint64_t>(std::stoull(pin.substr(0, 12)), 0xffffffffu))));   // `to` pins "now" and caps every moment
    std::vector<Moment> moments;
    if (!qparam(rq.query, "snaps").empty()) {   // invscreen: `if qs.get("snaps")` is true for any non-empty value, "0" included
        for (auto& [k, off] : kSnapOffsets) moments.push_back({k, off == 0 ? b0 : s.block_at(uint32_t(t0 + off)), t0 + off});
        moments.push_back({"now", head, s.time_at(head)});
    } else if (is_digits(qparam(rq.query, "block"))) {
        uint64_t b = std::stoull(qparam(rq.query, "block"));
        moments.push_back({"block", b, s.time_at(b)});
    } else if (is_digits(qparam(rq.query, "t"))) {
        uint64_t t = std::stoull(qparam(rq.query, "t"));
        if (t > 100000000000ULL) t /= 1000;
        moments.push_back({"t", s.block_at(uint32_t(t)), t});
    } else {
        return bad("give t (unix seconds), block, or snaps=1.");
    }
    // the moment the answer stands at: the asked block / t (capped at the map end) or, for snaps, "now" (head, or `to`)
    uint64_t pin_block = moments.size() == 1 ? std::min(moments[0].block, head) : head;
    uint64_t pin_time = s.time_at(pin_block);
    bool any = false, all = true;
    Json sn; sn.ab();
    for (auto& mo : moments) {
        if (budget.expired()) return err(504, "budget", "the request ran out of its time budget", build);
        sn.ob();
        if (mo.block < b0) {
            sn.ks("key", mo.key).ku("block", mo.block).ku("time", mo.time).kb("available", true).ku("holders", 0).k("top").ab().ae()
              .ks("note", "before the token's first transfer").oe();
            any = true;
            continue;
        }
        uint64_t blk = std::min(mo.block, head);
        sn.ks("key", mo.key).ku("block", blk).ku("time", mo.time).ksn("timeUtc", utc_iso(mo.time));
        HoldersAt h;
        Avail av = s.holders_at(tid, blk, lim, h);
        if (av != Avail::ok) {
            sn.kb("available", false).ks("reason", "The token's transfer history up to this moment is being prepared.")
              .k("status").ob().ks("state", av == Avail::preparing ? "preparing" : "missing").oe().oe();
            all = false;
            continue;
        }
        any = true;
        double held = h.held.scaled(ta.decimals);
        bool denom_is_int = false;
        double denom = supply ? *supply : held;
        if (!(denom != 0)) { denom = 1; denom_is_int = !supply && held == 0; }
        sn.kb("available", true).ks("source", "emv2 exact balances (token transfer history)").ku("holders", h.holders);
        if (denom_is_int) sn.ki("supply", 1); else sn.kn("supply", denom);
        sn.ks("supplyBasis", supply ? "totalSupply()" : "sum of balances");
        sn.k("top").ab();
        double top_pct = 0;
        for (auto& r : h.top) {
            NodeAttr na; Addr a{};
            if (s.node_attr(r.holder, na)) a = na.addr;
            double v = r.amount.scaled(ta.decimals);
            double pct = py_round(v / denom * 100, 4);
            top_pct += pct;
            sn.ob(); who(sn, s, r.holder, a);
            sn.kn("amount", v).kn("pct", pct).kb("isCreator", creator && *creator == a).oe();
        }
        sn.ae().kn("topPct", py_round(top_pct, 4));
        if (h.tier == Tier::cold) sn.ks("tier", "cold");
        sn.oe();
    }
    sn.ae();
    j.ob().ks("schema", "holders-at/1").ks("market", chain + ":" + token).ksn("symbol", ta.symbol).kb("available", any).kb("complete", all)
     .k("launch").ob().ku("block", b0).ku("time", t0).ksn("timeUtc", utc_iso(t0)).oe().ku("head", head).k("snaps").raw(sn.s)
     .k("notes").ab()
     .str("pct = share of totalSupply() at the head; holders = addresses with a positive balance (burn addresses excluded).")
     .str("Pools, routers and exchanges are holders too: kind tells them apart (pool, exchange, router, bridge, mev, whale, contract, safe, wallet).")
     .str("Moments before the analytics snapshot come from the token's own transfer history; while it is being prepared that snap is available:false with status.")
     .ae().oe();
    return reply_at(200, j.s, build, pin_block, pin_time);
}

// ------------------------------------------------------------------------------------------------ address-chains (invscreen address_chains)
HttpResponse address_chains(const ChainMap& chains, const HttpRequest& rq, Budget&) {
    if (!only(rq.query, {"address", "to"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");   // `to` accepted; counts are at the build head (as_of)
    std::string a = lower(qparam(rq.query, "address"));
    if (!is_addr(a)) return bad("address must be a 0x address.");
    Addr ad = graphloop::addr_from(a);
    struct Row { std::string chain; uint64_t chain_id; NodeAttr at; };
    std::vector<Row> rows;
    std::vector<std::string> used;
    for (auto& [name, st] : chains) {
        used.push_back(name);
        if (!(st->caps() & ChainStore::kDict)) continue;
        NodeId n = st->node_of(ad);
        NodeAttr at;
        if (n == kNone || !st->node_attr(n, at)) continue;
        rows.push_back({name, st->chain_id(), at});
    }
    std::set<std::string> kinds;
    auto kind = [](const NodeAttr& at) { return at.is_contract ? std::string("contract") : at.delegated ? std::string("eoa_delegated") : std::string("eoa"); };
    for (auto& r : rows) kinds.insert(kind(r.at));
    std::stable_sort(rows.begin(), rows.end(), [](const Row& x, const Row& y) { return x.at.tx_sent + x.at.tx_received > y.at.tx_sent + y.at.tx_received; });
    Json j;
    j.ob().ks("schema", "address-chains/1").kb("available", !rows.empty()).ks("address", a).knull("entity").k("chains").ab();
    for (auto& r : rows) {
        std::string k = kind(r.at);
        j.ob().ks("chain", r.chain).ku("chainId", r.chain_id).ku("txSent", r.at.tx_sent).ku("txReceived", r.at.tx_received)
         .ku("firstSeen", r.at.first_time).ku("lastSeen", r.at.last_time).ku("firstBlock", r.at.first_block).ku("lastBlock", r.at.last_block)
         .ks("kind", k).ku("contractsCreated", r.at.contracts_created).ku("dexTrades", r.at.dex_trades).ks("source", "address index (full map)")
         .ks("kindBasis", "code at the emv2 build head");
        std::string why;
        if (rows.size() < 2) j.knull("sameOwner").knull("grade").ks("why", "seen on one chain only");
        else if (k == "contract" || kinds.count("contract"))
            j.kb("sameOwner", false).knull("grade").ks("why", "a contract wallet (Safe etc.) can have different owners on each chain: the same address is not grouped");
        else {
            why = "same externally owned address = the same private key on every chain";
            if (r.at.tx_sent == 0) why += " (receive only here: no transaction signed on this chain yet)";
            j.kb("sameOwner", true).ks("grade", "proven").ks("why", why);
            if (r.at.tx_sent == 0) j.kb("receiveOnly", true);
        }
        j.ku("txs", r.at.tx_sent + r.at.tx_received).ksn("firstSeenUtc", utc_iso(r.at.first_time)).ksn("lastSeenUtc", utc_iso(r.at.last_time)).oe();
    }
    j.ae().k("notes").ab()
     .str("txSent / txReceived count transactions sent from / to the address (internal transfers excluded).")
     .str("Only EOAs are grouped across chains by address. A delegated EOA (EIP-7702) still has its key, so it stays grouped.")
     .ae().oe();
    auto r = reply(200, j.s, build_header(chains, used));
    r.ctype += "\r\nX-EMV2-Multi: 1";   // a cross-chain answer: as_of per chain even when one chain is loaded
    return r;
}

// ------------------------------------------------------------------------------------------------ trace-graph (invscreen trace)
struct TNode { std::string id, address, chain, kind, category, entity; int hop; std::string stop; };
struct TLink {
    std::string a, b, chain, symbol, asset; bool asset_null = true, sym_null = true, amount_null = true;
    uint64_t n = 0, first_block = 0, last_block = 0, first_time = 0, last_time = 0; double amount = 0; size_t assets = 0;
};

std::string hub_reason(const ChainStore& s, NodeId p) {
    NodeAttr a;
    if (p == kNone || !s.node_attr(p, a)) return {};
    if (kHubCats.count(a.category)) {
        std::string c = a.category; std::replace(c.begin(), c.end(), '_', ' ');
        return (c.empty() ? std::string("hub") : c) + (a.entity.empty() ? "" : " (" + a.entity + ")");
    }
    if (a.out_degree > kFunderMaxFanout * 20 || a.in_degree > 5000)
        return "high fan-out (" + std::to_string(a.out_degree) + " out / " + std::to_string(a.in_degree) + " in)";
    return {};
}

void py_json_str(std::string& o, std::string_view s) { graphloop::json_escape(o, s); }   // ASCII-only inputs here

HttpResponse trace_graph(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    if (!only(rq.query, {"chain", "address", "direction", "hops", "chains", "window", "from", "to"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");
    std::string chain = lower(qparam(rq.query, "chain")), a = lower(qparam(rq.query, "address"));
    std::string direction = lower(qparam(rq.query, "direction", "in")); if (direction.empty()) direction = "in";
    if (!is_addr(a) || (direction != "in" && direction != "out")) return bad("chain must be a served chain, address a 0x address, direction in|out.");
    auto it = chains.find(chain);
    if (it == chains.end()) return unsupported("chain " + chain + " is not served by emv2 yet");
    if (!(it->second->caps() & ChainStore::kGraph)) {
        std::string why = it->second->missing_reason(ChainStore::kGraph);
        if (why.empty()) return unsupported("this build carries no graph", chain + ":" + it->second->build_id());
        Json p; p.ob().ks("schema", "trace/1").kb("available", false).ks("reason", "The graph of this build is being prepared.")
                .k("status").ob().ks("state", "preparing").ks("detail", why).oe().oe();
        return reply(200, p.s, chain + ":" + it->second->build_id());
    }
    std::string hs = qparam(rq.query, "hops");
    int hops = is_digits(hs) ? int(std::clamp<uint64_t>(std::stoull(hs.substr(0, 6)), 1, 3)) : 2;
    std::vector<std::string> cl;
    {
        std::string cs = lower(qparam(rq.query, "chains", chain)); if (cs.empty()) cs = chain;
        size_t i = 0;
        while (i <= cs.size()) {
            size_t z = cs.find(',', i); if (z == std::string::npos) z = cs.size();
            std::string c = cs.substr(i, z - i);
            if (chains.count(c) && std::find(cl.begin(), cl.end(), c) == cl.end()) cl.push_back(c);
            i = z + 1;
        }
        if (cl.empty()) cl.push_back(chain);
    }
    std::string win = lower(qparam(rq.query, "window", "all")); if (win.empty()) win = "all";
    uint64_t ref = 0;
    for (auto& c : cl) { uint64_t t = chains.at(c)->head_time(); if (t && (!ref || t < ref)) ref = t; }
    if (!ref) ref = graphloop::now_ms() / 1000 / 3600 * 3600;
    uint64_t t0 = 0, t1 = ref;
    std::string fs = qparam(rq.query, "from"), ts = qparam(rq.query, "to");
    if (is_digits(fs) || is_digits(ts)) { t0 = is_digits(fs) ? std::stoull(fs) : 0; t1 = is_digits(ts) ? std::stoull(ts) : ref; }
    else if (kWindows.count(win)) { t0 = ref - kWindows.at(win); t1 = ref; }
    else { win = "all"; t0 = 0; t1 = ref; }
    t1 = std::min(t1, ref);   // never past the map end: the bridge re-asks the old service at as_of
    std::map<std::string, std::pair<uint64_t, uint64_t>> blocks;
    for (auto& c : cl) { auto& st = *chains.at(c); blocks[c] = {t0 ? st.block_at(uint32_t(t0)) : 0, st.block_at(uint32_t(std::min<uint64_t>(t1, 0xffffffffu)))}; }

    std::map<std::string, TNode> nodes;            // key chain:address
    std::map<std::tuple<std::string, std::string, std::string>, TLink> links;
    struct Stop { std::string address, chain, why; int hop; };
    std::vector<Stop> stops;
    Json frontier; frontier.ab();
    auto node = [&](const std::string& ch, const std::string& x, NodeId nid, int hop, const std::string& stop) -> TNode& {
        std::string k = ch + ":" + x;
        auto f = nodes.find(k);
        if (f != nodes.end()) return f->second;
        Label l = label_of(*chains.at(ch), nid);
        TNode n{k, x, ch, kind_of(l.category, ""), l.category, l.entity, hop, stop};
        return nodes.emplace(k, std::move(n)).first->second;
    };
    const ChainStore& s0 = *chains.at(chain);
    NodeId root = s0.node_of(graphloop::addr_from(a));
    node(chain, a, root, 0, "");
    std::deque<std::tuple<std::string, NodeId, std::string, int>> q;
    q.emplace_back(chain, root, a, 0);
    Dir d = direction == "in" ? Dir::in : Dir::out;
    bool cold = false;
    while (!q.empty()) {
        if (budget.expired()) return err(504, "budget", "the request ran out of its time budget", build_header(chains, cl));
        auto [ch, xid, x, hop] = q.front(); q.pop_front();
        if (hop >= hops || xid == kNone) continue;
        const ChainStore& s = *chains.at(ch);
        auto [bf, bt] = blocks.count(ch) ? blocks[ch] : std::pair<uint64_t, uint64_t>{0, ~0ULL};
        struct Agg { NodeId peer; std::string addr; uint64_t n = 0, first = ~0ULL, last = 0; std::vector<std::tuple<uint64_t, std::string, Raw256, uint8_t, TokenId>> assets; };
        std::unordered_map<NodeId, Agg> agg;
        bool truncated = false; Tier tier = Tier::hot;
        s.edges(xid, d, bf, bt, [&](const EdgeAgg& e) {
            if (e.last_block < bf || e.first_block > bt || e.peer == xid) return true;
            auto& g = agg[e.peer];
            if (g.addr.empty()) {
                NodeAttr pa; if (!s.node_attr(e.peer, pa)) return true;
                if (burn(pa.addr)) { agg.erase(e.peer); return true; }
                g.peer = e.peer; g.addr = addr_hex(pa.addr);
            }
            g.n += e.n; g.first = std::min(g.first, e.first_block); g.last = std::max(g.last, e.last_block);
            // "native", the token address, or "" when the build does not say which token (asset/symbol/amount null)
            std::string asset = e.asset == kNative ? "native" : [&] { TokenAttr t; return e.asset != kNone && s.token_attr(e.asset, t) ? addr_hex(t.addr) : std::string(); }();
            g.assets.emplace_back(e.n, asset, e.amount, e.has_amount ? e.kind : uint8_t(2), e.asset);   // no exact total read: amount null
            return agg.size() < 200000;
        }, truncated, tier);
        if (tier == Tier::cold) cold = true;
        std::vector<Agg*> ranked;
        for (auto& [k, g] : agg) if (!g.addr.empty()) ranked.push_back(&g);
        std::sort(ranked.begin(), ranked.end(), [](const Agg* p, const Agg* r) { return p->n != r->n ? p->n > r->n : p->addr < r->addr; });
        size_t k = kFanout.count(hop + 1) ? kFanout.at(hop + 1) : 3;
        if (ranked.size() > k)
            frontier.ob().ks("id", ch + ":" + x).ks("address", x).ks("chain", ch).ku("hidden", ranked.size() - k)
                    .ks("why", "showing the top " + std::to_string(k) + " peers by transfer count").oe();
        for (size_t i = 0; i < std::min(k, ranked.size()); i++) {
            Agg& g = *ranked[i];
            if (nodes.size() >= kMaxNodes) {
                frontier.ob().ks("id", ch + ":" + x).ks("address", x).ks("chain", ch).ks("why", "node cap " + std::to_string(kMaxNodes) + " reached").oe();
                break;
            }
            std::string hub = hub_reason(s, g.peer);
            node(ch, g.addr, g.peer, hop + 1, hub);
            std::string src = d == Dir::in ? g.addr : x, dst = d == Dir::in ? x : g.addr;
            auto lk = std::make_tuple(ch, src, dst);
            if (links.count(lk)) continue;
            std::sort(g.assets.begin(), g.assets.end(), [](auto& p, auto& r) { return std::get<0>(p) != std::get<0>(r) ? std::get<0>(p) > std::get<0>(r) : std::get<1>(p) < std::get<1>(r); });
            auto& [n0, asset, amt, kd, tid] = g.assets[0];
            (void)n0;
            std::string sym; int dec = 18;
            if (asset == "native") sym = native_symbol(ch);
            else if (!asset.empty()) { TokenAttr t; if (s.token_attr(tid, t)) { sym = t.symbol; dec = t.decimals; } }
            TLink L;
            L.a = src; L.b = dst; L.chain = ch; L.n = g.n;
            L.asset_null = asset == "native" || asset.empty(); L.asset = asset;
            L.sym_null = sym.empty(); L.symbol = sym;
            L.amount_null = !(kd == 0 || kd == 1) || asset.empty(); L.amount = L.amount_null ? 0 : amt.scaled(dec);
            L.assets = g.assets.size(); L.first_block = g.first; L.last_block = g.last;
            L.first_time = s.time_at(g.first); L.last_time = s.time_at(g.last);
            links.emplace(lk, std::move(L));
            if (!hub.empty()) stops.push_back({g.addr, ch, hub, hop + 1});
            else if (hop + 1 < hops) q.emplace_back(ch, g.peer, g.addr, hop + 1);
        }
    }
    frontier.ae();
    // params / dataVersion / inputHash exactly as invscreen builds them
    Json params; params.ob().ks("address", a).ks("chain", chain).ks("direction", direction).ki("hops", hops).k("chains").ab();
    for (auto& c : cl) params.str(c);
    params.ae().ks("window", win).ku("from", t0).ku("to", t1).oe();
    Json ver; ver.ob(); for (auto& c : cl) ver.ku(c, chains.at(c)->head_block()); ver.oe();
    // json.dumps([params, ver], sort_keys=True): keys sorted, ", " and ": " separators
    std::string hx = "[{";
    {
        std::vector<std::pair<std::string, std::string>> kv = {{"address", ""}, {"chain", ""}, {"chains", ""}, {"direction", ""}, {"from", ""}, {"hops", ""}, {"to", ""}, {"window", ""}};
        bool first = true;
        for (auto& [key, _] : kv) {
            if (!first) hx += ", ";
            first = false;
            py_json_str(hx, key); hx += ": ";
            if (key == "address") py_json_str(hx, a);
            else if (key == "chain") py_json_str(hx, chain);
            else if (key == "chains") { hx += '['; for (size_t i = 0; i < cl.size(); i++) { if (i) hx += ", "; py_json_str(hx, cl[i]); } hx += ']'; }
            else if (key == "direction") py_json_str(hx, direction);
            else if (key == "from") hx += std::to_string(t0);
            else if (key == "hops") hx += std::to_string(hops);
            else if (key == "to") hx += std::to_string(t1);
            else py_json_str(hx, win);
        }
        hx += "}, {";
        std::vector<std::string> sc(cl.begin(), cl.end()); std::sort(sc.begin(), sc.end());
        for (size_t i = 0; i < sc.size(); i++) { if (i) hx += ", "; py_json_str(hx, sc[i]); hx += ": " + std::to_string(chains.at(sc[i])->head_block()); }
        hx += "}]";
    }
    std::vector<const TNode*> nl; for (auto& [k, n] : nodes) nl.push_back(&n);
    std::sort(nl.begin(), nl.end(), [](const TNode* p, const TNode* r) { return std::tie(p->hop, p->chain, p->address) < std::tie(r->hop, r->chain, r->address); });
    std::sort(stops.begin(), stops.end(), [](const Stop& p, const Stop& r) { return std::tie(p.hop, p.address) < std::tie(r.hop, r.address); });
    Json j;
    j.ob().ks("schema", "trace/1").kb("available", true).k("params").raw(params.s).ks("inputHash", sha1_16(hx)).k("dataVersion").raw(ver.s).k("nodes").ab();
    for (auto* n : nl) {
        j.ob().ks("id", n->id).ks("address", n->address).ks("chain", n->chain).ks("kind", n->kind).ksn("label", n->entity).ksn("category", n->category)
         .ksn("entity", n->entity).ki("hop", n->hop).ksn("stop", n->stop).oe();
    }
    j.ae().k("links").ab();
    for (auto& [k, L] : links) {   // std::map order == sorted by (chain, a, b)
        j.ob().ks("a", L.a).ks("b", L.b).ks("chain", L.chain).ks("grade", "proven").ks("kind", "transfer").ku("txCount", L.n)
         .ks("why", std::to_string(L.n) + " direct transfer(s)" + (L.sym_null ? "" : ", mostly " + L.symbol));
        if (L.asset_null) j.knull("asset"); else j.ks("asset", L.asset);
        if (L.sym_null) j.knull("symbol"); else j.ks("symbol", L.symbol);
        if (L.amount_null) j.knull("amount"); else j.kn("amount", L.amount);
        j.ku("assets", L.assets).ku("firstBlock", L.first_block).ku("lastBlock", L.last_block).ku("firstTime", L.first_time).ku("lastTime", L.last_time)
         .ks("evidence", "/api/public/em/edge-evidence?chain=" + L.chain + "&a=" + L.a + "&b=" + L.b).oe();
    }
    j.ae().k("stops").ab();
    for (auto& st : stops) j.ob().ks("address", st.address).ks("chain", st.chain).ks("why", st.why).ki("hop", st.hop).oe();
    j.ae().k("frontier").raw(frontier.s).k("asOf").ob();
    for (auto& c : cl) {
        auto& st = *chains.at(c);
        j.k(c).ob().ku("block", st.head_block()).ku("time", st.head_time()).ksn("timeUtc", utc_iso(st.head_time())).ks("source", "emv2 build " + st.build_id()).oe();
    }
    j.oe();
    if (cold) j.ks("tier", "cold");
    j.k("notes").ab()
     .str("direction=in: where the money came from (links point toward the wallet); out: where it went.")
     .str("Links are direct transfers (proven) and bridge moves with both legs matched (matched; heuristic = inferred). A shared funder never becomes a link between the wallets it funded.")
     .str("Exchanges, routers, pools, bridges and high fan-out addresses are shown and then not expanded (stops).")
     .str("Deterministic: peers are ranked by transfer count, then address; the same input and data version give the same graph.")
     .ae().oe();
    if (cl.size() == 1) return reply_at(200, j.s, build_header(chains, cl), blocks[chain].second, t1);
    return reply(200, j.s, build_header(chains, cl));
}

// ------------------------------------------------------------------------------------------------ path (glserve /tools/path semantics)
// Bounded bidirectional BFS over transfer edges (design §8): hubs are not expanded, fan-out 128 (1,024 at the two ends), budget
// 4,096 visited nodes, at most 3 hops from each side. Answer in glserve's shape with addresses as refs.
HttpResponse path(const ChainMap& chains, const HttpRequest& rq, Budget& budget, const std::string& chain, const std::string& from, const std::string& to,
                  int max_depth) {
    auto it = chains.find(chain);
    if (it == chains.end()) return unsupported("chain " + chain + " is not served by emv2 yet");
    const ChainStore& s = *it->second;
    const std::string build = chain + ":" + s.build_id();
    if (!(s.caps() & ChainStore::kGraph)) {
        std::string why = s.missing_reason(ChainStore::kGraph);
        if (why.empty()) return unsupported("this build carries no graph", build);
        Json p; p.ob().kb("found", false).kb("available", false).ks("reason", "The graph of this build is being prepared.").ks("detail", why).oe();
        return reply(200, p.s, build);
    }
    NodeId src = s.node_of(graphloop::addr_from(from)), dst = s.node_of(graphloop::addr_from(to));
    if (src == kNone || dst == kNone) return err(404, "REF_NOT_FOUND", "address not in this chain's dictionary", build);
    max_depth = std::clamp(max_depth, 1, 6);
    constexpr size_t fan = 128, fan_end = 1024, budget_nodes = 4096;
    constexpr uint32_t hub_limit = 20000;
    auto brief = [&](Json& j, NodeId v) {
        NodeAttr a; s.node_attr(v, a);
        j.ks("ref", addr_hex(a.addr)).ks("type", a.is_contract ? "c" : "e").ks("category", a.category.empty() ? "unknown" : a.category)
         .kn("confidence", std::round(double(a.confidence) * 100) / 100);
        if (!a.entity.empty()) j.ks("entity", a.entity);
    };
    Json j;
    if (src == dst) { j.ob().kb("found", true).ki("hops", 0).k("path").ab().ob(); brief(j, src); j.oe().ae().oe(); return reply(200, j.s, build); }
    std::unordered_map<NodeId, std::pair<NodeId, uint8_t>> pf, pb;
    pf[src] = {kNone, 0}; pb[dst] = {kNone, 0};
    std::vector<NodeId> ff{src}, fb{dst};
    int df = 0, dbk = 0; NodeId meet = kNone; size_t visited = 0;
    auto expand = [&](std::vector<NodeId>& front, auto& mine, auto& other, bool forward) {
        std::vector<NodeId> next;
        for (NodeId u : front) {
            if (budget.expired()) break;
            NodeAttr ua;
            if (u != src && u != dst && s.node_attr(u, ua) && ua.in_degree + ua.out_degree > hub_limit) continue;
            size_t cap = (u == src || u == dst) ? fan_end : fan, taken = 0;
            bool trunc = false; Tier tier = Tier::hot;
            // strongest edges first: collect then keep the top `cap` by transfer count
            std::vector<std::pair<uint64_t, NodeId>> nb;
            std::unordered_map<NodeId, std::pair<uint64_t, uint8_t>> best;
            s.edges(u, forward ? Dir::out : Dir::in, 0, ~0ULL, [&](const EdgeAgg& e) {
                auto& b = best[e.peer]; if (e.n > b.first) b = {e.n, e.kind}; else if (!b.first) b.second = e.kind;
                return best.size() < 100000;
            }, trunc, tier);
            for (auto& [p, nk] : best) nb.push_back({nk.first, p});
            std::sort(nb.begin(), nb.end(), [](auto& x, auto& y) { return x.first != y.first ? x.first > y.first : x.second < y.second; });
            for (auto& [n, w] : nb) {
                if (taken++ >= cap) break;
                if (mine.count(w)) continue;
                mine[w] = {u, best[w].second};
                visited++;
                if (other.count(w)) { meet = w; return next; }
                next.push_back(w);
                if (visited > budget_nodes) return next;
            }
        }
        return next;
    };
    while (meet == kNone && df + dbk < max_depth && visited <= budget_nodes && !ff.empty() && !fb.empty() && !budget.expired()) {
        if (ff.size() <= fb.size()) { ff = expand(ff, pf, pb, true); df++; } else { fb = expand(fb, pb, pf, false); dbk++; }
    }
    if (meet == kNone) {
        j.ob().kb("found", false).ku("visited", visited).ki("max_depth", max_depth).kb("budget_exhausted", visited > budget_nodes).oe();
        return reply(200, j.s, build);
    }
    std::vector<NodeId> ns; std::vector<uint8_t> es;
    for (NodeId x = meet; x != kNone; x = pf[x].first) { ns.push_back(x); es.push_back(pf[x].second); }
    std::reverse(ns.begin(), ns.end()); std::reverse(es.begin(), es.end());
    for (NodeId x = meet; pb.count(x) && pb[x].first != kNone; x = pb[x].first) { ns.push_back(pb[x].first); es.push_back(pb[x].second); }
    j.ob().kb("found", true).ku("hops", ns.size() - 1).ku("visited", visited).k("path").ab();
    for (size_t i = 0; i < ns.size(); i++) {
        j.ob(); brief(j, ns[i]);
        if (i) j.ks("edge_in", es[i] == 0 ? "native" : es[i] == 1 ? "erc20" : "other");
        j.oe();
    }
    j.ae().oe();
    return reply(200, j.s, build);
}

HttpResponse trace_public(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    // /api/public/em/trace?chain=&from=[&to=]: with `to` a path; without it the 3-hop outflow of `from` (trace-graph, direction out)
    if (!only(rq.query, {"chain", "from", "to", "max_depth"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");
    std::string chain = lower(qparam(rq.query, "chain")), from = lower(qparam(rq.query, "from")), to = lower(qparam(rq.query, "to"));
    bool to_time = is_digits(to);   // `to` as unix seconds pins the outflow's moment; as an address it asks for a path
    if (!is_addr(from) || (!to.empty() && !to_time && !is_addr(to))) return bad("from must be a 0x address; to a 0x address or unix seconds.");
    if (!to.empty() && !to_time) {
        std::string md = qparam(rq.query, "max_depth");
        return path(chains, rq, budget, chain, from, to, is_digits(md) ? int(std::min<uint64_t>(std::stoull(md.substr(0, 3)), 6)) : 6);
    }
    HttpRequest r2 = rq;
    r2.query = "chain=" + chain + "&address=" + from + "&direction=out&hops=3" + (to_time ? "&to=" + to : "");
    return trace_graph(chains, r2, budget);
}

// ------------------------------------------------------------------------------------------------ emv2 debug: neighbors of one address
HttpResponse neighbors(const ChainMap& chains, const HttpRequest& rq, Budget&) {
    std::string chain = lower(qparam(rq.query, "chain")), a = lower(qparam(rq.query, "address")), dir = lower(qparam(rq.query, "dir", "out"));
    std::string ls = qparam(rq.query, "limit");
    size_t lim = is_digits(ls) ? std::clamp<size_t>(size_t(std::min<uint64_t>(std::stoull(ls.substr(0, 6)), 1000)), 1, 200) : 20;
    if (!is_addr(a) || (dir != "in" && dir != "out")) return bad("chain, address (0x) and dir in|out are required.");
    auto it = chains.find(chain);
    if (it == chains.end()) return unsupported("chain " + chain + " is not served by emv2 yet");
    const ChainStore& s = *it->second;
    const std::string build = chain + ":" + s.build_id();
    NodeId n = s.node_of(graphloop::addr_from(a));
    if (n == kNone) return err(404, "not_found", "address not in this chain's dictionary", build);
    std::vector<EdgeAgg> es;
    bool trunc = false; Tier tier = Tier::hot;
    s.edges(n, dir == "in" ? Dir::in : Dir::out, 0, ~0ULL, [&](const EdgeAgg& e) { es.push_back(e); return es.size() < 1000000; }, trunc, tier);
    std::sort(es.begin(), es.end(), [](auto& x, auto& y) { return x.n != y.n ? x.n > y.n : x.peer < y.peer; });
    Json j;
    j.ob().ks("schema", "emv2-neighbors/1").ks("chain", chain).ks("address", a).ks("dir", dir).ku("edges", es.size()).kb("truncated", trunc);
    if (tier == Tier::cold) j.ks("tier", "cold");
    j.k("rows").ab();
    for (size_t i = 0; i < std::min(lim, es.size()); i++) {
        auto& e = es[i];
        NodeAttr pa; s.node_attr(e.peer, pa);
        j.ob().ks("peer", addr_hex(pa.addr)).ksn("category", pa.category).ksn("entity", pa.entity);
        if (e.asset == kNative) j.ks("asset", "native"); else { TokenAttr t; if (s.token_attr(e.asset, t)) j.ks("asset", addr_hex(t.addr)).ksn("symbol", t.symbol); else j.knull("asset"); }
        j.ku("n", e.n).ks("amountRaw", e.amount.dec()).ku("firstBlock", e.first_block).ku("lastBlock", e.last_block).oe();
    }
    j.ae().oe();
    return reply(200, j.s, build);
}

HttpResponse health(const ChainMap& chains) {
    Json j; j.ob().kb("ok", true).k("chains").ob();
    for (auto& [c, s] : chains)
        j.k(c).ob().ks("build", s->build_id()).ku("head", s->head_block()).ku("headTime", s->head_time()).ku("caps", s->caps()).oe();
    j.oe().oe();
    return reply(200, j.s, build_header(chains, [&] { std::vector<std::string> v; for (auto& [c, s] : chains) v.push_back(c); return v; }()));
}
} // namespace

namespace {
// Every object answer carries as_of: each used chain's data end (the build head), so a comparison can tell a time skew from a
// difference. The used chains are the ones named in X-EMV2-Build.
HttpResponse with_as_of(HttpResponse r, const ChainMap& chains) {
    if (r.body.size() < 2 || r.body.front() != '{' || r.body.back() != '}') return r;
    std::vector<StoreRef> used;
    auto h = r.ctype.find("X-EMV2-Build: ");
    if (h != std::string::npos) {
        size_t e = r.ctype.find("\r\n", h); std::string list = r.ctype.substr(h + 14, e == std::string::npos ? std::string::npos : e - h - 14);
        size_t i = 0;
        while (i < list.size()) {
            size_t z = list.find(',', i); if (z == std::string::npos) z = list.size();
            std::string item = list.substr(i, z - i);
            auto it = chains.find(item.substr(0, item.find(':')));
            if (it != chains.end()) used.push_back(it->second);
            i = z + 1;
        }
    }
    Json a;
    if (used.size() == 1 && r.ctype.find("X-EMV2-Multi: 1") == std::string::npos) {
        auto& st = *used[0];
        uint64_t b = st.head_block(), t = st.head_time();
        auto p = r.ctype.find("X-EMV2-As-Of: ");
        if (p != std::string::npos) { std::sscanf(r.ctype.c_str() + p + 14, "%lu:%lu", &b, &t); }
        else r.ctype += "\r\nX-EMV2-As-Of: " + std::to_string(b) + ":" + std::to_string(t);
        a.ob().ks("chain", st.chain()).ku("block", b).ku("time", t).ksn("timeUtc", utc_iso(t)).ks("build", st.build_id())
         .ku("mapEndBlock", st.head_block()).oe();
    } else {
        a.ob();
        for (auto& sp : used)
            a.k(sp->chain()).ob().ku("block", sp->head_block()).ku("time", sp->head_time()).ksn("timeUtc", utc_iso(sp->head_time())).ks("build", sp->build_id()).oe();
        a.oe();
    }
    r.body.pop_back();
    if (r.body.size() > 1) r.body += ',';
    r.body += "\"as_of\":" + a.s + "}";
    return r;
}
HttpResponse dispatch(const ChainMap& chains, const HttpRequest& rq, Budget& budget);
} // namespace

HttpResponse handle(const ChainMap& chains, const HttpRequest& rq, Budget& budget) { return with_as_of(dispatch(chains, rq, budget), chains); }

namespace {
HttpResponse dispatch(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    if (rq.method != "GET") return err(405, "method", "GET only");
    std::string p = rq.path;
    if (p == "/health") return health(chains);
    if (p.rfind(kPrefix, 0) == 0) p = p.substr(std::string_view(kPrefix).size());
    if (p == "/holders-at") return holders_at(chains, rq, budget);
    if (p == "/address-chains") return address_chains(chains, rq, budget);
    if (p == "/trace-graph") return trace_graph(chains, rq, budget);
    if (p == "/trace") return trace_public(chains, rq, budget);
    if (p == "/emv2/neighbors") return neighbors(chains, rq, budget);
    // bridge paths this phase does not answer (wallet-trades / acquisition need DEX fills, tools/* and g/* need the ledger and prices)
    static const char* later[] = {"/wallet-trades", "/acquisition", "/wallet-links", "/token-launch", "/launch-candles", "/edge-evidence",
                                  "/bundle-summary", "/freshness", "/tools/", "/g/", "/wallet-xc", "/search"};
    for (auto* l : later) if (p.rfind(l, 0) == 0) return unsupported(std::string("emv2 phase 1 does not answer ") + l);
    return err(404, "not_found", "unknown route");
}
} // namespace
} // namespace machine::engine::serve
