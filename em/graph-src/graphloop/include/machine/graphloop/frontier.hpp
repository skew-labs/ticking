#pragma once
// Agent frontier: priority = uncertainty x importance.  64 shards (node % 64), each a lazy-deletion
// max-heap + per-node state under its own mutex; a cached top priority per shard lets lease() pick
// the globally best shard without a global lock.  A node is leased to at most one task at a time
// (TTL); a changed node that is currently leased is marked dirty and re-queued on release.
#include "core.hpp"
#include <mutex>
#include <unordered_map>

namespace machine::graphloop {

class Frontier {
public:
    static constexpr size_t shard_count = 64;
    struct Stats { uint64_t queued = 0, leased = 0, members = 0, leases = 0, expired = 0, dedup_skips = 0, done = 0; };
private:
    struct Item { float pri; uint32_t node; uint32_t gen; bool operator<(const Item& o) const { return pri < o.pri || (pri == o.pri && node > o.node); } };
    struct State { uint32_t gen = 0, lease_until = 0; uint64_t task = 0, last_hash = 0; float pri = 0; uint8_t queued = 0, leased = 0, dirty = 0, done = 0; };
    struct Exp { uint32_t until; uint32_t node; uint64_t task; bool operator<(const Exp& o) const { return until > o.until; } };
    struct alignas(64) Shard {
        std::mutex mu; std::vector<Item> heap; std::vector<Exp> exp; std::unordered_map<uint32_t, State> st;
        std::atomic<float> top{-1.f}; uint64_t queued = 0, leased = 0, leases = 0, expired = 0, dedup = 0, done = 0;
        void refresh_top() {
            while (!heap.empty()) {
                auto& it = heap.front(); auto s = st.find(it.node);
                if (s != st.end() && s->second.queued && s->second.gen == it.gen) break;
                std::pop_heap(heap.begin(), heap.end()); heap.pop_back();
            }
            top.store(heap.empty() ? -1.f : heap.front().pri, std::memory_order_relaxed);
        }
    };
    std::array<Shard, shard_count> sh_;
    static size_t shard_of(uint32_t v) { return size_t(mix64(v) & (shard_count - 1)); }
public:
    // Enqueue / re-prioritise.  Leased nodes are marked dirty (re-queued when the lease ends).
    void push(uint32_t v, float pri) {
        auto& s = sh_[shard_of(v)]; std::lock_guard<std::mutex> l(s.mu);
        auto& x = s.st[v];
        if (x.leased) { x.dirty = 1; x.pri = pri; return; }
        if (!x.queued) { x.queued = 1; s.queued++; }
        x.gen++; x.pri = pri; x.done = 0;
        s.heap.push_back({pri, v, x.gen}); std::push_heap(s.heap.begin(), s.heap.end());
        s.refresh_top();
    }
    void remove(uint32_t v) {
        auto& s = sh_[shard_of(v)]; std::lock_guard<std::mutex> l(s.mu);
        auto it = s.st.find(v); if (it == s.st.end()) return;
        if (it->second.queued) { it->second.queued = 0; s.queued--; it->second.gen++; }
        it->second.dirty = 0; s.refresh_top();
    }
    // Lease up to n nodes, best priority first.  accept(node) may veto (dedupe) -> node marked done.
    template <class Accept> size_t lease(size_t n, uint32_t now_s, uint32_t ttl_s, uint64_t task, std::vector<uint32_t>& out, Accept&& accept) {
        size_t got = 0, misses = 0;
        while (got < n && misses < 4 * shard_count) {
            size_t best = shard_count; float bp = -1.f;
            for (size_t i = 0; i < shard_count; i++) { float t = sh_[i].top.load(std::memory_order_relaxed); if (t > bp) { bp = t; best = i; } }
            if (best == shard_count) break;
            auto& s = sh_[best]; std::unique_lock<std::mutex> l(s.mu);
            s.refresh_top();
            if (s.heap.empty()) { misses++; continue; }
            Item it = s.heap.front(); std::pop_heap(s.heap.begin(), s.heap.end()); s.heap.pop_back();
            auto& x = s.st[it.node];      // unordered_map references survive rehash
            x.queued = 0; s.queued--;
            x.leased = 1; x.lease_until = now_s + ttl_s; x.task = task; s.leased++;   // tentative: concurrent push() -> dirty
            l.unlock();
            bool ok = accept(it.node);     // pack render + hash compare, outside the shard lock
            l.lock();
            if (!ok) {
                x.leased = 0; s.leased--; x.done = 1; s.dedup++;
                if (x.dirty) { x.dirty = 0; x.gen++; x.queued = 1; s.queued++; s.heap.push_back({x.pri, it.node, x.gen}); std::push_heap(s.heap.begin(), s.heap.end()); }
                s.refresh_top(); continue;
            }
            s.leases++;
            s.exp.push_back({x.lease_until, it.node, task}); std::push_heap(s.exp.begin(), s.exp.end());
            s.refresh_top();
            out.push_back(it.node); got++;
        }
        return got;
    }
    bool holds(uint32_t v, uint64_t task) {
        auto& s = sh_[shard_of(v)]; std::lock_guard<std::mutex> l(s.mu);
        auto it = s.st.find(v); return it != s.st.end() && it->second.leased && it->second.task == task;
    }
    // Lease finished.  completed=true records the input hash (identical input is never re-leased).
    void release(uint32_t v, uint64_t task, bool completed, uint64_t input_hash) {
        auto& s = sh_[shard_of(v)]; std::lock_guard<std::mutex> l(s.mu);
        auto it = s.st.find(v); if (it == s.st.end()) return;
        auto& x = it->second;
        if (!x.leased || x.task != task) return;
        x.leased = 0; s.leased--;
        if (completed) { x.last_hash = input_hash; x.done = 1; s.done++; }
        if (x.dirty || !completed) {
            x.dirty = 0; x.gen++; x.queued = 1; s.queued++;
            s.heap.push_back({x.pri, v, x.gen}); std::push_heap(s.heap.begin(), s.heap.end());
            s.refresh_top();
        }
    }
    uint64_t last_hash(uint32_t v) {
        auto& s = sh_[shard_of(v)]; std::lock_guard<std::mutex> l(s.mu);
        auto it = s.st.find(v); return it == s.st.end() ? 0 : it->second.last_hash;
    }
    void set_last_hash(uint32_t v, uint64_t h) {
        auto& s = sh_[shard_of(v)]; std::lock_guard<std::mutex> l(s.mu); s.st[v].last_hash = h;
    }
    // Expired leases go back to the queue.
    size_t expire(uint32_t now_s) {
        size_t n = 0;
        for (auto& s : sh_) {
            std::lock_guard<std::mutex> l(s.mu);
            while (!s.exp.empty() && s.exp.front().until <= now_s) {
                Exp e = s.exp.front(); std::pop_heap(s.exp.begin(), s.exp.end()); s.exp.pop_back();
                auto it = s.st.find(e.node);
                if (it == s.st.end() || !it->second.leased || it->second.task != e.task || it->second.lease_until > now_s) continue;
                auto& x = it->second; x.leased = 0; s.leased--; s.expired++; n++;
                x.dirty = 0; x.gen++; x.queued = 1; s.queued++;
                s.heap.push_back({x.pri, e.node, x.gen}); std::push_heap(s.heap.begin(), s.heap.end());
            }
            s.refresh_top();
        }
        return n;
    }
    Stats stats() {
        Stats t;
        for (auto& s : sh_) { std::lock_guard<std::mutex> l(s.mu); t.queued += s.queued; t.leased += s.leased; t.members += s.st.size(); t.leases += s.leases; t.expired += s.expired; t.dedup_skips += s.dedup; t.done += s.done; }
        return t;
    }
    std::vector<std::pair<uint32_t, float>> top(size_t k) {
        std::vector<std::pair<uint32_t, float>> o;
        for (auto& s : sh_) { std::lock_guard<std::mutex> l(s.mu); for (auto& [v, x] : s.st) if (x.queued) o.push_back({v, x.pri}); }
        std::partial_sort(o.begin(), o.begin() + std::min(k, o.size()), o.end(), [](auto& a, auto& b) { return a.second > b.second || (a.second == b.second && a.first < b.first); });
        o.resize(std::min(k, o.size()));
        return o;
    }
};
} // namespace machine::graphloop
