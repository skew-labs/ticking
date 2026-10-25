#pragma once
#include "durable.hpp"
#include <exception>
#include <mutex>
#include <pthread.h>
#include <sched.h>
#include <thread>
#include <unordered_map>

namespace machine::graphloop {

// Pin the calling thread to one CPU of the given list (round-robin by index).  Empty list = no-op.
inline void pin_thread(const std::vector<int>& cpus, size_t index) {
    if (cpus.empty()) return;
    cpu_set_t set; CPU_ZERO(&set); CPU_SET(cpus[index % cpus.size()], &set);
    ::pthread_setaffinity_np(::pthread_self(), sizeof set, &set);
}
inline std::vector<int> parse_cpus(std::string_view s) {   // "0-7,16,18"
    std::vector<int> o; size_t i = 0;
    while (i < s.size()) {
        size_t j = s.find(',', i); if (j == s.npos) j = s.size();
        auto part = s.substr(i, j - i); auto d = part.find('-');
        if (!part.empty()) {
            int a = std::stoi(std::string(part.substr(0, d))), b = d == part.npos ? a : std::stoi(std::string(part.substr(d + 1)));
            need(a >= 0 && b >= a && b < 4096, "cpu list");
            for (int c = a; c <= b; c++) o.push_back(c);
        }
        i = j + 1;
    }
    return o;
}

// Dynamic-chunk parallel loop; the first exception is rethrown after all workers joined.
template <class F> void parallel_for(size_t n, int threads, F&& f, size_t grain = 1 << 14, const std::vector<int>* cpus = nullptr, int max_threads = 0) {
    if (n == 0) return;
    if (max_threads > 0) threads = std::min(threads, max_threads);
    threads = std::max(1, std::min<int>(threads, int((n + grain - 1) / grain)));
    std::atomic<size_t> next{0}; std::exception_ptr err; std::mutex em;
    auto body = [&](int t) {
        if (cpus) pin_thread(*cpus, size_t(t));
        try {
            for (;;) { size_t b = next.fetch_add(grain); if (b >= n) break; f(b, std::min(n, b + grain), t); }
        } catch (...) { std::lock_guard<std::mutex> l(em); if (!err) err = std::current_exception(); next = n; }
    };
    if (threads == 1) { body(0); } else {
        std::vector<std::thread> ts; ts.reserve(size_t(threads));
        for (int t = 0; t < threads; t++) ts.emplace_back(body, t);
        for (auto& x : ts) x.join();
    }
    if (err) std::rethrow_exception(err);
}

inline std::vector<fs::path> list_files(const fs::path& dir, std::string_view ext, bool recursive) {
    std::vector<fs::path> o;
    if (!fs::exists(dir)) return o;
    if (fs::is_regular_file(dir)) { o.push_back(dir); return o; }
    auto take = [&](const fs::directory_entry& e) {
        auto name = e.path().filename().string();
        if (e.is_regular_file() && name.size() > ext.size() && name.ends_with(ext) && name.find(".tmp") == std::string::npos) o.push_back(e.path());
    };
    if (recursive) { for (auto& e : fs::recursive_directory_iterator(dir)) take(e); }
    else { for (auto& e : fs::directory_iterator(dir)) take(e); }
    std::sort(o.begin(), o.end());
    return o;
}

// RFC-4180-style CSV line split (quotes, doubled quotes); no embedded newlines.
inline std::vector<std::string> csv_split(std::string_view line) {
    std::vector<std::string> o; std::string cur; bool q = false;
    for (size_t i = 0; i < line.size(); i++) {
        char c = line[i];
        if (q) { if (c == '"') { if (i + 1 < line.size() && line[i + 1] == '"') { cur += '"'; i++; } else q = false; } else cur += c; }
        else if (c == '"') q = true;
        else if (c == ',') { o.push_back(std::move(cur)); cur.clear(); }
        else if (c != '\r') cur += c;
    }
    o.push_back(std::move(cur));
    return o;
}
template <class F> void csv_rows(const fs::path& p, F&& f) {   // f(header_index_map, fields)
    auto text = load_text(p, size_t(512) << 20);
    std::vector<std::string> header; size_t pos = 0; bool first = true;
    while (pos < text.size()) {
        size_t e = text.find('\n', pos); if (e == text.npos) e = text.size();
        std::string_view line(text.data() + pos, e - pos); pos = e + 1;
        if (line.empty()) continue;
        auto fields = csv_split(line);
        if (first) { header = std::move(fields); first = false; continue; }
        f(header, fields);
    }
}
inline const std::string& csv_get(const std::vector<std::string>& h, const std::vector<std::string>& f, std::string_view name) {
    static const std::string empty;
    for (size_t i = 0; i < h.size(); i++) if (h[i] == name) return i < f.size() ? f[i] : empty;
    return empty;
}

// Simple string interner (build time) -> str_off / str_bytes segments.
struct Strings {
    std::vector<std::string> v; std::unordered_map<std::string, uint32_t>* idx;
    Strings() : idx(new std::unordered_map<std::string, uint32_t>()) {}
    ~Strings() { delete idx; }
    Strings(const Strings&) = delete;
    uint32_t id(const std::string& s) {
        auto it = idx->find(s); if (it != idx->end()) return it->second;
        uint32_t i = uint32_t(v.size()); v.push_back(s); (*idx)[s] = i; return i;
    }
};

inline std::string human(double x) {
    char b[32];
    double a = std::fabs(x);
    if (a >= 1e9) std::snprintf(b, sizeof b, "%.3gB", x / 1e9);
    else if (a >= 1e6) std::snprintf(b, sizeof b, "%.3gM", x / 1e6);
    else if (a >= 1e3) std::snprintf(b, sizeof b, "%.3gk", x / 1e3);
    else if (a >= 1 || a == 0) std::snprintf(b, sizeof b, "%.3g", x);
    else std::snprintf(b, sizeof b, "%.2g", x);
    return b;
}
} // namespace machine::graphloop
