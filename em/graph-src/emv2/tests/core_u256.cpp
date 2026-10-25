// core_u256: U256 / I256 against __int128 and known vectors.
#include "machine/engine/u256.hpp"
#include <cassert>
#include <cstdio>
#include <random>
using namespace machine::engine;

int main() {
    std::mt19937_64 rng(7);
    for (int it = 0; it < 200000; it++) {
        u128 a = (u128(rng()) << 64 | rng()) >> (rng() % 128), b = (u128(rng()) << 64 | rng()) >> (rng() % 128);
        U256 A = U256::from_u128(a), B = U256::from_u128(b), S, D;
        assert(add(A, B, S));
        u128 lo = a + b; bool carry = lo < a;
        assert(S.low128() == lo && S.w[2] == (carry ? 1u : 0u) && S.w[3] == 0);
        assert(sub(A, B, D) == (a >= b));
        if (a >= b) assert(D.low128() == a - b && D.fits_u128());
        assert((A <=> B) == (a <=> b));
        i128 x = i128(a) >> (rng() % 64), y = i128(b) >> (rng() % 64);
        if (rng() & 1) x = -x;
        if (rng() & 1) y = -y;
        I256 X = I256::from_i128(x), Y = I256::from_i128(y), Z;
        assert(X.fits_i128() && X.to_i128() == x);
        assert(add(X, Y, Z));
        i128 zs; bool ov = __builtin_add_overflow(x, y, &zs);
        if (!ov) assert(Z.fits_i128() && Z.to_i128() == zs);
        assert((X <=> Y) == (x <=> y));
        assert(X.sign() == (x > 0) - (x < 0));
        assert(to_dec(X) == (x < 0 ? "-" : "") + to_dec(X.abs()));
        auto back = i256_from_dec(to_dec(X)); assert(back && *back == X);
    }
    // decimal round trips and extremes
    assert(to_dec(U256{}) == "0");
    assert(to_dec(U256::max()) == "115792089237316195423570985008687907853269984665640564039457584007913129639935");
    assert(*from_dec("115792089237316195423570985008687907853269984665640564039457584007913129639935") == U256::max());
    assert(!from_dec("115792089237316195423570985008687907853269984665640564039457584007913129639936"));
    assert(!from_dec("") && !from_dec("12a"));
    assert(to_dec(*from_dec("10000000000000000000")) == "10000000000000000000");
    assert(to_dec(*from_dec("1000000000000000000000000000000000000001")) == "1000000000000000000000000000000000000001");
    assert(format_units(*from_dec("1234500000000000000000"), 18) == "1234.5");
    assert(format_units(*from_dec("5"), 18) == "0.000000000000000005");
    assert(format_units(*from_dec("7000000"), 6) == "7");
    // big endian bytes
    uint8_t be[32] = {}; be[31] = 1; be[0] = 0x80;
    U256 v = U256::from_be32(be); assert(v.w[0] == 1 && v.w[3] == 0x8000000000000000ull);
    uint8_t out[32]; v.to_be32(out); assert(std::memcmp(be, out, 32) == 0);
    // I256 bounds
    I256 minv; minv.v.w[3] = 0x8000000000000000ull; I256 t;
    assert(!neg(minv, t));
    assert(to_dec(minv) == "-57896044618658097711785492504343953926634992332820282019728792003956564819968");
    assert(*i256_from_dec(to_dec(minv)) == minv);
    I256 maxv; maxv.v = U256::max(); maxv.v.w[3] >>= 1;
    assert(!add(maxv, I256::from_i64(1), t));
    assert(!sub(minv, I256::from_i64(1), t));
    assert(!I256::from_u256(U256::max()));
    // mul_div: (2^256-1) * (2^256-1) / (2^256-1) and rounding
    assert(*mul_div(U256::max(), U256::max(), U256::max()) == U256::max());
    assert(*mul_div(U256::from_u64(10), U256::from_u64(10), U256::from_u64(3)) == U256::from_u64(33));
    assert(*mul_div(U256::from_u64(10), U256::from_u64(10), U256::from_u64(3), Rounding::ceil) == U256::from_u64(34));
    assert(!mul_div(U256::max(), U256::from_u64(2), U256::from_u64(1)));
    assert(!mul_div(U256::from_u64(1), U256::from_u64(1), U256{}));
    for (int it = 0; it < 20000; it++) {
        uint64_t a = rng(), b = rng(), d = rng() | 1;
        auto r = mul_div(U256::from_u64(a), U256::from_u64(b), U256::from_u64(d));
        assert(r && r->fits_u128() && r->low128() == (u128(a) * b) / d);
    }
    // LP-scale product beyond __int128: 1e27 * 1e27 / 1e27
    auto e27 = *from_dec("1000000000000000000000000000");
    assert(*mul_div(e27, e27, e27) == e27);
    std::printf("core_u256 ok\n");
}
