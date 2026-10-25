#pragma once
// ledger.hpp: token events (tt) and balances (bal) for EMV2.
//
// Semantics match mapping/build_map.sql exactly, so address_tokens.holds equals (balance > 0):
//   * kind 1 erc20  = ERC20, WETH_DEPOSIT (from = 0x0), WETH_WITHDRAWAL (to = 0x0)
//     kind 2 erc721 = ERC721, counted as 1 per transfer (balance = n_in - n_out)
//     kind 3 erc1155 = ERC1155_SINGLE and ERC1155_BATCH (one row per element), summed across token ids
//     other standards are skipped and counted.
//   * token_transfers only come from successful receipts, so there is no failed-tx filter.
//   * mint: from = 0x0 adds to supply; burn: to = 0x0 subtracts (supply = totalSupply).  The 0x0 holder keeps
//     its row but is never counted as a holder, and neither is 0x...dEaD (invscreen rule).  A self transfer
//     (from = to) counts once in n_in and once in n_out.
//   * balances that go negative (rebasing tokens, missing events) stay exact; the token gets flag `irregular`.
//   * order inside a token: (block, tx_index, log_index, batch_index).
//   * sums use 320-bit two's complement (Acc320): spam tokens move 2^256-1 more than once, and the map
//     sums limbs in HUGEINT, so 256 bits would not reproduce its sign test.
//
// Layout: core seg v2 files (seg.hpp) directly in the build directory, partition p of P stored as part p+1
// (tid % P == p), so every name is segment_file_name(kind, p + 1):
//   tok.stats.NNN      TokDir per tid of the partition, ascending tid                       (elem 64)
//   tt.tok.index.NNN   Group before events 0, 64, 128, ... of each token                    (elem 64)
//   tt.tok.data.NNN    by_token event stream                                                (bytes)
//   bal.ckpt.index.NNN CkptDir per checkpoint                                               (elem 64)
//   bal.top.NNN        top 1,000 positive holders (no 0x0 / dEaD) per checkpoint,           (bytes)
//                      balance desc then id: n_top x (u32 id, Acc320)
//   bal.ckpt.data.NNN  every nonzero balance per checkpoint, sorted by id (cold tier):      (bytes)
//                      u64 n, u64 n_idx, n_idx x (u32 id, u32 0, u64 rel_off), then n x (u32 id, Acc320)
//   bal.head.index.NNN Run per (address, tid) sorted by (address, tid); binary search       (elem 64)
//   bal.head.data.NNN  head balance per run (Acc320 encoding)                                (bytes)
//   tt.addr.index.NNN  AddrGroup before events 64, 128, ... of a run                         (elem 64)
//   tt.addr.data.NNN   by_addr event stream                                                 (bytes)
// Dictionaries are core's: dict.addr.* (graph writes it; a standalone ledger build writes the same format)
// and dict.tok / dict.tok.str (ledger writes them).  Address ids are AddrDict ranks; tid = TokenDict rank.
#include "machine/engine/api.hpp"
#include "machine/engine/dict.hpp"
#include "machine/engine/seg.hpp"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <functional>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace machine::engine { struct TokenRec; }

namespace machine::engine::ledger::fmt {

using Addr = std::array<uint8_t, 20>;
inline constexpr Addr zero_addr{};
inline constexpr Addr dead_addr{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xde, 0xad};
inline constexpr uint32_t no_id = 0xffffffffu;
inline constexpr uint32_t tok_irregular = tf_irregular;  // TokDir.flags

inline std::optional<TokenKind> kind_of(std::string_view standard) noexcept {
    if (standard == "ERC20" || standard == "WETH_DEPOSIT" || standard == "WETH_WITHDRAWAL") return TokenKind::erc20;
    if (standard == "ERC721") return TokenKind::erc721;
    if (standard == "ERC1155_SINGLE" || standard == "ERC1155_BATCH") return TokenKind::erc1155;
    return std::nullopt;
}
inline const char* kind_name(TokenKind k) noexcept { return k == TokenKind::erc20 ? "erc20" : k == TokenKind::erc721 ? "erc721" : "erc1155"; }

// ------------------------------------------------------------------ 320-bit signed accumulator
struct Acc320 {
    uint64_t w[5]{};  // little-endian limbs, two's complement

    static Acc320 from_u256(const uint64_t m[4]) noexcept { Acc320 a; std::memcpy(a.w, m, 32); return a; }
    static Acc320 from_i64(int64_t v) noexcept { Acc320 a; uint64_t f = v < 0 ? ~uint64_t(0) : 0; a.w[0] = uint64_t(v); for (int i = 1; i < 5; ++i) a.w[i] = f; return a; }
    bool is_zero() const noexcept { return !(w[0] | w[1] | w[2] | w[3] | w[4]); }
    bool negative() const noexcept { return w[4] >> 63; }
    int sign() const noexcept { return negative() ? -1 : is_zero() ? 0 : 1; }
    Acc320& operator+=(const Acc320& o) noexcept {
        unsigned __int128 c = 0;
        for (int i = 0; i < 5; ++i) { c += (unsigned __int128)w[i] + o.w[i]; w[i] = uint64_t(c); c >>= 64; }
        return *this;
    }
    Acc320 operator-() const noexcept {
        Acc320 r; unsigned __int128 c = 1;
        for (int i = 0; i < 5; ++i) { c += (unsigned __int128)(~w[i]); r.w[i] = uint64_t(c); c >>= 64; }
        return r;
    }
    Acc320& operator-=(const Acc320& o) noexcept { return *this += -o; }
    void add_u256(const uint64_t m[4]) noexcept { *this += from_u256(m); }
    void sub_u256(const uint64_t m[4]) noexcept { *this -= from_u256(m); }
    friend bool operator==(const Acc320& a, const Acc320& b) noexcept { return std::memcmp(a.w, b.w, sizeof a.w) == 0; }
    friend int cmp(const Acc320& a, const Acc320& b) noexcept {
        if (a.negative() != b.negative()) return a.negative() ? -1 : 1;
        for (int i = 4; i >= 0; --i) if (a.w[i] != b.w[i]) return a.w[i] < b.w[i] ? -1 : 1;
        return 0;
    }
    Acc320 magnitude() const noexcept { return negative() ? -*this : *this; }
    // fits a signed 256-bit integer
    bool fits_i256() const noexcept {
        uint64_t f = negative() ? ~uint64_t(0) : 0;
        return w[4] == f && ((w[3] >> 63) == (f & 1));
    }
    std::string to_dec() const {
        Acc320 m = magnitude();
        if (m.is_zero()) return "0";
        std::string s;
        while (!m.is_zero()) {  // divide by 1e19, emit 19 digits per step
            unsigned __int128 r = 0;
            for (int i = 4; i >= 0; --i) { unsigned __int128 cur = (r << 64) | m.w[i]; m.w[i] = uint64_t(cur / 10000000000000000000ULL); r = cur % 10000000000000000000ULL; }
            uint64_t chunk = uint64_t(r);
            for (int d = 0; d < 19; ++d) { s.push_back(char('0' + chunk % 10)); chunk /= 10; if (m.is_zero() && chunk == 0) break; }
        }
        while (s.size() > 1 && s.back() == '0') s.pop_back();
        if (negative()) s.push_back('-');
        std::reverse(s.begin(), s.end());
        return s;
    }
    static std::optional<Acc320> from_dec(std::string_view s) {
        bool neg = !s.empty() && s[0] == '-';
        if (neg) s.remove_prefix(1);
        if (s.empty() || s.size() > 96) return std::nullopt;
        Acc320 a;
        for (char ch : s) {
            if (ch < '0' || ch > '9') return std::nullopt;
            unsigned __int128 c = uint64_t(ch - '0');
            for (int i = 0; i < 5; ++i) { c += (unsigned __int128)a.w[i] * 10; a.w[i] = uint64_t(c); c >>= 64; }
            if (c || a.negative()) return std::nullopt;
        }
        return neg ? -a : a;
    }
};

// ------------------------------------------------------------------ byte codecs
inline void put_varint(std::vector<uint8_t>& o, uint64_t v) { while (v >= 0x80) { o.push_back(uint8_t(v) | 0x80); v >>= 7; } o.push_back(uint8_t(v)); }
inline uint64_t get_varint(const uint8_t*& p) noexcept {
    uint64_t v = 0; int s = 0;
    for (;;) { uint8_t b = *p++; v |= uint64_t(b & 0x7f) << s; if (!(b & 0x80)) return v; s += 7; }
}
inline void put_u32(std::vector<uint8_t>& o, uint32_t v) { uint8_t b[4]; std::memcpy(b, &v, 4); o.insert(o.end(), b, b + 4); }
inline uint32_t get_u32(const uint8_t*& p) noexcept { uint32_t v; std::memcpy(&v, p, 4); p += 4; return v; }
// Acc320 as: header byte (bit 7 = negative, low 6 bits = magnitude byte length 0..40) + magnitude bytes LE
inline void put_acc(std::vector<uint8_t>& o, const Acc320& a) {
    Acc320 m = a.magnitude(); uint8_t b[40]; std::memcpy(b, m.w, 40);
    int n = 40; while (n > 0 && b[n - 1] == 0) --n;
    o.push_back(uint8_t((a.negative() ? 0x80 : 0) | n)); o.insert(o.end(), b, b + n);
}
inline Acc320 get_acc(const uint8_t*& p) noexcept {
    uint8_t h = *p++; int n = h & 0x3f; Acc320 a; std::memcpy(a.w, p, size_t(n)); p += n;
    return (h & 0x80) ? -a : a;
}
// unsigned 256-bit magnitude as: length byte + bytes LE (amounts are never negative)
inline void put_u256(std::vector<uint8_t>& o, const uint64_t m[4]) {
    uint8_t b[32]; std::memcpy(b, m, 32); int n = 32; while (n > 0 && b[n - 1] == 0) --n;
    o.push_back(uint8_t(n)); o.insert(o.end(), b, b + n);
}
inline void get_u256(const uint8_t*& p, uint64_t m[4]) noexcept { int n = *p++; std::memset(m, 0, 32); std::memcpy(m, p, size_t(n)); p += n; }
inline void be32_to_limbs(const uint8_t* be, uint64_t m[4]) noexcept {
    for (int i = 0; i < 4; ++i) { uint64_t v = 0; for (int j = 0; j < 8; ++j) v = (v << 8) | be[(3 - i) * 8 + j]; m[i] = v; }
}

// ------------------------------------------------------------------ build input
struct Xfer {               // one decoded token_transfers row with ledger ids
    uint32_t block, tx, log, batch;
    uint32_t tid, from, to;
    uint32_t pad{};
    uint64_t amt[4];        // raw amount; 1 for erc721
};
static_assert(sizeof(Xfer) == 64);

// ------------------------------------------------------------------ fixed tables
struct TokDir {             // per tid in its partition
    uint64_t ev_off;        // byte offset into tok_ev
    uint64_t n_events;
    uint64_t grp_begin;     // first Group index
    uint64_t ckpt_begin, ckpt_end;  // CkptDir range
    int64_t holders_end;    // holders after the last event
    uint32_t tid, first_block, last_block, flags;
};
static_assert(sizeof(TokDir) == 64);

struct Group {              // state before event (index * 64) of a token
    uint64_t ev_off;        // byte offset of that event in tok_ev
    uint32_t block;         // block of that event (search key)
    uint32_t prev_block;    // block of the event before it (block deltas run continuously across groups)
    int64_t holders;        // holder count before it
    uint64_t supply[5];     // Acc320 supply before it
};
static_assert(sizeof(Group) == 64);

struct CkptDir {            // state after the first `n_applied` events of a token
    uint64_t n_applied;
    uint32_t block, tid;    // block of the last applied event
    int64_t holders;
    uint64_t top_off, full_off;      // byte offsets into bal.top / bal.ckpt.data (full_off = ~0: top-only checkpoint)
    uint32_t n_top, pad;
    uint64_t n_full;
    uint64_t full_idx_off;  // unused (sparse index lives inline every 128 entries); kept for layout
};
static_assert(sizeof(CkptDir) == 64);

struct Run {                // one (address, tid) pair: head balance and by_addr range
    uint32_t addr, tid;
    uint64_t ev_off;        // byte offset into addr_ev
    uint64_t n_in, n_out;
    uint32_t first_block, last_block, first_in_block;
    uint32_t grp_begin;     // AddrGroup index of event 64 (group k >= 1 is grp_begin + k - 1)
    uint64_t bal_off;       // byte offset into bal
    uint64_t n_events;      // by_addr records (a self transfer is one record counted in n_in and n_out)
};
static_assert(sizeof(Run) == 64);

struct AddrGroup {          // balance before event (index * 64) of a run
    uint64_t ev_off;
    uint32_t block, pad;    // block of that event (search key)
    uint64_t prev_idx;      // token event index of the record before it (deltas run continuously)
    uint64_t bal[5];
};
static_assert(sizeof(AddrGroup) == 64);

// by_token event record: varint dblock, varint tx, varint log, varint batch, u32 from, u32 to,
//                        u8 flags (bits 0-1: holder delta 0 / 1 = +1 / 2 = -1), u256 amount
// by_addr event record:  varint (token event index - previous record's index); everything else comes from the
//                        token's tt.tok.data record (in = to is the address, out = from is the address)
inline constexpr uint32_t group_every = 64;
inline constexpr uint32_t ledger_format = 2;   // facts.ledger_format; bump on any read-format change (2: by_addr = token event index)
inline constexpr uint32_t top_keep = 1000;
inline constexpr uint64_t top_every = 16384;   // top-only checkpoint every max(this, live holders / 8) events
inline constexpr uint32_t full_index_every = 128;

struct Event {              // decoded by_token record
    uint32_t block, tx, log, batch, from, to;
    int8_t dholders;
    uint64_t amt[4];
};
struct AddrEvent {          // decoded by_addr record
    uint32_t block, tx, log, batch, other;
    bool in, out;
    uint64_t amt[4];
    Acc320 delta() const noexcept { Acc320 d; if (in && !out) d.add_u256(amt); else if (out && !in) d.sub_u256(amt); return d; }
};

struct Holder { uint32_t addr; Acc320 balance; };

// ------------------------------------------------------------------ build
struct PartOptions {
    uint32_t zero_id = no_id, dead_id = no_id;  // ledger ids of 0x0 and 0x...dEaD (no_id when absent)
    uint32_t parts = 1;               // tid % parts partitions, built one at a time
    uint32_t ckpt_min_events = 256;   // full checkpoint after max(this, live holders) events
    bool full_ckpt = false;           // write full holder lists (bal.ckpt.data, cold); holders_at does not need them
    uint32_t threads = 4;
};

struct BuildStats {
    uint64_t events[4]{};             // by kind, [0] = total
    uint64_t sum_n_in{}, sum_n_out{};
    uint64_t pairs{}, checkpoints{};
};

// Builds partition `part` (0-based; files use part + 1) from its transfers (any order across tids).
// `xs` is consumed.  Returns the written segments for the part manifest.
std::vector<SegResult> build_partition(const std::string& dir, uint32_t part, uint32_t n_addr, uint32_t n_tok,
                                       std::vector<Xfer>& xs, const PartOptions& opt, BuildStats& st);
// Standalone builds only (no graph dictionary): dict.addr.* from sorted unique addresses.
std::vector<SegResult> write_addr_dict(const std::string& dir, std::span<const Addr> sorted_addrs);

// ------------------------------------------------------------------ read
class Reader {
public:
    explicit Reader(const std::string& dir);   // opens dict.addr.*, dict.tok and every partition by path
    ~Reader();
    Reader(const Reader&) = delete;

    uint32_t n_addr() const noexcept;
    uint32_t n_tok() const noexcept;
    std::optional<uint32_t> addr_id(const Addr& a) const noexcept;
    Addr addr(uint32_t id) const noexcept;
    std::optional<uint32_t> tid(const Addr& token, TokenKind k) const noexcept;
    const TokenRec& token(uint32_t tid) const noexcept;
    uint32_t parts() const noexcept;

    // head: exact current balance and counters; nullopt when the pair never moved
    struct Head { Acc320 balance; uint64_t n_in, n_out; uint32_t first_block, last_block, first_in_block; };
    std::optional<Head> head(uint32_t addr, uint32_t tid) const;
    // all pairs of one address (every partition), sorted by tid
    std::vector<std::pair<uint32_t, Head>> holdings(uint32_t addr) const;

    // balance after every event with block <= b
    Acc320 balance_at(uint32_t addr, uint32_t tid, uint32_t b) const;
    // by_addr events of one pair in [b0, b1] (block order), at most `limit`
    std::vector<AddrEvent> addr_events(uint32_t addr, uint32_t tid, uint32_t b0, uint32_t b1, size_t limit) const;

    // supply and holder count after every event with block <= b (exact, O(log n) + <= 63 events)
    Acc320 supply_at(uint32_t tid, uint32_t b) const;
    int64_t holders_count_at(uint32_t tid, uint32_t b) const;
    // top `n` holders after every event with block <= b.  exact = false only when the ranking below
    // the checkpoint's top list could not be proven (then the first rows are still exact balances).
    struct Top { std::vector<Holder> rows; int64_t holders; Acc320 supply; bool exact; };
    Top holders_at(uint32_t tid, uint32_t b, uint32_t n) const;
    // by_token events in [b0, b1], at most `limit`
    std::vector<Event> token_events(uint32_t tid, uint32_t b0, uint32_t b1, size_t limit) const;

    // newest first: events with block in [b0, b1], at most `limit`
    std::vector<AddrEvent> addr_events_rev(uint32_t addr, uint32_t tid, uint32_t b0, uint32_t b1, size_t limit) const;
    std::vector<Event> token_events_rev(uint32_t tid, uint32_t b0, uint32_t b1, size_t limit) const;
    // balance and counters after every event with block <= b
    struct At { Acc320 balance; uint64_t n_in, n_out; };
    At at(uint32_t addr, uint32_t tid, uint32_t b) const;
    const TokDir* tok_dir(uint32_t tid) const noexcept;
    uint32_t zero_id() const noexcept;

    // full scans (dumps, verification): every run with its head balance, every token directory row
    void each_run(const std::function<void(const Run&, const Acc320&)>& f) const;
    void each_token(const std::function<void(const TokDir&)>& f) const;

    struct Impl;
private:
    Impl* p_;
};

}  // namespace machine::engine::ledger::fmt

namespace machine::engine::ledger {
// The api.hpp Store: a finished ledger build opened read-only.
class Store {
public:
    explicit Store(const std::string& dir) : r(dir) {}
    fmt::Reader r;
    std::unique_ptr<BlockTime> times;   // blk.time when the build has it
};
}  // namespace machine::engine::ledger
