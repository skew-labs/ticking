#pragma once
// Overlay: mutable, address-keyed facts served beside an immutable build (<root>/<chain>/overlay/). Labels change every day
// (Muse, label deltas) and token metadata comes from the econ token table, so neither belongs inside a sealed build: the overlay is
// rewritten by tools/emv2_overlay.py and re-read on SIGHUP. Keyed by the 20-byte address, so it stays valid across rebuilds.
//   ovl.label.seg  OvlLabel[]  sorted by address   (graphloop labels.parquet: category_code, confidence, source, tier, entity_name)
//   ovl.token.seg  OvlToken[]  sorted by address   (econ token_meta.parquet: symbol, name, decimals)
//   ovl.str.seg    bytes       strings referenced by both
//   overlay.json   {"schema":"economic-machine.emv2.overlay.v1","chain":...,"labels":N,"tokens":M,"sources":[...]}
#include "machine/engine/seg.hpp"
#include "machine/graphloop/core.hpp"
#include <algorithm>
#include <cctype>
#include <optional>
#include <vector>
#include <string_view>

namespace machine::engine {
using Addr = machine::graphloop::Addr;

struct OvlLabel {
    uint8_t addr[20];
    uint8_t category;       // graphloop Category code
    uint8_t conf;           // confidence * 255
    uint8_t source;         // graphloop Source code
    uint8_t tier;
    uint32_t entity_off;    // into ovl.str
    uint16_t entity_len;
    uint16_t pad;
};
static_assert(sizeof(OvlLabel) == 32);
struct OvlToken {
    uint8_t addr[20];
    uint8_t decimals;       // 255 = unknown
    uint8_t pad;
    uint16_t sym_len;
    uint32_t sym_off;       // symbol bytes then name bytes in ovl.str
    uint16_t name_len;
    uint16_t pad2;
};
static_assert(sizeof(OvlToken) == 32);

struct OvlEntity {
    uint64_t hash;          // fnv1a64 of the lowercase entity name (tools/emv2_overlay.py)
    uint8_t addr[20];
    uint32_t pad;
};
static_assert(sizeof(OvlEntity) == 32);
inline uint64_t entity_hash(std::string_view name) noexcept {
    uint64_t h = 14695981039346656037ULL;
    for (unsigned char c : name) { if (c >= 'A' && c <= 'Z') c = char(c + 32); h ^= c; h *= 1099511628211ULL; }
    return h;
}

class Overlay {
    SegView lab_, tok_, str_, ent_; std::span<const OvlLabel> l_; std::span<const OvlToken> t_; std::span<const OvlEntity> e_; std::string_view s_;
    template <class T> static const T* find(std::span<const T> v, const uint8_t* a) noexcept {
        size_t lo = 0, hi = v.size();
        while (lo < hi) { size_t mid = (lo + hi) / 2; int c = std::memcmp(v[mid].addr, a, 20); if (c == 0) return &v[mid]; if (c < 0) lo = mid + 1; else hi = mid; }
        return nullptr;
    }
public:
    Overlay() = default;
    explicit Overlay(const fs::path& dir) {
        if (fs::exists(dir / segment_file_name(Kind::ovl_label))) { lab_ = SegView(dir / segment_file_name(Kind::ovl_label), Kind::ovl_label); l_ = lab_.as<OvlLabel>(); }
        if (fs::exists(dir / segment_file_name(Kind::ovl_token))) { tok_ = SegView(dir / segment_file_name(Kind::ovl_token), Kind::ovl_token); t_ = tok_.as<OvlToken>(); }
        if (fs::exists(dir / segment_file_name(Kind::ovl_entity))) { ent_ = SegView(dir / segment_file_name(Kind::ovl_entity), Kind::ovl_entity); e_ = ent_.as<OvlEntity>(); }
        if (fs::exists(dir / segment_file_name(Kind::ovl_str))) {
            str_ = SegView(dir / segment_file_name(Kind::ovl_str), Kind::ovl_str);
            s_ = {reinterpret_cast<const char*>(str_.payload().data()), str_.payload().size()};
        }
        for (size_t i = 1; i < l_.size(); i++) need(std::memcmp(l_[i - 1].addr, l_[i].addr, 20) < 0, "overlay labels not sorted");
        for (size_t i = 1; i < t_.size(); i++) need(std::memcmp(t_[i - 1].addr, t_[i].addr, 20) < 0, "overlay tokens not sorted");
    }
    size_t labels() const noexcept { return l_.size(); }
    size_t tokens() const noexcept { return t_.size(); }
    const OvlLabel* label(const uint8_t* a20) const noexcept { return find(l_, a20); }
    const OvlToken* token(const uint8_t* a20) const noexcept { return find(t_, a20); }
    // Addresses carrying an entity name (case-insensitive exact match), at most `cap`.
    std::vector<Addr> entity_addrs(std::string_view name, size_t cap = 50) const {
        std::vector<Addr> out; uint64_t h = entity_hash(name);
        auto it = std::lower_bound(e_.begin(), e_.end(), h, [](const OvlEntity& e, uint64_t k) { return e.hash < k; });
        for (; it != e_.end() && it->hash == h && out.size() < cap; ++it) {
            const OvlLabel* l = label(it->addr);   // guard against hash collisions: the label's entity must match
            if (!l) continue;
            std::string_view en = str(l->entity_off, l->entity_len);
            if (en.size() != name.size()) continue;
            bool same = true; for (size_t i = 0; i < en.size() && same; i++) same = std::tolower((unsigned char)en[i]) == std::tolower((unsigned char)name[i]);
            if (same) { Addr a; std::memcpy(a.b, it->addr, 20); out.push_back(a); }
        }
        return out;
    }
    size_t entities() const noexcept { return e_.size(); }
    std::string_view str(uint32_t off, uint32_t len) const noexcept { return off + uint64_t(len) <= s_.size() ? s_.substr(off, len) : std::string_view(); }
};

} // namespace machine::engine
