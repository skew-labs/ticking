// core_codec: varints, exact amounts, bit packing and Elias-Fano against reference values.
#include "machine/engine/codec.hpp"
#include <algorithm>
#include <cassert>
#include <cstdio>
#include <random>
using namespace machine::engine;

int main() {
    std::mt19937_64 rng(11);
    Bytes b;
    std::vector<uint64_t> u; std::vector<int64_t> s; std::vector<I256> big;
    for (int i = 0; i < 100000; i++) {
        uint64_t x = rng() >> (rng() % 64); u.push_back(x); put_uvarint(b, x);
        int64_t y = int64_t(rng()) >> (rng() % 64); s.push_back(y); put_svarint(b, y);
        I256 z;
        switch (rng() % 4) {
        case 0: z = I256::from_i64(int64_t(rng()) >> (rng() % 64)); break;
        case 1: z = I256::from_i128(i128(u128(rng()) << 64 | rng()) >> (rng() % 128)); break;
        case 2: z.v = U256::max(); break;                                   // -1
        default: z.v.w[0] = rng(); z.v.w[1] = rng(); z.v.w[2] = rng(); z.v.w[3] = rng(); break;
        }
        big.push_back(z); put_i256(b, z);
    }
    put_u256(b, U256::max()); put_u256(b, U256::from_u64(5));
    Cursor c(b.data(), b.size());
    for (int i = 0; i < 100000; i++) {
        assert(get_uvarint(c) == u[i]);
        assert(get_svarint(c) == s[i]);
        assert(get_i256(c) == big[i]);
    }
    assert(get_u256(c) == U256::max() && get_u256(c) == U256::from_u64(5) && c.done());
    // a typical 18-decimal amount stays short
    Bytes t; put_i256(t, I256::from_i128(i128(1234567) * 1000000000000000000LL)); assert(t.size() <= 12);   // 1.23e24 = 81 bits + sign + tag
    bool threw = false; try { Cursor tc(t.data(), t.size() - 1); (void)get_i256(tc); } catch (const std::exception&) { threw = true; } assert(threw);
    // bit packing
    for (unsigned w : {0u, 1u, 3u, 7u, 13u, 31u, 32u, 33u, 63u, 64u}) {
        BitWriter bw(w); std::vector<uint64_t> v;
        for (int i = 0; i < 5000; i++) { uint64_t x = w == 64 ? rng() : (w ? rng() & ((uint64_t(1) << w) - 1) : 0); v.push_back(x); bw.push(x); }
        Bytes o; bw.finish(o); assert(o.size() == BitWriter::packed_bytes(v.size(), w));
        for (size_t i = 0; i < v.size(); i++) assert(bit_get(o.data(), w, i) == v[i]);
    }
    // Elias-Fano: dense, sparse, duplicates, empty, single
    for (int trial = 0; trial < 60; trial++) {
        size_t n = trial == 0 ? 0 : trial == 1 ? 1 : size_t(rng() % 20000);
        uint64_t universe = 1 + (rng() >> (rng() % 60));
        std::vector<uint64_t> v(n); for (auto& x : v) x = rng() % universe;
        if (trial % 3 == 0) for (auto& x : v) x = (x % 17) * (universe / 17 + 1) % universe;   // many duplicates
        std::sort(v.begin(), v.end());
        Bytes o; o.push_back(0xaa);   // unaligned start on purpose
        ef_encode(v, universe, o);
        EFView ef(o.data() + 1, n, universe);
        assert(ef.bytes() == o.size() - 1);
        for (size_t i = 0; i < n; i++) assert(ef.get(i) == v[i]);
        size_t k = 0; ef.for_each(0, n, [&](uint64_t i, uint64_t x) { assert(i == k && x == v[k]); k++; }); assert(k == n);
        if (n > 10) { size_t from = n / 3; size_t kk = from; ef.for_each(from, n, [&](uint64_t i, uint64_t x) { assert(i == kk && x == v[kk]); kk++; }); }
        for (int q = 0; q < 200; q++) {
            uint64_t x = rng() % (universe + 2);
            assert(ef.lower_bound(x) == uint64_t(std::lower_bound(v.begin(), v.end(), x) - v.begin()));
        }
    }
    std::printf("core_codec ok\n");
}
