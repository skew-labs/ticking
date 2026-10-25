#pragma once
// emv2-serve read interface: what the HTTP routes need from one chain's sealed build. The routes never touch segment formats;
// store_v2.cpp adapts the dict / blk / graph / ledger readers to this, store_synth.cpp gives tests and smoke runs a small chain
// from one JSON file. Every call is bounded (callbacks return false to stop) and reports the tier it read from.
#include <machine/engine/u256.hpp>
#include <machine/graphloop/core.hpp>
#include <cmath>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace machine::engine::serve {
using graphloop::Addr;
using graphloop::Hash;
using NodeId = uint32_t;
using TokenId = uint32_t;
inline constexpr uint32_t kNone = 0xffffffffu;
inline constexpr TokenId kNative = 0xfffffffeu;   // the chain's native coin in edge assets

// Unsigned 256-bit raw amount, little-endian 64-bit limbs. Only what serving needs: compare, add, scale to a decimal number.
// (Aliased to core's U256 once include/machine/engine/u256.hpp lands; the layout is the same.)
struct Raw256 {
    uint64_t w[4]{0, 0, 0, 0};
    bool zero() const noexcept { return (w[0] | w[1] | w[2] | w[3]) == 0; }
    friend bool operator==(const Raw256& a, const Raw256& b) noexcept { return a.w[0] == b.w[0] && a.w[1] == b.w[1] && a.w[2] == b.w[2] && a.w[3] == b.w[3]; }
    friend bool operator<(const Raw256& a, const Raw256& b) noexcept {
        for (int i = 3; i >= 0; i--) if (a.w[i] != b.w[i]) return a.w[i] < b.w[i];
        return false;
    }
    Raw256& operator+=(const Raw256& o) noexcept {
        unsigned __int128 c = 0;
        for (int i = 0; i < 4; i++) { c += (unsigned __int128)w[i] + o.w[i]; w[i] = uint64_t(c); c >>= 64; }
        return *this;
    }
    static Raw256 from_u64(uint64_t v) noexcept { Raw256 r; r.w[0] = v; return r; }
    long double to_ld() const noexcept {
        long double x = 0;
        for (int i = 3; i >= 0; i--) x = x * 18446744073709551616.0L + (long double)w[i];
        return x;
    }
    // value / 10^decimals as a double (the Python services divide exact integers the same way)
    double scaled(int decimals) const noexcept { return double(to_ld() / std::pow(10.0L, (long double)decimals)); }
    std::string dec() const {   // exact decimal string
        Raw256 t = *this; std::string s;
        if (t.zero()) return "0";
        while (!t.zero()) {
            unsigned __int128 rem = 0;
            for (int i = 3; i >= 0; i--) { unsigned __int128 cur = (rem << 64) | t.w[i]; t.w[i] = uint64_t(cur / 10); rem = cur % 10; }
            s += char('0' + int(rem));
        }
        return std::string(s.rbegin(), s.rend());
    }
};

enum class Dir : uint8_t { in = 1, out = 2 };
enum class Tier : uint8_t { hot = 0, cold = 1 };          // cold = read from the Blob tier (answer carries tier: "cold")
enum class Avail : uint8_t { ok = 0, missing = 1, preparing = 2 };

struct NodeAttr {
    Addr addr{};
    std::string category, entity, cls, protocol, label_source;   // empty = unknown (JSON null)
    float confidence = 0;
    bool is_contract = false, delegated = false, known = false;  // known: the address is in the chain's dictionary
    bool hub = false;                                            // graph hub (traces stop here; hot list keeps its top peers)
    uint64_t first_block = 0, last_block = 0;
    uint32_t first_time = 0, last_time = 0;
    uint64_t tx_sent = 0, tx_received = 0, dex_trades = 0;
    uint32_t contracts_created = 0, in_degree = 0, out_degree = 0;
};

struct TokenAttr {
    Addr addr{};
    std::string symbol;
    int decimals = 18;
    bool has_supply = false;      // totalSupply() at the build head
    Raw256 supply{};
    bool has_creator = false;
    Addr creator{};               // tx.from of the token's first transfer (its mint)
    uint64_t first_block = 0, last_block = 0, transfers = 0, holders_approx = 0;
    uint32_t first_time = 0, last_time = 0;
};

struct EdgeAgg {                  // all transfers between a node and one peer for one asset inside the asked block window
    NodeId peer = kNone;
    TokenId asset = kNative;
    NodeId asset_node = kNone;    // the asset contract's node when `asset` is unknown (graph-only build: no token dictionary)
    uint8_t kind = 0;             // 0 native_tx, 1 erc20, 2 other (amount not comparable)
    uint64_t n = 0;
    Raw256 amount{};
    bool has_amount = true;       // false: the exact total lives in the cold tier and was not read (JSON null, never 0)
    uint64_t first_block = 0, last_block = 0;
};

struct Holding { NodeId holder = kNone; Raw256 amount{}; };

struct TokenEvent {
    uint64_t block = 0;
    uint32_t time = 0, log_index = 0;
    Hash tx{};
    NodeId from = kNone, to = kNone;
    Raw256 amount{};
};

// Net balance change of one address for one token over a block window (accumulators): exact, signed.
struct NetChange { NodeId node = kNone; machine::engine::I256 delta; uint32_t n_in = 0, n_out = 0; };

struct HoldersAt {
    std::vector<Holding> top;     // largest first, ties by address
    uint64_t holders = 0;         // addresses with a positive balance (burn addresses excluded)
    Raw256 held{};                // sum of positive balances
    Tier tier = Tier::hot;
};

class ChainStore {
public:
    virtual ~ChainStore() = default;
    virtual const std::string& chain() const = 0;
    virtual uint64_t chain_id() const = 0;
    virtual const std::string& build_id() const = 0;
    virtual uint64_t head_block() const = 0;
    virtual uint32_t head_time() const = 0;
    // dictionary
    virtual NodeId node_of(const Addr& a) const = 0;
    virtual bool node_attr(NodeId n, NodeAttr& out) const = 0;
    virtual TokenId token_of(const Addr& a) const = 0;
    virtual bool token_attr(TokenId t, TokenAttr& out) const = 0;
    // blocks
    virtual uint64_t block_at(uint32_t unix_t) const = 0;      // last block with time <= t (0 before the first block)
    virtual uint32_t time_at(uint64_t block) const = 0;
    // graph: aggregated edges of one node; cb returns false to stop. truncated = a hub whose hot list keeps only its top peers.
    virtual bool edges(NodeId n, Dir d, uint64_t b0, uint64_t b1, const std::function<bool(const EdgeAgg&)>& cb, bool& truncated, Tier& tier) const = 0;
    // The heaviest `limit` peers of one side with exact per-asset totals (only those peers' detail rows are read).
    // Default: the full edges() read, cut after `limit` records.
    virtual bool edges_top(NodeId n, Dir d, uint64_t b0, uint64_t b1, size_t limit, const std::function<bool(const EdgeAgg&)>& cb, bool& truncated,
                           Tier& tier) const {
        size_t k = 0;
        return edges(n, d, b0, b1, [&](const EdgeAgg& e) { return k++ < limit && cb(e); }, truncated, tier);
    }
    // Neighbour ids only, heaviest first, from the hot lists (no exact amounts, no cold reads): for path search.
    // Default: the full edges() read. cb(peer, n, kind) returns false to stop.
    virtual void peers(NodeId n, Dir d, size_t limit, const std::function<bool(NodeId, uint64_t, uint8_t)>& cb) const {
        bool tr = false; Tier t = Tier::hot; size_t k = 0;
        edges(n, d, 0, ~0ULL, [&](const EdgeAgg& e) { return k++ < limit && cb(e.peer, e.n, e.kind); }, tr, t);
    }
    // ledger
    virtual Avail holders_at(TokenId t, uint64_t block, size_t limit, HoldersAt& out) const = 0;
    virtual Avail token_events(TokenId t, NodeId n, const std::function<bool(const TokenEvent&)>& cb, Tier& tier) const = 0;
    virtual Avail balance_at(NodeId n, TokenId t, uint64_t block, Raw256& out) const = 0;
    // Net change per address over blocks (b0, b1] from the token's event stream, at most max_events events (truncated = more).
    // Burn sinks (0x0, 0x...dEaD) and self transfers are left out. Default: not available (synthetic stores).
    virtual Avail net_changes(TokenId t, uint64_t b0, uint64_t b1, size_t max_events, std::vector<NetChange>& out, bool& truncated,
                              uint64_t& scanned) const {
        (void)t; (void)b0; (void)b1; (void)max_events; (void)out; truncated = false; scanned = 0; return Avail::missing;
    }
    // which reads this build carries (an older build may lack the ledger): routes answer 501 for a missing capability
    enum Cap : uint32_t { kDict = 1, kBlocks = 2, kGraph = 4, kHolders = 8, kEvents = 16, kLabels = 32 };
    virtual uint32_t caps() const = 0;
    // Why a capability is missing when its segments are in the build but the reader refused them (format guard, older layout):
    // the routes answer "being prepared" (available:false) instead of 501. Empty = the build simply does not carry it.
    virtual std::string missing_reason(uint32_t cap) const { (void)cap; return {}; }
    // Nodes whose overlay entity name equals `name` (case-insensitive), at most cap. Default: none.
    virtual std::vector<NodeId> entity_nodes(std::string_view name, size_t cap) const { (void)name; (void)cap; return {}; }
    // mtime (ns) of <chain>/overlay/overlay.json when this store was opened (0 = none): the registry reopens on change
    virtual int64_t overlay_stamp() const { return 0; }
    // Cold tier: fetch these nodes' attribute records in one batch before they are read one by one (no-op when hot).
    virtual void prefetch_nodes(const std::vector<NodeId>& nodes) const { (void)nodes; }
    // in + out degree from the hot neighbour lists (cheap, never cold); ~0 when unknown
    virtual uint64_t hot_degree(NodeId n) const { (void)n; return ~0ull; }
    // Token flows of one address inside (b0, b1], exact, from the ledger's address events: one record per (peer, token, side).
    // Native value is not in the ledger. Returns false when the build has no ledger events; truncated when max_events was reached.
    virtual bool window_flows(NodeId n, uint64_t b0, uint64_t b1, size_t max_events, std::vector<EdgeAgg>& out_, std::vector<EdgeAgg>& in_,
                              bool& truncated) const { (void)n; (void)b0; (void)b1; (void)max_events; (void)out_; (void)in_; truncated = false; return false; }
    // Symbol and decimals of a token contract from the overlay (econ token_meta), for builds without a token dictionary.
    virtual bool token_meta(const Addr& a, std::string& symbol, int& decimals) const { (void)a; (void)symbol; (void)decimals; return false; }
    // Tokens whose symbol equals `symbol` (ASCII case-insensitive; the token table's symbol, else the overlay's), up to cap.
    virtual void find_tokens(std::string_view symbol, size_t cap, std::vector<TokenId>& out) const { (void)symbol; (void)cap; out.clear(); }
};

using StoreRef = std::shared_ptr<const ChainStore>;

// Opens <dir>/manifest.json: a real EMV2 build (store_v2.cpp) or, for tests and smoke runs, {"kind":"synth", ...} (store_synth.cpp).
StoreRef open_store(const std::string& dir);
StoreRef open_v2(const std::string& dir);
StoreRef open_synth(const std::string& json_path);
} // namespace machine::engine::serve
