// ledger_roundtrip: random transfers -> build_partition -> Reader, checked against a brute-force replay.
#include "machine/engine/ledger.hpp"
#include "machine/engine/dict.hpp"

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <map>
#include <random>

using namespace machine::engine::ledger::fmt;
using machine::engine::TokenKind;
using machine::engine::TokenDictWriter;

static int fails = 0;
#define CHECK(c) do { if (!(c)) { if (++fails < 20) std::fprintf(stderr, "FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

struct Scenario { uint32_t n_addr, n_tok, n_ev, parts, ckpt_min; uint64_t seed; };

static void run(const Scenario& sc, const std::string& dir) {
    std::filesystem::remove_all(dir);
    std::mt19937_64 rng(sc.seed);
    // addresses: 0x0, then random, then 0x...dEaD placed by sorting
    std::vector<Addr> addrs{zero_addr, dead_addr};
    while (addrs.size() < sc.n_addr) { Addr a; for (auto& b : a) b = uint8_t(rng()); addrs.push_back(a); }
    struct Tok { Addr addr; uint8_t kind; };
    std::vector<Tok> toks;
    for (uint32_t i = 0; i < sc.n_tok; ++i) { Tok r{}; for (auto& b : r.addr) b = uint8_t(rng()); r.kind = uint8_t(1 + i % 3); toks.push_back(r); addrs.push_back(r.addr); }
    std::sort(addrs.begin(), addrs.end()); addrs.erase(std::unique(addrs.begin(), addrs.end()), addrs.end());
    uint32_t n_addr = uint32_t(addrs.size());
    auto id_of = [&](const Addr& a) { return uint32_t(std::lower_bound(addrs.begin(), addrs.end(), a) - addrs.begin()); };
    uint32_t zero = id_of(zero_addr), dead = id_of(dead_addr);
    std::sort(toks.begin(), toks.end(), [](auto& x, auto& y) { return x.addr != y.addr ? x.addr < y.addr : x.kind < y.kind; });
    std::filesystem::create_directories(dir);
    write_addr_dict(dir, addrs);
    { TokenDictWriter w(dir); for (auto& t : toks) w.push(id_of(t.addr), TokenKind(t.kind), 18, 0, 0, "T", "Token"); w.finish(); }

    // events: increasing blocks, several per block; mixed amounts incl. 2^256-1, zero, self, mint/burn, dead
    std::vector<Xfer> xs;
    uint32_t block = 100;
    for (uint32_t i = 0; i < sc.n_ev; ++i) {
        if (rng() % 3 == 0) block += 1 + uint32_t(rng() % 4);
        Xfer x{}; x.block = block; x.tx = uint32_t(rng() % 50); x.log = i; x.batch = 0;
        x.tid = uint32_t(rng() % sc.n_tok);
        auto pick = [&]() { uint32_t r = uint32_t(rng() % 20); return r == 0 ? zero : r == 1 ? dead : uint32_t(rng() % n_addr); };
        x.from = pick(); x.to = rng() % 15 == 0 ? x.from : pick();
        if (toks[x.tid].kind == 2) x.amt[0] = 1;
        else switch (rng() % 6) {
            case 0: break;  // zero amount
            case 1: x.amt[0] = x.amt[1] = x.amt[2] = x.amt[3] = ~uint64_t(0); break;
            case 2: x.amt[2] = rng(); x.amt[1] = rng(); break;
            default: x.amt[0] = rng() % 1000000;
        }
        xs.push_back(x);
    }
    const auto orig = xs;
    PartOptions opt; opt.parts = sc.parts; opt.ckpt_min_events = sc.ckpt_min; opt.zero_id = zero; opt.dead_id = dead;
    BuildStats st;
    for (uint32_t p = 0; p < sc.parts; ++p) {
        std::vector<Xfer> part;
        for (auto& x : orig) if (x.tid % sc.parts == p) part.push_back(x);
        build_partition(dir, p, n_addr, uint32_t(toks.size()), part, opt, st);
    }
    Reader R(dir);
    CHECK(R.n_addr() == n_addr && R.n_tok() == toks.size());
    CHECK(R.addr_id(zero_addr) == zero && R.addr_id(dead_addr) == dead);
    CHECK(R.tid(toks.back().addr, TokenKind(toks.back().kind)) == uint32_t(toks.size() - 1));
    CHECK(R.parts() == sc.parts && R.addr(dead) == dead_addr);

    // brute-force state after all events with block <= b
    auto brute = [&](uint32_t tid, uint32_t b) {
        std::map<uint32_t, Acc320> bal; Acc320 sup;
        for (auto& x : orig) {
            if (x.tid != tid || x.block > b || x.from == x.to) continue;
            bal[x.from].sub_u256(x.amt); bal[x.to].add_u256(x.amt);
            if (x.from == zero) sup.add_u256(x.amt);
            if (x.to == zero) sup.sub_u256(x.amt);
        }
        return std::pair{bal, sup};
    };
    std::vector<uint32_t> probes{0, 99, 100, block / 2, block - 1, block, block + 10};
    for (int i = 0; i < 12; ++i) probes.push_back(100 + uint32_t(rng() % (block - 99)));
    for (uint32_t tid = 0; tid < toks.size(); ++tid)
        for (uint32_t b : probes) {
            auto [bal, sup] = brute(tid, b);
            std::vector<Holder> pos;
            for (auto& [a, v] : bal) if (v.sign() > 0 && a != zero && a != dead) pos.push_back({a, v});
            std::sort(pos.begin(), pos.end(), [](auto& x, auto& y) { int c = cmp(x.balance, y.balance); return c ? c > 0 : x.addr < y.addr; });
            CHECK(R.holders_count_at(tid, b) == int64_t(pos.size()));
            CHECK(R.supply_at(tid, b) == sup);
            for (uint32_t n : {1u, 5u, 50u}) {
                auto t = R.holders_at(tid, b, n);
                CHECK(t.holders == int64_t(pos.size()));
                size_t m = std::min<size_t>(n, pos.size());
                if (t.exact) {
                    CHECK(t.rows.size() == m);
                    for (size_t k = 0; k < std::min(m, t.rows.size()); ++k) CHECK(t.rows[k].addr == pos[k].addr && t.rows[k].balance == pos[k].balance);
                } else {
                    for (auto& h : t.rows) CHECK(bal[h.addr] == h.balance);  // rows are still exact balances
                }
            }
            for (int k = 0; k < 8; ++k) {
                uint32_t a = uint32_t(rng() % n_addr);
                CHECK(R.balance_at(a, tid, b) == bal[a]);
            }
        }
    // heads and counters
    std::map<std::pair<uint32_t, uint32_t>, Reader::Head> want;
    for (auto& x : orig) {
        for (int side = 0; side < 2; ++side) {
            uint32_t a = side ? x.to : x.from;
            if (side && x.from == x.to) continue;
            auto [it, fresh] = want.try_emplace({a, x.tid}, Reader::Head{{}, 0, 0, x.block, x.block, 0xffffffffu});
            auto& h = it->second; h.last_block = x.block;
            bool in = side == 1 || x.from == x.to, out = side == 0;
            if (in) { ++h.n_in; if (h.first_in_block == 0xffffffffu) h.first_in_block = x.block; }
            if (out) ++h.n_out;
            if (x.from != x.to) { if (in) h.balance.add_u256(x.amt); else h.balance.sub_u256(x.amt); }
        }
    }
    uint64_t sum_in = 0, sum_out = 0;
    for (auto& [k, h] : want) {
        auto g = R.head(k.first, k.second);
        CHECK(g.has_value());
        if (!g) continue;
        CHECK(g->balance == h.balance && g->n_in == h.n_in && g->n_out == h.n_out && g->first_block == h.first_block &&
              g->last_block == h.last_block && g->first_in_block == h.first_in_block);
        sum_in += h.n_in; sum_out += h.n_out;
        auto ev = R.addr_events(k.first, k.second, 0, 0xffffffffu, 1u << 30);
        CHECK(ev.size() == g->n_in + g->n_out - (g->n_in + g->n_out - ev.size()));  // self transfers are one record
        Acc320 s; for (auto& e : ev) s += e.delta();
        CHECK(s == h.balance);
    }
    CHECK(st.sum_n_in == sum_in && st.sum_n_out == sum_out && st.pairs == want.size());
    CHECK(sum_in == orig.size() && sum_out == orig.size());  // every transfer counts once in and once out
    // token_events covers every event once
    uint64_t total = 0;
    for (uint32_t tid = 0; tid < toks.size(); ++tid) total += R.token_events(tid, 0, 0xffffffffu, 1u << 30).size();
    CHECK(total == orig.size());
    // a window
    auto win = R.token_events(0, 120, 140, 1u << 30);
    uint64_t want_win = 0; for (auto& x : orig) want_win += x.tid == 0 && x.block >= 120 && x.block <= 140;
    CHECK(win.size() == want_win);
    // holdings of one address
    uint32_t a = orig[0].to; size_t nh = 0; for (auto& [k, h] : want) nh += k.first == a;
    CHECK(R.holdings(a).size() == nh);
    // api.hpp adapters over the same build
    {
        namespace L = machine::engine::ledger;
        L::Store S(dir);
        for (uint32_t tid = 0; tid < toks.size(); ++tid) {
            auto ts = L::token_stats(S, tid);
            uint64_t n = 0; for (auto& x : orig) n += x.tid == tid;
            CHECK(ts.has_value() == (n > 0));
            if (ts) CHECK(ts->n_transfers == n);
            std::vector<L::Event> fwd, rev;
            L::EventQuery q; q.tid = tid; q.limit = 1u << 30; q.newest_first = false;
            CHECK(L::events(S, q, [&](const L::Event& e) { fwd.push_back(e); }));
            q.newest_first = true;
            L::events(S, q, [&](const L::Event& e) { rev.push_back(e); });
            CHECK(fwd.size() == n && rev.size() == n);
            {   // forward events equal the source rows of this token in (block, tx, log, batch) order
                std::vector<Xfer> want_ev; for (auto& x : orig) if (x.tid == tid) want_ev.push_back(x);
                std::stable_sort(want_ev.begin(), want_ev.end(), [](auto& x, auto& y) { return std::tie(x.block, x.tx, x.log, x.batch) < std::tie(y.block, y.tx, y.log, y.batch); });
                for (size_t i = 0; i < std::min(fwd.size(), want_ev.size()); ++i)
                    CHECK(fwd[i].block == want_ev[i].block && fwd[i].tx_index == want_ev[i].tx && fwd[i].log_index == want_ev[i].log && fwd[i].from == want_ev[i].from &&
                          fwd[i].to == want_ev[i].to && std::memcmp(fwd[i].amount.w, want_ev[i].amt, 32) == 0);
            }
            std::reverse(rev.begin(), rev.end());
            for (size_t i = 0; i < std::min(fwd.size(), rev.size()); ++i) {
                if (!(fwd[i].block == rev[i].block && fwd[i].log_index == rev[i].log_index) && fails < 3)
                    std::fprintf(stderr, "tid %u i %zu fwd (%llu,%u,%u) rev (%llu,%u,%u)\n", tid, i, (unsigned long long)fwd[i].block, fwd[i].tx_index, fwd[i].log_index,
                                 (unsigned long long)rev[i].block, rev[i].tx_index, rev[i].log_index);
                CHECK(fwd[i].block == rev[i].block && fwd[i].log_index == rev[i].log_index);
            }
            q.limit = 7; q.block0 = 110; q.block1 = 150; rev.clear();
            bool full = L::events(S, q, [&](const L::Event& e) { rev.push_back(e); });
            uint64_t want = 0; for (auto& x : orig) want += x.tid == tid && x.block >= 110 && x.block <= 150;
            CHECK(rev.size() == std::min<uint64_t>(7, want) && full == (want <= 7));
            for (auto& e : rev) CHECK(e.block >= 110 && e.block <= 150);
        }
        uint32_t a = orig[1].to;
        std::vector<L::Event> all; L::EventQuery q; q.addr = a; q.limit = 1u << 30;
        L::events(S, q, [&](const L::Event& e) { all.push_back(e); });
        uint64_t want = 0; for (auto& x : orig) want += x.from == a || x.to == a;
        CHECK(all.size() == want);
        for (size_t i = 1; i < all.size(); ++i) CHECK(all[i - 1].block >= all[i].block);
        std::vector<L::WalletToken> wt; L::wallet_tokens(S, a, wt);
        CHECK(wt.size() == R.holdings(a).size());
        for (auto& w : wt) { auto b = L::balance_at(S, a, w.tid, 0xffffffffu); CHECK(b.balance == w.balance && b.n_in == w.n_in && b.n_out == w.n_out); }
        auto h = L::holders_at(S, 0, block, 10);
        CHECK(h.holders == uint64_t(std::max<int64_t>(0, R.holders_count_at(0, block))));
    }
    std::printf("scenario n_addr=%u n_tok=%u n_ev=%u parts=%u ckpt=%u: checkpoints=%llu pairs=%llu\n", n_addr, sc.n_tok, sc.n_ev, sc.parts,
                sc.ckpt_min, (unsigned long long)st.checkpoints, (unsigned long long)st.pairs);
}

int main(int argc, char** argv) {
    std::string dir = argc > 1 ? argv[1] : "/tmp/ledger_roundtrip";
    // Acc320 basics
    {
        uint64_t mx[4] = {~0ull, ~0ull, ~0ull, ~0ull};
        Acc320 a; a.add_u256(mx); a.add_u256(mx); CHECK(!a.fits_i256() && a.sign() > 0);
        CHECK(a.to_dec() == "231584178474632390847141970017375815706539969331281128078915168015826259279870");
        CHECK(Acc320::from_dec(a.to_dec()) == a);
        Acc320 b = -a; CHECK(b.sign() < 0 && b.to_dec() == "-" + a.to_dec() && Acc320::from_dec(b.to_dec()) == b);
        CHECK(Acc320::from_i64(-5).to_dec() == "-5" && Acc320{}.to_dec() == "0");
        CHECK(Acc320::from_dec("10000000000000000000")->to_dec() == "10000000000000000000");
        std::vector<uint8_t> v; put_acc(v, b); const uint8_t* p = v.data(); CHECK(get_acc(p) == b && p == v.data() + v.size());
        uint8_t be[32]{}; be[31] = 7; be[0] = 1; uint64_t m[4]; be32_to_limbs(be, m); CHECK(m[0] == 7 && m[3] == (1ull << 56));
    }
    run({60, 6, 20000, 1, 256, 1}, dir + "/a");
    run({60, 7, 20000, 3, 64, 2}, dir + "/b");
    run({3000, 2, 60000, 2, 64, 3}, dir + "/c");    // > 1,000 holders: threshold path
    run({8, 3, 5000, 1, 256, 4}, dir + "/d");        // dense, many negative balances
    std::printf(fails ? "ledger_roundtrip FAILED (%d)\n" : "ledger_roundtrip OK\n", fails);
    return fails ? 1 : 0;
}
