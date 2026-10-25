// glbuild: mapping-layer Parquet (+ econ layer, + protocol registry) -> immutable graph image.
//   glbuild --chain robinhood --chain-id 4663 --map /data/mapping/rh20m --registry DIR --out DIR
//           [--econ DIR] [--threads 8] [--cpus 0-15] [--eth-usd 2500] [--no-dex-trades]
// Output: <out>/graph-<id12>/ (segments + manifest.json, published by directory rename) and a
// JSON build report on stdout.  Restartable: a failed build leaves only <out>/.build-* temp dirs.
#include "machine/graphloop/arrow_io.hpp"
#include "machine/graphloop/classify.hpp"
#include <iostream>
#include <map>
#include <unordered_set>

using namespace machine::graphloop;

namespace {
struct Opt {
    fs::path map, econ, registry, out; std::string chain; uint64_t chain_id{}; int threads = 8; std::vector<int> cpus;
    double eth_usd = 2500; bool dex_trades = true; int dex_threads = 4; ClassifyParams cls;
};

struct Timer { uint64_t t0 = mono_ns(); json* j; const char* name; Timer(json& o, const char* n) : j(&o), name(n) {} ~Timer() { (*j)[name] = double(mono_ns() - t0) / 1e9; } };

struct ARow {
    Addr a; uint32_t first, last, tx_sent, tx_fail, tx_recv, rx_fail, tt_out, tt_in, cp, outdeg, indeg, created, logs, dex;
    float eth_out, eth_in; Addr creator; uint8_t has_creator, is_contract, ovf, pad;
};
inline uint32_t sat32(uint64_t x) { return x > 0xffffffffull ? 0xffffffffu : uint32_t(x); }

struct EdgeRec { uint32_t s, d, asset; uint8_t t, pad[3]; uint64_t n; float usd, pad2; };
static_assert(sizeof(EdgeRec) == 32);

struct Build {
    Opt o; json report = json::object(), timing = json::object(); fs::path tmp;
    uint32_t n = 0; std::vector<Addr> addr; std::vector<uint32_t> prefix; uint32_t prefix_bits = 0;
    std::array<std::vector<uint32_t>, dense_u32_count> u32; std::array<std::vector<float>, dense_f32_count> f32; std::array<std::vector<uint16_t>, dense_u16_count> u16;
    std::vector<SparseEntry> sparse; Strings strings; std::vector<BaseLabel> preset;
    std::vector<Addr> creator_addr; std::vector<uint8_t> has_creator;
    std::vector<uint64_t> topics_bits; std::vector<float> usd_tok;
    json files = json::object();

    uint32_t find(const Addr& a) const {
        uint32_t p = prefix_bits ? uint32_t(((uint32_t(a.b[0]) << 24) | (uint32_t(a.b[1]) << 16) | (uint32_t(a.b[2]) << 8) | a.b[3]) >> (32 - prefix_bits)) : 0;
        uint32_t lo = prefix[p], hi = prefix[p + 1];
        while (lo < hi) { uint32_t mid = lo + (hi - lo) / 2; int c = std::memcmp(addr[mid].b, a.b, 20); if (c == 0) return mid; if (c < 0) lo = mid + 1; else hi = mid; }
        return none;
    }
    template <class T> void seg(const std::string& name, std::span<const T> data, uint64_t aux = 0) {
        SegmentWriter w(tmp / (name + ".seg"), sizeof(T), data.size(), aux);
        w.put(data.data(), data.size_bytes());
        auto [bytes, sha] = w.finish();
        files[name + ".seg"] = {{"bytes", bytes}, {"sha256", sha}, {"elem", sizeof(T)}, {"count", data.size()}};
    }
    template <class T> void seg(const std::string& name, const std::vector<T>& v, uint64_t aux = 0) { seg(name, std::span<const T>(v.data(), v.size()), aux); }

    // ------------------------------------------------------------ phase A: addresses
    void addresses() {
        Timer tm(timing, "addresses_s");
        auto fl = list_files(o.map / "addresses", ".parquet", false);
        need(!fl.empty(), "no addresses/*.parquet under map dir");
        std::vector<std::vector<ARow>> parts(fl.size());
        parallel_for(fl.size(), o.threads, [&](size_t b, size_t e, int) {
            for (size_t fi = b; fi < e; fi++) {
                PqReader r(fl[fi]);
                std::vector<int> cols{r.col("address"), r.col("first_time"), r.col("last_time"), r.col("tx_sent"), r.col("tx_sent_failed"),
                    r.col("tx_received"), r.col("tx_received_failed"), r.col("native_value_sent"), r.col("native_value_received"),
                    r.col("native_value_overflow"), r.col("token_transfers_out"), r.col("token_transfers_in"), r.col("distinct_counterparties"),
                    r.col("out_degree"), r.col("in_degree"), r.col("is_contract"), r.col("creator"), r.col("contracts_created"),
                    r.col("logs_emitted"), r.col("dex_trades")};
                need(cols[0] >= 0, "addresses.address column");
                std::vector<Col> c; auto& out = parts[fi];
                for (int rg = 0; rg < r.row_groups(); rg++) {
                    int64_t rows = r.read(rg, cols, c);
                    for (int64_t i = 0; i < rows; i++) {
                        ARow a{}; if (!c[0].addr(i, a.a)) continue;
                        a.first = uint32_t(c[1].ts_s(i)); a.last = uint32_t(c[2].ts_s(i));
                        a.tx_sent = sat32(c[3].u64(i)); a.tx_fail = sat32(c[4].u64(i)); a.tx_recv = sat32(c[5].u64(i)); a.rx_fail = sat32(c[6].u64(i));
                        a.eth_out = float(c[7].f64(i) / 1e18); a.eth_in = float(c[8].f64(i) / 1e18); a.ovf = c[9] ? c[9].b(i) : 0;
                        a.tt_out = sat32(c[10].u64(i)); a.tt_in = sat32(c[11].u64(i)); a.cp = sat32(c[12].u64(i));
                        a.outdeg = sat32(c[13].u64(i)); a.indeg = sat32(c[14].u64(i)); a.is_contract = c[15] ? c[15].b(i) : 0;
                        a.has_creator = c[16] && c[16].addr(i, a.creator); a.created = sat32(c[17].u64(i)); a.logs = sat32(c[18].u64(i));
                        a.dex = c[19] ? sat32(c[19].u64(i)) : 0;
                        out.push_back(a);
                    }
                }
            }
        }, 1, &o.cpus);
        size_t total = 0; for (auto& p : parts) total += p.size();
        // bucket by first address byte, then sort buckets in parallel (addresses are keccak-uniform)
        std::vector<std::vector<std::pair<Addr, uint64_t>>> bk(256);
        {
            std::array<size_t, 256> cnt{};
            for (auto& p : parts) for (auto& a : p) cnt[a.a.b[0]]++;
            for (int i = 0; i < 256; i++) bk[i].reserve(cnt[i]);
            for (size_t pi = 0; pi < parts.size(); pi++) for (size_t i = 0; i < parts[pi].size(); i++) bk[parts[pi][i].a.b[0]].push_back({parts[pi][i].a, (uint64_t(pi) << 40) | i});
        }
        parallel_for(256, o.threads, [&](size_t b, size_t e, int) {
            for (size_t i = b; i < e; i++) std::sort(bk[i].begin(), bk[i].end(), [](auto& x, auto& y) { int c = std::memcmp(x.first.b, y.first.b, 20); return c < 0 || (c == 0 && x.second < y.second); });
        }, 1, &o.cpus);
        addr.reserve(total); std::vector<uint64_t> src; src.reserve(total); uint64_t dups = 0;
        for (auto& b : bk) { for (auto& [a, s] : b) { if (!addr.empty() && addr.back() == a) { dups++; continue; } addr.push_back(a); src.push_back(s); } std::vector<std::pair<Addr, uint64_t>>().swap(b); }
        need(addr.size() < 0xffffffffull, "too many addresses for 32-bit node ids");
        n = uint32_t(addr.size());
        for (auto& v : u32) v.assign(n, 0);
        for (auto& v : f32) v.assign(n, 0.f);
        for (auto& v : u16) v.assign(n, 0);
        creator_addr.assign(n, Addr{}); has_creator.assign(n, 0); preset.assign(n, BaseLabel{unknown, 0, s_none, 0, 0, 0, 0, 0});
        std::vector<uint32_t> logs(n), created(n), dex(n), rxf(n);
        parallel_for(n, o.threads, [&](size_t b, size_t e, int) {
            for (size_t v = b; v < e; v++) {
                const ARow& a = parts[src[v] >> 40][src[v] & ((1ull << 40) - 1)];
                u32[d_tx_sent][v] = a.tx_sent; u32[d_tx_recv][v] = a.tx_recv; u32[d_tx_fail][v] = a.tx_fail; u32[d_tt_out][v] = a.tt_out;
                u32[d_tt_in][v] = a.tt_in; u32[d_cp][v] = a.cp; u32[d_outdeg][v] = a.outdeg; u32[d_indeg][v] = a.indeg;
                u32[d_first][v] = a.first; u32[d_last][v] = a.last;
                f32[d_eth_out][v] = a.eth_out; f32[d_eth_in][v] = a.eth_in;
                uint32_t fl = 0;
                if (a.is_contract) fl |= nf_created_tx | nf_contract;
                if (a.logs) fl |= nf_emits | nf_contract;
                if (a.tx_sent) fl |= nf_sends;
                if (a.created) fl |= nf_deployer;
                if (a.ovf) fl |= nf_native_ovf;
                bool zero16 = true; for (int i = 0; i < 16; i++) zero16 &= a.a.b[i] == 0;
                if (zero16) fl |= nf_system;   // 0x0, precompiles (0x...64..), ArbOS 0x...0a4b05
                u32[d_flags][v] = fl;
                logs[v] = a.logs; created[v] = a.created; dex[v] = a.dex; rxf[v] = a.rx_fail;
                if (a.has_creator) { creator_addr[v] = a.creator; has_creator[v] = 1; }
            }
        }, 1 << 15, &o.cpus);
        std::vector<std::vector<ARow>>().swap(parts);
        // radix prefix index
        int lg = 0; while ((1ull << (lg + 1)) <= n) lg++;
        prefix_bits = uint32_t(std::clamp(lg - 3, 4, 24));
        prefix.assign((size_t(1) << prefix_bits) + 1, 0);
        { size_t j = 0; for (size_t p = 0; p < (size_t(1) << prefix_bits); p++) {
            prefix[p] = uint32_t(j);
            while (j < n && (((uint32_t(addr[j].b[0]) << 24) | (uint32_t(addr[j].b[1]) << 16) | (uint32_t(addr[j].b[2]) << 8) | addr[j].b[3]) >> (32 - prefix_bits)) == p) j++;
        } prefix[size_t(1) << prefix_bits] = n; }
        for (uint32_t v = 0; v < n; v++) {
            if (logs[v]) sparse.push_back({v, logs[v], k_logs, {}});
            if (created[v]) sparse.push_back({v, created[v], k_created, {}});
            if (dex[v]) sparse.push_back({v, dex[v], k_dex, {}});
            if (rxf[v]) sparse.push_back({v, rxf[v], k_rx_fail, {}});
        }
        uint64_t creators_resolved = 0;
        for (uint32_t v = 0; v < n; v++) if (has_creator[v]) { uint32_t c = find(creator_addr[v]); if (c != none) { sparse.push_back({v, c, k_creator, {}}); creators_resolved++; } }
        std::vector<Addr>().swap(creator_addr);
        report["addresses"] = {{"rows", total}, {"nodes", n}, {"duplicates", dups}, {"files", fl.size()}, {"prefix_bits", prefix_bits}, {"creators_resolved", creators_resolved}};
    }

    // ------------------------------------------------------------ topic registry
    struct TopicInfo { uint64_t bit; std::string name; };
    std::map<Hash, TopicInfo> known;   // topic0 -> family bit + display name
    void registry_topics() {
        auto t = keccak256(std::string_view("Transfer(address,address,uint256)"));
        need(hex(View(t.data(), 32)) == "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", "keccak self-test (CryptoPP Keccak_256)");
        for (auto& e : known_events) {
            Hash h{};
            if (!e.sig.empty()) h = keccak256(e.sig); else need(unhex_exact(e.topic0_hex, h.data(), 32), "builtin topic hex");
            known.emplace(h, TopicInfo{e.bit, std::string(e.name)});
        }
        uint64_t ev = 0, dict = 0;
        auto evp = o.registry / "events.csv";
        if (fs::exists(evp)) csv_rows(evp, [&](auto& h, auto& f) {
            Hash t{}; if (!unhex_exact(csv_get(h, f, "topic0"), t.data(), 32)) return;
            auto fam = csv_get(h, f, "family"); auto canon = csv_get(h, f, "canonical");
            if (!canon.empty()) { auto k = keccak256(canon); if (k != t) return; }   // hash-check the registry itself
            if (known.emplace(t, TopicInfo{family_topic_bit(fam), csv_get(h, f, "event") + "(" + fam + ")"}).second) ev++;
        });
        auto dp = o.registry / "topic_dict.csv";
        if (fs::exists(dp)) csv_rows(dp, [&](auto& h, auto& f) {
            Hash t{}; if (!unhex_exact(csv_get(h, f, "t0"), t.data(), 32)) return;
            auto sig = csv_get(h, f, "signature");
            if (!sig.empty() && known.emplace(t, TopicInfo{tf_known_other, sig}).second) dict++;
        });
        report["topic_registry"] = {{"builtin", std::size(known_events)}, {"events_csv", ev}, {"topic_dict", dict}, {"total", known.size()}};
    }

    // ------------------------------------------------------------ phase B: emitted topics
    void emitter_topics() {
        Timer tm(timing, "emitter_topics_s");
        struct TRow { uint32_t v; uint32_t n; Hash t; };
        auto fl = list_files(o.map / "emitter_topics", ".parquet", false);
        std::vector<std::vector<TRow>> parts(fl.size());
        parallel_for(fl.size(), o.threads, [&](size_t b, size_t e, int) {
            for (size_t fi = b; fi < e; fi++) {
                PqReader r(fl[fi]); std::vector<int> cols{r.col("address"), r.col("topic0"), r.col("n")}; std::vector<Col> c;
                for (int rg = 0; rg < r.row_groups(); rg++) {
                    int64_t rows = r.read(rg, cols, c);
                    for (int64_t i = 0; i < rows; i++) {
                        Addr a; if (!c[0].addr(i, a)) continue; auto tb = c[1].bytes(i); if (tb.size() != 32) continue;
                        uint32_t v = find(a); if (v == none) continue;
                        TRow t{v, sat32(c[2].u64(i)), {}}; std::memcpy(t.t.data(), tb.data(), 32); parts[fi].push_back(t);
                    }
                }
            }
        }, 1, &o.cpus);
        std::vector<TRow> rows; for (auto& p : parts) rows.insert(rows.end(), p.begin(), p.end());
        std::vector<Hash> dict; dict.reserve(rows.size()); for (auto& r : rows) dict.push_back(r.t);
        std::sort(dict.begin(), dict.end()); dict.erase(std::unique(dict.begin(), dict.end()), dict.end());
        auto tid = [&](const Hash& h) { return uint32_t(std::lower_bound(dict.begin(), dict.end(), h) - dict.begin()); };
        std::vector<uint64_t> tbits(dict.size()); std::vector<uint32_t> tname(dict.size(), none);
        uint64_t named = 0;
        for (size_t i = 0; i < dict.size(); i++) {
            auto it = known.find(dict[i]);
            if (it != known.end()) { tbits[i] = it->second.bit; tname[i] = strings.id(it->second.name); named++; } else tbits[i] = tf_unknown;
        }
        std::sort(rows.begin(), rows.end(), [](const TRow& a, const TRow& b) { return a.v < b.v || (a.v == b.v && (a.n > b.n || (a.n == b.n && a.t < b.t))); });
        std::vector<uint32_t> idx, ttopic, tn; std::vector<uint64_t> off{0};
        topics_bits.assign(n, 0);
        for (size_t i = 0; i < rows.size();) {
            size_t j = i; uint64_t bits = 0;
            while (j < rows.size() && rows[j].v == rows[i].v) { uint32_t t = tid(rows[j].t); ttopic.push_back(t); tn.push_back(rows[j].n); bits |= tbits[t]; j++; }
            idx.push_back(rows[i].v); off.push_back(ttopic.size()); topics_bits[rows[i].v] = bits;
            u32[d_flags][rows[i].v] |= nf_emits | nf_contract;
            i = j;
        }
        // reverse: topic -> emitters (for graph-loop re-enqueue when a protocol mapping is learned)
        std::vector<uint64_t> te_off(dict.size() + 1, 0); for (auto t : ttopic) te_off[t + 1]++;
        for (size_t i = 0; i < dict.size(); i++) te_off[i + 1] += te_off[i];
        std::vector<uint32_t> te(ttopic.size()); { auto cur = te_off; for (size_t k = 0; k < idx.size(); k++) for (uint64_t q = off[k]; q < off[k + 1]; q++) te[cur[ttopic[q]]++] = idx[k]; }
        for (uint32_t v = 0; v < n; v++) if (topics_bits[v]) { sparse.push_back({v, uint32_t(topics_bits[v]), k_topics_lo, {}}); if (topics_bits[v] >> 32) sparse.push_back({v, uint32_t(topics_bits[v] >> 32), k_topics_hi, {}}); }
        seg("nt_idx", idx); seg("nt_off", off); seg("nt_topic", ttopic); seg("nt_n", tn);
        seg("topic", dict); seg("topic_bits", tbits); seg("topic_name", tname); seg("te_off", te_off); seg("te_node", te);
        report["emitter_topics"] = {{"rows", rows.size()}, {"emitters", idx.size()}, {"distinct_topics", dict.size()}, {"named_topics", named}};
    }

    // ------------------------------------------------------------ phase C/D: tokens, pools
    void tokens_pools() {
        Timer tm(timing, "tokens_pools_s");
        uint64_t toks = 0, pools = 0, venues = 0;
        for (auto& f : list_files(o.map / "token_stats", ".parquet", false)) {
            PqReader r(f); std::vector<int> cols{r.col("token"), r.col("standard"), r.col("n_transfers"), r.col("n_holders_approx")}; std::vector<Col> c;
            for (int rg = 0; rg < r.row_groups(); rg++) {
                int64_t rows = r.read(rg, cols, c);
                for (int64_t i = 0; i < rows; i++) {
                    Addr a; if (!c[0].addr(i, a)) continue; uint32_t v = find(a); if (v == none) continue;
                    auto st = c[1].bytes(i); uint32_t sb = 0;
                    if (st.find("erc20") != st.npos) { u32[d_flags][v] |= nf_token20; sb |= 1; }
                    if (st.find("erc721") != st.npos) { u32[d_flags][v] |= nf_token721; sb |= 2; }
                    if (st.find("erc1155") != st.npos) { u32[d_flags][v] |= nf_token1155; sb |= 4; }
                    u32[d_flags][v] |= nf_contract;
                    sparse.push_back({v, sb, k_tok_std, {}}); sparse.push_back({v, sat32(c[2].u64(i)), k_tok_transfers, {}});
                    sparse.push_back({v, sat32(c[3].u64(i)), k_tok_holders, {}}); toks++;
                }
            }
        }
        for (auto& f : list_files(o.map / "dex_pools", ".parquet", false)) {
            PqReader r(f); std::vector<int> cols{r.col("pool"), r.col("protocol"), r.col("venue"), r.col("n_swaps")}; std::vector<Col> c;
            std::unordered_set<uint32_t> seen_pool, seen_venue;
            for (int rg = 0; rg < r.row_groups(); rg++) {
                int64_t rows = r.read(rg, cols, c);
                for (int64_t i = 0; i < rows; i++) {
                    std::string proto(c[1].bytes(i)); uint32_t sid = strings.id(proto);
                    Addr a;
                    if (c[0].addr(i, a)) { uint32_t v = find(a); if (v != none && seen_pool.insert(v).second) { u32[d_flags][v] |= nf_pool | nf_contract; sparse.push_back({v, sid, k_pool_proto, {}}); sparse.push_back({v, sat32(c[3].u64(i)), k_pool_trades, {}}); pools++; } }
                    if (c[2].addr(i, a)) { uint32_t v = find(a); if (v != none && seen_venue.insert(v).second && !seen_pool.count(v)) { u32[d_flags][v] |= nf_venue | nf_contract; sparse.push_back({v, sid, k_pool_proto, {}}); venues++; } }
                }
            }
        }
        report["tokens_pools"] = {{"tokens", toks}, {"pools", pools}, {"venues", venues}};
    }

    // ------------------------------------------------------------ phase E: dex trades (routers + MEV scan)
    void dex_trades() {
        Timer tm(timing, "dex_trades_s");
        auto fl = list_files(o.map / "dex_trades", ".parquet", true);
        if (!o.dex_trades || fl.empty()) { report["dex_trades"] = {{"files", 0}}; return; }
        std::vector<std::atomic<uint32_t>> router(n), sandwich(n), arb(n);
        std::atomic<uint64_t> trades{0}, sw_found{0}, arb_found{0};
        struct T { uint64_t blk; uint32_t txi, logi; uint64_t pool; uint32_t trader, to, tin, tout; };
        parallel_for(fl.size(), o.threads, [&](size_t b, size_t e, int) {
            for (size_t fi = b; fi < e; fi++) {
                PqReader r(fl[fi]);
                std::vector<int> cols{r.col("block_number"), r.col("tx_index"), r.col("log_index"), r.col("pool"), r.col("trader"), r.col("tx_to"),
                                      r.col("token_in"), r.col("token_out"), r.col("router")};
                std::vector<Col> c; std::vector<T> ts;
                for (int rg = 0; rg < r.row_groups(); rg++) {
                    int64_t rows = r.read(rg, cols, c);
                    for (int64_t i = 0; i < rows; i++) {
                        Addr a; T t{c[0].u64(i), uint32_t(c[1].u64(i)), uint32_t(c[2].u64(i)), 0, none, none, none, none};
                        auto pb = c[3].bytes(i); t.pool = fnv1a(pb.data(), pb.size());
                        if (c[4].addr(i, a)) t.trader = find(a);
                        if (c[5].addr(i, a)) t.to = find(a);
                        if (c[6].addr(i, a)) t.tin = find(a);
                        if (c[7].addr(i, a)) t.tout = find(a);
                        if (c[8].addr(i, a)) { uint32_t rv = find(a); if (rv != none) router[rv].fetch_add(1, std::memory_order_relaxed); }
                        ts.push_back(t);
                    }
                }
                trades += ts.size();
                // cyclic arbitrage: one tx, >= 2 swaps, first token_in == last token_out
                std::sort(ts.begin(), ts.end(), [](const T& x, const T& y) { return x.blk < y.blk || (x.blk == y.blk && (x.txi < y.txi || (x.txi == y.txi && x.logi < y.logi))); });
                for (size_t i = 0; i < ts.size();) {
                    size_t j = i; while (j < ts.size() && ts[j].blk == ts[i].blk && ts[j].txi == ts[i].txi) j++;
                    if (j - i >= 2 && ts[i].tin != none && ts[i].tin == ts[j - 1].tout) {
                        if (ts[i].trader != none) arb[ts[i].trader].fetch_add(1, std::memory_order_relaxed);
                        if (ts[i].to != none && ts[i].to != ts[i].trader) arb[ts[i].to].fetch_add(1, std::memory_order_relaxed);
                        arb_found++;
                    }
                    i = j;
                }
                // sandwich: same pool+block, X buys (tx a) -> victim Y same direction (tx b) -> X sells (tx c), a<b<c
                std::sort(ts.begin(), ts.end(), [](const T& x, const T& y) { return x.pool < y.pool || (x.pool == y.pool && (x.blk < y.blk || (x.blk == y.blk && (x.txi < y.txi || (x.txi == y.txi && x.logi < y.logi))))); });
                for (size_t i = 0; i < ts.size();) {
                    size_t j = i; while (j < ts.size() && ts[j].pool == ts[i].pool && ts[j].blk == ts[i].blk) j++;
                    if (j - i >= 3 && j - i <= 64) {
                        for (size_t a = i; a < j; a++) {
                            if (ts[a].trader == none) continue;
                            bool hit = false;
                            for (size_t cc = a + 2; cc < j && !hit; cc++) {
                                if (ts[cc].trader != ts[a].trader || ts[cc].txi == ts[a].txi || ts[cc].tin != ts[a].tout || ts[cc].tin == none) continue;
                                for (size_t bb = a + 1; bb < cc; bb++)
                                    if (ts[bb].trader != ts[a].trader && ts[bb].txi > ts[a].txi && ts[bb].txi < ts[cc].txi && ts[bb].tin == ts[a].tin) { hit = true; break; }
                            }
                            if (hit) {
                                sandwich[ts[a].trader].fetch_add(1, std::memory_order_relaxed);
                                if (ts[a].to != none && ts[a].to != ts[a].trader) sandwich[ts[a].to].fetch_add(1, std::memory_order_relaxed);
                                sw_found++;
                            }
                        }
                    }
                    i = j;
                }
            }
        }, 1, &o.cpus, o.dex_threads);
        uint64_t routers = 0;
        for (uint32_t v = 0; v < n; v++) {
            uint32_t rt = router[v].load(), s = sandwich[v].load(), a = arb[v].load();
            if (rt) { sparse.push_back({v, rt, k_router, {}}); if (rt >= 50) { u32[d_flags][v] |= nf_router | nf_contract; routers++; } }
            if (s) { sparse.push_back({v, s, k_sandwich, {}}); u32[d_flags][v] |= nf_sandwich; }
            if (a) { sparse.push_back({v, a, k_arb, {}}); u32[d_flags][v] |= nf_cyc_arb; }
        }
        report["dex_trades"] = {{"files", fl.size()}, {"trades", trades.load()}, {"routers", routers}, {"sandwich_legs", sw_found.load()}, {"cyclic_arb_txs", arb_found.load()}};
    }

    // ------------------------------------------------------------ phase G: protocol registry
    std::vector<uint32_t> stable_dec;   // node -> decimals (0 = not a stablecoin)
    uint32_t weth = none;
    void registry() {
        Timer tm(timing, "registry_s");
        stable_dec.assign(n, 0);
        auto p = o.registry / "contracts.csv"; uint64_t hit = 0, miss = 0;
        static const std::map<std::string, uint32_t> dec = {{"USDC", 6}, {"USDC.e", 6}, {"USDT", 6}, {"USDT0", 6}, {"PYUSD", 6}, {"USDG", 6}, {"DAI", 18}, {"frxUSD", 18}, {"USDe", 18}, {"sUSDe", 18}, {"USDS", 18}};
        if (fs::exists(p)) csv_rows(p, [&](auto& h, auto& f) {
            if (csv_get(h, f, "chain") != std::to_string(o.chain_id)) return;
            Addr a; if (!parse_addr(csv_get(h, f, "address"), a)) return;
            uint32_t v = find(a); if (v == none) { miss++; return; }
            auto fam = csv_get(h, f, "family"), role = csv_get(h, f, "role"), proto = csv_get(h, f, "protocol");
            auto fc = family_class(fam, role);
            preset[v] = BaseLabel{fc.category, conf8(0.99), s_registry, fc.subtype, 0, r_registry, 0, 0};
            u32[d_flags][v] |= nf_registry | nf_contract;
            sparse.push_back({v, strings.id(proto + ":" + role), k_entity, {}});
            sparse.push_back({v, strings.id(fam), k_role, {}});
            if (fam == "weth") { u32[d_flags][v] |= nf_weth; weth = v; }
            if (fam == "stable" && role.rfind("token:", 0) == 0) {
                auto sym = role.substr(6); auto it = dec.find(sym); stable_dec[v] = it == dec.end() ? 18 : it->second; u32[d_flags][v] |= nf_stable;
                sparse.push_back({v, strings.id(sym), k_symbol, {}});
            }
            hit++;
        });
        report["registry"] = {{"contracts_in_graph", hit}, {"contracts_not_in_graph", miss}};
    }

    // ------------------------------------------------------------ phase F: econ actions -> typed edges
    std::vector<EdgeRec> econ_edges() {
        Timer tm(timing, "econ_s");
        std::vector<EdgeRec> out;
        auto fl = o.econ.empty() ? std::vector<fs::path>{} : list_files(o.econ / "econ_actions", ".parquet", true);
        if (fl.empty()) { report["econ"] = {{"files", 0}}; return out; }
        std::vector<std::vector<EdgeRec>> parts(fl.size()); std::atomic<uint64_t> rows_all{0}, unresolved{0};
        parallel_for(fl.size(), o.threads, [&](size_t b, size_t e, int) {
            for (size_t fi = b; fi < e; fi++) {
                PqReader r(fl[fi]); std::vector<int> cols{r.col("wallet"), r.col("action"), r.col("counterparty_contract"), r.col("emitter")}; std::vector<Col> c;
                std::unordered_map<uint64_t, uint64_t> agg;   // (s<<32|d)<<8|type -> n  (aggregated per file)
                std::unordered_map<uint64_t, uint32_t> keyd;
                for (int rg = 0; rg < r.row_groups(); rg++) {
                    int64_t rows = r.read(rg, cols, c); rows_all += rows;
                    for (int64_t i = 0; i < rows; i++) {
                        Addr w, k; if (!c[0].addr(i, w)) continue;
                        if (!c[2].addr(i, k) && !c[3].addr(i, k)) continue;
                        int t = econ_action_edge(c[1].bytes(i)); if (t < 0) continue;
                        uint32_t s = find(w), d = find(k); if (s == none || d == none || s == d) { unresolved++; continue; }
                        auto act = c[1].bytes(i);
                        bool inflow = act == "withdraw" || act == "borrow" || act == "lp_remove" || act == "bridge_in" || act == "unstake" || act == "unwrap" || act == "perp_decrease";
                        if (inflow) std::swap(s, d);   // money direction: protocol -> wallet
                        agg[(uint64_t(s) << 32 | d) * 16 + uint64_t(t)]++;
                    }
                }
                for (auto& [key, cnt] : agg) { uint64_t sd = key / 16; parts[fi].push_back({uint32_t(sd >> 32), uint32_t(sd), none, uint8_t((key % 16) | ef_econ), {}, cnt, 0.f, 0.f}); }
            }
        }, 1, &o.cpus);
        for (auto& p : parts) out.insert(out.end(), p.begin(), p.end());
        for (auto& e : out) {
            bool wallet_src = !(u32[d_flags][e.s] & nf_contract);
            if (wallet_src) { u32[d_flags][e.s] |= nf_econ_wallet; u32[d_flags][e.d] |= nf_econ_core; } else { u32[d_flags][e.d] |= nf_econ_wallet; u32[d_flags][e.s] |= nf_econ_core; }
        }
        report["econ"] = {{"files", fl.size()}, {"actions", rows_all.load()}, {"typed_edges", out.size()}, {"unresolved", unresolved.load()}};
        return out;
    }

    // ------------------------------------------------------------ phase H: edges -> CSR
    void edges(std::vector<EdgeRec> econ) {
        Timer tm(timing, "edges_s");
        const uint64_t n_econ = econ.size();
        auto fl = list_files(o.map / "edges", ".parquet", false);
        need(!fl.empty(), "no edges/*.parquet under map dir");
        std::vector<std::vector<EdgeRec>> parts(fl.size()); std::atomic<uint64_t> rows_all{0}, unresolved{0}, selfe{0}, unknown_kind{0};
        usd_tok.assign(n, 0.f);
        parallel_for(fl.size(), o.threads, [&](size_t b, size_t e, int) {
            for (size_t fi = b; fi < e; fi++) {
                PqReader r(fl[fi]);
                std::vector<int> cols{r.col("from"), r.col("to"), r.col("asset"), r.col("kind"), r.col("n"), r.col("total_amount"), r.col("total_amount_big")};
                std::vector<Col> c; auto& out = parts[fi];
                for (int rg = 0; rg < r.row_groups(); rg++) {
                    int64_t rows = r.read(rg, cols, c); rows_all += rows;
                    for (int64_t i = 0; i < rows; i++) {
                        Addr fa, ta; if (!c[0].addr(i, fa) || !c[1].addr(i, ta)) { unresolved++; continue; }
                        uint32_t s = find(fa), d = find(ta);
                        if (s == none || d == none) { unresolved++; continue; }
                        if (s == d) { selfe++; continue; }
                        int k = edge_code(c[3].bytes(i)); if (k < 0) { unknown_kind++; continue; }
                        uint32_t asset = none; Addr aa; if (k != e_native && c[2].addr(i, aa)) asset = find(aa);
                        double amt = c[5].null(i) ? (c[6] ? c[6].f64(i) : 0.0) : c[5].f64(i);
                        float usd = 0;
                        if (asset != none && stable_dec[asset]) usd = float(amt / std::pow(10.0, stable_dec[asset]));
                        else if (asset != none && asset == weth) usd = float(amt / 1e18 * o.eth_usd);
                        out.push_back({s, d, asset, uint8_t(k), {}, c[4].u64(i), usd, 0.f});
                    }
                }
            }
        }, 1, &o.cpus);
        size_t total = econ.size(); for (auto& p : parts) total += p.size();
        // counting sort by source
        std::vector<uint64_t> off(size_t(n) + 1, 0);
        for (auto& p : parts) for (auto& e : p) off[e.s + 1]++;
        for (auto& e : econ) off[e.s + 1]++;
        for (uint32_t v = 0; v < n; v++) off[v + 1] += off[v];
        std::vector<EdgeRec> es(total);
        { auto cur = off; for (auto& p : parts) { for (auto& e : p) es[cur[e.s]++] = e; std::vector<EdgeRec>().swap(p); } for (auto& e : econ) es[cur[e.s]++] = e; std::vector<EdgeRec>().swap(econ); }
        // per-source: merge (d, type) duplicates across assets, stats, final order (n desc, d asc)
        std::vector<uint64_t> cnt(n, 0);
        std::vector<float> usd_out(n, 0.f);
        parallel_for(n, o.threads, [&](size_t b, size_t e, int) {
            for (size_t v = b; v < e; v++) {
                auto* L = es.data() + off[v]; size_t len = off[v + 1] - off[v];
                if (!len) continue;
                std::sort(L, L + len, [](const EdgeRec& x, const EdgeRec& y) { return x.d < y.d || (x.d == y.d && ((x.t & et_mask) < (y.t & et_mask) || ((x.t & et_mask) == (y.t & et_mask) && x.n > y.n))); });
                size_t w = 0; float uo = 0;
                for (size_t i = 0; i < len;) {
                    size_t j = i; EdgeRec m = L[i];
                    for (j = i + 1; j < len && L[j].d == L[i].d && (L[j].t & et_mask) == (L[i].t & et_mask); j++) { m.n += L[j].n; m.usd += L[j].usd; if (L[j].asset != m.asset) m.t |= ef_multi_asset; }
                    uo += m.usd; L[w++] = m; i = j;
                }
                cnt[v] = w; usd_out[v] = uo;
                std::sort(L, L + w, [](const EdgeRec& x, const EdgeRec& y) { return x.n > y.n || (x.n == y.n && (x.d < y.d || (x.d == y.d && x.t < y.t))); });
                // concentration over distinct counterparties (sum n per counterparty, all edge types)
                uint64_t tot = 0; for (size_t i = 0; i < w; i++) tot += L[i].n;
                std::vector<std::pair<uint64_t, uint32_t>> per; per.reserve(w);
                { std::vector<std::pair<uint32_t, uint64_t>> tmpv; tmpv.reserve(w); for (size_t i = 0; i < w; i++) tmpv.push_back({L[i].d, L[i].n});
                  std::sort(tmpv.begin(), tmpv.end());
                  for (size_t i = 0; i < tmpv.size();) { size_t j = i; uint64_t s = 0; while (j < tmpv.size() && tmpv[j].first == tmpv[i].first) s += tmpv[j++].second; per.push_back({s, tmpv[i].first}); i = j; } }
                std::partial_sort(per.begin(), per.begin() + std::min<size_t>(2, per.size()), per.end(), [](auto& x, auto& y) { return x.first > y.first || (x.first == y.first && x.second < y.second); });
                u32[d_out_n][v] = sat32(tot);
                if (!per.empty() && tot) { u32[d_top1o][v] = per[0].second; u16[d_top1o_sh][v] = share16(double(per[0].first) / tot);
                    u16[d_top2o_sh][v] = share16(double(per[0].first + (per.size() > 1 ? per[1].first : 0)) / tot); }
                else u32[d_top1o][v] = none;
            }
        }, 1 << 12, &o.cpus);
        // compact into final out-CSR arrays
        std::vector<uint64_t> fo(size_t(n) + 1, 0); for (uint32_t v = 0; v < n; v++) fo[v + 1] = fo[v] + cnt[v];
        uint64_t m = fo[n];
        std::vector<uint32_t> odst(m), oasset(m); std::vector<uint16_t> ow(m); std::vector<uint8_t> ot(m);
        std::vector<uint64_t> in_cnt(size_t(n) + 1, 0);
        parallel_for(n, o.threads, [&](size_t b, size_t e, int) {
            for (size_t v = b; v < e; v++) for (uint64_t i = 0; i < cnt[v]; i++) {
                const auto& x = es[off[v] + i]; uint64_t q = fo[v] + i;
                odst[q] = x.d; ow[q] = wcode(x.n); ot[q] = x.t; oasset[q] = x.asset;
            }
        }, 1 << 14, &o.cpus);
        for (uint64_t q = 0; q < m; q++) in_cnt[odst[q] + 1]++;
        for (uint32_t v = 0; v < n; v++) in_cnt[v + 1] += in_cnt[v];
        // transpose (sources ascending), keep exact n for the in-side ordering
        struct InRec { uint64_t n; uint32_t s; uint8_t t; };
        std::vector<InRec> ins(m);
        { auto cur = in_cnt; for (uint32_t v = 0; v < n; v++) for (uint64_t i = 0; i < cnt[v]; i++) { const auto& x = es[off[v] + i]; ins[cur[x.d]++] = {x.n, v, x.t}; } }
        std::vector<float> usd_in(n, 0.f);
        for (uint32_t v = 0; v < n; v++) for (uint64_t i = 0; i < cnt[v]; i++) { const auto& x = es[off[v] + i]; usd_in[x.d] += x.usd; }
        std::vector<EdgeRec>().swap(es);
        std::vector<uint32_t> isrc(m); std::vector<uint16_t> iw(m); std::vector<uint8_t> it(m);
        parallel_for(n, o.threads, [&](size_t b, size_t e, int) {
            for (size_t v = b; v < e; v++) {
                auto* L = ins.data() + in_cnt[v]; size_t len = in_cnt[v + 1] - in_cnt[v];
                uint64_t tot = 0; for (size_t i = 0; i < len; i++) tot += L[i].n;
                // top-1 inbound counterparty (sum over types; L sorted by source)
                uint64_t best = 0; uint32_t bs = none;
                for (size_t i = 0; i < len;) { size_t j = i; uint64_t s = 0; while (j < len && L[j].s == L[i].s) s += L[j++].n; if (s > best) { best = s; bs = L[i].s; } i = j; }
                u32[d_in_n][v] = sat32(tot); u32[d_top1i][v] = bs; u16[d_top1i_sh][v] = tot ? share16(double(best) / tot) : 0;
                std::sort(L, L + len, [](const InRec& x, const InRec& y) { return x.n > y.n || (x.n == y.n && (x.s < y.s || (x.s == y.s && x.t < y.t))); });
                for (size_t i = 0; i < len; i++) { isrc[in_cnt[v] + i] = L[i].s; iw[in_cnt[v] + i] = wcode(L[i].n); it[in_cnt[v] + i] = L[i].t; }
            }
        }, 1 << 12, &o.cpus);
        std::vector<InRec>().swap(ins);
        for (uint32_t v = 0; v < n; v++) {
            f32[d_usd][v] = float((double(f32[d_eth_in][v]) + f32[d_eth_out][v]) * o.eth_usd + usd_in[v] + usd_out[v]);
        }
        seg("out_off", fo); seg("out_dst", odst); seg("out_w", ow); seg("out_t", ot); seg("out_asset", oasset);
        seg("in_off", in_cnt); seg("in_src", isrc); seg("in_w", iw); seg("in_t", it);
        report["edges"] = {{"rows", rows_all.load()}, {"econ_typed", n_econ}, {"input_after_resolve", total},
                           {"csr_edges", m}, {"unresolved", unresolved.load()}, {"self_edges", selfe.load()}, {"unknown_kind", unknown_kind.load()}};
    }

    // ------------------------------------------------------------ creator -> children CSR, strings, features
    void creators_and_write_inputs() {
        uint64_t eoa = 0, contract = 0, recv_only = 0, inferred = 0;
        for (uint32_t v = 0; v < n; v++) {
            auto& f = u32[d_flags][v];
            // No creation row (deployed before the mapped range, or internal CREATE without traces),
            // no logs, never sends a tx, yet moves tokens out >= 50 times: behaves as code.
            if (!(f & (nf_contract | nf_sends | nf_system)) && u32[d_tt_out][v] >= 50) { f |= nf_contract | nf_code_inferred; inferred++; }
            if ((f & nf_sends) && !(f & nf_contract)) { f |= nf_eoa; eoa++; } else if (f & nf_contract) contract++; else recv_only++;
        }
        report["node_types"] = {{"eoa", eoa}, {"contract", contract}, {"receive_only", recv_only}, {"code_inferred", inferred}};
        std::vector<std::pair<uint32_t, uint32_t>> cr;
        for (auto& e : sparse) if (e.key == k_creator) cr.push_back({e.value, e.node});
        std::sort(cr.begin(), cr.end());
        std::vector<uint32_t> idx, nodes; std::vector<uint64_t> off{0};
        for (size_t i = 0; i < cr.size();) { size_t j = i; while (j < cr.size() && cr[j].first == cr[i].first) nodes.push_back(cr[j++].second); idx.push_back(cr[i].first); off.push_back(nodes.size()); i = j; }
        seg("cr_idx", idx); seg("cr_off", off); seg("cr_node", nodes);
        seg("addr", addr); seg("prefix", prefix, prefix_bits);
        for (int i = 0; i < dense_u32_count; i++) seg(std::string(dense_u32[i].name), u32[i]);
        for (int i = 0; i < dense_f32_count; i++) if (i != d_imp) seg(std::string(dense_f32[i].name), f32[i]);
        for (int i = 0; i < dense_u16_count; i++) seg(std::string(dense_u16[i].name), u16[i]);
        std::sort(sparse.begin(), sparse.end(), [](const SparseEntry& a, const SparseEntry& b) { return a.node < b.node || (a.node == b.node && a.key < b.key); });
        seg("sparse", sparse);
    }
    void write_strings() {
        std::vector<uint64_t> off{0}; std::vector<uint8_t> bytes;
        for (auto& s : strings.v) { bytes.insert(bytes.end(), s.begin(), s.end()); off.push_back(bytes.size()); }
        seg("str_off", off); seg("str_bytes", bytes);
    }

    void run() {
        Timer total(timing, "total_s");
        need(!o.chain.empty() && o.chain_id && !o.map.empty() && !o.out.empty(), "usage: glbuild --chain NAME --chain-id ID --map DIR --out DIR [--registry DIR] [--econ DIR]");
        fs::create_directories(o.out);
        tmp = o.out / (".build-" + std::to_string(::getpid()) + "-" + std::to_string(now_ms()));
        fs::create_directories(tmp);
        need(free_bytes(o.out) > (uint64_t(8) << 30), "build disk reserve (8 GiB)");
        registry_topics();
        addresses();
        emitter_topics();
        tokens_pools();
        dex_trades();
        registry();
        edges(econ_edges());
        creators_and_write_inputs();
        // free build-side copies before the classifier maps the written segments
        for (auto& v : u32) std::vector<uint32_t>().swap(v);
        for (auto& v : f32) std::vector<float>().swap(v);
        for (auto& v : u16) std::vector<uint16_t>().swap(v);
        std::vector<SparseEntry>().swap(sparse);
        // ---- phase 2: classifier over the mapped image
        {
            Timer tm(timing, "classify_s");
            write_strings();
            Graph g; g.open(tmp, "size", false, true);
            auto R = classify(g, o.cls, preset, o.threads, &o.cpus);
            seg("labels_base", R.lab); seg("f_imp", R.imp);
            std::sort(R.extra.begin(), R.extra.end(), [](const SparseEntry& a, const SparseEntry& b) { return a.node < b.node || (a.node == b.node && a.key < b.key); });
            seg("sparse_cls", R.extra);
            // residual analysis: importance quantiles of unresolved nodes
            std::vector<float> imp_sorted(R.imp); std::sort(imp_sorted.begin(), imp_sorted.end());
            auto q = [&](double p) { return imp_sorted.empty() ? 0.f : imp_sorted[std::min(imp_sorted.size() - 1, size_t(p * double(imp_sorted.size())))]; };
            json bands = json::array();
            for (double p : {0.5, 0.9, 0.99, 0.999, 0.9999}) {
                float th = q(p); uint64_t res = 0, pd = 0;
                for (uint32_t v = 0; v < g.n; v++) if (R.imp[v] >= th && conf_of(R.lab[v].conf) < o.cls.resolve) { res++; if (R.lab[v].subtype == st_unknown_topics) pd++; }
                bands.push_back({{"importance_quantile", p}, {"importance_min", th}, {"llm_address_tasks", res}, {"llm_protocol_tasks", pd}});
            }
            R.stats["llm_frontier_by_threshold"] = bands;
            report["classifier"] = R.stats;
        }
        // ---- manifest + publication
        json counts = report.value("edges", json::object());
        json man = {{"schema", "economic-machine.graphloop.graph.v1"}, {"chain", o.chain}, {"chain_id", o.chain_id}, {"created_at_ms", now_ms()},
                    {"nodes", n}, {"edges", counts.value("csr_edges", 0)}, {"files", files}, {"eth_usd", o.eth_usd},
                    {"inputs", {{"map", o.map.string()}, {"econ", o.econ.string()}, {"registry", o.registry.string()}}}};
        std::string fstext = files.dump();
        man["graph_id"] = hex(View(sha256(fstext).data(), 32));
        std::string mt = man.dump(1);
        need(seal_text(tmp / "manifest.json", mt), "manifest already present");
        auto final_dir = o.out / ("graph-" + man["graph_id"].get<std::string>().substr(0, 12));
        if (fs::exists(final_dir)) { fs::remove_all(tmp); report["note"] = "identical graph already published"; }
        else { need(::rename(tmp.c_str(), final_dir.c_str()) == 0, "graph publish rename"); sync_directory(o.out); }
        replace_file(o.out / "CURRENT", final_dir.filename().string() + "\n");
        uint64_t bytes = 0; for (auto& [k, f] : files.items()) bytes += f["bytes"].get<uint64_t>();
        report["graph_dir"] = final_dir.string(); report["graph_id"] = man["graph_id"]; report["image_bytes"] = bytes;
        report["bytes_per_node"] = n ? double(bytes) / n : 0; report["peak_rss_bytes"] = peak_rss_bytes();
    }
};
} // namespace

int main(int argc, char** argv) {
    try {
        ::umask(0022);
        Build b;
        for (int i = 1; i < argc; i++) {
            std::string a = argv[i];
            auto val = [&]() -> std::string { need(i + 1 < argc, "missing value for " + a); return argv[++i]; };
            if (a == "--chain") b.o.chain = val();
            else if (a == "--chain-id") b.o.chain_id = std::stoull(val());
            else if (a == "--map") b.o.map = val();
            else if (a == "--econ") b.o.econ = val();
            else if (a == "--registry") b.o.registry = val();
            else if (a == "--out") b.o.out = val();
            else if (a == "--threads") b.o.threads = std::stoi(val());
            else if (a == "--cpus") b.o.cpus = parse_cpus(val());
            else if (a == "--eth-usd") b.o.eth_usd = std::stod(val());
            else if (a == "--no-dex-trades") b.o.dex_trades = false;
            else if (a == "--dex-threads") b.o.dex_threads = std::stoi(val());
            else if (a == "--resolve") b.o.cls.resolve = std::stod(val());
            else if (a == "--deposit-share") b.o.cls.deposit_share = std::stod(val());
            else if (a == "--sweep-min") b.o.cls.sweep_min = uint32_t(std::stoul(val()));
            else throw std::runtime_error("unknown argument " + a);
        }
        need(b.o.threads >= 1 && b.o.threads <= 64, "thread bound");
        b.run();
        b.report["timing"] = b.timing;
        std::cout << b.report.dump(1) << "\n";
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << "\n"; return 1; }
}
