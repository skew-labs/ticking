// core_ring: SPSC order, back-pressure and close semantics.
#include "machine/engine/ring.hpp"
#include <cassert>
#include <cstdio>
#include <thread>
#include <vector>
using namespace machine::engine;

int main() {
    for (size_t cap : {1u, 2u, 7u, 64u}) {
        Ring<std::vector<uint64_t>> r(cap);
        const uint64_t n = 200000;
        std::thread prod([&] { for (uint64_t i = 0; i < n; i++) assert(r.push(std::vector<uint64_t>{i, i * 3})); r.close(); });
        uint64_t expect = 0; std::vector<uint64_t> v;
        while (r.pop(v)) { assert(v.size() == 2 && v[0] == expect && v[1] == expect * 3); expect++; }
        prod.join();
        assert(expect == n);
        assert(!r.push(std::vector<uint64_t>{1}));   // closed
    }
    // close with nothing pushed wakes a waiting consumer
    Ring<int> e(4); std::thread t([&] { int x; assert(!e.pop(x)); }); std::this_thread::sleep_for(std::chrono::milliseconds(20)); e.close(); t.join();
    std::printf("core_ring ok\n");
}
