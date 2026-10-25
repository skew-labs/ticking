#pragma once
// emv2-serve read interface: what the HTTP routes need from one chain's sealed build. The routes never touch segment formats;
// store_v2.cpp adapts the dict / blk / graph / ledger readers to this, store_synth.cpp gives tests and smoke runs a small chain
// from one JSON file. Every call is bounded (callbacks return false to stop) and reports the tier it read from.
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
    // ledger
    virtual Avail holders_at(TokenId t, uint64_t block, size_t limit, HoldersAt& out) const = 0;
    virtual Avail token_events(TokenId t, NodeId n, const std::function<bool(const TokenEvent&)>& cb, Tier& tier) const = 0;
    virtual Avail balance_at(NodeId n, TokenId t, uint64_t block, Raw256& out) const = 0;
    // which reads this build carries (an older build may lack the ledger): routes answer 501 for a missing capability
    enum Cap : uint32_t { kDict = 1, kBlocks = 2, kGraph = 4, kHolders = 8, kEvents = 16, kLabels = 32 };
    virtual uint32_t caps() const = 0;
};

using StoreRef = std::shared_ptr<const ChainStore>;

// Opens <dir>/manifest.json: a real EMV2 build (store_v2.cpp) or, for tests and smoke runs, {"kind":"synth", ...} (store_synth.cpp).
StoreRef open_store(const std::string& dir);
StoreRef open_v2(const std::string& dir);
StoreRef open_synth(const std::string& json_path);
} // namespace machine::engine::serve
