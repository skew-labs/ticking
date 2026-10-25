// glsynth: synthetic wallet map in the mapping-layer Parquet schema (addresses/, edges/,
// emitter_topics/, token_stats/) with planted ground truth, for scale and accuracy tests.
//   glsynth --out DIR --nodes 5000000 --edges 50000000 [--files 32] [--threads 8] [--seed 1]
// Planted (truth.jsonl): CEX hot wallets + their deposit addresses, V2/V3 pools, ERC-20/721
// tokens, Safe wallets, MEV operator+contract pairs, whales.  Background: heavy-tailed random
// transfers between EOAs / receive-only addresses / contracts with unknown events.
#include "machine/graphloop/arrow_io.hpp"
#include "machine/graphloop/schema.hpp"
#include "machine/graphloop/util.hpp"
#include <nlohmann/json.hpp>
#include <fstream>
#include <iostream>
#include <random>

using namespace machine::graphloop;
using json = nlohmann::json;

namespace {
template <class B, class V> void app(B& b, V v) { need(b.Append(v).ok(), "arrow append"); }
std::shared_ptr<arrow::Array> fin(arrow::ArrayBuilder& b) { std::shared_ptr<arrow::Array> a; need(b.Finish(&a).ok(), "arrow finish"); return a; }
Addr addr_of(uint64_t i, uint64_t seed) { Addr a; uint64_t x = mix64(i * 0x9e3779b97f4a7c15ULL + seed), y = mix64(x ^ 0x5555), z = mix64(y + i);
    std::memcpy(a.b, &x, 8); std::memcpy(a.b + 8, &y, 8); std::memcpy(a.b + 16, &z, 4); a.b[0] |= 0x10; return a; }   // never in the 16-zero-byte system range
enum Kind : uint8_t { k_eoa, k_recv, k_contract, k_hot, k_dep, k_pool2, k_pool3, k_token, k_nft, k_safe, k_mevop, k_mevc, k_whale };
}

int main(int argc, char** argv) {
    try {
        fs::path out; uint64_t N = 5000000, M = 50000000, seed = 1; int files = 32, threads = 8;
        for (int i = 1; i < argc; i++) {
            std::string a = argv[i]; auto val = [&]() -> std::string { need(i + 1 < argc, "missing value"); return argv[++i]; };
            if (a == "--out") out = val(); else if (a == "--nodes") N = std::stoull(val()); else if (a == "--edges") M = std::stoull(val());
            else if (a == "--files") files = std::stoi(val()); else if (a == "--threads") threads = std::stoi(val()); else if (a == "--seed") seed = std::stoull(val());
            else throw std::runtime_error("unknown argument " + a);
        }
        need(!out.empty() && N >= 100000, "usage: glsynth --out DIR --nodes N --edges M");
        for (auto d : {"addresses", "edges", "emitter_topics", "token_stats"}) fs::create_directories(out / d);
        // ---- node kinds (planted structures at fixed id ranges, background elsewhere)
        const uint64_t H = 40, DEP = 400, POOLS = std::max<uint64_t>(2000, N / 2000), TOKENS = std::max<uint64_t>(2000, N / 2000), NFTS = 500, SAFES = 2000, MEV = 50, WHALES = 300;
        std::vector<uint8_t> kind(N, k_eoa);
        uint64_t p = 0; auto range = [&](uint64_t cnt, uint8_t k) { uint64_t b = p; for (uint64_t i = 0; i < cnt; i++) kind[p++] = k; return b; };
        uint64_t hot0 = range(H, k_hot), dep0 = range(H * DEP, k_dep), pool0 = range(POOLS / 2, k_pool2); range(POOLS - POOLS / 2, k_pool3);
        uint64_t tok0 = range(TOKENS, k_token), nft0 = range(NFTS, k_nft); range(SAFES, k_safe); uint64_t mevop0 = range(MEV, k_mevop), mevc0 = range(MEV, k_mevc); range(WHALES, k_whale);
        uint64_t bg0 = p;
        std::mt19937_64 krng(seed);
        for (uint64_t i = bg0; i < N; i++) { uint64_t r = krng() % 100; kind[i] = r < 78 ? k_eoa : r < 97 ? k_recv : k_contract; }
        // ---- edges: planted first, background to fill M
        struct E { uint32_t f, t, asset; uint8_t k; uint32_t n; float amt; };
        auto zipf = [&](std::mt19937_64& r, uint64_t lo, uint64_t cnt, double e) { double u = std::uniform_real_distribution<double>(0, 1)(r); return lo + std::min<uint64_t>(cnt - 1, uint64_t(double(cnt) * std::pow(u, e))); };
        std::vector<std::vector<E>> part; part.resize(size_t(files));
        std::vector<std::atomic<uint32_t>> out_n(N), in_n(N), outd(N), ind(N);
        auto put = [&](size_t f, uint32_t a, uint32_t b, uint32_t asset, uint8_t k, uint32_t n, float amt) { part[f].push_back({a, b, asset, k, n, amt}); out_n[a] += n; in_n[b] += n; outd[a]++; ind[b]++; };
        std::mt19937_64 r(seed * 31 + 7);
        for (uint64_t h = 0; h < H; h++) {
            for (uint64_t d = 0; d < DEP; d++) {
                uint32_t dep = uint32_t(dep0 + h * DEP + d);
                for (int s = 0; s < 1 + int(r() % 3); s++) put(size_t(r() % files), uint32_t(zipf(r, bg0, N - bg0, 1.0)), dep, none, e_native, 1 + uint32_t(r() % 2), 0.5f);
                put(size_t(r() % files), dep, uint32_t(hot0 + h), none, e_native, 1 + uint32_t(r() % 3), 0.5f);
            }
            for (int w = 0; w < 3000; w++) put(size_t(r() % files), uint32_t(hot0 + h), uint32_t(zipf(r, bg0, N - bg0, 1.0)), none, e_native, 1, 0.3f);
        }
        for (uint64_t m = 0; m < MEV; m++) {
            put(size_t(r() % files), uint32_t(mevop0 + m), uint32_t(mevc0 + m), none, e_native, 5000 + uint32_t(r() % 5000), 0.f);
            for (int q = 0; q < 50; q++) put(size_t(r() % files), uint32_t(mevc0 + m), uint32_t(pool0 + r() % POOLS), uint32_t(tok0 + r() % TOKENS), e_erc20, 100, 0.f);
        }
        std::atomic<uint64_t> made{0};
        uint64_t planted = 0; for (auto& pt : part) planted += pt.size();
        uint64_t per = (M > planted ? M - planted : 0) / uint64_t(files);
        parallel_for(size_t(files), threads, [&](size_t b, size_t e, int) {
            for (size_t f = b; f < e; f++) {
                std::mt19937_64 rr(seed * 1000003 + f);
                std::vector<E> local; local.reserve(per);
                for (uint64_t i = 0; i < per; i++) {
                    uint32_t a = uint32_t(zipf(rr, bg0, N - bg0, 1.6)), t = uint32_t(zipf(rr, bg0, N - bg0, 2.5));
                    if (a == t) continue;
                    if (kind[a] == k_recv) a = uint32_t(bg0 + (a - bg0 + 1) % (N - bg0));
                    uint64_t x = rr() % 100; uint8_t k = x < 45 ? e_native : x < 90 ? e_erc20 : x < 97 ? e_erc721 : e_erc1155;
                    uint32_t asset = none;
                    if (k == e_erc20) asset = uint32_t(zipf(rr, tok0, TOKENS, 3.0));
                    if (k == e_erc721 || k == e_erc1155) asset = uint32_t(nft0 + rr() % NFTS);
                    if (k == e_erc20 && x % 7 == 0) t = uint32_t(zipf(rr, pool0, POOLS, 2.0));   // swaps into pools
                    uint32_t n = 1 + uint32_t(std::min(1e6, std::pow(std::uniform_real_distribution<double>(1e-6, 1)(rr), -0.9) - 1));
                    local.push_back({a, t, asset, k, n, float(n) * 0.1f});
                }
                for (auto& x : local) { out_n[x.f] += x.n; in_n[x.t] += x.n; outd[x.f]++; ind[x.t]++; }
                made += local.size();
                part[f].insert(part[f].end(), local.begin(), local.end());
            }
        }, 1);
        // ---- write edges
        static const char* kname[] = {"native_tx", "erc20", "erc721", "erc1155"};
        auto wv = [&](uint32_t v) { Addr a = addr_of(v, seed); return a; };
        parallel_for(size_t(files), threads, [&](size_t b, size_t e, int) {
            for (size_t f = b; f < e; f++) {
                arrow::BinaryBuilder fb, tb, ab; arrow::StringBuilder kb; arrow::UInt64Builder nb, f0, l0; arrow::DoubleBuilder amt;
                for (auto& x : part[f]) {
                    Addr fa = wv(x.f), ta = wv(x.t); app(fb, std::string_view(reinterpret_cast<const char*>(fa.b), 20)); app(tb, std::string_view(reinterpret_cast<const char*>(ta.b), 20));
                    if (x.asset == none) app(ab, std::string_view("native")); else { Addr aa = wv(x.asset); app(ab, std::string_view(reinterpret_cast<const char*>(aa.b), 20)); }
                    app(kb, std::string(kname[x.k])); app(nb, uint64_t(x.n)); app(amt, double(x.amt) * 1e18); app(f0, uint64_t(1)); app(l0, uint64_t(2));
                }
                auto sc = arrow::schema({arrow::field("from", arrow::binary()), arrow::field("to", arrow::binary()), arrow::field("asset", arrow::binary()), arrow::field("kind", arrow::utf8()),
                    arrow::field("n", arrow::uint64()), arrow::field("total_amount", arrow::float64()), arrow::field("first_block", arrow::uint64()), arrow::field("last_block", arrow::uint64())});
                write_parquet_atomic(arrow::Table::Make(sc, {fin(fb), fin(tb), fin(ab), fin(kb), fin(nb), fin(amt), fin(f0), fin(l0)}), out / "edges" / ("part-" + std::to_string(f) + ".parquet"));
                std::vector<E>().swap(part[f]);
            }
        }, 1);
        // ---- topics
        auto T = [](const char* sig) { return keccak256(std::string_view(sig)); };
        Hash t_transfer = T("Transfer(address,address,uint256)"), t_approval = T("Approval(address,address,uint256)"), t_swap2 = T("Swap(address,uint256,uint256,uint256,uint256,address)"),
             t_sync = T("Sync(uint112,uint112)"), t_swap3 = T("Swap(address,address,int256,int256,uint160,uint128,int24)"), t_afa = T("ApprovalForAll(address,address,bool)"),
             t_safe = T("ExecutionSuccess(bytes32,uint256)"), t_safe2 = T("SafeReceived(address,uint256)");
        // ---- addresses + emitter_topics + token_stats (per file, by node id mod files)
        std::ofstream truth(out / "truth.jsonl");
        parallel_for(size_t(files), threads, [&](size_t b, size_t e, int) {
            for (size_t f = b; f < e; f++) {
                std::mt19937_64 rr(seed * 7777 + f);
                arrow::BinaryBuilder ad, cr; arrow::TimestampBuilder ft(arrow::timestamp(arrow::TimeUnit::MICRO), arrow::default_memory_pool()), lt(arrow::timestamp(arrow::TimeUnit::MICRO), arrow::default_memory_pool());
                arrow::UInt64Builder txs, txf, txr, txrf, tto, tti, cp, od, idg, cc, logs; arrow::DoubleBuilder nvs, nvr; arrow::BooleanBuilder isc, ovf;
                arrow::BinaryBuilder ea, et; arrow::UInt64Builder en; arrow::BinaryBuilder tk; arrow::StringBuilder ts; arrow::UInt64Builder tn, th;
                for (uint64_t v = f; v < N; v += uint64_t(files)) {
                    Addr a = wv(uint32_t(v)); uint8_t k = kind[v];
                    uint64_t on = out_n[v], inn = in_n[v], odv = outd[v], idv = ind[v];
                    bool contract = k == k_contract || k == k_pool2 || k == k_pool3 || k == k_token || k == k_nft || k == k_safe || k == k_mevc;
                    uint64_t sent = contract || k == k_recv ? 0 : on, failed = k == k_mevop ? sent / 3 : sent / 50;
                    double eth_o = k == k_whale ? 5000 : double(on) * 0.05, eth_i = k == k_whale ? 6000 : double(inn) * 0.05;
                    app(ad, std::string_view(reinterpret_cast<const char*>(a.b), 20));
                    app(ft, int64_t(1700000000) * 1000000); app(lt, int64_t(1750000000) * 1000000);
                    app(txs, sent); app(txf, failed); app(txr, inn); app(txrf, 0ull); app(nvs, eth_o * 1e18); app(nvr, eth_i * 1e18); app(ovf, false);
                    app(tto, contract ? on : on / 2); app(tti, inn / 2); app(cp, odv + idv); app(od, odv); app(idg, idv);
                    app(isc, contract && (rr() % 3 == 0)); app(cc, 0ull);
                    uint64_t lg = 0;
                    auto topic = [&](const Hash& t, uint64_t n) { app(ea, std::string_view(reinterpret_cast<const char*>(a.b), 20)); app(et, std::string_view(reinterpret_cast<const char*>(t.data()), 32)); app(en, n); lg += n; };
                    if (k == k_token) { topic(t_transfer, 1000 + inn); topic(t_approval, 100); app(tk, std::string_view(reinterpret_cast<const char*>(a.b), 20)); app(ts, std::string("erc20")); app(tn, inn + 1000); app(th, 100 + rr() % 10000); }
                    if (k == k_nft) { topic(t_transfer, 500 + inn); topic(t_afa, 50); app(tk, std::string_view(reinterpret_cast<const char*>(a.b), 20)); app(ts, std::string("erc721")); app(tn, inn + 500); app(th, 100); }
                    if (k == k_pool2) { topic(t_swap2, 1000 + inn); topic(t_sync, 1000 + inn); topic(t_transfer, 50); }
                    if (k == k_pool3) { topic(t_swap3, 1000 + inn); }
                    if (k == k_safe) { topic(t_safe, 20); topic(t_safe2, 5); }
                    if (k == k_contract) { Hash u = keccak256(std::string_view(std::to_string(v % 997) + "UnknownEvent()")); topic(u, 1 + rr() % 1000); }
                    app(logs, lg); app(cr, std::string_view());
                    static const char* names[] = {"eoa_user", "eoa_user", "contract_other", "cex_hot_wallet", "cex_deposit", "dex_pool", "dex_pool", "token_contract", "nft_contract", "multisig_or_safe", "mev_bot", "mev_bot", "eoa_whale"};
                    if (v < bg0) { std::lock_guard<std::mutex> l(*[]{ static std::mutex m; return &m; }()); truth << json({{"address", hex0x(a)}, {"category", names[k]}}).dump() << "\n"; }
                }
                auto sc = arrow::schema({arrow::field("address", arrow::binary()), arrow::field("first_time", arrow::timestamp(arrow::TimeUnit::MICRO)), arrow::field("last_time", arrow::timestamp(arrow::TimeUnit::MICRO)),
                    arrow::field("tx_sent", arrow::uint64()), arrow::field("tx_sent_failed", arrow::uint64()), arrow::field("tx_received", arrow::uint64()), arrow::field("tx_received_failed", arrow::uint64()),
                    arrow::field("native_value_sent", arrow::float64()), arrow::field("native_value_received", arrow::float64()), arrow::field("native_value_overflow", arrow::boolean()),
                    arrow::field("token_transfers_out", arrow::uint64()), arrow::field("token_transfers_in", arrow::uint64()), arrow::field("distinct_counterparties", arrow::uint64()),
                    arrow::field("out_degree", arrow::uint64()), arrow::field("in_degree", arrow::uint64()), arrow::field("is_contract", arrow::boolean()), arrow::field("contracts_created", arrow::uint64()),
                    arrow::field("logs_emitted", arrow::uint64()), arrow::field("creator", arrow::binary())});
                write_parquet_atomic(arrow::Table::Make(sc, {fin(ad), fin(ft), fin(lt), fin(txs), fin(txf), fin(txr), fin(txrf), fin(nvs), fin(nvr), fin(ovf), fin(tto), fin(tti), fin(cp), fin(od), fin(idg), fin(isc), fin(cc), fin(logs), fin(cr)}),
                                     out / "addresses" / ("part-" + std::to_string(f) + ".parquet"));
                auto s2 = arrow::schema({arrow::field("address", arrow::binary()), arrow::field("topic0", arrow::binary()), arrow::field("n", arrow::uint64())});
                write_parquet_atomic(arrow::Table::Make(s2, {fin(ea), fin(et), fin(en)}), out / "emitter_topics" / ("part-" + std::to_string(f) + ".parquet"));
                auto s3 = arrow::schema({arrow::field("token", arrow::binary()), arrow::field("standard", arrow::utf8()), arrow::field("n_transfers", arrow::uint64()), arrow::field("n_holders_approx", arrow::uint64())});
                write_parquet_atomic(arrow::Table::Make(s3, {fin(tk), fin(ts), fin(tn), fin(th)}), out / "token_stats" / ("part-" + std::to_string(f) + ".parquet"));
            }
        }, 1);
        std::cout << json({{"nodes", N}, {"edges_written", made.load() + planted}, {"planted_nodes", bg0}, {"files", files}}).dump() << "\n";
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << "\n"; return 1; }
}
