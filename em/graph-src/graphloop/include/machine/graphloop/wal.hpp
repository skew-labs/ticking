#pragma once
// Append-only commit journal.  Files <state>/wal/wal-<startseq:020>.log, 64-byte header
// ("EMGLWAL1", u64 start_seq, graph id hex[32]...), then frames [u32 len][u32 crc32c][payload].
// payload = u64 seq | u64 ts_ms | u8 kind | body.  A group-commit thread writes queued frames with
// one write() and one fdatasync() per batch (<= batch_ms / batch_records) and only then runs the
// completions (HTTP acknowledgements).  Recovery replays every valid frame and truncates a torn
// tail (partial frame or CRC mismatch) of the newest file, like a database redo log.
// Snapshots: <state>/snap/snap-<seq:020>.bin (+ .json manifest with sha256, sealed); after a
// sealed snapshot at S every WAL file whose records are all <= S is removed.
#include "durable.hpp"
#include <nlohmann/json.hpp>
#include <condition_variable>
#include <optional>
#include <functional>
#include <mutex>
#include <thread>

namespace machine::graphloop {

struct Enc {
    std::string b;
    Enc& u8(uint8_t x) { b.push_back(char(x)); return *this; }
    Enc& u16(uint16_t x) { b.append(reinterpret_cast<const char*>(&x), 2); return *this; }
    Enc& u32(uint32_t x) { b.append(reinterpret_cast<const char*>(&x), 4); return *this; }
    Enc& u64(uint64_t x) { b.append(reinterpret_cast<const char*>(&x), 8); return *this; }
    Enc& f32(float x) { b.append(reinterpret_cast<const char*>(&x), 4); return *this; }
    Enc& raw(const void* p, size_t n) { b.append(static_cast<const char*>(p), n); return *this; }
    Enc& str(std::string_view s) { need(s.size() <= 0xffff, "wal string bound"); u16(uint16_t(s.size())); b.append(s); return *this; }
};
struct Dec {
    const uint8_t* p; size_t n, i = 0;
    Dec(const void* d, size_t len) : p(static_cast<const uint8_t*>(d)), n(len) {}
    void take(void* o, size_t k) { need(i + k <= n, "wal record truncated"); std::memcpy(o, p + i, k); i += k; }
    uint8_t u8() { uint8_t x; take(&x, 1); return x; }
    uint16_t u16() { uint16_t x; take(&x, 2); return x; }
    uint32_t u32() { uint32_t x; take(&x, 4); return x; }
    uint64_t u64() { uint64_t x; take(&x, 8); return x; }
    float f32() { float x; take(&x, 4); return x; }
    std::string str() { uint16_t k = u16(); need(i + k <= n, "wal string truncated"); std::string s(reinterpret_cast<const char*>(p + i), k); i += k; return s; }
    bool done() const { return i == n; }
};

class Wal {
public:
    struct Options { uint32_t batch_us = 2000; uint32_t batch_records = 4096; bool fsync = true; };
    struct Stats { uint64_t next_seq, durable_seq, fsyncs, frames, bytes, max_batch; double avg_batch; };
private:
    fs::path dir_; std::string graph_id_; Options opt_;
    int fd_ = -1; fs::path cur_; uint64_t cur_start_ = 0, next_seq_ = 1; std::atomic<uint64_t> durable_{0};
    struct Pending { std::string frame; uint64_t seq; std::function<void()> done; };
    mutable std::mutex mu_; std::condition_variable cv_, drained_; std::vector<Pending> q_; bool stop_ = false; std::thread th_;
    std::atomic<uint64_t> fsyncs_{0}, frames_{0}, bytes_{0}, max_batch_{0};
    static std::string name_for(uint64_t start) { char b[64]; std::snprintf(b, sizeof b, "wal-%020llu.log", (unsigned long long)start); return b; }
    static uint64_t start_of(const fs::path& p) { auto s = p.filename().string(); return std::stoull(s.substr(4, 20)); }

    void open_new(uint64_t start) {
        cur_ = dir_ / name_for(start); cur_start_ = start;
        fd_ = ::open(cur_.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        need(fd_ >= 0, "wal create");
        char h[64] = {}; std::memcpy(h, "EMGLWAL1", 8); std::memcpy(h + 8, &start, 8);
        std::memcpy(h + 16, graph_id_.data(), std::min<size_t>(graph_id_.size(), 48));
        write_all(fd_, h, 64);
        need(::fdatasync(fd_) == 0, "wal header sync");
        sync_directory(dir_);
    }
    void loop() {
        std::vector<Pending> batch;
        for (;;) {
            {
                std::unique_lock<std::mutex> l(mu_);
                cv_.wait(l, [&] { return stop_ || !q_.empty(); });
                if (q_.empty() && stop_) return;
                // group window: let concurrent proposals join this fsync
                if (q_.size() < opt_.batch_records && !stop_) cv_.wait_for(l, std::chrono::microseconds(opt_.batch_us), [&] { return stop_ || q_.size() >= opt_.batch_records; });
                batch.swap(q_);
            }
            std::string buf; size_t total = 0; for (auto& p : batch) total += p.frame.size(); buf.reserve(total);
            for (auto& p : batch) buf += p.frame;
            write_all(fd_, buf.data(), buf.size());
            if (opt_.fsync) need(::fdatasync(fd_) == 0, "wal fdatasync");
            fsyncs_++; frames_ += batch.size(); bytes_ += buf.size();
            if (batch.size() > max_batch_) max_batch_ = batch.size();
            durable_.store(batch.back().seq, std::memory_order_release);
            for (auto& p : batch) if (p.done) p.done();
            batch.clear();
            { std::lock_guard<std::mutex> l(mu_); if (q_.empty()) drained_.notify_all(); }
        }
    }
public:
    // Replays all valid frames (seq > after) through f(seq, ts, kind, Dec&), repairs a torn tail,
    // then opens a fresh file for appends.  Returns the number of replayed records.
    template <class F> uint64_t open(const fs::path& dir, const std::string& graph_id, uint64_t after, const Options& o, F&& f, bool read_only = false) {
        dir_ = dir; graph_id_ = graph_id; opt_ = o;
        if (!read_only) fs::create_directories(dir_);
        uint64_t replayed = 0, last = after;
        std::vector<fs::path> files;
        if (fs::exists(dir_)) for (auto& e : fs::directory_iterator(dir_)) { auto s = e.path().filename().string(); if (s.rfind("wal-", 0) == 0 && s.ends_with(".log")) files.push_back(e.path()); }
        std::sort(files.begin(), files.end());
        for (size_t fi = 0; fi < files.size(); fi++) {
            auto bytes = load_file(files[fi], size_t(64) << 30);
            if (bytes.size() < 64 || std::memcmp(bytes.data(), "EMGLWAL1", 8) != 0) {
                need(fi + 1 == files.size(), "wal header corrupt in non-final file");
                if (!read_only) fs::remove(files[fi]);
                break;
            }
            size_t i = 64, good = 64;
            while (i + 8 <= bytes.size()) {
                uint32_t len, crc; std::memcpy(&len, bytes.data() + i, 4); std::memcpy(&crc, bytes.data() + i + 4, 4);
                if (len < 17 || len > (64u << 20) || i + 8 + len > bytes.size()) break;
                if (crc32c(bytes.data() + i + 8, len) != crc) break;
                Dec d(bytes.data() + i + 8, len);
                uint64_t seq = d.u64(), ts = d.u64(); uint8_t kind = d.u8();
                if (seq > last) { f(seq, ts, kind, d); replayed++; last = seq; }
                i += 8 + len; good = i;
            }
            if (good < bytes.size()) {
                need(fi + 1 == files.size(), "wal corruption before the last file");
                if (!read_only) {   // torn tail: keep the valid prefix
                    int fd = ::open(files[fi].c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW); need(fd >= 0, "wal repair open");
                    need(::ftruncate(fd, off_t(good)) == 0 && ::fdatasync(fd) == 0, "wal repair truncate"); ::close(fd);
                }
                log_event("wal_torn_tail_truncated", "\"file\":" + jstr(files[fi].filename().string()) + ",\"kept_bytes\":" + std::to_string(good) + ",\"dropped_bytes\":" + std::to_string(bytes.size() - good));
            }
        }
        next_seq_ = last + 1; durable_ = last;
        if (!read_only) {
            // A file named for the next seq can only be an empty leftover (rotation at shutdown or a
            // torn header): records in it would have advanced next_seq_.  Replace it.
            auto stale = dir_ / name_for(next_seq_);
            if (fs::exists(stale)) { need(fs::file_size(stale) <= 64, "wal next-seq file not empty"); fs::remove(stale); }
            open_new(next_seq_); th_ = std::thread([this] { loop(); });
        }
        return replayed;
    }
    ~Wal() { close(); }
    void close() {
        { std::lock_guard<std::mutex> l(mu_); stop_ = true; }
        cv_.notify_all();
        if (th_.joinable()) th_.join();
        if (fd_ >= 0) { ::fdatasync(fd_); ::close(fd_); fd_ = -1; }
    }
    // Caller holds its own ordering lock when sequence order must match apply order.
    uint64_t append(uint8_t kind, std::string_view body, std::function<void()> done) {
        std::lock_guard<std::mutex> l(mu_);
        need(!stop_ && fd_ >= 0, "wal closed");
        uint64_t seq = next_seq_++;
        Enc e; e.u64(seq).u64(now_ms()).u8(kind).raw(body.data(), body.size());
        uint32_t len = uint32_t(e.b.size()), crc = crc32c(reinterpret_cast<const uint8_t*>(e.b.data()), e.b.size());
        std::string frame; frame.reserve(8 + len);
        frame.append(reinterpret_cast<const char*>(&len), 4).append(reinterpret_cast<const char*>(&crc), 4).append(e.b);
        q_.push_back({std::move(frame), seq, std::move(done)});
        if (q_.size() == 1 || q_.size() >= opt_.batch_records) cv_.notify_one();
        return seq;
    }
    uint64_t last_seq() { std::lock_guard<std::mutex> l(mu_); return next_seq_ - 1; }
    uint64_t durable_seq() const { return durable_.load(std::memory_order_acquire); }
    void drain() { std::unique_lock<std::mutex> l(mu_); drained_.wait(l, [&] { return q_.empty() && durable_.load() + 1 >= next_seq_; }); }
    // Start a new file; returns the paths that only contain records < new start (deletable once a
    // snapshot covering them is sealed).
    std::vector<fs::path> rotate() {
        drain();
        std::lock_guard<std::mutex> l(mu_);
        if (next_seq_ != cur_start_) {   // current file holds records: seal it and start a new one
            ::fdatasync(fd_); ::close(fd_); fd_ = -1;
            open_new(next_seq_);
        }
        std::vector<fs::path> out;
        for (auto& e : fs::directory_iterator(dir_)) { auto s = e.path().filename().string(); if (s.rfind("wal-", 0) == 0 && s.ends_with(".log") && e.path() != cur_) out.push_back(e.path()); }
        std::sort(out.begin(), out.end());
        return out;
    }
    Stats stats() const {
        uint64_t f = fsyncs_.load(), fr = frames_.load(); uint64_t ns; { std::lock_guard<std::mutex> l(mu_); ns = next_seq_; }
        return {ns, durable_.load(), f, fr, bytes_.load(), max_batch_.load(), f ? double(fr) / double(f) : 0.0};
    }
};

// ------------------------------------------------------------------ snapshots
struct SnapshotInfo { uint64_t seq = 0; fs::path file; };
inline std::optional<SnapshotInfo> latest_snapshot(const fs::path& dir) {
    if (!fs::exists(dir)) return std::nullopt;
    std::vector<fs::path> mans;
    for (auto& e : fs::directory_iterator(dir)) { auto s = e.path().filename().string(); if (s.rfind("snap-", 0) == 0 && s.ends_with(".json")) mans.push_back(e.path()); }
    std::sort(mans.rbegin(), mans.rend());
    for (auto& m : mans) {
        try {
            auto j = nlohmann::json::parse(load_text(m));
            fs::path bin = dir / j.at("file").get<std::string>();
            auto bytes = load_file(bin, size_t(64) << 30);
            if (bytes.size() != j.at("bytes").get<uint64_t>()) continue;
            auto h = sha256(View(bytes.data(), bytes.size()));
            if (hex(View(h.data(), 32)) != j.at("sha256").get<std::string>()) continue;
            return SnapshotInfo{j.at("seq").get<uint64_t>(), bin};
        } catch (...) { continue; }
    }
    return std::nullopt;
}
// Snapshot body = sequence of [u32 len][u32 crc][kind u8 | record] using the WAL record bodies.
inline void write_snapshot(const fs::path& dir, uint64_t seq, const std::vector<std::pair<uint8_t, std::string>>& recs, const std::string& graph_id, const nlohmann::json& extra) {
    fs::create_directories(dir);
    char nb[64]; std::snprintf(nb, sizeof nb, "snap-%020llu", (unsigned long long)seq);
    fs::path bin = dir / (std::string(nb) + ".bin");
    std::string body = "EMGLSNP1"; body.append(reinterpret_cast<const char*>(&seq), 8);
    for (auto& [kind, r] : recs) {
        std::string p; p.push_back(char(kind)); p += r;
        uint32_t len = uint32_t(p.size()), crc = crc32c(reinterpret_cast<const uint8_t*>(p.data()), p.size());
        body.append(reinterpret_cast<const char*>(&len), 4).append(reinterpret_cast<const char*>(&crc), 4).append(p);
    }
    if (fs::exists(bin)) fs::remove(bin);   // an unsealed leftover from a crash
    seal_text(bin, body);
    auto h = sha256(body);
    nlohmann::json man = {{"schema", "economic-machine.graphloop.snapshot.v1"}, {"seq", seq}, {"file", bin.filename().string()}, {"bytes", body.size()},
                          {"sha256", hex(View(h.data(), 32))}, {"records", recs.size()}, {"graph_id", graph_id}, {"created_at_ms", now_ms()}, {"extra", extra}};
    auto mp = dir / (std::string(nb) + ".json");
    if (fs::exists(mp)) fs::remove(mp);
    seal_text(mp, man.dump(1));
}
template <class F> uint64_t read_snapshot(const fs::path& bin, F&& f) {
    auto bytes = load_file(bin, size_t(64) << 30);
    need(bytes.size() >= 16 && std::memcmp(bytes.data(), "EMGLSNP1", 8) == 0, "snapshot magic");
    size_t i = 16; uint64_t n = 0;
    while (i + 8 <= bytes.size()) {
        uint32_t len, crc; std::memcpy(&len, bytes.data() + i, 4); std::memcpy(&crc, bytes.data() + i + 4, 4);
        need(i + 8 + len <= bytes.size() && crc32c(bytes.data() + i + 8, len) == crc && len >= 1, "snapshot record corrupt");
        Dec d(bytes.data() + i + 9, len - 1);
        f(bytes[i + 8], d); n++; i += 8 + len;
    }
    need(i == bytes.size(), "snapshot trailing bytes");
    return n;
}
} // namespace machine::graphloop
