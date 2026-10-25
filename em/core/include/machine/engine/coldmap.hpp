#pragma once
// Cold tier for segments too big for skew's disk (design/EMV2-NO-NVME.md §3): a segment file F in a build directory is "cold"
// when F.cold exists. F is then a sparse local cache of the Blob object (same size, holes where nothing was fetched) and F.cold
// holds one byte per 64 KiB block (1 = present in F). emv2_coldfetch.py (root, has the network and the SAS) fills blocks on
// request; emv2-serve (sandboxed, no network) maps F as anonymous memory registered with userfaultfd. The first touch of a page
// faults to a handler thread that copies the whole 64 KiB block from F (pread), asking the fetcher first when F.cold says the
// block is missing. Readers keep using plain pointers and never see a hole as zeros. Resident cold pages are capped
// (MADV_DONTNEED of the oldest blocks; a later touch faults again and is served from the local cache).
#include <linux/userfaultfd.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <fcntl.h>
#include <unistd.h>
#include <atomic>
#include <chrono>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <deque>
#include <filesystem>
#include <map>
#include <mutex>
#include <set>
#include <shared_mutex>
#include <string>
#include <thread>
#include <vector>

namespace machine::engine::cold {

inline constexpr size_t block_bytes = size_t(64) << 10;

class Mapper {
    struct Region {
        uint8_t* base; size_t bytes, mapped; int fd; std::string path;
        const uint8_t* present; size_t present_len; int present_fd;
    };
    int uffd_ = -1, fifo_ = -1;
    size_t cap_ = size_t(1) << 30, resident_ = 0;
    std::shared_mutex mu_;                                  // regions
    std::map<uintptr_t, Region> regions_;                   // by base address
    std::mutex lru_mu_; std::deque<std::pair<uintptr_t, size_t>> lru_;   // (block address, region base) in copy order
    std::atomic<uint64_t> faults_{0}, misses_{0}, fetch_wait_ms_{0};
    std::vector<std::thread> threads_;

    Region* find(uintptr_t a) {
        auto it = regions_.upper_bound(a);
        if (it == regions_.begin()) return nullptr;
        --it;
        return a < it->first + it->second.mapped ? &it->second : nullptr;
    }
    void evict_if_needed() {
        std::lock_guard g(lru_mu_);
        while (resident_ > cap_ && !lru_.empty()) {
            auto [addr, base] = lru_.front(); lru_.pop_front();
            std::shared_lock rl(mu_);
            if (!regions_.count(base)) continue;            // region unmapped meanwhile
            ::madvise(reinterpret_cast<void*>(addr), block_bytes, MADV_DONTNEED);
            resident_ -= block_bytes;
        }
    }
    // Wait until block idx of r is present locally, asking the fetcher once per second while it is missing.
    void ensure(const Region& r, size_t idx) {
        if (r.present[idx]) return;
        misses_++;
        auto t0 = std::chrono::steady_clock::now();
        for (int i = 0;; i++) {
            if (i % 500 == 0 && fifo_ >= 0) {
                std::string line = r.path + "\t" + std::to_string(idx) + "\n";
                ssize_t w = ::write(fifo_, line.data(), line.size()); (void)w;   // a full FIFO drops the line; the next round asks again
            }
            if (__atomic_load_n(&r.present[idx], __ATOMIC_ACQUIRE)) break;
            struct timespec ts{0, 2'000'000}; ::nanosleep(&ts, nullptr);
        }
        fetch_wait_ms_ += uint64_t(std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0).count());
    }
    void serve_fault(uintptr_t addr) {
        faults_++;
        std::vector<uint8_t> buf(block_bytes);
        uintptr_t blk = 0, base = 0;
        {
            std::shared_lock rl(mu_);
            Region* r = find(addr);
            if (!r) return;                                 // not ours (cannot happen: only our ranges are registered)
            size_t off = (addr - uintptr_t(r->base)) / block_bytes * block_bytes, idx = off / block_bytes;
            blk = uintptr_t(r->base) + off; base = uintptr_t(r->base);
            for (int attempt = 0; attempt < 3; attempt++) {
                ensure(*r, idx);
                size_t n = std::min(block_bytes, r->bytes > off ? r->bytes - off : 0), got = 0;
                std::memset(buf.data(), 0, buf.size());
                while (got < n) {
                    ssize_t k = ::pread(r->fd, buf.data() + got, n - got, off_t(off + got));
                    if (k < 0 && errno == EINTR) continue;
                    if (k <= 0) break;
                    got += size_t(k);
                }
                // the fetcher may evict a block (punch hole, clear flag) while it is read: re-check after the read
                if (got == n && __atomic_load_n(&r->present[idx], __ATOMIC_ACQUIRE)) break;
            }
            uffdio_copy c{}; c.dst = blk; c.src = uintptr_t(buf.data()); c.len = block_bytes; c.mode = 0;
            if (::ioctl(uffd_, UFFDIO_COPY, &c) != 0 && errno == EEXIST) {   // another thread copied it first: wake our faulting thread
                uffdio_range w{blk, block_bytes}; ::ioctl(uffd_, UFFDIO_WAKE, &w);
                return;
            }
        }
        { std::lock_guard g(lru_mu_); lru_.push_back({blk, base}); resident_ += block_bytes; }
        evict_if_needed();
    }
    void loop() {
        for (;;) {
            uffd_msg m{};
            ssize_t n = ::read(uffd_, &m, sizeof m);
            if (n < 0 && errno == EINTR) continue;
            if (n != sizeof m) { struct timespec ts{0, 1'000'000}; ::nanosleep(&ts, nullptr); continue; }
            if (m.event == UFFD_EVENT_PAGEFAULT) serve_fault(uintptr_t(m.arg.pagefault.address));
        }
    }
public:
    static Mapper& get() { static Mapper m; return m; }
    bool ready() const noexcept { return uffd_ >= 0; }
    // Before the sandbox: create the userfaultfd (user-mode faults only: no privilege needed), open the fetcher FIFO
    // (O_RDWR so the open never blocks and a restarted fetcher keeps the same pipe) and start the handler threads.
    bool init(const std::string& fifo_path, size_t resident_cap, unsigned threads = 4) {
        if (uffd_ >= 0) return true;
        int fd = int(::syscall(SYS_userfaultfd, O_CLOEXEC | UFFD_USER_MODE_ONLY));
        if (fd < 0) return false;
        uffdio_api api{}; api.api = UFFD_API; api.features = 0;
        if (::ioctl(fd, UFFDIO_API, &api) != 0) { ::close(fd); return false; }
        uffd_ = fd; cap_ = resident_cap;
        fifo_ = ::open(fifo_path.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);   // missing FIFO: faults wait for blocks a fetcher may still fill
        for (unsigned i = 0; i < threads; i++) threads_.emplace_back([this] { loop(); });
        for (auto& t : threads_) t.detach();
        return true;
    }
    // Map a cold file: anonymous range of the file's size (rounded to blocks) registered for missing-page faults.
    void* map(const std::filesystem::path& file, size_t bytes) {
        if (uffd_ < 0) throw std::runtime_error("cold tier not initialised: " + file.filename().string());
        int fd = ::open(file.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (fd < 0) throw std::runtime_error("cold file open: " + file.filename().string());
        std::string pres = file.string() + ".cold";
        int pfd = ::open(pres.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        struct stat st{};
        size_t blocks = (bytes + block_bytes - 1) / block_bytes;
        if (pfd < 0 || ::fstat(pfd, &st) != 0 || size_t(st.st_size) != blocks) { if (pfd >= 0) ::close(pfd); ::close(fd); throw std::runtime_error("cold block map: " + file.filename().string()); }
        void* p = ::mmap(nullptr, blocks, PROT_READ, MAP_SHARED, pfd, 0);
        if (p == MAP_FAILED) { ::close(pfd); ::close(fd); throw std::runtime_error("cold block map mmap: " + file.filename().string()); }
        size_t mapped = blocks * block_bytes;
        void* base = ::mmap(nullptr, mapped, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
        if (base == MAP_FAILED) { ::munmap(p, blocks); ::close(pfd); ::close(fd); throw std::runtime_error("cold range mmap: " + file.filename().string()); }
        uffdio_register reg{}; reg.range.start = uintptr_t(base); reg.range.len = mapped; reg.mode = UFFDIO_REGISTER_MODE_MISSING;
        if (::ioctl(uffd_, UFFDIO_REGISTER, &reg) != 0) { ::munmap(base, mapped); ::munmap(p, blocks); ::close(pfd); ::close(fd); throw std::runtime_error("cold uffd register: " + file.filename().string()); }
        std::unique_lock wl(mu_);
        regions_[uintptr_t(base)] = Region{static_cast<uint8_t*>(base), bytes, mapped, fd, file.string(), static_cast<const uint8_t*>(p), blocks, pfd};
        return base;
    }
    void unmap(void* base) {
        std::unique_lock wl(mu_);
        auto it = regions_.find(uintptr_t(base));
        if (it == regions_.end()) return;
        Region r = it->second; regions_.erase(it);
        uffdio_range rg{uintptr_t(r.base), r.mapped}; ::ioctl(uffd_, UFFDIO_UNREGISTER, &rg);
        ::munmap(r.base, r.mapped); ::munmap(const_cast<uint8_t*>(r.present), r.present_len); ::close(r.present_fd); ::close(r.fd);
    }
    // Ask the fetcher at once for every missing block of these ranges, then wait (up to timeout_ms) until they are local. A query
    // that knows what it will read (detail slices of its peers) pays about one round trip instead of one per block it touches.
    // Ranges outside cold regions are ignored. Returns false on timeout (the reads then fault and wait one by one as usual).
    bool prefetch(const std::vector<std::pair<const void*, size_t>>& ranges, int timeout_ms = 3000) {
        if (uffd_ < 0 || ranges.empty()) return true;
        std::vector<std::pair<const Region*, size_t>> want;   // (region, block)
        std::string batch;
        std::shared_lock rl(mu_);
        if (regions_.empty()) return true;
        std::set<std::pair<const Region*, size_t>> asked;
        for (auto& [p, n] : ranges) {
            if (!n) continue;
            Region* r = find(uintptr_t(p));
            if (!r) continue;
            size_t off = uintptr_t(p) - uintptr_t(r->base);
            size_t b0 = off / block_bytes, b1 = std::min(r->present_len, (off + n + block_bytes - 1) / block_bytes);
            for (size_t b = b0; b < b1; b++) {
                if (__atomic_load_n(&r->present[b], __ATOMIC_ACQUIRE)) continue;
                want.push_back({r, b});
                if (!asked.insert({r, b / 4}).second) continue;   // the fetcher reads 4-block groups: one line per group
                batch += r->path + "\t" + std::to_string(b) + "\n";
                if (batch.size() > 3500 && fifo_ >= 0) { ssize_t w = ::write(fifo_, batch.data(), batch.size()); (void)w; batch.clear(); }
            }
        }
        if (want.empty()) return true;
        if (!batch.empty() && fifo_ >= 0) { ssize_t w = ::write(fifo_, batch.data(), batch.size()); (void)w; }
        misses_ += want.size();
        auto t0 = std::chrono::steady_clock::now();
        for (int i = 0;; i++) {
            bool all = true;
            for (auto& [r, b] : want) if (!__atomic_load_n(&r->present[b], __ATOMIC_ACQUIRE)) { all = false; break; }
            if (all) break;
            if (std::chrono::steady_clock::now() - t0 > std::chrono::milliseconds(timeout_ms)) return false;
            struct timespec ts{0, 2'000'000}; ::nanosleep(&ts, nullptr);
        }
        fetch_wait_ms_ += uint64_t(std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0).count());
        return true;
    }
    struct Stats { uint64_t faults, misses, fetch_wait_ms; size_t resident, regions; };
    Stats stats() {
        std::shared_lock rl(mu_); std::lock_guard g(lru_mu_);
        return {faults_.load(), misses_.load(), fetch_wait_ms_.load(), resident_, regions_.size()};
    }
};

} // namespace machine::engine::cold
