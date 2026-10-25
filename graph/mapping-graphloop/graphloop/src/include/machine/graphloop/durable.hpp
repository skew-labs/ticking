#pragma once
// Durability primitives: sealed files (tmp + fdatasync + link/rename + directory fsync),
// single-writer flock, immutable mmap'd segments with a fixed 64-byte header.
// Same pattern as ticking::evm recovery.hpp seal_file()/sync_directory().
#include "core.hpp"
#include <fcntl.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>
#include <cerrno>
#include <filesystem>

namespace machine::graphloop {
namespace fs = std::filesystem;

inline void sync_directory(const fs::path& p) {
    int fd = ::open(p.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    need(fd >= 0, "directory open");
    int r = ::fsync(fd); ::close(fd);
    need(r == 0, "directory sync");
}

inline void write_all(int fd, const void* p, size_t n) {
    auto b = static_cast<const uint8_t*>(p);
    while (n) {
        auto r = ::write(fd, b, std::min<size_t>(n, size_t(1) << 26));
        if (r < 0 && errno == EINTR) continue;
        need(r > 0, "file write");
        b += r; n -= size_t(r);
    }
}

inline Bytes load_file(const fs::path& p, size_t cap = size_t(64) << 20) {
    int fd = ::open(p.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    need(fd >= 0, "file open");
    struct stat st{};
    if (::fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < 0 || uint64_t(st.st_size) > cap) { ::close(fd); throw std::runtime_error("file bound/type"); }
    Bytes out(size_t(st.st_size)); size_t n = 0;
    while (n < out.size()) {
        auto r = ::read(fd, out.data() + n, out.size() - n);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) { ::close(fd); throw std::runtime_error("file read"); }
        n += size_t(r);
    }
    ::close(fd);
    return out;
}
inline std::string load_text(const fs::path& p, size_t cap = size_t(64) << 20) { auto b = load_file(p, cap); return {b.begin(), b.end()}; }

// Atomically publish bytes at p. An existing identical file is accepted; a different one is refused.
inline bool seal_file(const fs::path& p, View bytes) {
    if (fs::exists(p)) { need(load_file(p, bytes.size() + 1) == Bytes(bytes.begin(), bytes.end()), "sealed object conflict"); return false; }
    std::string tmp = p.string() + ".tmp-XXXXXX";
    int fd = ::mkostemp(tmp.data(), O_CLOEXEC);
    need(fd >= 0, "sealed temp open");
    try { write_all(fd, bytes.data(), bytes.size()); } catch (...) { ::close(fd); ::unlink(tmp.c_str()); throw; }
    int rc = ::fdatasync(fd); ::close(fd);
    if (rc != 0) { ::unlink(tmp.c_str()); throw std::runtime_error("sealed sync"); }
    if (::link(tmp.c_str(), p.c_str()) != 0) {
        bool same = errno == EEXIST && load_file(p, bytes.size() + 1) == Bytes(bytes.begin(), bytes.end());
        ::unlink(tmp.c_str());
        need(same, "sealed rename conflict");
    } else need(::unlink(tmp.c_str()) == 0, "sealed temp unlink");
    sync_directory(p.parent_path());
    return true;
}
inline bool seal_text(const fs::path& p, std::string_view s) { return seal_file(p, View(reinterpret_cast<const uint8_t*>(s.data()), s.size())); }

// Replace p atomically (rename) — used for "latest" pointers whose old content is superseded.
inline void replace_file(const fs::path& p, std::string_view s) {
    std::string tmp = p.string() + ".tmp-XXXXXX";
    int fd = ::mkostemp(tmp.data(), O_CLOEXEC);
    need(fd >= 0, "replace temp open");
    try { write_all(fd, s.data(), s.size()); need(::fdatasync(fd) == 0, "replace sync"); } catch (...) { ::close(fd); ::unlink(tmp.c_str()); throw; }
    ::close(fd);
    if (::rename(tmp.c_str(), p.c_str()) != 0) { ::unlink(tmp.c_str()); throw std::runtime_error("replace rename"); }
    sync_directory(p.parent_path());
}

inline uint64_t free_bytes(const fs::path& p) { struct statvfs s{}; need(::statvfs(p.c_str(), &s) == 0, "statvfs"); return uint64_t(s.f_bavail) * s.f_frsize; }

// Exclusive single-writer lock (same as Recovery: writer.lock, LOCK_EX|LOCK_NB).
class WriterLock {
    int fd_{-1};
public:
    explicit WriterLock(const fs::path& p) {
        fd_ = ::open(p.c_str(), O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
        need(fd_ >= 0 && ::flock(fd_, LOCK_EX | LOCK_NB) == 0, "state writer locked");
    }
    ~WriterLock() { if (fd_ >= 0) ::close(fd_); }
    WriterLock(const WriterLock&) = delete;
};

// ---------------------------------------------------------------- segments
// File = 64-byte header + count * elem bytes.  Header: "EMGLSEG1", u32 version, u32 elem,
// u64 count, u64 aux, 32 bytes reserved (zero).  All little-endian.
struct SegHeader {
    char magic[8]; uint32_t version; uint32_t elem; uint64_t count; uint64_t aux; uint8_t reserved[32];
};
static_assert(sizeof(SegHeader) == 64);
inline constexpr char seg_magic[8] = {'E', 'M', 'G', 'L', 'S', 'E', 'G', '1'};

// Streaming writer: tmp file in the target directory, SHA-256 over the whole file, fdatasync, rename.
class SegmentWriter {
    fs::path final_; std::string tmp_; int fd_{-1}; CryptoPP::SHA256 sha_; uint64_t bytes_{}, count_{}; uint32_t elem_{};
    std::vector<uint8_t> buf_;
    void flush() { if (!buf_.empty()) { write_all(fd_, buf_.data(), buf_.size()); buf_.clear(); } }
public:
    SegmentWriter(const fs::path& p, uint32_t elem, uint64_t count, uint64_t aux = 0) : final_(p), elem_(elem) {
        tmp_ = p.string() + ".tmp-XXXXXX";
        fd_ = ::mkostemp(tmp_.data(), O_CLOEXEC);
        need(fd_ >= 0, "segment temp open");
        SegHeader h{}; std::memcpy(h.magic, seg_magic, 8); h.version = 1; h.elem = elem; h.count = count; h.aux = aux;
        buf_.reserve(size_t(1) << 22);
        put(&h, sizeof h);
        count_ = count;
    }
    ~SegmentWriter() { if (fd_ >= 0) { ::close(fd_); ::unlink(tmp_.c_str()); } }
    SegmentWriter(const SegmentWriter&) = delete;
    void put(const void* p, size_t n) {
        sha_.Update(static_cast<const uint8_t*>(p), n); bytes_ += n;
        if (n >= (size_t(1) << 20)) { flush(); write_all(fd_, p, n); return; }
        auto b = static_cast<const uint8_t*>(p); buf_.insert(buf_.end(), b, b + n);
        if (buf_.size() >= (size_t(1) << 22)) flush();
    }
    template <class T> void put_span(std::span<const T> s) { put(s.data(), s.size_bytes()); }
    // Returns (bytes, sha256 hex). The data length must match the declared count.
    std::pair<uint64_t, std::string> finish() {
        need(bytes_ == 64 + count_ * elem_, "segment length mismatch");
        flush();
        need(::fdatasync(fd_) == 0, "segment sync");
        ::close(fd_); fd_ = -1;
        need(::rename(tmp_.c_str(), final_.c_str()) == 0, "segment rename");
        Hash d; sha_.Final(d.data());
        return {bytes_, hex(View(d.data(), d.size()))};
    }
};

// Read-only mapping of a segment.  populate=true pre-faults (MAP_POPULATE) for predictable latency.
class Segment {
    void* base_{MAP_FAILED}; size_t bytes_{}; const SegHeader* h_{};
public:
    Segment() = default;
    // ram=true: copy into anonymous memory (MADV_HUGEPAGE) - immune to page-cache eviction, fewer TLB misses.
    Segment(const fs::path& p, uint32_t elem, bool populate, int advice, bool ram = false) {
        int fd = ::open(p.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        need(fd >= 0, "segment open: " + p.filename().string());
        struct stat st{};
        if (::fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < 64) { ::close(fd); throw std::runtime_error("segment type: " + p.filename().string()); }
        bytes_ = size_t(st.st_size);
        if (ram) {
            base_ = ::mmap(nullptr, bytes_, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (base_ == MAP_FAILED) { ::close(fd); throw std::runtime_error("segment ram alloc: " + p.filename().string()); }
            if (bytes_ >= (size_t(2) << 20)) ::madvise(base_, bytes_, MADV_HUGEPAGE);
            size_t off = 0;
            while (off < bytes_) { auto r = ::pread(fd, static_cast<uint8_t*>(base_) + off, std::min<size_t>(bytes_ - off, size_t(1) << 26), off_t(off));
                if (r < 0 && errno == EINTR) continue; if (r <= 0) { ::close(fd); throw std::runtime_error("segment ram read: " + p.filename().string()); } off += size_t(r); }
            ::close(fd); ::mprotect(base_, bytes_, PROT_READ); advice = 0;
        } else {
            base_ = ::mmap(nullptr, bytes_, PROT_READ, MAP_SHARED | (populate ? MAP_POPULATE : 0), fd, 0);
            ::close(fd);
        }
        need(base_ != MAP_FAILED, "segment mmap: " + p.filename().string());
        h_ = static_cast<const SegHeader*>(base_);
        need(std::memcmp(h_->magic, seg_magic, 8) == 0 && h_->version == 1, "segment magic: " + p.filename().string());
        need(h_->elem == elem && bytes_ == 64 + h_->count * h_->elem, "segment shape: " + p.filename().string());
        if (advice) ::madvise(base_, bytes_, advice);
    }
    ~Segment() { if (base_ != MAP_FAILED) ::munmap(base_, bytes_); }
    Segment(Segment&& o) noexcept : base_(o.base_), bytes_(o.bytes_), h_(o.h_) { o.base_ = MAP_FAILED; o.h_ = nullptr; }
    Segment& operator=(Segment&& o) noexcept { if (this != &o) { if (base_ != MAP_FAILED) ::munmap(base_, bytes_); base_ = o.base_; bytes_ = o.bytes_; h_ = o.h_; o.base_ = MAP_FAILED; o.h_ = nullptr; } return *this; }
    template <class T> std::span<const T> as() const { return {reinterpret_cast<const T*>(static_cast<const uint8_t*>(base_) + 64), size_t(h_ ? h_->count : 0)}; }
    uint64_t aux() const { return h_ ? h_->aux : 0; }
    size_t bytes() const { return bytes_; }
    bool valid() const { return base_ != MAP_FAILED; }
};

// Anonymous, lazily-backed, zero-initialised array (untouched pages cost no RAM).  Used for the
// mutable label overlay so a 100M-node graph only pays for pages that actually carry overrides.
template <class T> class LazyArray {
    T* p_{nullptr}; size_t n_{}, bytes_{};
public:
    LazyArray() = default;
    explicit LazyArray(size_t n) : n_(n) {
        bytes_ = std::max<size_t>(sizeof(T), n * sizeof(T));
        void* m = ::mmap(nullptr, bytes_, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
        need(m != MAP_FAILED, "anonymous mmap");
        p_ = static_cast<T*>(m);
    }
    ~LazyArray() { if (p_) ::munmap(p_, bytes_); }
    LazyArray(const LazyArray&) = delete;
    LazyArray(LazyArray&& o) noexcept : p_(o.p_), n_(o.n_), bytes_(o.bytes_) { o.p_ = nullptr; }
    LazyArray& operator=(LazyArray&& o) noexcept { if (this != &o) { if (p_) ::munmap(p_, bytes_); p_ = o.p_; n_ = o.n_; bytes_ = o.bytes_; o.p_ = nullptr; } return *this; }
    T& operator[](size_t i) noexcept { return p_[i]; }
    const T& operator[](size_t i) const noexcept { return p_[i]; }
    T* data() noexcept { return p_; }
    size_t size() const noexcept { return n_; }
};

// Resident set size of this process (bytes), for reporting.
inline uint64_t rss_bytes() {
    FILE* f = std::fopen("/proc/self/statm", "r"); if (!f) return 0;
    unsigned long long size = 0, res = 0; int r = std::fscanf(f, "%llu %llu", &size, &res); std::fclose(f);
    return r == 2 ? res * uint64_t(::sysconf(_SC_PAGESIZE)) : 0;
}
inline uint64_t peak_rss_bytes() {
    FILE* f = std::fopen("/proc/self/status", "r"); if (!f) return 0;
    char line[256]; uint64_t kb = 0;
    while (std::fgets(line, sizeof line, f)) if (std::strncmp(line, "VmHWM:", 6) == 0) { kb = std::strtoull(line + 6, nullptr, 10); break; }
    std::fclose(f); return kb * 1024;
}
} // namespace machine::graphloop
