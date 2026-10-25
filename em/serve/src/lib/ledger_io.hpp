#pragma once
// ledger_io.hpp (private to the ledger): ledger segments are core seg v2 files (seg.hpp) named
// segment_file_name(kind, partition) in the build directory.  Byte blobs use elem 0 (count = bytes).
#include "machine/engine/dict.hpp"
#include "machine/engine/seg.hpp"

#include <filesystem>
#include <span>
#include <string>
#include <vector>

namespace machine::engine::ledger::fmt::io {

class Out {
    SegWriter w_; uint32_t elem_;
public:
    Out(const std::filesystem::path& dir, Kind k, uint32_t part, uint32_t elem)
        : w_(dir / segment_file_name(k, part), k, elem == 1 ? 0 : elem, elem == 1 ? Codec::mixed : Codec::raw), elem_(elem) {}
    void put(const void* p, size_t n) { w_.put(p, n); }
    template <class T> void put_one(const T& v) { w_.put(&v, sizeof v); }
    void put_bytes(const std::vector<uint8_t>& v) { if (!v.empty()) w_.put(v.data(), v.size()); }
    uint64_t size() const noexcept { return w_.payload_bytes(); }
    SegResult finish() { uint64_t b = w_.payload_bytes(); return w_.finish(elem_ == 1 ? b : b / elem_); }
};

class In {
    SegView v_;
public:
    In() = default;
    In(const std::filesystem::path& dir, Kind k, uint32_t part) : v_(dir / segment_file_name(k, part), k) {}
    const uint8_t* data() const noexcept { return v_.payload().data(); }
    uint64_t count() const noexcept { return v_.count(); }
    template <class T> std::span<const T> as() const { return v_.as<T>(); }
};

// Address dictionary opened by path (the build may not have a merged manifest.json yet).
class Dict {
    SegView sfx_, pfx_, tok_; unsigned k_{}; uint32_t width_{}; uint64_t n_{}; const uint8_t* s_{}; const uint32_t* p_{};
public:
    Dict() = default;
    explicit Dict(const std::filesystem::path& dir) {
        sfx_ = SegView(dir / segment_file_name(Kind::dict_addr_sfx), Kind::dict_addr_sfx);
        pfx_ = SegView(dir / segment_file_name(Kind::dict_addr_pfx), Kind::dict_addr_pfx);
        tok_ = SegView(dir / segment_file_name(Kind::dict_tok), Kind::dict_tok);
        k_ = unsigned(sfx_.aux()); width_ = 20 - k_; n_ = sfx_.count();
        s_ = sfx_.payload().data(); p_ = pfx_.as<uint32_t>().data();
    }
    uint64_t size() const noexcept { return n_; }
    std::optional<uint32_t> find(const uint8_t* a20) const noexcept {
        uint32_t b = 0; for (unsigned i = 0; i < k_; i++) b = b << 8 | a20[i];
        uint32_t lo = p_[b], hi = p_[b + 1]; const uint8_t* key = a20 + k_;
        while (lo < hi) { uint32_t mid = lo + (hi - lo) / 2; int c = std::memcmp(s_ + size_t(mid) * width_, key, width_); if (!c) return mid; if (c < 0) lo = mid + 1; else hi = mid; }
        return std::nullopt;
    }
    void get(uint32_t id, uint8_t out[20]) const noexcept {
        uint64_t nb = uint64_t(1) << (8 * k_);
        uint32_t b = uint32_t(std::upper_bound(p_, p_ + nb + 1, id) - p_ - 1);
        for (unsigned i = 0; i < k_; i++) out[i] = uint8_t(b >> (8 * (k_ - 1 - i)));
        std::memcpy(out + k_, s_ + size_t(id) * width_, width_);
    }
    std::span<const TokenRec> tokens() const { return tok_.as<TokenRec>(); }
};

}  // namespace machine::engine::ledger::fmt::io
