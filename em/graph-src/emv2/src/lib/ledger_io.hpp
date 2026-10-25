#pragma once
// ledger_io.hpp (private to the ledger): segment files written once and mapped read-only.
// File = payload + 64-byte trailer: "EMV2LSEG", u32 version, u32 elem, u64 count, u64 payload bytes, 32 zero bytes.
// TODO(core): switch to seg.hpp SegWriter/SegView once it lands; the call sites only use put/finish/payload.
#include <cryptopp/sha.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstdint>
#include <cstring>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

namespace machine::engine::ledger::io {

struct Trailer { char magic[8]; uint32_t version, elem; uint64_t count, bytes; uint8_t reserved[32]; };
static_assert(sizeof(Trailer) == 64);
inline constexpr char magic[8] = {'E', 'M', 'V', '2', 'L', 'S', 'E', 'G'};

inline void must(bool ok, const std::string& what) { if (!ok) throw std::runtime_error(what + ": " + std::strerror(errno)); }

class Out {
    std::string final_, tmp_; int fd_{-1}; CryptoPP::SHA256 sha_; uint64_t bytes_{}; uint32_t elem_; std::vector<uint8_t> buf_;
    void raw(const void* p, size_t n) {
        sha_.Update(static_cast<const uint8_t*>(p), n);
        auto b = static_cast<const uint8_t*>(p);
        while (n) { auto w = ::write(fd_, b, n); if (w < 0 && errno == EINTR) continue; must(w > 0, "write " + final_); b += w; n -= size_t(w); }
    }
    void flush() { if (!buf_.empty()) { raw(buf_.data(), buf_.size()); buf_.clear(); } }
public:
    Out(std::string path, uint32_t elem) : final_(std::move(path)), elem_(elem) {
        tmp_ = final_ + ".tmp-XXXXXX";
        fd_ = ::mkostemp(tmp_.data(), O_CLOEXEC);
        must(fd_ >= 0, "create " + final_);
        buf_.reserve(size_t(1) << 22);
    }
    ~Out() { if (fd_ >= 0) { ::close(fd_); ::unlink(tmp_.c_str()); } }
    Out(const Out&) = delete;
    void put(const void* p, size_t n) {
        bytes_ += n;
        if (n >= (size_t(1) << 20)) { flush(); raw(p, n); return; }
        auto b = static_cast<const uint8_t*>(p); buf_.insert(buf_.end(), b, b + n);
        if (buf_.size() >= (size_t(1) << 22)) flush();
    }
    template <class T> void put_one(const T& v) { put(&v, sizeof v); }
    void put_bytes(const std::vector<uint8_t>& v) { put(v.data(), v.size()); }
    uint64_t size() const noexcept { return bytes_; }
    struct Done { uint64_t bytes; std::string sha256; };
    Done finish() {
        if (bytes_ % elem_) throw std::runtime_error("segment size not a multiple of elem: " + final_);
        Trailer t{}; std::memcpy(t.magic, magic, 8); t.version = 1; t.elem = elem_; t.count = bytes_ / elem_; t.bytes = bytes_;
        flush(); raw(&t, sizeof t);
        must(::fdatasync(fd_) == 0, "sync " + final_);
        ::close(fd_); fd_ = -1;
        must(::rename(tmp_.c_str(), final_.c_str()) == 0, "rename " + final_);
        uint8_t d[32]; sha_.Final(d);
        static const char* hx = "0123456789abcdef"; std::string h;
        for (uint8_t c : d) { h.push_back(hx[c >> 4]); h.push_back(hx[c & 15]); }
        return {bytes_ + sizeof t, h};
    }
};

class In {
    void* base_{MAP_FAILED}; size_t bytes_{}; uint64_t payload_{}, count_{};
public:
    In() = default;
    In(const std::string& path, uint32_t elem) {
        int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
        must(fd >= 0, "open " + path);
        struct stat st{};
        must(::fstat(fd, &st) == 0, "stat " + path);
        bytes_ = size_t(st.st_size);
        if (bytes_ < sizeof(Trailer)) { ::close(fd); throw std::runtime_error("short segment " + path); }
        base_ = ::mmap(nullptr, bytes_, PROT_READ, MAP_SHARED, fd, 0);
        ::close(fd);
        must(base_ != MAP_FAILED, "mmap " + path);
        Trailer t; std::memcpy(&t, static_cast<const uint8_t*>(base_) + bytes_ - sizeof t, sizeof t);
        if (std::memcmp(t.magic, magic, 8) || t.version != 1 || t.elem != elem || t.bytes + sizeof t != bytes_ || t.count * elem != t.bytes)
            throw std::runtime_error("bad segment " + path);
        payload_ = t.bytes; count_ = t.count;
    }
    ~In() { if (base_ != MAP_FAILED) ::munmap(base_, bytes_); }
    In(In&& o) noexcept : base_(o.base_), bytes_(o.bytes_), payload_(o.payload_), count_(o.count_) { o.base_ = MAP_FAILED; }
    In& operator=(In&& o) noexcept {
        if (this != &o) { if (base_ != MAP_FAILED) ::munmap(base_, bytes_); base_ = o.base_; bytes_ = o.bytes_; payload_ = o.payload_; count_ = o.count_; o.base_ = MAP_FAILED; }
        return *this;
    }
    const uint8_t* data() const noexcept { return static_cast<const uint8_t*>(base_); }
    uint64_t count() const noexcept { return count_; }
    template <class T> std::span<const T> as() const noexcept { return {reinterpret_cast<const T*>(base_), size_t(count_)}; }
};

}  // namespace machine::engine::ledger::io
