#pragma once
// Bounded single-producer / single-consumer ring for builder pipeline stages.
// Adapted from economic-machine native/src/solution_pipeline.cpp `Ring` (64-byte separated
// cursors, acquire/release publication) with two changes for long builds:
//   * waiting uses C++20 atomic wait/notify (futex) instead of a yield spin, so an idle stage
//     costs no CPU on a shared machine;
//   * close() ends the stream: pop() returns false once the ring is closed and drained.
// Exactly one thread may push and one thread may pop. Move big batches by value (std::vector)
// or by pointer; the slot array is fixed at construction.
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

namespace machine::engine {

template <class T> class Ring {
    std::vector<T> slots_;
    alignas(64) std::atomic<uint64_t> write_{0};
    alignas(64) std::atomic<uint64_t> read_{0};
    // Every push, pop and close bumps event_; waiters snapshot it before testing their condition,
    // so a change between the test and the wait can never be missed.
    alignas(64) std::atomic<uint32_t> event_{0};
    std::atomic<bool> closed_{false};
    void signal() noexcept { event_.fetch_add(1, std::memory_order_acq_rel); event_.notify_all(); }
public:
    explicit Ring(size_t capacity) : slots_(capacity) {}
    Ring(const Ring&) = delete;
    Ring& operator=(const Ring&) = delete;
    size_t capacity() const noexcept { return slots_.size(); }

    // Blocks while full. Returns false (value dropped) if the ring was closed.
    bool push(T value) {
        for (;;) {
            uint32_t e = event_.load(std::memory_order_acquire);
            if (closed_.load(std::memory_order_acquire)) return false;
            uint64_t w = write_.load(std::memory_order_relaxed), r = read_.load(std::memory_order_acquire);
            if (w - r < slots_.size()) {
                slots_[w % slots_.size()] = std::move(value);
                write_.store(w + 1, std::memory_order_release);
                signal();
                return true;
            }
            event_.wait(e, std::memory_order_acquire);
        }
    }
    // Blocks while empty. Returns false when closed and drained.
    bool pop(T& out) {
        for (;;) {
            uint32_t e = event_.load(std::memory_order_acquire);
            uint64_t r = read_.load(std::memory_order_relaxed), w = write_.load(std::memory_order_acquire);
            if (r != w) {
                out = std::move(slots_[r % slots_.size()]);
                read_.store(r + 1, std::memory_order_release);
                signal();
                return true;
            }
            if (closed_.load(std::memory_order_acquire)) {
                // close() follows the last push; re-read write_ so a push that raced the first load is not lost
                if (read_.load(std::memory_order_relaxed) == write_.load(std::memory_order_acquire)) return false;
                continue;
            }
            event_.wait(e, std::memory_order_acquire);
        }
    }
    // Producer side: no more pushes. Wakes waiters on both sides.
    void close() noexcept { closed_.store(true, std::memory_order_release); signal(); }
    bool closed() const noexcept { return closed_.load(std::memory_order_acquire); }
};

} // namespace machine::engine
