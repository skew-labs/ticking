#pragma once
// Economic Machine graph-loop: shared primitives.
// Conventions follow the Economic Machine native code (ticking::evm evm_wire.hpp / recovery.hpp,
// machine::economics value.hpp): need(ok, "reason") throws std::runtime_error with a short,
// secret-free reason; CryptoPP Keccak_256 (Ethereum keccak, not SHA3-256) and SHA256; bounded
// inputs everywhere; durable writes go through seal_file / sync_directory.
#include <cryptopp/keccak.h>
#include <cryptopp/sha.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace machine::graphloop {
using Bytes = std::vector<uint8_t>;
using View = std::span<const uint8_t>;
using Hash = std::array<uint8_t, 32>;
inline constexpr uint32_t none = 0xffffffffu;

inline void need(bool ok, const char* reason) { if (!ok) throw std::runtime_error(reason); }
inline void need(bool ok, const std::string& reason) { if (!ok) throw std::runtime_error(reason); }

// 20-byte EVM address, ordered by bytes (== numeric order of the address).
struct Addr {
    uint8_t b[20];
    friend bool operator==(const Addr& a, const Addr& c) noexcept { return std::memcmp(a.b, c.b, 20) == 0; }
    friend bool operator<(const Addr& a, const Addr& c) noexcept { return std::memcmp(a.b, c.b, 20) < 0; }
};
static_assert(sizeof(Addr) == 20);

inline Hash keccak256(View x) { Hash o; CryptoPP::Keccak_256().CalculateDigest(o.data(), x.data(), x.size()); return o; }
inline Hash keccak256(std::string_view s) { return keccak256(View(reinterpret_cast<const uint8_t*>(s.data()), s.size())); }
inline Hash sha256(View x) { Hash o; CryptoPP::SHA256().CalculateDigest(o.data(), x.data(), x.size()); return o; }
inline Hash sha256(std::string_view s) { return sha256(View(reinterpret_cast<const uint8_t*>(s.data()), s.size())); }

inline std::string hex(View x) {
    std::string o; o.reserve(2 * x.size());
    for (auto b : x) { o += "0123456789abcdef"[b >> 4]; o += "0123456789abcdef"[b & 15]; }
    return o;
}
inline std::string hex0x(View x) { return "0x" + hex(x); }
inline std::string hex0x(const Addr& a) { return hex0x(View(a.b, 20)); }
inline int hexval(char c) noexcept {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
// Exact-width hex decoding ("0x" optional). Returns false on any malformed input.
inline bool unhex_exact(std::string_view s, uint8_t* out, size_t n) noexcept {
    if (s.size() >= 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) s.remove_prefix(2);
    if (s.size() != 2 * n) return false;
    for (size_t i = 0; i < n; i++) {
        int h = hexval(s[2 * i]), l = hexval(s[2 * i + 1]);
        if (h < 0 || l < 0) return false;
        out[i] = uint8_t(h << 4 | l);
    }
    return true;
}
inline bool parse_addr(std::string_view s, Addr& a) noexcept { return unhex_exact(s, a.b, 20); }
inline Addr addr_from(std::string_view s) { Addr a{}; need(parse_addr(s, a), "address hex"); return a; }

// CRC-32C (Castagnoli) for WAL record framing; hardware instruction when compiled for SSE4.2.
inline uint32_t crc32c(const uint8_t* p, size_t n) noexcept {
    uint32_t c = 0xffffffffu;
#if defined(__SSE4_2__)
    while (n >= 8) { uint64_t v; std::memcpy(&v, p, 8); c = uint32_t(__builtin_ia32_crc32di(c, v)); p += 8; n -= 8; }
    while (n--) c = __builtin_ia32_crc32qi(c, *p++);
#else
    static const auto table = [] { std::array<uint32_t, 256> t{}; for (uint32_t i = 0; i < 256; i++) { uint32_t r = i; for (int k = 0; k < 8; k++) r = (r >> 1) ^ (0x82f63b78u & (0u - (r & 1))); t[i] = r; } return t; }();
    while (n--) c = table[(c ^ *p++) & 255] ^ (c >> 8);
#endif
    return ~c;
}

inline uint64_t now_ms() { return uint64_t(std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count()); }
inline uint64_t mono_ns() { return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count()); }

// Minimal JSON string escaping for log lines and hand-written responses.
inline void json_escape(std::string& o, std::string_view s) {
    o += '"';
    for (unsigned char c : s) {
        switch (c) {
        case '"': o += "\\\""; break;
        case '\\': o += "\\\\"; break;
        case '\n': o += "\\n"; break;
        case '\r': o += "\\r"; break;
        case '\t': o += "\\t"; break;
        default:
            if (c < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); o += b; } else o += char(c);
        }
    }
    o += '"';
}
inline std::string jstr(std::string_view s) { std::string o; json_escape(o, s); return o; }

// One JSON object per line on stderr (operational log); results go to stdout as JSON.
inline void log_event(const char* event, const std::string& fields = {}) {
    std::string line = "{\"ts_ms\":" + std::to_string(now_ms()) + ",\"event\":" + jstr(event);
    if (!fields.empty()) { line += ','; line += fields; }
    line += "}\n";
    std::fwrite(line.data(), 1, line.size(), stderr);
}

// Deterministic diagnostic identity (FNV-1a), not a commitment; commitments use SHA-256.
inline uint64_t fnv1a(const void* p, size_t n, uint64_t h = 14695981039346656037ULL) noexcept {
    auto b = static_cast<const uint8_t*>(p);
    for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 1099511628211ULL; }
    return h;
}
inline uint64_t mix64(uint64_t x) noexcept { x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL; x ^= x >> 33; return x; }

// Edge weight (transfer count) code: exact below 32768, else 1/1024-log2 steps (< 0.07 % error).
inline uint16_t wcode(uint64_t n) noexcept {
    if (n < 32768) return uint16_t(n);
    double l = std::log2(double(n)) - 15.0;          // >= 0
    uint32_t q = uint32_t(std::lround(l * 1024.0));
    return uint16_t(0x8000u | std::min<uint32_t>(q, 0x7fffu));
}
inline double wvalue(uint16_t w) noexcept {
    if (!(w & 0x8000u)) return double(w);
    return std::exp2(15.0 + double(w & 0x7fffu) / 1024.0);
}
inline uint64_t wcount(uint16_t w) noexcept { return uint64_t(std::llround(wvalue(w))); }

// Fixed-precision helpers for shares (u16 = share * 65535) and confidence (u8 = conf * 255).
inline uint16_t share16(double s) noexcept { return uint16_t(std::lround(std::clamp(s, 0.0, 1.0) * 65535.0)); }
inline double share_of(uint16_t s) noexcept { return double(s) / 65535.0; }
inline uint8_t conf8(double c) noexcept { return uint8_t(std::lround(std::clamp(c, 0.0, 1.0) * 255.0)); }
inline double conf_of(uint8_t c) noexcept { return double(c) / 255.0; }
} // namespace machine::graphloop
