// core_dict: address/token dictionaries, block time, segments, manifests and build ids.
#include "machine/engine/dict.hpp"
#include <cassert>
#include <cstdio>
#include <random>
#include <set>
using namespace machine::engine;

static fs::path tmpdir(const char* tag) {
    fs::path p = fs::temp_directory_path() / (std::string("emv2-") + tag + "-" + std::to_string(::getpid()));
    fs::remove_all(p); fs::create_directories(p); return p;
}

static void dict_case(uint64_t n, uint64_t expected, const char* tag) {
    std::mt19937_64 rng(n);
    std::set<std::array<uint8_t, 20>> set;
    while (set.size() < n) { std::array<uint8_t, 20> a; for (auto& x : a) x = uint8_t(rng()); if (set.size() % 7 == 0) a[0] = a[1] = a[2] = 0; set.insert(a); }
    auto dir = tmpdir(tag);
    PartManifest pm("emvtest", "core", "0123456789abcdef");
    {
        AddrDictWriter w(dir, expected); uint32_t id = 0;
        for (auto& a : set) { Addr x; std::memcpy(x.b, a.data(), 20); assert(w.push(x) == id++); }
        auto [r1, r2] = w.finish(); pm.file(r1); pm.file(r2);
        bool threw = false; try { AddrDictWriter w2(dir / "nope", 1); } catch (...) { threw = true; } assert(threw);
    }
    {
        TokenDictWriter tw(dir);
        tw.push(3, TokenKind::erc20, 18, 0, 100, "AAA", "Token A");
        tw.push(3, TokenKind::erc721, 255, tf_no_decimals, 120, "AAA-NFT", "");
        tw.push(9, TokenKind::erc20, 6, 0, 7, "USDC", "USD Coin");
        auto [a, b] = tw.finish(); pm.file(a); pm.file(b);
    }
    {
        BlockTimeWriter bw(dir, 1000);
        uint32_t t = 1600000000;
        for (int i = 0; i < 5000; i++) { bw.push(t); t += uint32_t(i % 5 == 0 ? 0 : (i % 7)); }
        pm.file(bw.finish());
    }
    pm.write(dir);
    auto m = merge_manifests(dir);
    assert(m["files"].size() == 5);
    BuildDir b(dir, true);
    AddrDict d(b);
    assert(d.size() == n && d.radix_bytes() == (expected > (uint64_t(1) << 26) ? 3u : 2u));
    uint32_t id = 0;
    for (auto& a : set) {
        auto f = d.find(a.data()); assert(f && *f == id);
        Addr back = d.get(id); assert(std::memcmp(back.b, a.data(), 20) == 0);
        id++;
    }
    for (int i = 0; i < 2000; i++) { std::array<uint8_t, 20> a; for (auto& x : a) x = uint8_t(rng()); assert(d.find(a.data()).has_value() == (set.count(a) > 0)); }
    TokenDict td(b);
    assert(td.size() == 3 && *td.find(3, TokenKind::erc721) == 1 && !td.find(3, TokenKind::erc1155) && *td.find(9, TokenKind::erc20) == 2);
    assert(td.symbol(2) == "USDC" && td.name(2) == "USD Coin" && td.name(1).empty());
    uint32_t first; auto recs = td.by_addr(3, first); assert(first == 0 && recs.size() == 2);
    BlockTime bt(b);
    std::vector<uint32_t> ts; uint32_t t = 1600000000; for (int i = 0; i < 5000; i++) { ts.push_back(t); t += uint32_t(i % 5 == 0 ? 0 : (i % 7)); }
    for (int i = 0; i < 5000; i += 13) assert(*bt.time_at(1000 + uint64_t(i)) == ts[size_t(i)]);
    assert(!bt.time_at(999) && !bt.time_at(6000));
    for (int q = 0; q < 3000; q++) {
        uint32_t x = ts.front() - 2 + uint32_t(rng() % (ts.back() - ts.front() + 5));
        auto it = std::upper_bound(ts.begin(), ts.end(), x);
        auto r = bt.block_at(x);
        if (it == ts.begin()) assert(!r); else assert(r && *r == 1000 + uint64_t(it - ts.begin() - 1));
    }
    // a corrupted file is rejected by verification
    { FILE* f = std::fopen((dir / "dict.tok.str.seg").c_str(), "r+b"); std::fputc('Z', f); std::fclose(f); }
    bool threw = false; try { BuildDir bad(dir, true); } catch (...) { threw = true; } assert(threw);
    fs::remove_all(dir);
}

int main() {
    dict_case(50000, 50000, "small");          // k = 2
    dict_case(30000, uint64_t(1) << 27, "big"); // k = 3 path
    // build ids depend only on chain + inputs (order-free)
    std::vector<BuildInput> in = {{"b", "22", 2, 2}, {"a", "11", 1, 1}};
    auto id1 = build_id_of("arc", in); std::swap(in[0], in[1]);
    assert(id1 == build_id_of("arc", in) && id1 != build_id_of("linea", in) && id1.size() == 16);
    // segment kind and shape checks
    auto dir = tmpdir("seg");
    { SegWriter w(dir / "x.seg", Kind::g_meta, 8); uint64_t v[3] = {1, 2, 3}; w.put(v, sizeof v); auto r = w.finish(3, 42, 43); assert(r.bytes == 24 + 64); }
    SegView v(dir / "x.seg", Kind::g_meta); assert(v.count() == 3 && v.aux() == 42 && v.aux2() == 43 && v.as<uint64_t>()[2] == 3);
    bool threw = false; try { SegView bad(dir / "x.seg", Kind::g_top); } catch (...) { threw = true; } assert(threw);
    threw = false; try { SegWriter w(dir / "y.seg", Kind::g_meta, 8); uint8_t z = 0; w.put(&z, 1); (void)w.finish(1); } catch (...) { threw = true; } assert(threw);
    assert(!fs::exists(dir / "y.seg"));
    assert(segment_file_name(Kind::tt_tok_data, 7) == "tt.tok.data.007.seg" && segment_file_name(Kind::blk_time) == "blk.time.seg");
    for (auto& k : kind_names) assert(kind_from_name(k.name) == k.kind);
    fs::remove_all(dir);
    std::printf("core_dict ok\n");
}
