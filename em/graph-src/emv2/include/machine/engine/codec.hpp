#pragma once
// Byte codecs for sealed segments (EM-ENGINE-V2 §5): LEB128 varints, zigzag, exact I256/U256
// amounts, fixed-width bit packing and Elias-Fano monotone sequences.
// Writers append to std::vector<uint8_t>; readers take a bounded Cursor and throw on truncation
// (sealed data is sha256-pinned, so a throw means corruption or a bug, never normal flow).
#include "machine/engine/u256.hpp"
#include <span>
#include <stdexcept>
#include <vector>

namespace machine::engine {

using Bytes = std::vector<uint8_t>;

inline void codec_need(bool ok, const char* why) { if (!ok) throw std::runtime_error(why); }

struct Cursor {
    const uint8_t* p{};
    const uint8_t* e{};
    Cursor() = default;
    Cursor(const uint8_t* b, size_t n) : p(b), e(b + n) {}
    explicit Cursor(std::span<const uint8_t> s) : p(s.data()), e(s.data() + s.size()) {}
    size_t left() const noexcept { return size_t(e - p); }
    bool done() const noexcept { return p >= e; }
    uint8_t byte() { codec_need(p < e, "codec: truncated"); return *p++; }
    void raw(void* out, size_t n) { codec_need(left() >= n, "codec: truncated"); std::memcpy(out, p, n); p += n; }
    void skip(size_t n) { codec_need(left() >= n, "codec: truncated"); p += n; }
};

// ---------------------------------------------------------------- varints
inline void put_uvarint(Bytes& o, uint64_t v) { while (v >= 0x80) { o.push_back(uint8_t(v) | 0x80); v >>= 7; } o.push_back(uint8_t(v)); }
inline void put_uvarint128(Bytes& o, u128 v) { while (v >= 0x80) { o.push_back(uint8_t(v) | 0x80); v >>= 7; } o.push_back(uint8_t(v)); }
inline constexpr uint64_t zigzag(int64_t v) noexcept { return (uint64_t(v) << 1) ^ uint64_t(v >> 63); }
inline constexpr int64_t unzigzag(uint64_t v) noexcept { return int64_t(v >> 1) ^ -int64_t(v & 1); }
inline constexpr u128 zigzag128(i128 v) noexcept { return (u128(v) << 1) ^ u128(v >> 127); }
inline constexpr i128 unzigzag128(u128 v) noexcept { return i128(v >> 1) ^ -i128(v & 1); }
inline void put_svarint(Bytes& o, int64_t v) { put_uvarint(o, zigzag(v)); }

inline uint64_t get_uvarint(Cursor& c) {
    uint64_t v = 0;
    for (int s = 0; s < 64; s += 7) {
        uint8_t b = c.byte();
        v |= uint64_t(b & 0x7f) << s;
        if (!(b & 0x80)) { codec_need(s < 63 || b <= 1, "codec: uvarint overflow"); return v; }
    }
    throw std::runtime_error("codec: uvarint too long");
}
inline u128 get_uvarint128(Cursor& c) {
    u128 v = 0;
    for (int s = 0; s < 128; s += 7) {
        uint8_t b = c.byte();
        v |= u128(b & 0x7f) << s;
        if (!(b & 0x80)) { codec_need(s < 126 || b <= 3, "codec: uvarint128 overflow"); return v; }
    }
    throw std::runtime_error("codec: uvarint128 too long");
}
inline int64_t get_svarint(Cursor& c) { return unzigzag(get_uvarint(c)); }

// Exact amounts. Fast path: |v| < 2^126 -> LEB128(zigzag128(v) << 1). Otherwise a single 0x01
// byte (tag bit set, zero payload) followed by the 32 raw little-endian limbs. Typical ERC-20
// amounts take 6-12 bytes; spam max-uint transfers take 33.
inline void put_i256(Bytes& o, const I256& v) {
    if (v.fits_i128()) {
        i128 x = v.to_i128();
        u128 z = zigzag128(x);
        if (!(z >> 127)) { put_uvarint128(o, z << 1); return; }
    }
    o.push_back(0x01);
    const uint8_t* p = reinterpret_cast<const uint8_t*>(v.v.w);
    o.insert(o.end(), p, p + 32);
}
inline I256 get_i256(Cursor& c) {
    u128 x = get_uvarint128(c);
    if (x & 1) { codec_need(x == 1, "codec: i256 tag"); I256 r; c.raw(r.v.w, 32); return r; }
    return I256::from_i128(unzigzag128(x >> 1));
}
inline void put_u256(Bytes& o, const U256& v) {
    if (v.fits_u128() && !(v.low128() >> 127)) { put_uvarint128(o, v.low128() << 1); return; }
    o.push_back(0x01);
    const uint8_t* p = reinterpret_cast<const uint8_t*>(v.w);
    o.insert(o.end(), p, p + 32);
}
inline U256 get_u256(Cursor& c) {
    u128 x = get_uvarint128(c);
    if (x & 1) { codec_need(x == 1, "codec: u256 tag"); U256 r; c.raw(r.w, 32); return r; }
    return U256::from_u128(x >> 1);
}
template <class T> inline void put_pod(Bytes& o, const T& v) { const uint8_t* p = reinterpret_cast<const uint8_t*>(&v); o.insert(o.end(), p, p + sizeof(T)); }
template <class T> inline T get_pod(Cursor& c) { T v; c.raw(&v, sizeof(T)); return v; }

// ---------------------------------------------------------------- bit packing
inline constexpr unsigned bits_for(uint64_t max_value) noexcept { return max_value ? 64 - unsigned(__builtin_clzll(max_value)) : 0; }

// Fixed-width packing of `width` bits per value (0..64), LSB-first in little-endian u64 words.
// The packed buffer is padded with one extra word so readers may load two words unconditionally.
class BitWriter {
    std::vector<uint64_t> w_; uint64_t bits_{}; unsigned width_;
public:
    explicit BitWriter(unsigned width) : width_(width) { codec_need(width <= 64, "codec: width"); }
    void push(uint64_t v) {
        if (!width_) return;
        codec_need(width_ == 64 || v >> width_ == 0, "codec: value exceeds width");
        size_t word = size_t(bits_ >> 6); unsigned off = unsigned(bits_ & 63);
        if (w_.size() < word + 2) w_.resize(word + 2, 0);
        w_[word] |= v << off;
        if (off && off + width_ > 64) w_[word + 1] |= v >> (64 - off);
        bits_ += width_;
    }
    unsigned width() const noexcept { return width_; }
    uint64_t bit_count() const noexcept { return bits_; }
    // Appends the packed words (always (bits+63)/64 + 1 words).
    void finish(Bytes& o) {
        size_t words = size_t((bits_ + 63) >> 6) + 1;
        w_.resize(words, 0);
        const uint8_t* p = reinterpret_cast<const uint8_t*>(w_.data());
        o.insert(o.end(), p, p + words * 8);
    }
    static size_t packed_bytes(uint64_t n, unsigned width) noexcept { return size_t(((n * width + 63) >> 6) + 1) * 8; }
};
// Reads value i of a BitWriter buffer (base must be 8-byte aligned or unaligned-safe x86).
inline uint64_t bit_get(const uint8_t* base, unsigned width, uint64_t i) noexcept {
    if (!width) return 0;
    uint64_t bit = i * width; uint64_t a, b;
    std::memcpy(&a, base + (bit >> 6) * 8, 8); std::memcpy(&b, base + ((bit >> 6) + 1) * 8, 8);
    unsigned off = unsigned(bit & 63);
    uint64_t v = off ? (a >> off) | (b << (64 - off)) : a;
    return width == 64 ? v : v & ((uint64_t(1) << width) - 1);
}

// ---------------------------------------------------------------- Elias-Fano
// Monotone non-decreasing sequence v[0..n) with v < universe. Layout (all derivable from n and
// universe, so callers store only those two): low bits (BitWriter, width l) then high bits
// (n + (universe >> l) + 1 bits, unary), then one u64 sample per 256 ones = bit position of the
// (256k)-th one, for select. l = floor(log2(universe / n)) (0 when universe <= n).
struct EFShape {
    uint64_t n{}, universe{}; unsigned l{}; size_t low_bytes{}, high_words{}, samples{};
    static EFShape of(uint64_t n, uint64_t universe) noexcept {
        EFShape s; s.n = n; s.universe = universe;
        s.l = (n && universe > n) ? bits_for(universe / n) - 1 : 0;
        s.low_bytes = BitWriter::packed_bytes(n, s.l);
        uint64_t high_bits = n + (universe >> s.l) + 1;
        s.high_words = size_t((high_bits + 63) >> 6) + 1;
        s.samples = size_t((n + 255) / 256);
        return s;
    }
    size_t bytes() const noexcept { return low_bytes + high_words * 8 + samples * 8; }
};
inline void ef_encode(std::span<const uint64_t> v, uint64_t universe, Bytes& o) {
    EFShape s = EFShape::of(v.size(), universe);
    BitWriter low(s.l);
    std::vector<uint64_t> high(s.high_words, 0), samples; samples.reserve(s.samples);
    uint64_t prev = 0;
    for (uint64_t i = 0; i < v.size(); i++) {
        codec_need(v[i] >= prev && v[i] < universe, "codec: ef not monotone or out of universe");
        prev = v[i];
        low.push(s.l ? v[i] & ((uint64_t(1) << s.l) - 1) : 0);
        uint64_t pos = (v[i] >> s.l) + i;
        high[pos >> 6] |= uint64_t(1) << (pos & 63);
        if (i % 256 == 0) samples.push_back(pos);
    }
    size_t at = o.size();
    low.finish(o);
    o.resize(at + s.low_bytes, 0);
    const uint8_t* hp = reinterpret_cast<const uint8_t*>(high.data()); o.insert(o.end(), hp, hp + high.size() * 8);
    const uint8_t* sp = reinterpret_cast<const uint8_t*>(samples.data()); o.insert(o.end(), sp, sp + samples.size() * 8);
}
class EFView {
    const uint8_t* low_{}; const uint8_t* high_{}; const uint8_t* samp_{}; EFShape s_{};
    uint64_t hword(size_t i) const noexcept { uint64_t x; std::memcpy(&x, high_ + i * 8, 8); return x; }
public:
    EFView() = default;
    EFView(const uint8_t* p, uint64_t n, uint64_t universe) : s_(EFShape::of(n, universe)) {
        low_ = p; high_ = p + s_.low_bytes; samp_ = high_ + s_.high_words * 8;
    }
    uint64_t size() const noexcept { return s_.n; }
    size_t bytes() const noexcept { return s_.bytes(); }
    // Bit position of the i-th one in the high bits.
    uint64_t select1(uint64_t i) const noexcept {
        uint64_t pos; std::memcpy(&pos, samp_ + (i / 256) * 8, 8);
        uint64_t need = i % 256;
        size_t word = size_t(pos >> 6); uint64_t x = hword(word) & (~uint64_t(0) << (pos & 63));
        for (;;) {
            unsigned c = unsigned(__builtin_popcountll(x));
            if (need < c) { for (; need; need--) x &= x - 1; return uint64_t(word) * 64 + unsigned(__builtin_ctzll(x)); }
            need -= c; x = hword(++word);
        }
    }
    uint64_t get(uint64_t i) const noexcept {
        uint64_t hi = select1(i) - i;
        return (hi << s_.l) | bit_get(low_, s_.l, i);
    }
    // First index with v[i] >= x (size() when none).
    uint64_t lower_bound(uint64_t x) const noexcept {
        uint64_t lo = 0, hi = s_.n;
        while (lo < hi) { uint64_t mid = (lo + hi) / 2; if (get(mid) < x) lo = mid + 1; else hi = mid; }
        return lo;
    }
    // Sequential decode of [from, to) into out; much faster than repeated get().
    template <class F> void for_each(uint64_t from, uint64_t to, F&& f) const {
        if (from >= to) return;
        uint64_t pos = select1(from); size_t word = size_t(pos >> 6);
        uint64_t x = hword(word) & (~uint64_t(0) << (pos & 63));
        for (uint64_t i = from; i < to; i++) {
            while (!x) x = hword(++word);
            uint64_t p = uint64_t(word) * 64 + unsigned(__builtin_ctzll(x)); x &= x - 1;
            f(i, ((p - i) << s_.l) | bit_get(low_, s_.l, i));
        }
    }
};

} // namespace machine::engine
