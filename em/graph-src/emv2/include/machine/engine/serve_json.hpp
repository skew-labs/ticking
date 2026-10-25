#pragma once
// JSON writer for emv2-serve. Numbers follow Python's json.dumps so a shadow comparison against the Python services sees the same
// text: floats print as repr() (shortest round-trip digits, fixed notation for 1e-4 <= |x| < 1e16, ".0" on integral values,
// exponent as e+16 / e-05), NaN and infinities print as null. Strings are escaped like graphloop's json_escape.
#include <machine/graphloop/core.hpp>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <ctime>
#include <string>
#include <string_view>
#include <vector>

namespace machine::engine::serve {

inline void py_float(std::string& o, double v) {
    if (!std::isfinite(v)) { o += "null"; return; }
    if (v == 0) { o += std::signbit(v) ? "-0.0" : "0.0"; return; }
    char buf[64];
    auto r = std::to_chars(buf, buf + sizeof buf, v, std::chars_format::scientific);   // shortest round-trip, d.ddde±XX
    std::string_view s(buf, size_t(r.ptr - buf));
    bool neg = s.front() == '-';
    if (neg) s.remove_prefix(1);
    auto e = s.find('e');
    std::string digits; digits += s[0];
    if (e > 1) digits.append(s.substr(2, e - 2));
    int exp10 = 0; std::from_chars(s.data() + e + 1 + (s[e + 1] == '+'), s.data() + s.size(), exp10);
    while (digits.size() > 1 && digits.back() == '0') digits.pop_back();
    int decpt = exp10 + 1;                                      // value = 0.DIGITS * 10^decpt
    if (neg) o += '-';
    if (decpt > -4 && decpt <= 16) {
        int n = int(digits.size());
        if (decpt <= 0) { o += "0."; o.append(size_t(-decpt), '0'); o += digits; }
        else if (decpt < n) { o.append(digits, 0, size_t(decpt)); o += '.'; o.append(digits, size_t(decpt)); }
        else { o += digits; o.append(size_t(decpt - n), '0'); o += ".0"; }
    } else {
        o += digits[0];
        if (digits.size() > 1) { o += '.'; o.append(digits, 1); }
        int x = decpt - 1;
        o += 'e'; o += x < 0 ? '-' : '+';
        int ax = x < 0 ? -x : x;
        if (ax < 10) o += '0';
        o += std::to_string(ax);
    }
}

// Python round(x, n) for display values (round-half-even on the binary value, like CPython's correctly rounded round()).
inline double py_round(double x, int n) {
    if (!std::isfinite(x)) return x;
    char buf[64];
    auto r = std::to_chars(buf, buf + sizeof buf, x, std::chars_format::fixed, n);
    double out = 0; std::from_chars(buf, r.ptr, out);
    return out;
}

class Json {
public:
    std::string s;
    Json() { s.reserve(4096); first_.reserve(16); }
    Json& ob() { sep(); s += '{'; first_.push_back(true); return *this; }
    Json& oe() { s += '}'; first_.pop_back(); return *this; }
    Json& ab() { sep(); s += '['; first_.push_back(true); return *this; }
    Json& ae() { s += ']'; first_.pop_back(); return *this; }
    Json& k(std::string_view key) { sep(); graphloop::json_escape(s, key); s += ':'; pending_value_ = true; return *this; }
    Json& str(std::string_view v) { sep(); graphloop::json_escape(s, v); return *this; }
    Json& str_or_null(std::string_view v) { return v.empty() ? null() : str(v); }
    Json& num(double v) { sep(); py_float(s, v); return *this; }
    Json& i(int64_t v) { sep(); s += std::to_string(v); return *this; }
    Json& u(uint64_t v) { sep(); s += std::to_string(v); return *this; }
    Json& b(bool v) { sep(); s += v ? "true" : "false"; return *this; }
    Json& null() { sep(); s += "null"; return *this; }
    Json& raw(std::string_view v) { sep(); s += v; return *this; }
    // key + value shorthands
    Json& ks(std::string_view key, std::string_view v) { return k(key).str(v); }
    Json& ksn(std::string_view key, std::string_view v) { return k(key).str_or_null(v); }
    Json& kn(std::string_view key, double v) { return k(key).num(v); }
    Json& ki(std::string_view key, int64_t v) { return k(key).i(v); }
    Json& ku(std::string_view key, uint64_t v) { return k(key).u(v); }
    Json& kb(std::string_view key, bool v) { return k(key).b(v); }
    Json& knull(std::string_view key) { return k(key).null(); }
private:
    std::vector<bool> first_;
    bool pending_value_ = false;
    void sep() {
        if (pending_value_) { pending_value_ = false; return; }   // value right after its key
        if (!first_.empty()) { if (!first_.back()) s += ','; first_.back() = false; }
    }
};

inline std::string addr_hex(const graphloop::Addr& a) { return graphloop::hex0x(a); }
inline std::string hash_hex(const graphloop::Hash& h) { return graphloop::hex0x(graphloop::View(h.data(), h.size())); }

// "%Y-%m-%dT%H:%M:%SZ" like the Python services' utc(); empty for 0 (they print null)
inline std::string utc_iso(uint64_t t) {
    if (!t) return {};
    time_t tt = time_t(t); struct tm g; gmtime_r(&tt, &g);
    char b[32]; std::strftime(b, sizeof b, "%Y-%m-%dT%H:%M:%SZ", &g);
    return b;
}
} // namespace machine::engine::serve
