#pragma once
// Exact 256-bit integers for raw token amounts, balances and supplies (EM-ENGINE-V2 §5.6).
// U256 is unsigned, I256 is two's complement; limbs are little-endian (w[0] least significant).
// Every operation that can leave the range reports it (bool / std::optional), never wraps
// silently. USD values and ratios do not live here: they use machine::economics (scale 1e6 i64).
#include <array>
#include <compare>
#include <cstdint>
#include <cstring>
#include <optional>
#include <string>
#include <string_view>

namespace machine::engine {

using u128 = unsigned __int128;
using i128 = __int128;

enum class Rounding : uint8_t { floor, ceil };

struct U256 {
    uint64_t w[4]{};

    static constexpr U256 from_u64(uint64_t v) noexcept { U256 r; r.w[0] = v; return r; }
    static constexpr U256 from_u128(u128 v) noexcept { U256 r; r.w[0] = uint64_t(v); r.w[1] = uint64_t(v >> 64); return r; }
    static constexpr U256 max() noexcept { U256 r; r.w[0] = r.w[1] = r.w[2] = r.w[3] = ~uint64_t(0); return r; }
    // 32-byte big-endian (extractor FIXED_LEN_BYTE_ARRAY(32): value, amount, token_id).
    static U256 from_be32(const uint8_t* p) noexcept {
        U256 r;
        for (int i = 0; i < 4; i++) { uint64_t v = 0; for (int k = 0; k < 8; k++) v = v << 8 | p[(3 - i) * 8 + k]; r.w[i] = v; }
        return r;
    }
    void to_be32(uint8_t* p) const noexcept {
        for (int i = 0; i < 4; i++) for (int k = 0; k < 8; k++) p[(3 - i) * 8 + k] = uint8_t(w[i] >> (56 - 8 * k));
    }
    constexpr bool is_zero() const noexcept { return (w[0] | w[1] | w[2] | w[3]) == 0; }
    constexpr bool fits_u64() const noexcept { return (w[1] | w[2] | w[3]) == 0; }
    constexpr bool fits_u128() const noexcept { return (w[2] | w[3]) == 0; }
    constexpr u128 low128() const noexcept { return u128(w[1]) << 64 | w[0]; }
    constexpr int bit_length() const noexcept {
        for (int i = 3; i >= 0; i--) if (w[i]) return 64 * i + 64 - __builtin_clzll(w[i]);
        return 0;
    }
    constexpr bool bit(int i) const noexcept { return (w[i >> 6] >> (i & 63)) & 1; }
    friend constexpr bool operator==(const U256& a, const U256& b) noexcept {
        return a.w[0] == b.w[0] && a.w[1] == b.w[1] && a.w[2] == b.w[2] && a.w[3] == b.w[3];
    }
    friend constexpr std::strong_ordering operator<=>(const U256& a, const U256& b) noexcept {
        for (int i = 3; i >= 0; i--) if (a.w[i] != b.w[i]) return a.w[i] < b.w[i] ? std::strong_ordering::less : std::strong_ordering::greater;
        return std::strong_ordering::equal;
    }
};
static_assert(sizeof(U256) == 32);

// out = a + b; returns false on overflow (out then holds the value mod 2^256).
[[nodiscard]] inline bool add(const U256& a, const U256& b, U256& out) noexcept {
    u128 c = 0;
    for (int i = 0; i < 4; i++) { c += u128(a.w[i]) + b.w[i]; out.w[i] = uint64_t(c); c >>= 64; }
    return c == 0;
}
// out = a - b; returns false when b > a.
[[nodiscard]] inline bool sub(const U256& a, const U256& b, U256& out) noexcept {
    uint64_t borrow = 0;
    for (int i = 0; i < 4; i++) {
        u128 d = u128(a.w[i]) - b.w[i] - borrow;
        out.w[i] = uint64_t(d); borrow = uint64_t(d >> 64) & 1;
    }
    return borrow == 0;
}
[[nodiscard]] inline bool mul_u64(const U256& a, uint64_t m, U256& out) noexcept {
    u128 c = 0;
    for (int i = 0; i < 4; i++) { c += u128(a.w[i]) * m; out.w[i] = uint64_t(c); c >>= 64; }
    return c == 0;
}
// Quotient of a / d (d != 0), remainder in rem.
inline U256 divmod_u64(const U256& a, uint64_t d, uint64_t& rem) noexcept {
    U256 q; u128 r = 0;
    for (int i = 3; i >= 0; i--) { r = r << 64 | a.w[i]; q.w[i] = uint64_t(r / d); r %= d; }
    rem = uint64_t(r);
    return q;
}

struct U512 { uint64_t w[8]{}; };
inline U512 mul_full(const U256& a, const U256& b) noexcept {
    U512 r;
    for (int i = 0; i < 4; i++) {
        u128 c = 0;
        for (int j = 0; j < 4; j++) { c += u128(a.w[i]) * b.w[j] + r.w[i + j]; r.w[i + j] = uint64_t(c); c >>= 64; }
        r.w[i + 4] = uint64_t(c);
    }
    return r;
}

// floor or ceil of a * b / d with a 512-bit intermediate. nullopt when d == 0 or the result needs > 256 bits.
// Shift-subtract division: a few microseconds, meant for valuation paths, not inner loops.
[[nodiscard]] inline std::optional<U256> mul_div(const U256& a, const U256& b, const U256& d, Rounding r = Rounding::floor) noexcept {
    if (d.is_zero()) return std::nullopt;
    U512 p = mul_full(a, b);
    uint64_t rem[5]{};   // remainder < d < 2^256, one guard limb for the shift
    U512 q;
    for (int bit = 511; bit >= 0; bit--) {
        uint64_t top = rem[4] << 1 | rem[3] >> 63;
        for (int i = 3; i > 0; i--) rem[i] = rem[i] << 1 | rem[i - 1] >> 63;
        rem[0] = rem[0] << 1 | ((p.w[bit >> 6] >> (bit & 63)) & 1);
        rem[4] = top;
        bool ge = rem[4] != 0;
        if (!ge) {
            ge = true;
            for (int i = 3; i >= 0; i--) if (rem[i] != d.w[i]) { ge = rem[i] > d.w[i]; break; }
        }
        if (ge) {
            uint64_t borrow = 0;
            for (int i = 0; i < 4; i++) { u128 x = u128(rem[i]) - d.w[i] - borrow; rem[i] = uint64_t(x); borrow = uint64_t(x >> 64) & 1; }
            rem[4] -= borrow;
            q.w[bit >> 6] |= uint64_t(1) << (bit & 63);
        }
    }
    for (int i = 4; i < 8; i++) if (q.w[i]) return std::nullopt;
    U256 out; std::memcpy(out.w, q.w, 32);
    if (r == Rounding::ceil && (rem[0] | rem[1] | rem[2] | rem[3])) {
        U256 one = U256::from_u64(1);
        if (!add(out, one, out)) return std::nullopt;
    }
    return out;
}

inline std::string to_dec(const U256& v) {
    if (v.is_zero()) return "0";
    char buf[80]; int n = 0; U256 x = v;
    while (!x.is_zero()) {
        uint64_t rem; x = divmod_u64(x, 10'000'000'000'000'000'000ull, rem);
        for (int k = 0; k < 19; k++) { buf[n++] = char('0' + rem % 10); rem /= 10; if (x.is_zero() && rem == 0) break; }
    }
    std::string s(buf, size_t(n));
    while (s.size() > 1 && s.back() == '0') s.pop_back();
    return {s.rbegin(), s.rend()};
}
// Plain decimal digits only (no sign, no separators). nullopt on empty input, bad digit or > 2^256-1.
[[nodiscard]] inline std::optional<U256> from_dec(std::string_view s) noexcept {
    if (s.empty() || s.size() > 78) return std::nullopt;
    U256 v;
    for (char c : s) {
        if (c < '0' || c > '9') return std::nullopt;
        U256 t;
        if (!mul_u64(v, 10, t) || !add(t, U256::from_u64(uint64_t(c - '0')), v)) return std::nullopt;
    }
    return v;
}
// "1234.5678" for decimals = 4, trailing fraction zeros trimmed ("12" not "12.0000").
inline std::string format_units(const U256& v, unsigned decimals) {
    std::string d = to_dec(v);
    if (decimals == 0) return d;
    if (d.size() <= decimals) d.insert(0, decimals + 1 - d.size(), '0');
    std::string i = d.substr(0, d.size() - decimals), f = d.substr(d.size() - decimals);
    while (!f.empty() && f.back() == '0') f.pop_back();
    return f.empty() ? i : i + "." + f;
}
inline double to_double(const U256& v) noexcept {
    double r = 0;
    for (int i = 3; i >= 0; i--) r = r * 18446744073709551616.0 + double(v.w[i]);
    return r;
}

// Two's complement signed 256-bit integer.
struct I256 {
    U256 v;

    static constexpr I256 from_i64(int64_t x) noexcept {
        I256 r; uint64_t f = x < 0 ? ~uint64_t(0) : 0;
        r.v.w[0] = uint64_t(x); r.v.w[1] = r.v.w[2] = r.v.w[3] = f; return r;
    }
    static constexpr I256 from_i128(i128 x) noexcept {
        I256 r; uint64_t f = x < 0 ? ~uint64_t(0) : 0;
        r.v.w[0] = uint64_t(u128(x)); r.v.w[1] = uint64_t(u128(x) >> 64); r.v.w[2] = r.v.w[3] = f; return r;
    }
    // nullopt when u >= 2^255.
    static std::optional<I256> from_u256(const U256& u) noexcept { if (u.w[3] >> 63) return std::nullopt; I256 r; r.v = u; return r; }
    constexpr bool negative() const noexcept { return v.w[3] >> 63; }
    constexpr int sign() const noexcept { return negative() ? -1 : (v.is_zero() ? 0 : 1); }
    constexpr bool is_zero() const noexcept { return v.is_zero(); }
    // |x| as U256 (exact also for -2^255).
    U256 abs() const noexcept {
        if (!negative()) return v;
        U256 n; for (int i = 0; i < 4; i++) n.w[i] = ~v.w[i];
        U256 out; (void)add(n, U256::from_u64(1), out); return out;
    }
    constexpr bool fits_i128() const noexcept {
        uint64_t f = negative() ? ~uint64_t(0) : 0;
        return v.w[2] == f && v.w[3] == f && ((v.w[1] >> 63) == (f & 1));
    }
    constexpr i128 to_i128() const noexcept { return i128(u128(v.w[1]) << 64 | v.w[0]); }
    friend constexpr bool operator==(const I256& a, const I256& b) noexcept { return a.v == b.v; }
    friend constexpr std::strong_ordering operator<=>(const I256& a, const I256& b) noexcept {
        if (a.negative() != b.negative()) return a.negative() ? std::strong_ordering::less : std::strong_ordering::greater;
        return a.v <=> b.v;
    }
};
static_assert(sizeof(I256) == 32);

[[nodiscard]] inline bool neg(const I256& a, I256& out) noexcept {
    U256 n; for (int i = 0; i < 4; i++) n.w[i] = ~a.v.w[i];
    (void)add(n, U256::from_u64(1), out.v);
    return !(a.negative() && out.negative());   // -(-2^255) overflows
}
[[nodiscard]] inline bool add(const I256& a, const I256& b, I256& out) noexcept {
    (void)add(a.v, b.v, out.v);
    return !(a.negative() == b.negative() && out.negative() != a.negative());
}
[[nodiscard]] inline bool sub(const I256& a, const I256& b, I256& out) noexcept {
    (void)sub(a.v, b.v, out.v);
    return !(a.negative() != b.negative() && out.negative() != a.negative());
}
inline std::string to_dec(const I256& x) { return x.negative() ? "-" + to_dec(x.abs()) : to_dec(x.v); }
[[nodiscard]] inline std::optional<I256> i256_from_dec(std::string_view s) noexcept {
    bool minus = !s.empty() && s[0] == '-';
    auto u = from_dec(minus ? s.substr(1) : s);
    if (!u) return std::nullopt;
    if (minus) {
        if (u->w[3] >> 63) {   // only -2^255 itself fits
            U256 m; m.w[3] = uint64_t(1) << 63;
            if (!(*u == m)) return std::nullopt;
            I256 r; r.v = m; return r;
        }
        I256 p; p.v = *u; I256 r; (void)neg(p, r); return r;
    }
    return I256::from_u256(*u);
}
inline double to_double(const I256& x) noexcept { return x.negative() ? -to_double(x.abs()) : to_double(x.v); }

} // namespace machine::engine
