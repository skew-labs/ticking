#pragma once
// prices.hpp: the one place raw token amounts become USD (EM-ENGINE-V2 §5.6).
//   raw amounts      exact integers (ledger Acc320 / I256)
//   price            decimal float: USD per whole token = mantissa * 10^exp10 (1e6 fixed point would round
//                    sub-$0.000001 meme prices to zero)
//   USD value        machine_economics fixed point (i64, scale 1e6), floor rounding
// usd_value() is exact: raw * mantissa is formed in 448 bits and divided by a power of ten once.
#include "machine/economics/value.hpp"
#include "machine/engine/ledger.hpp"

#include <cstdint>

namespace machine::engine::prices {

namespace econ = machine::economics;

struct DecPrice { int64_t mantissa = 0; int8_t exp10 = 0; };   // USD per 1 whole token

namespace detail {
struct W7 { uint64_t w[7]{}; };   // unsigned 448-bit
inline bool is_zero(const W7& a) { for (auto x : a.w) if (x) return false; return true; }
inline bool mul_small(W7& a, uint64_t m) {   // false on overflow
    unsigned __int128 c = 0;
    for (auto& x : a.w) { c += (unsigned __int128)x * m; x = uint64_t(c); c >>= 64; }
    return c == 0;
}
inline uint64_t div_small(W7& a, uint64_t d) {   // returns remainder
    unsigned __int128 r = 0;
    for (int i = 6; i >= 0; --i) { unsigned __int128 cur = (r << 64) | a.w[i]; a.w[i] = uint64_t(cur / d); r = cur % d; }
    return uint64_t(r);
}
}  // namespace detail

// floor(raw * price / 10^decimals) in USD 1e6 units.  Error::overflow beyond +-9.2e12 USD, Error::invalid for a
// negative price or decimals > 77.
[[nodiscard]] inline econ::Result<int64_t> usd_value(const ledger::fmt::Acc320& raw, uint8_t decimals, DecPrice p) noexcept {
    using detail::W7;
    if (p.mantissa < 0 || decimals > 77) return econ::failure<int64_t>(econ::Error::invalid);
    if (p.mantissa == 0 || raw.is_zero()) return econ::success<int64_t>(0);
    bool neg = raw.negative();
    ledger::fmt::Acc320 m = raw.magnitude();
    W7 v; for (int i = 0; i < 5; ++i) v.w[i] = m.w[i];
    if (!detail::mul_small(v, uint64_t(p.mantissa))) return econ::failure<int64_t>(econ::Error::overflow);
    // value_1e6 = raw * mantissa * 10^(exp10 + 6 - decimals)
    int shift = int(p.exp10) + 6 - int(decimals);
    bool inexact = false;
    for (; shift > 0; --shift) if (!detail::mul_small(v, 10)) return econ::failure<int64_t>(econ::Error::overflow);
    for (; shift < 0; ++shift) { if (detail::is_zero(v)) break; inexact |= detail::div_small(v, 10) != 0; }
    for (int i = 1; i < 7; ++i) if (v.w[i]) return econ::failure<int64_t>(econ::Error::overflow);
    if (v.w[0] > uint64_t(INT64_MAX)) return econ::failure<int64_t>(econ::Error::overflow);
    int64_t r = int64_t(v.w[0]);
    if (neg) r = -r - (inexact ? 1 : 0);   // floor toward -inf
    return econ::success<int64_t>(r);
}

// Parses "0.000000123" / "1.5e-9" / "42" into a DecPrice (at most 18 significant digits; extra digits are cut).
[[nodiscard]] inline std::optional<DecPrice> parse_price(std::string_view s) noexcept {
    int64_t m = 0; int exp = 0, digits = 0; bool dot = false, any = false;
    size_t i = 0;
    for (; i < s.size(); ++i) {
        char c = s[i];
        if (c == '.') { if (dot) return std::nullopt; dot = true; continue; }
        if (c == 'e' || c == 'E') break;
        if (c < '0' || c > '9') return std::nullopt;
        any = true;
        if (digits < 18) { if (m || c != '0') ++digits; m = m * 10 + (c - '0'); if (dot) --exp; }
        else if (!dot) ++exp;
    }
    if (!any) return std::nullopt;
    if (i < s.size()) {
        ++i; bool en = false; if (i < s.size() && (s[i] == '-' || s[i] == '+')) en = s[i++] == '-';
        int e = 0; if (i >= s.size()) return std::nullopt;
        for (; i < s.size(); ++i) { if (s[i] < '0' || s[i] > '9' || e > 1000) return std::nullopt; e = e * 10 + (s[i] - '0'); }
        exp += en ? -e : e;
    }
    while (m && m % 10 == 0) { m /= 10; ++exp; }
    if (exp < -127 || exp > 127) return std::nullopt;
    return DecPrice{m, int8_t(exp)};
}

}  // namespace machine::engine::prices
