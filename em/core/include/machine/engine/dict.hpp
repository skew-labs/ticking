#pragma once
// Address / token dictionaries and block time (EM-ENGINE-V2 §5.1, §5.2).
//
// AddrDict: node id = rank of the address in byte order (u32). Stored as the address suffix after a
// k-byte radix prefix (k = 2 for N <= 2^26, else 3) plus a prefix table of 2^(8k)+1 u32 offsets:
//   dict.addr.sfx  elem = 20-k, count = N, aux = k
//   dict.addr.pfx  elem = 4,    count = 2^(8k)+1
// TokenDict: tid = rank of (token address, kind) -- the same key as map address_tokens/token_stats.
//   dict.tok      elem = sizeof(TokenRec), sorted by (addr_id, kind)
//   dict.tok.str  variable, symbol/name bytes referenced by TokenRec
// BlockTime: blk.time = per-block timestamp deltas as uvarints with a checkpoint every 1024 blocks:
//   payload = [ckpt array: count_ckpt * BlockCkpt][varint stream]; aux = first block, aux2 = ckpt count.
//   A checkpoint block has no delta; block first+1024c+j (j>0) adds the j-th varint after ck[c].offset.
#include "machine/engine/codec.hpp"
#include "machine/engine/seg.hpp"
#include <algorithm>
#include <optional>

namespace machine::engine {

using Addr = machine::graphloop::Addr;

class AddrDict {
    SegView sfx_, pfx_; unsigned k_{}; uint32_t width_{}; uint64_t n_{};
    const uint8_t* s_{}; const uint32_t* p_{};
    static uint32_t bucket(const uint8_t* a, unsigned k) noexcept { uint32_t b = 0; for (unsigned i = 0; i < k; i++) b = b << 8 | a[i]; return b; }
public:
    AddrDict() = default;
    explicit AddrDict(const BuildDir& b, bool populate = false) {
        sfx_ = b.open(Kind::dict_addr_sfx, 0, populate);
        pfx_ = b.open(Kind::dict_addr_pfx, 0, true);
        k_ = unsigned(sfx_.aux()); need(k_ == 2 || k_ == 3, "dict: radix width");
        width_ = 20 - k_; n_ = sfx_.count();
        need(sfx_.trailer().elem == width_ && pfx_.count() == (uint64_t(1) << (8 * k_)) + 1, "dict: shape");
        s_ = sfx_.payload().data(); p_ = pfx_.as<uint32_t>().data();
        need(p_[pfx_.count() - 1] == n_, "dict: prefix total");
    }
    uint64_t size() const noexcept { return n_; }
    unsigned radix_bytes() const noexcept { return k_; }
    std::optional<uint32_t> find(const uint8_t* a20) const noexcept {
        uint32_t b = bucket(a20, k_); uint32_t lo = p_[b], hi = p_[b + 1];
        const uint8_t* key = a20 + k_;
        while (lo < hi) {
            uint32_t mid = lo + (hi - lo) / 2;
            int c = std::memcmp(s_ + size_t(mid) * width_, key, width_);
            if (c == 0) return mid;
            if (c < 0) lo = mid + 1; else hi = mid;
        }
        return std::nullopt;
    }
    std::optional<uint32_t> find(const Addr& a) const noexcept { return find(a.b); }
    Addr get(uint32_t id) const noexcept {
        // bucket = last b with p_[b] <= id
        uint64_t nb = (uint64_t(1) << (8 * k_));
        const uint32_t* it = std::upper_bound(p_, p_ + nb + 1, id);
        uint32_t b = uint32_t(it - p_ - 1);
        Addr a{};
        for (unsigned i = 0; i < k_; i++) a.b[i] = uint8_t(b >> (8 * (k_ - 1 - i)));
        std::memcpy(a.b + k_, s_ + size_t(id) * width_, width_);
        return a;
    }
};

// Writes dict.addr.{sfx,pfx} from addresses pushed in strictly increasing byte order.
class AddrDictWriter {
    SegWriter sfx_; fs::path dir_; unsigned k_; uint64_t n_{}; std::vector<uint32_t> counts_; Addr last_{}; bool any_{};
public:
    // expected_n decides the radix width; it must be >= the real count only for the k choice.
    AddrDictWriter(const fs::path& dir, uint64_t expected_n)
        : sfx_(dir / segment_file_name(Kind::dict_addr_sfx), Kind::dict_addr_sfx, expected_n > (uint64_t(1) << 26) ? 17 : 18),
          dir_(dir), k_(expected_n > (uint64_t(1) << 26) ? 3 : 2), counts_(size_t(1) << (8 * k_), 0) {}
    uint32_t push(const Addr& a) {
        need(!any_ || last_ < a, "dict: addresses must be strictly increasing");
        need(n_ < 0xffffffffull, "dict: more than 2^32-1 addresses");
        uint32_t b = 0; for (unsigned i = 0; i < k_; i++) b = b << 8 | a.b[i];
        counts_[b]++; sfx_.put(a.b + k_, 20 - k_); last_ = a; any_ = true;
        return uint32_t(n_++);
    }
    std::pair<SegResult, SegResult> finish() {
        auto r1 = sfx_.finish(n_, k_);
        SegWriter pfx(dir_ / segment_file_name(Kind::dict_addr_pfx), Kind::dict_addr_pfx, 4);
        uint32_t acc = 0;
        for (auto c : counts_) { pfx.put(&acc, 4); acc += c; }
        pfx.put(&acc, 4);
        auto r2 = pfx.finish(counts_.size() + 1);
        return {r1, r2};
    }
};

struct TokenRec {
    uint32_t addr_id;       // node id of the token contract
    uint8_t kind;           // TokenKind
    uint8_t decimals;       // 255 = unknown
    uint16_t flags;         // TokenFlag bits
    uint32_t first_block;
    uint32_t sym_off;       // into dict.tok.str
    uint16_t sym_len;
    uint16_t name_len;      // name bytes follow the symbol bytes
};
static_assert(sizeof(TokenRec) == 20);

class TokenDict {
    SegView rec_, str_; std::span<const TokenRec> r_;
public:
    TokenDict() = default;
    explicit TokenDict(const BuildDir& b) {
        rec_ = b.open(Kind::dict_tok, 0, true); str_ = b.open(Kind::dict_tok_str, 0, true);
        r_ = rec_.as<TokenRec>();
    }
    uint64_t size() const noexcept { return r_.size(); }
    const TokenRec& rec(uint32_t tid) const noexcept { return r_[tid]; }
    std::optional<uint32_t> find(uint32_t addr_id, TokenKind kind) const noexcept {
        auto key = std::pair{addr_id, uint8_t(kind)};
        auto it = std::lower_bound(r_.begin(), r_.end(), key, [](const TokenRec& t, const std::pair<uint32_t, uint8_t>& k) { return std::pair{t.addr_id, t.kind} < k; });
        if (it == r_.end() || it->addr_id != addr_id || it->kind != uint8_t(kind)) return std::nullopt;
        return uint32_t(it - r_.begin());
    }
    // All kinds registered for one contract (usually one).
    std::span<const TokenRec> by_addr(uint32_t addr_id, uint32_t& first_tid) const noexcept {
        auto lo = std::lower_bound(r_.begin(), r_.end(), addr_id, [](const TokenRec& t, uint32_t a) { return t.addr_id < a; });
        auto hi = std::upper_bound(lo, r_.end(), addr_id, [](uint32_t a, const TokenRec& t) { return a < t.addr_id; });
        first_tid = uint32_t(lo - r_.begin());
        return {&*lo, size_t(hi - lo)};
    }
    std::string_view symbol(uint32_t tid) const noexcept { auto& t = r_[tid]; return {reinterpret_cast<const char*>(str_.payload().data()) + t.sym_off, t.sym_len}; }
    std::string_view name(uint32_t tid) const noexcept { auto& t = r_[tid]; return {reinterpret_cast<const char*>(str_.payload().data()) + t.sym_off + t.sym_len, t.name_len}; }
};

// Writes dict.tok{,.str}; records must be pushed in (addr_id, kind) order. Returns the tid.
class TokenDictWriter {
    SegWriter rec_, str_; uint64_t n_{}, str_bytes_{}; std::pair<uint32_t, uint8_t> last_{}; bool any_{};
public:
    explicit TokenDictWriter(const fs::path& dir)
        : rec_(dir / segment_file_name(Kind::dict_tok), Kind::dict_tok, sizeof(TokenRec)), str_(dir / segment_file_name(Kind::dict_tok_str), Kind::dict_tok_str) {}
    uint32_t push(uint32_t addr_id, TokenKind kind, uint8_t decimals, uint16_t flags, uint32_t first_block, std::string_view symbol, std::string_view name) {
        auto key = std::pair{addr_id, uint8_t(kind)};
        need(!any_ || last_ < key, "token dict: keys must be strictly increasing");
        symbol = symbol.substr(0, 64); name = name.substr(0, 128);
        TokenRec t{addr_id, uint8_t(kind), decimals, flags, first_block, uint32_t(str_bytes_), uint16_t(symbol.size()), uint16_t(name.size())};
        need(str_bytes_ + symbol.size() + name.size() < 0xffffffffull, "token dict: string blob");
        str_.put(symbol.data(), symbol.size()); str_.put(name.data(), name.size()); str_bytes_ += symbol.size() + name.size();
        rec_.put(&t, sizeof t); last_ = key; any_ = true;
        return uint32_t(n_++);
    }
    std::pair<SegResult, SegResult> finish() { auto a = rec_.finish(n_); auto b = str_.finish(str_bytes_); return {a, b}; }
};

struct BlockCkpt { uint32_t time; uint32_t pad; uint64_t offset; };   // time of block first+1024*i, byte offset of its delta
static_assert(sizeof(BlockCkpt) == 16);
inline constexpr uint32_t block_ckpt_every = 1024;

class BlockTime {
    SegView seg_; uint64_t first_{}, count_{}, nck_{}; const BlockCkpt* ck_{}; const uint8_t* stream_{}; size_t stream_len_{};
public:
    BlockTime() = default;
    explicit BlockTime(const BuildDir& b) {
        seg_ = b.open(Kind::blk_time, 0, true);
        first_ = seg_.aux(); count_ = seg_.count(); nck_ = seg_.aux2();
        auto p = seg_.payload();
        need(nck_ * sizeof(BlockCkpt) <= p.size(), "blk: shape");
        ck_ = reinterpret_cast<const BlockCkpt*>(p.data());
        stream_ = p.data() + nck_ * sizeof(BlockCkpt); stream_len_ = p.size() - nck_ * sizeof(BlockCkpt);
    }
    uint64_t first_block() const noexcept { return first_; }
    uint64_t last_block() const noexcept { return first_ + count_ - 1; }
    uint64_t count() const noexcept { return count_; }
    // Timestamp of block b; nullopt outside [first, last].
    std::optional<uint32_t> time_at(uint64_t b) const {
        if (b < first_ || b >= first_ + count_) return std::nullopt;
        uint64_t i = b - first_, c = i / block_ckpt_every;
        Cursor cur(stream_ + ck_[c].offset, stream_len_ - ck_[c].offset);
        uint32_t t = ck_[c].time;
        for (uint64_t k = c * block_ckpt_every + 1; k <= i; k++) t += uint32_t(get_uvarint(cur));
        return t;
    }
    // Last block with time <= t (nullopt when t precedes the first block). Times are non-decreasing.
    std::optional<uint64_t> block_at(uint32_t t) const {
        if (!count_ || t < ck_[0].time) return std::nullopt;
        uint64_t lo = 0, hi = nck_;   // last checkpoint with time <= t
        while (hi - lo > 1) { uint64_t mid = (lo + hi) / 2; if (ck_[mid].time <= t) lo = mid; else hi = mid; }
        uint64_t i0 = lo * block_ckpt_every, end = std::min<uint64_t>(count_, i0 + block_ckpt_every);
        Cursor cur(stream_ + ck_[lo].offset, stream_len_ - ck_[lo].offset);
        uint32_t cur_t = ck_[lo].time;
        for (uint64_t k = i0 + 1; k < end; k++) {
            uint32_t d = uint32_t(get_uvarint(cur));
            if (cur_t + d > t) return first_ + k - 1;
            cur_t += d;
        }
        return first_ + end - 1;
    }
};

// Writes blk.time from consecutive block timestamps starting at first_block.
class BlockTimeWriter {
    fs::path path_; uint64_t first_; std::vector<BlockCkpt> ck_; Bytes stream_; uint64_t n_{}; uint32_t last_{};
public:
    BlockTimeWriter(const fs::path& dir, uint64_t first_block) : path_(dir / segment_file_name(Kind::blk_time)), first_(first_block) {}
    void push(uint32_t time) {
        need(n_ == 0 || time >= last_, "blk: timestamps must be non-decreasing");
        if (n_ % block_ckpt_every == 0) ck_.push_back({time, 0, stream_.size()});   // checkpoint blocks carry no delta
        else put_uvarint(stream_, time - last_);
        last_ = time; n_++;
    }
    SegResult finish() {
        SegWriter w(path_, Kind::blk_time);
        w.put(ck_.data(), ck_.size() * sizeof(BlockCkpt)); w.put(stream_.data(), stream_.size());
        return w.finish(n_, first_, ck_.size());
    }
};

} // namespace machine::engine
