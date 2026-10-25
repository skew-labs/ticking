#pragma once
// Immutable graph image: mmap'd segments written by glbuild, pinned by manifest.json (sha256 per
// file).  Layout (little-endian, 32-bit node ids = rank in sorted address order):
//   addr[N] (20 B)  prefix[2^b+1] (u32 radix index on the first b address bits)
//   out_off[N+1] u64, out_dst[M] u32, out_w[M] u16 (wcode), out_t[M] u8 (type|flags), out_asset[M] u32
//   in_off[N+1]  u64, in_src[M]  u32, in_w[M]  u16,         in_t[M]  u8
//   adjacency lists are sorted by (weight desc, neighbour asc): top-k neighbours = first k entries.
//   dense SoA features (u32/f32/u16 per node), base labels (8 B per node), sparse (node,key,value)
//   table for rare attributes, emitter topic CSR, topic dictionary, creator->children CSR, strings.
#include "durable.hpp"
#include "schema.hpp"
#include <nlohmann/json.hpp>
#include <optional>

namespace machine::graphloop {
using json = nlohmann::json;

struct BaseLabel { uint8_t category, conf, source, subtype, tier, rule, flags, pad; };
static_assert(sizeof(BaseLabel) == 8);
struct SparseEntry { uint32_t node; uint32_t value; uint8_t key; uint8_t pad[3]; };
static_assert(sizeof(SparseEntry) == 12);
struct TopicCount { uint32_t topic; uint32_t n; };

enum SparseKey : uint8_t {
    k_logs = 1, k_created, k_dex, k_creator, k_topics_lo, k_topics_hi, k_router, k_sandwich, k_arb, k_sweep,
    k_tok_holders, k_tok_transfers, k_tok_std, k_pool_proto, k_entity, k_role, k_econ, k_rx_fail, k_symbol,
    k_pool_trades, k_mev_cp
};

// Segment catalogue: name, element size.  Shared by builder and loader.
struct SegSpec { std::string_view name; uint32_t elem; };
inline constexpr SegSpec dense_u32[] = {
    {"f_tx_sent", 4}, {"f_tx_recv", 4}, {"f_tx_fail", 4}, {"f_tt_out", 4}, {"f_tt_in", 4}, {"f_cp", 4}, {"f_outdeg", 4},
    {"f_indeg", 4}, {"f_first", 4}, {"f_last", 4}, {"f_flags", 4}, {"f_top1o", 4}, {"f_top1i", 4}, {"f_out_n", 4}, {"f_in_n", 4}};
enum DenseU32 { d_tx_sent, d_tx_recv, d_tx_fail, d_tt_out, d_tt_in, d_cp, d_outdeg, d_indeg, d_first, d_last, d_flags, d_top1o, d_top1i, d_out_n, d_in_n, dense_u32_count };
inline constexpr SegSpec dense_f32[] = {{"f_eth_out", 4}, {"f_eth_in", 4}, {"f_usd", 4}, {"f_imp", 4}};
enum DenseF32 { d_eth_out, d_eth_in, d_usd, d_imp, dense_f32_count };
inline constexpr SegSpec dense_u16[] = {{"f_top1o_sh", 2}, {"f_top2o_sh", 2}, {"f_top1i_sh", 2}};
enum DenseU16 { d_top1o_sh, d_top2o_sh, d_top1i_sh, dense_u16_count };

class Graph {
    std::vector<std::pair<std::string, Segment>> segs_;
    const Segment& seg(std::string_view name) const {
        for (auto& [n, s] : segs_) if (n == name) return s;
        throw std::runtime_error("segment missing: " + std::string(name));
    }
    template <class T> std::span<const T> map(const fs::path& dir, std::string_view name, bool populate, int advice) {
        bool ram = ram_ && name != "out_asset" && name != "te_off" && name != "te_node" && name != "cr_node";   // cold segments stay mmap'd
        segs_.emplace_back(std::string(name), Segment(dir / (std::string(name) + ".seg"), sizeof(T), populate, advice, ram));
        return segs_.back().second.as<T>();
    }
    bool ram_ = false;
public:
    void set_ram(bool r) { ram_ = r; }
    fs::path dir; json manifest; std::string graph_id, chain; uint64_t chain_id{};
    uint32_t n{}; uint64_t m{}; uint32_t prefix_bits{};
    std::span<const Addr> addr; std::span<const uint32_t> prefix;
    std::span<const uint64_t> out_off, in_off; std::span<const uint32_t> out_dst, in_src, out_asset;
    std::span<const uint16_t> out_w, in_w; std::span<const uint8_t> out_t, in_t;
    std::array<std::span<const uint32_t>, dense_u32_count> u32{};
    std::array<std::span<const float>, dense_f32_count> f32{};
    std::array<std::span<const uint16_t>, dense_u16_count> u16{};
    std::span<const BaseLabel> base;
    std::span<const SparseEntry> sparse_tab, sparse_cls;
    std::span<const uint32_t> nt_idx, nt_topic_ids, te_node, cr_idx, cr_node, topic_name;
    std::span<const uint64_t> nt_off, te_off, cr_off, topic_bits, str_off;
    std::span<const uint32_t> nt_n;
    std::span<const Hash> topic;
    std::span<const uint8_t> str_bytes;

    // verify: "size" (bytes only) or "full" (sha256 of every segment against the manifest).
    // partial: builder phase 2 (no manifest yet; classifier outputs not mapped).
    void open(const fs::path& d, const std::string& verify, bool populate, bool partial = false) {
        dir = d;
        if (!partial) {
        manifest = json::parse(load_text(d / "manifest.json"));
        need(manifest.value("schema", "") == "economic-machine.graphloop.graph.v1", "graph manifest schema");
        graph_id = manifest.at("graph_id"); chain = manifest.at("chain"); chain_id = manifest.at("chain_id");
        }
        if (!partial) for (auto& [name, f] : manifest.at("files").items()) {
            auto p = d / name; need(fs::exists(p), "graph file missing: " + name);
            need(fs::file_size(p) == f.at("bytes").get<uint64_t>(), "graph file size: " + name);
            if (verify == "full") {
                CryptoPP::SHA256 h; int fd = ::open(p.c_str(), O_RDONLY | O_CLOEXEC); need(fd >= 0, "verify open");
                std::vector<uint8_t> buf(size_t(1) << 22); ssize_t r;
                while ((r = ::read(fd, buf.data(), buf.size())) > 0) h.Update(buf.data(), size_t(r));
                ::close(fd); Hash dg; h.Final(dg.data());
                need(hex(View(dg.data(), 32)) == f.at("sha256").get<std::string>(), "graph file sha256: " + name);
            }
        }
        const int rnd = MADV_RANDOM, seq = 0;
        addr = map<Addr>(d, "addr", populate, rnd);
        n = uint32_t(addr.size());
        prefix = map<uint32_t>(d, "prefix", populate, seq); prefix_bits = uint32_t(seg("prefix").aux());
        out_off = map<uint64_t>(d, "out_off", populate, rnd); in_off = map<uint64_t>(d, "in_off", populate, rnd);
        out_dst = map<uint32_t>(d, "out_dst", populate, rnd); in_src = map<uint32_t>(d, "in_src", populate, rnd);
        out_w = map<uint16_t>(d, "out_w", populate, rnd); in_w = map<uint16_t>(d, "in_w", populate, rnd);
        out_t = map<uint8_t>(d, "out_t", populate, rnd); in_t = map<uint8_t>(d, "in_t", populate, rnd);
        out_asset = map<uint32_t>(d, "out_asset", false, rnd);   // cold: only touched by tools
        m = out_dst.size();
        for (int i = 0; i < dense_u32_count; i++) u32[i] = map<uint32_t>(d, dense_u32[i].name, populate, rnd);
        for (int i = 0; i < dense_f32_count; i++) if (!(partial && i == d_imp)) f32[i] = map<float>(d, dense_f32[i].name, populate, rnd);
        for (int i = 0; i < dense_u16_count; i++) u16[i] = map<uint16_t>(d, dense_u16[i].name, populate, rnd);
        if (!partial) { base = map<BaseLabel>(d, "labels_base", populate, rnd); sparse_cls = map<SparseEntry>(d, "sparse_cls", populate, rnd); }
        sparse_tab = map<SparseEntry>(d, "sparse", populate, rnd);
        nt_idx = map<uint32_t>(d, "nt_idx", populate, rnd); nt_off = map<uint64_t>(d, "nt_off", populate, rnd);
        nt_topic_ids = map<uint32_t>(d, "nt_topic", populate, rnd); nt_n = map<uint32_t>(d, "nt_n", populate, rnd);
        topic = map<Hash>(d, "topic", populate, rnd); topic_bits = map<uint64_t>(d, "topic_bits", populate, rnd);
        topic_name = map<uint32_t>(d, "topic_name", populate, rnd);
        te_off = map<uint64_t>(d, "te_off", false, rnd); te_node = map<uint32_t>(d, "te_node", false, rnd);
        cr_idx = map<uint32_t>(d, "cr_idx", populate, rnd); cr_off = map<uint64_t>(d, "cr_off", populate, rnd); cr_node = map<uint32_t>(d, "cr_node", false, rnd);
        str_off = map<uint64_t>(d, "str_off", populate, rnd); str_bytes = map<uint8_t>(d, "str_bytes", populate, rnd);
        need(out_off.size() == size_t(n) + 1 && in_off.size() == size_t(n) + 1 && out_off[n] == m && in_off[n] == in_src.size(), "graph CSR shape");
        need((partial || base.size() == n) && u32[0].size() == n, "graph feature shape");
    }
    size_t mapped_bytes() const { size_t b = 0; for (auto& [nm, s] : segs_) b += s.bytes(); return b; }
    std::vector<std::pair<std::string, size_t>> segment_sizes() const { std::vector<std::pair<std::string, size_t>> o; for (auto& [nm, s] : segs_) o.emplace_back(nm, s.bytes()); return o; }

    uint32_t find(const Addr& a) const {
        uint32_t p = prefix_bits ? uint32_t(((uint32_t(a.b[0]) << 24) | (uint32_t(a.b[1]) << 16) | (uint32_t(a.b[2]) << 8) | a.b[3]) >> (32 - prefix_bits)) : 0;
        uint32_t lo = prefix[p], hi = prefix[p + 1];
        while (lo < hi) {
            uint32_t mid = lo + (hi - lo) / 2;
            int c = std::memcmp(addr[mid].b, a.b, 20);
            if (c == 0) return mid;
            if (c < 0) lo = mid + 1; else hi = mid;
        }
        return none;
    }
    uint32_t find(std::string_view hexaddr) const { Addr a{}; if (!parse_addr(hexaddr, a)) return none; return find(a); }

    struct Adj { const uint32_t* nb; const uint16_t* w; const uint8_t* t; uint64_t len; };
    Adj out(uint32_t v) const { auto b = out_off[v], e = out_off[v + 1]; return {out_dst.data() + b, out_w.data() + b, out_t.data() + b, e - b}; }
    Adj in(uint32_t v) const { auto b = in_off[v], e = in_off[v + 1]; return {in_src.data() + b, in_w.data() + b, in_t.data() + b, e - b}; }
    uint64_t out_degree(uint32_t v) const { return out_off[v + 1] - out_off[v]; }
    uint64_t in_degree(uint32_t v) const { return in_off[v + 1] - in_off[v]; }

    static uint32_t sparse_in(std::span<const SparseEntry> t, uint32_t v, uint8_t key, uint32_t def) {
        auto it = std::lower_bound(t.begin(), t.end(), v, [](const SparseEntry& e, uint32_t x) { return e.node < x; });
        for (; it != t.end() && it->node == v; ++it) if (it->key == key) return it->value;
        return def;
    }
    uint32_t sparse(uint32_t v, uint8_t key, uint32_t def = 0) const {
        uint32_t r = sparse_in(sparse_tab, v, key, none);
        if (r != none) return r;
        return sparse_cls.empty() ? def : sparse_in(sparse_cls, v, key, def);
    }
    uint64_t topics_bits(uint32_t v) const { return uint64_t(sparse(v, k_topics_lo)) | (uint64_t(sparse(v, k_topics_hi)) << 32); }
    std::span<const uint32_t> topic_ids(uint32_t v, std::span<const uint32_t>* counts = nullptr) const {
        auto it = std::lower_bound(nt_idx.begin(), nt_idx.end(), v);
        if (it == nt_idx.end() || *it != v) return {};
        size_t i = size_t(it - nt_idx.begin());
        if (counts) *counts = nt_n.subspan(nt_off[i], nt_off[i + 1] - nt_off[i]);
        return nt_topic_ids.subspan(nt_off[i], nt_off[i + 1] - nt_off[i]);
    }
    uint32_t topic_find(const Hash& t) const {
        auto it = std::lower_bound(topic.begin(), topic.end(), t, [](const Hash& a, const Hash& b) { return std::memcmp(a.data(), b.data(), 32) < 0; });
        return it != topic.end() && *it == t ? uint32_t(it - topic.begin()) : none;
    }
    std::span<const uint32_t> topic_emitters(uint32_t t) const { return te_node.subspan(te_off[t], te_off[t + 1] - te_off[t]); }
    std::span<const uint32_t> children(uint32_t creator) const {
        auto it = std::lower_bound(cr_idx.begin(), cr_idx.end(), creator);
        if (it == cr_idx.end() || *it != creator) return {};
        size_t i = size_t(it - cr_idx.begin());
        return cr_node.subspan(cr_off[i], cr_off[i + 1] - cr_off[i]);
    }
    std::string_view str(uint32_t id) const {
        if (id == none || id + 1 >= str_off.size()) return {};
        return {reinterpret_cast<const char*>(str_bytes.data() + str_off[id]), size_t(str_off[id + 1] - str_off[id])};
    }
    bool is_contract(uint32_t v) const { return u32[d_flags][v] & nf_contract; }
    bool is_eoa(uint32_t v) const { return u32[d_flags][v] & nf_eoa; }
};

// Node type letter used in packs: C contract, E EOA (sends txs), R receive-only (no code evidence).
inline char node_type(const Graph& g, uint32_t v) { return g.is_contract(v) ? 'C' : g.is_eoa(v) ? 'E' : 'R'; }
} // namespace machine::graphloop
