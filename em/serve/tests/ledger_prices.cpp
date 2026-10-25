// ledger_prices: usd_value / parse_price boundaries.
#include "machine/engine/prices.hpp"
#include <cstdio>
using namespace machine::engine::prices;
using machine::engine::ledger::fmt::Acc320;
static int fails = 0;
#define CHECK(c) do { if (!(c)) { ++fails; std::fprintf(stderr, "FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)
int main() {
    auto raw = [](const char* s) { return *Acc320::from_dec(s); };
    auto v = [](const Acc320& r, uint8_t d, const char* p) { return usd_value(r, d, *parse_price(p)); };
    CHECK(v(raw("1000000"), 6, "1").value == 1000000);                       // 1 USDC at $1 = 1.000000
    CHECK(v(raw("1500000000000000000"), 18, "2000").value == 3000000000);    // 1.5 ETH at $2000 = $3000
    CHECK(v(raw("123456789"), 0, "0.000000001").value == 123456);            // 0.123456789 USD -> floor 0.123456
    CHECK(v(raw("1"), 18, "0.0000000000001").value == 0);                     // dust
    CHECK(v(raw("-1"), 18, "1").value == -1);                                 // negative floors toward -inf
    CHECK(v(raw("-1000000"), 6, "1").value == -1000000);
    CHECK(v(raw("0"), 18, "5").value == 0);
    CHECK(!v(raw("1000000000000000000000000000000"), 18, "1000000"));         // 1e12 tokens * $1e6 -> overflow
    CHECK(v(raw("1000000000000000000000000000"), 18, "1000").value == 1000000000000000000);  // $1e12 fits
    auto mx = Acc320{}; uint64_t m[4] = {~0ull, ~0ull, ~0ull, ~0ull}; mx.add_u256(m); mx.add_u256(m);
    CHECK(!usd_value(mx, 18, DecPrice{1, 0}));                               // 2^257 raw -> overflow, no crash
    CHECK(usd_value(mx, 77, DecPrice{1, -30}).value == 0);
    auto p = parse_price("1.5e-9"); CHECK(p && p->mantissa == 15 && p->exp10 == -10);
    p = parse_price("2000"); CHECK(p && p->mantissa == 2 && p->exp10 == 3);
    p = parse_price("0.000000123"); CHECK(p && p->mantissa == 123 && p->exp10 == -9);
    CHECK(!parse_price("abc") && !parse_price("") && !parse_price("1e"));
    CHECK(!usd_value(raw("1"), 18, DecPrice{-1, 0}));
    std::printf(fails ? "ledger_prices FAILED (%d)\n" : "ledger_prices OK\n", fails);
    return fails ? 1 : 0;
}
