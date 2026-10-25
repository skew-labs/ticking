// EMV2 graph builder tests: codec round trips, a synthetic map (hubs both ways, self rows, several kinds and
// assets per pair, NULL and 256-bit amounts, assets missing from the dictionary) built and compared with a
// brute-force model, and byte-identical rebuilds.
#include "../src/build/graph_core.hpp"
#include "machine/engine/api.hpp"
#include <iostream>
#include <map>
#include <random>

using namespace machine::engine::graph;
using machine::engine::BuildInput;

static int failures = 0;
#define CHECK(c) do { if (!(c)) { failures++; std::cerr << "FAIL " << __LINE__ << ": " #c << std::endl; } } while (0)

static void test_codec() {
    std::mt19937_64 rng(7);
    for (int it = 0; it < 3000; it++) {
        uint32_t c = 1 + uint32_t(rng() % ef_block); uint32_t span = it % 3 == 0 ? 0 : uint32_t(rng() % (it % 2 ? 1000 : 4000000000u));
        std::vector<Entry> e(c); uint32_t base = uint32_t(rng() % 100000);
        for (auto& x : e) { x.nbr = base + (span ? uint32_t(rng() % span) : 0); x.kind = uint8_t(rng() % 14); x.wq = uint8_t(rng() % 16); x.last_day = uint16_t(rng()); x.span = uint32_t(rng() % 5000); }
        std::sort(e.begin(), e.end(), entry_key_less);
        for (bool t : {false, true}) {
            ListFmt f{uint32_t(1 + it % 4), uint16_t(rng())}; if (f.id_bytes < 4) for (auto& x : e) x.nbr &= (1u << (8 * f.id_bytes)) - 1;
            std::sort(e.begin(), e.end(), entry_key_less);
            std::string s; encode_block(s, e.data(), c, t, f);
            std::vector<Entry> d; auto* p = reinterpret_cast<const uint8_t*>(s.data());
            auto* q = decode_block(p, p + s.size(), c, t, f, d);
            CHECK(q == p + s.size()); CHECK(d.size() == c);
            for (uint32_t i = 0; i < c && i < d.size(); i++) {
                CHECK(d[i].nbr == e[i].nbr); CHECK(d[i].kind == e[i].kind); CHECK(d[i].wq == e[i].wq);
                if (t) { CHECK(d[i].last_day == e[i].last_day); CHECK(d[i].span == e[i].span); }
            }
        }
    }
    for (std::string v : {"0", "1", "255", "256", "18446744073709551616", "115792089237316195423570985008687907853269984665640564039457584007913129639935"}) {
        std::string le; CHECK(decimal_to_le(v, le)); CHECK(le_to_decimal(le) == v);
    }
    std::string le; CHECK(decimal_to_le("115792089237316195423570985008687907853269984665640564039457584007913129639936", le));   // 2^256 (map totals can pass it)
    CHECK(le.size() == 33 && le_to_decimal(le) == "115792089237316195423570985008687907853269984665640564039457584007913129639936");
    CHECK(!decimal_to_le(std::string(160, '9'), le));   // > 512 bits
}

struct Synth : GraphSource {
    std::vector<std::vector<AddrRow>> ap; std::vector<std::vector<EdgeRow>> ep;
    size_t addr_parts() const override { return ap.size(); }
    void read_addr_part(size_t i, const std::function<void(AddrRow&)>& f) const override { for (auto r : ap[i]) f(r); }
    size_t edge_parts() const override { return ep.size(); }
    uint64_t edge_rows_hint() const override { uint64_t s = 0; for (auto& p : ep) s += p.size(); return s; }
    void read_edge_part(size_t i, const std::function<void(EdgeRow&)>& f) const override { for (auto r : ep[i]) f(r); }
    std::vector<BuildInput> inputs() const override { return {{"synthetic", "", 0, edge_rows_hint()}}; }
};

static Addr mk(std::mt19937_64& rng) { Addr a; for (auto& b : a.b) b = uint8_t(rng()); return a; }

static void test_build() {
    std::mt19937_64 rng(11);
    const int N = 6000, AP = 5, EP = 7;
    std::vector<Addr> A(N); for (auto& a : A) a = mk(rng);
    Synth s; s.ap.resize(AP); s.ep.resize(EP);
    std::vector<uint64_t> fb_of(N);
    for (int i = 0; i < N; i++) {
        AddrRow r; r.a = A[size_t(i)]; r.d.fb = 1000 + uint64_t(rng() % 100000); r.d.lb = r.d.fb + rng() % 50000; r.d.ft = day_epoch + 86400 * 3000 + r.d.fb * 2; r.d.lt = day_epoch + 86400 * 3000 + r.d.lb * 2;
        r.d.tx_sent = rng() % 50; r.d.tx_recv = rng() % 50; r.d.tt_in = rng() % 9; r.d.tt_out = rng() % 9; r.d.contract = rng() % 7 == 0; r.d.map_out = rng() % 99; r.d.map_in = rng() % 99;
        s.ap[size_t(fnv1a(r.a.b, 20) % AP)].push_back(r); fb_of[size_t(i)] = r.d.fb;
    }
    for (auto& p : s.ap) std::sort(p.begin(), p.end(), [](const AddrRow& x, const AddrRow& y) { return x.a < y.a; });
    // rows: (from, to, asset, kind) unique; hub sources 0..2 (out), hub targets 3..4 (in)
    std::map<std::tuple<int, int, int, int>, EdgeRow> rows;
    auto add = [&](int f, int t, int asset, int kind) {
        EdgeRow e; e.from = A[size_t(f)]; e.to = A[size_t(t)]; e.kind = uint8_t(kind);
        if (asset >= 0) { e.has_asset = true; e.asset = asset < N ? A[size_t(asset)] : mk(rng); }
        e.n = 1 + (rng() % 5 == 0 ? rng() % 100000 : rng() % 4); e.fb = 1000 + rng() % 100000; e.lb = e.fb + rng() % 40000;
        if (kind != 2) { e.amount_null = false; std::string dec = std::to_string(rng()); if (rng() % 9 == 0) dec += "123456789012345678901234567890123456789012345"; if (rng() % 97 == 0) dec += "9999999999999999999999999999999"; decimal_to_le(dec, e.amount_le); }
        rows[{f, t, asset, kind}] = e;
    };
    for (int f = 0; f < 3; f++) for (int t = 0; t < 2600; t++) add(f, 10 + int(rng() % (N - 10)), int(rng() % 3) - 1 + 7, int(rng() % 4));
    for (int t = 3; t < 5; t++) for (int f = 0; f < 1800; f++) add(10 + int(rng() % (N - 10)), t, 50 + int(rng() % 2), 1);
    for (int i = 0; i < 30000; i++) { int f = int(rng() % N), t = rng() % 50 == 0 ? f : int(rng() % N); add(f, t, rng() % 4 == 0 ? -1 : (rng() % 20 == 0 ? N + 1 : int(rng() % N)), int(rng() % 4)); }
    for (auto& [k, e] : rows) s.ep[size_t(fnv1a(e.from.b, 20) % EP)].push_back(e);
    for (auto& p : s.ep) std::sort(p.begin(), p.end(), [](const EdgeRow& x, const EdgeRow& y) { return x.from < y.from || (x.from == y.from && x.to < y.to); });

    fs::path root = fs::temp_directory_path() / ("emv2-graph-test-" + std::to_string(::getpid()));
    fs::remove_all(root);
    json sha[2];
    for (int run = 0; run < 2; run++) {
        BuildOpt o; o.chain = "synthetic"; o.out = root / ("img" + std::to_string(run)); o.tmp = root / ("tmp" + std::to_string(run)); o.threads = run ? 3 : 1; o.in_buckets = run ? 5 : 3;
        o.build_id = "test0000build000";
        Builder b(o, s); auto r = b.run();
        machine::engine::merge_manifests(o.out);
        machine::engine::BuildDir bd(o.out, true); Image g(bd);
        sha[run] = bd.manifest()["files"];
        if (run == 1) continue;
        CHECK(g.n == uint32_t(N));
        for (int i = 0; i < N; i++) CHECK(g.find(A[size_t(i)]) != none);
        // brute force model
        std::map<std::pair<uint32_t, uint32_t>, std::map<uint8_t, std::tuple<uint64_t, uint64_t, uint64_t>>> merged;   // (s,d) -> kind -> (n, fb, lb)
        std::map<uint32_t, std::vector<EdgeRow>> by_src;
        for (auto& [k, e] : rows) {
            uint32_t sv = g.find(e.from), dv = g.find(e.to);
            by_src[sv].push_back(e);
            if (sv == dv) continue;
            auto& x = merged[{sv, dv}][e.kind];
            if (std::get<0>(x) == 0) x = {e.n, e.fb, e.lb}; else x = {std::get<0>(x) + e.n, std::min(std::get<1>(x), e.fb), std::max(std::get<2>(x), e.lb)};
        }
        std::vector<std::vector<Entry>> outl(N), inl(N);
        for (auto& [sd, ks] : merged) for (auto& [kind, x] : ks) {
            Entry e{}; e.nbr = sd.second; e.kind = kind; e.n = std::get<0>(x); e.wq = weight_class(e.n);
            e.last_day = g.day_of_block(std::get<2>(x)); e.span = e.last_day - g.day_of_block(std::get<1>(x));
            outl[sd.first].push_back(e);
            Entry r = e; r.nbr = sd.first; inl[sd.second].push_back(r);
        }
        int hubs_seen = 0;
        ListView lv, hv;
        for (uint32_t v = 0; v < uint32_t(N); v++) {
            for (int d = 0; d < 2; d++) {
                auto& want = d == 0 ? outl[v] : inl[v]; std::sort(want.begin(), want.end(), entry_key_less);
                CHECK(g.full(v, Side(d), lv)); g.hot(v, Side(d), hv);
                CHECK(lv.total == want.size()); CHECK(lv.entries.size() == want.size());
                for (size_t i = 0; i < want.size() && i < lv.entries.size(); i++) {
                    CHECK(lv.entries[i].nbr == want[i].nbr); CHECK(lv.entries[i].kind == want[i].kind); CHECK(lv.entries[i].wq == want[i].wq);
                    if (d == 0) { CHECK(lv.entries[i].last_day == want[i].last_day); CHECK(lv.entries[i].span == want[i].span); }
                }
                if (want.size() > hub_degree) {
                    hubs_seen++; CHECK(g.is_hub(v, Side(d))); CHECK(hv.stored == hub_keep);
                    // hot = the 256 heaviest
                    std::vector<Entry> w = want; std::sort(w.begin(), w.end(), entry_weight_less); uint64_t cut = w[hub_keep - 1].n;
                    for (auto& x : hv.entries) { auto it = std::lower_bound(want.begin(), want.end(), x, entry_key_less); CHECK(it != want.end() && it->nbr == x.nbr && it->n >= cut); }
                } else CHECK(!g.is_hub(v, Side(d)));
                if (hv.stored > top_n) {   // top positions = heaviest by weight class
                    uint8_t minq = 255; for (auto p : hv.top) minq = std::min(minq, hv.entries[p].wq);
                    size_t heavier = 0; for (auto& x : hv.entries) heavier += x.wq > minq; CHECK(heavier <= top_n);
                }
            }
            // details = original rows in (to, kind, asset) order
            auto got = g.details(v); auto want = by_src[v];
            std::sort(want.begin(), want.end(), [&](const EdgeRow& x, const EdgeRow& y) {
                uint32_t a = g.find(x.to), b = g.find(y.to); if (a != b) return a < b; if (x.kind != y.kind) return x.kind < y.kind;
                if (x.has_asset != y.has_asset) return !x.has_asset; return std::memcmp(x.asset.b, y.asset.b, 20) < 0; });
            CHECK(got.size() == want.size());
            for (size_t i = 0; i < got.size() && i < want.size(); i++) {
                auto& a = got[i]; auto& b = want[i];
                CHECK(g.addr(a.d) == b.to); CHECK(a.kind == b.kind); CHECK(a.n == b.n); CHECK(a.fb == b.fb); CHECK(a.lb == b.lb);
                CHECK(bool(a.flags & df_amount_null) == b.amount_null); if (!b.amount_null) CHECK(a.amount_le == b.amount_le);
                CHECK(!(a.flags & df_asset_none) == b.has_asset);
                if (b.has_asset) { Addr x = (a.flags & df_asset_raw) ? a.asset_raw : g.addr(a.asset); CHECK(x == b.asset); }
            }
            auto nd = g.node_detail(v); CHECK(nd.has_value());
        }
        CHECK(hubs_seen >= 5);
        // api readers: exact edges (sum of original rows), path, trace, dump counts
        {
            namespace api = machine::engine::graph;
            auto img = api::open(bd);
            CHECK(api::node_count(*img) == uint64_t(N));
            int checked = 0;
            for (auto& [sv, rowsv] : by_src) {
                if (checked++ > 300) break;
                api::EdgeQuery q; q.dir = api::Dir::out; q.limit = 100000; q.want_amount = true;
                auto er = api::edges(*img, sv, q);
                for (auto& e : er.edges) {
                    uint64_t want_n = 0; for (auto& row : rowsv) if (g.find(row.to) == e.peer && row.kind == uint8_t(e.type)) want_n += row.n;
                    CHECK(e.n == want_n); CHECK(e.flags & api::ef_exact_n);
                }
            }
            checked = 0;
            for (auto& [sv, rowsv] : by_src) {   // split_assets: one record per original (to, kind, asset) row of this source
                if (checked++ > 300) break;
                api::EdgeQuery q; q.dir = api::Dir::out; q.limit = 100000; q.split_assets = true;
                auto er = api::edges(*img, sv, q);
                size_t want = 0; for (auto& row : rowsv) if (g.find(row.to) != sv) want++;
                CHECK(er.edges.size() == want);
                for (auto& e : er.edges) CHECK(!(e.flags & api::ef_multi_asset));
            }
            api::PathQuery pq; pq.from = g.find(A[20]); auto anyto = outl[pq.from].empty() ? pq.from : outl[pq.from][0].nbr; pq.to = anyto;
            auto pr = api::path(*img, pq); CHECK(pr.found); CHECK(!pr.nodes.empty() && pr.nodes.front() == pq.from && pr.nodes.back() == pq.to);
            api::TraceQuery tq; tq.root = pq.from; tq.dir = api::Dir::both; auto tr = api::trace(*img, tq); CHECK(!tr.nodes.empty() && tr.nodes[0] == pq.from);
        }
    }
    CHECK(sha[0] == sha[1]);   // 1 thread / 3 buckets vs 3 threads / 5 buckets: same bytes
    if (sha[0] != sha[1]) for (auto& [k, v] : sha[0].items()) if (v != sha[1][k]) std::cerr << "differs: " << k << std::endl;
    fs::remove_all(root);
}

int main() {
    try { test_codec(); test_build(); }
    catch (const std::exception& e) { std::cerr << "exception: " << e.what() << std::endl; return 2; }
    std::cout << (failures ? "graph_test FAIL " + std::to_string(failures) : std::string("graph_test OK")) << std::endl;
    return failures ? 1 : 0;
}
