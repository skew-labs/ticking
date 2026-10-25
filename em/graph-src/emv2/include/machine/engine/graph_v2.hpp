#pragma once
// EMV2 graph image "CSR-T" (design EM-ENGINE-V2.md 5.3): both directions, time on every edge, Elias-Fano
// neighbour blocks, hubs truncated to their top-256 neighbours in the hot tier with the full list in the
// cold tier, and the original map/edges rows kept verbatim in a cold detail stream (exact n, amounts,
// assets) so the image can be compared row by row with the map.
//
// Image directory (every *.seg is a graphloop segment: 64-byte header + elements, sha256 in manifest.json):
//   addr.seg       Addr[N]  sorted 20-byte addresses; node id = rank          prefix.seg u32[2^b+1] (aux = b)
//   node.seg       NodeV2[N] 16 B hot node record                             day.seg    u64[] first block of day aux+i (non-decreasing)
//   {out,in}_blk   u64[ceil(N/256)+1] byte offset of each 256-node block      {out,in}_rel u32[N+1] offset in block
//   {out,in}_adj   bytes: per node one list record (hot)                      empty record = no edges
//   {out,in}_cidx  ColdIdx[C+1] hubs (sorted) + sentinel                       {out,in}_cold bytes: full lists (blocks only)
//   det_{blk,rel}  per-node offsets into det (rel is u64 here: one hub's rows can pass 4 GB)   det: original edge rows
//   ndet_{blk,rel} per-node offsets into ndet (u64 rel)                                         ndet: original address rows
//
// List record: varint D (entries in the full list), varint K (entries stored here), ceil(K/128) blocks,
// then, in hot records with K > 64, 64 x u16 positions of the top-64 entries by weight.  Entries are
// (neighbour, kind) pairs sorted by (neighbour, kind); duplicates of a neighbour carry different kinds.
// Block: varint v0, u8 (c-1), varint xmax, low bits (c*l), high bits (xmax>>l)+c (Elias-Fano over
// x_i = v_i - v0, l = floor(log2((xmax+1)/c)) when xmax+1 > c), c x wk (weight class << 4 | kind),
// out direction only: c x u16 last_day, c x varint (last_day - first_day).
// Detail row: varint d (delta to the previous row's d in this source; the first row absolute), u8 kind |
// flags, asset (varint id+1 or 20 raw bytes, see flags), varint n, varint first_block, varint
// (last_block - first_block), amount (u8 length + little-endian bytes) unless the amount is NULL.
#include "machine/graphloop/durable.hpp"
#include "machine/graphloop/schema.hpp"
#include <nlohmann/json.hpp>
#include <bit>
#include <optional>
#include <queue>

namespace machine::engine::graph {
using namespace machine::graphloop;
using json = nlohmann::json;

inline constexpr const char* schema = "economic-machine.emv2.graph.v1";
inline constexpr uint32_t block_nodes = 256;     // offset table granularity
inline constexpr uint32_t ef_block = 128;        // entries per Elias-Fano block
inline constexpr uint32_t hub_degree = 1000;     // more entries than this: hub
inline constexpr uint32_t hub_keep = 256;        // hot entries kept for a hub
inline constexpr uint32_t top_n = 64;            // top-by-weight positions stored for K > 64
inline constexpr uint64_t day_epoch = 1438214400; // 2015-07-30 00:00 UTC (Ethereum genesis day)

enum Dir : int { out = 0, in = 1 };
inline constexpr const char* dir_name[2] = {"out", "in"};

// Edge kinds: graphloop EdgeType codes (map/edges uses the first four).
inline const char* kind_name(uint8_t k) { return k == 0 ? "native_tx" : k < edge_type_count ? edge_names[k].data() : "other"; }

// Weight class: floor(log2(n)) capped at 15 (n = 1 -> 0, 2..3 -> 1, ..., >= 32768 -> 15).
inline uint8_t weight_class(uint64_t n) { return n <= 1 ? 0 : uint8_t(std::min<int>(15, 63 - std::countl_zero(n))); }
inline uint64_t weight_floor(uint8_t q) { return uint64_t(1) << q; }

inline uint16_t day_of_ts(uint64_t ts) { return ts <= day_epoch ? 0 : uint16_t(std::min<uint64_t>(65535, (ts - day_epoch) / 86400)); }

// Node flags (NodeV2::flags).
enum : uint16_t { nv_contract = 1, nv_out_hub = 2, nv_in_hub = 4, nv_sends = 8, nv_receives = 16 };
struct NodeV2 {
    uint16_t first_day, last_day, flags;
    uint8_t tx_q, tt_q;              // weight classes of tx_sent + tx_received and token transfers in + out
    uint32_t out_deg, in_deg;        // entries in the full (neighbour, kind) lists
};
static_assert(sizeof(NodeV2) == 16);
struct ColdIdx { uint32_t node, count; uint64_t off; };   // count = entries in the full list; the sentinel's off = end
static_assert(sizeof(ColdIdx) == 16);

// Detail row flags (byte after d): low nibble kind, then:
enum : uint8_t { df_asset_raw = 0x10, df_asset_none = 0x20, df_amount_null = 0x40 };

// ------------------------------------------------------------------ bytes
inline void put_varint(std::string& o, uint64_t v) { while (v >= 0x80) { o += char(uint8_t(v) | 0x80); v >>= 7; } o += char(uint8_t(v)); }
inline uint64_t get_varint(const uint8_t*& p, const uint8_t* e) {
    uint64_t v = 0; int s = 0;
    for (;;) { need(p < e && s < 64, "graph varint"); uint8_t b = *p++; v |= uint64_t(b & 0x7f) << s; if (!(b & 0x80)) return v; s += 7; }
}
inline void put_u16(std::string& o, uint16_t v) { o += char(uint8_t(v)); o += char(uint8_t(v >> 8)); }
inline uint16_t get_u16(const uint8_t*& p, const uint8_t* e) { need(p + 2 <= e, "graph u16"); uint16_t v = uint16_t(p[0] | (p[1] << 8)); p += 2; return v; }

struct BitWriter {
    std::string& o; size_t start; uint64_t bits = 0;
    BitWriter(std::string& s, uint64_t nbits) : o(s), start(s.size()) { o.append(size_t((nbits + 7) / 8), '\0'); }
    void set(uint64_t pos) { o[start + pos / 8] = char(uint8_t(o[start + pos / 8]) | uint8_t(1u << (pos % 8))); }
    void put(uint64_t pos, uint64_t v, uint32_t width) { for (uint32_t i = 0; i < width; i++) if ((v >> i) & 1) set(pos + i); }
};
inline uint64_t get_bits(const uint8_t* base, uint64_t pos, uint32_t width) {
    uint64_t v = 0;
    for (uint32_t i = 0; i < width; i++) { uint64_t q = pos + i; v |= uint64_t((base[q / 8] >> (q % 8)) & 1) << i; }
    return v;
}

// ------------------------------------------------------------------ list entries
struct Entry {
    uint32_t nbr; uint8_t kind, wq; uint16_t last_day; uint32_t span;   // span = last_day - first_day
    uint64_t n;                                                        // exact count (build side only; decoders return weight_floor(wq))
};
inline bool entry_key_less(const Entry& a, const Entry& b) { return a.nbr < b.nbr || (a.nbr == b.nbr && a.kind < b.kind); }
inline bool entry_weight_less(const Entry& a, const Entry& b) {   // heavier first, then (nbr, kind)
    return a.n > b.n || (a.n == b.n && entry_key_less(a, b));
}

inline void encode_block(std::string& o, const Entry* e, uint32_t c, bool with_time) {
    need(c >= 1 && c <= ef_block, "graph block size");
    uint64_t v0 = e[0].nbr, xmax = uint64_t(e[c - 1].nbr) - v0;
    uint32_t l = (xmax + 1 > c) ? uint32_t(63 - std::countl_zero((xmax + 1) / c)) : 0;
    put_varint(o, v0); o += char(uint8_t(c - 1)); put_varint(o, xmax);
    { BitWriter w(o, uint64_t(c) * l); for (uint32_t i = 0; i < c; i++) w.put(uint64_t(i) * l, (e[i].nbr - v0) & ((uint64_t(1) << l) - 1), l); }
    { BitWriter w(o, (xmax >> l) + c); for (uint32_t i = 0; i < c; i++) w.set(((e[i].nbr - v0) >> l) + i); }
    for (uint32_t i = 0; i < c; i++) o += char(uint8_t(e[i].wq << 4 | (e[i].kind & 15)));
    if (with_time) {
        for (uint32_t i = 0; i < c; i++) put_u16(o, e[i].last_day);
        for (uint32_t i = 0; i < c; i++) put_varint(o, e[i].span);
    }
}
// Appends the block's entries to out; returns the pointer after the block.
inline const uint8_t* decode_block(const uint8_t* p, const uint8_t* end, bool with_time, std::vector<Entry>& out) {
    uint64_t v0 = get_varint(p, end); need(p < end, "graph block"); uint32_t c = uint32_t(*p++) + 1; uint64_t xmax = get_varint(p, end);
    uint32_t l = (xmax + 1 > c) ? uint32_t(63 - std::countl_zero((xmax + 1) / c)) : 0;
    uint64_t low_bytes = (uint64_t(c) * l + 7) / 8, high_bits = (xmax >> l) + c, high_bytes = (high_bits + 7) / 8;
    need(uint64_t(end - p) >= low_bytes + high_bytes + c, "graph block bounds");
    const uint8_t* low = p; const uint8_t* high = p + low_bytes; p += low_bytes + high_bytes;
    size_t base = out.size(); out.resize(base + c);
    uint64_t pos = 0;
    for (uint32_t i = 0; i < c; i++) {
        while (pos < high_bits && !((high[pos / 8] >> (pos % 8)) & 1)) pos++;
        need(pos < high_bits, "graph block high bits");
        uint64_t hi = pos - i; pos++;
        uint64_t x = (hi << l) | (l ? get_bits(low, uint64_t(i) * l, l) : 0);
        need(x <= xmax, "graph block value");
        out[base + i].nbr = uint32_t(v0 + x);
    }
    for (uint32_t i = 0; i < c; i++) { uint8_t wk = *p++; out[base + i].kind = wk & 15; out[base + i].wq = wk >> 4; out[base + i].n = weight_floor(wk >> 4); }
    if (with_time) {
        for (uint32_t i = 0; i < c; i++) out[base + i].last_day = get_u16(p, end);
        for (uint32_t i = 0; i < c; i++) out[base + i].span = uint32_t(get_varint(p, end));
    } else for (uint32_t i = 0; i < c; i++) { out[base + i].last_day = 0; out[base + i].span = 0; }
    return p;
}

// Encodes a list record.  e: the stored entries sorted by (nbr, kind); total = entries in the full list.
// top: positions (into e) of the heaviest entries, written when e.size() > top_n.
inline void encode_list(std::string& o, std::span<const Entry> e, uint64_t total, bool with_time, bool with_top) {
    put_varint(o, total); put_varint(o, e.size());
    for (size_t i = 0; i < e.size(); i += ef_block) encode_block(o, e.data() + i, uint32_t(std::min<size_t>(ef_block, e.size() - i)), with_time);
    if (with_top && e.size() > top_n) {
        std::vector<uint32_t> pos(e.size()); for (uint32_t i = 0; i < pos.size(); i++) pos[i] = i;
        std::partial_sort(pos.begin(), pos.begin() + top_n, pos.end(), [&](uint32_t a, uint32_t b) { return entry_weight_less(e[a], e[b]); });
        for (uint32_t i = 0; i < top_n; i++) put_u16(o, uint16_t(pos[i]));
    }
}
struct ListView { uint64_t total = 0, stored = 0; std::vector<Entry> entries; std::vector<uint16_t> top; };
inline void decode_list(const uint8_t* p, const uint8_t* end, bool with_time, bool with_top, ListView& lv) {
    lv.entries.clear(); lv.top.clear(); lv.total = lv.stored = 0;
    if (p == end) return;
    lv.total = get_varint(p, end); lv.stored = get_varint(p, end);
    need(lv.stored <= lv.total, "graph list counts");
    lv.entries.reserve(lv.stored);
    while (lv.entries.size() < lv.stored) p = decode_block(p, end, with_time, lv.entries);
    need(lv.entries.size() == lv.stored, "graph list length");
    if (with_top && lv.stored > top_n) { lv.top.resize(top_n); for (auto& t : lv.top) { t = get_u16(p, end); need(t < lv.stored, "graph top position"); } }
    need(p == end, "graph list trailing bytes");
}

// ------------------------------------------------------------------ amounts (exact, up to 256 bits)
// Little-endian magnitude bytes without trailing zeros; "0" -> empty.
inline bool decimal_to_le(std::string_view s, std::string& out) {
    out.clear(); if (s.empty()) return false;
    std::array<uint32_t, 9> w{};   // 288 bits
    for (char ch : s) {
        if (ch < '0' || ch > '9') return false;
        uint64_t carry = uint64_t(ch - '0');
        for (auto& x : w) { uint64_t t = uint64_t(x) * 10 + carry; x = uint32_t(t); carry = t >> 32; }
        if (carry) return false;
    }
    for (int i = 0; i < 36; i++) out += char(uint8_t(w[size_t(i / 4)] >> (8 * (i % 4))));
    while (!out.empty() && out.back() == '\0') out.pop_back();
    return out.size() <= 32;
}
inline std::string le_to_decimal(std::string_view le) {
    std::vector<uint32_t> w((le.size() + 3) / 4, 0);
    for (size_t i = 0; i < le.size(); i++) w[i / 4] |= uint32_t(uint8_t(le[i])) << (8 * (i % 4));
    std::string d;
    for (;;) {
        bool zero = true; for (auto x : w) if (x) { zero = false; break; }
        if (zero) break;
        uint64_t rem = 0;
        for (size_t i = w.size(); i-- > 0;) { uint64_t cur = (rem << 32) | w[i]; w[i] = uint32_t(cur / 10); rem = cur % 10; }
        d += char('0' + rem);
    }
    if (d.empty()) d = "0";
    std::reverse(d.begin(), d.end());
    return d;
}

// ------------------------------------------------------------------ original rows (cold detail)
struct DetailRow { uint32_t d; uint8_t kind, flags; uint32_t asset; Addr asset_raw; uint64_t n, fb, lb; std::string amount_le; };
inline void encode_detail(std::string& o, const DetailRow& r, uint32_t prev_d, bool first) {
    put_varint(o, first ? r.d : r.d - prev_d);
    o += char(uint8_t((r.kind & 15) | (r.flags & 0xf0)));
    if (r.flags & df_asset_raw) o.append(reinterpret_cast<const char*>(r.asset_raw.b), 20);
    else if (!(r.flags & df_asset_none)) put_varint(o, uint64_t(r.asset) + 1);
    put_varint(o, r.n); put_varint(o, r.fb); put_varint(o, r.lb - r.fb);
    if (!(r.flags & df_amount_null)) { o += char(uint8_t(r.amount_le.size())); o += r.amount_le; }
}
inline std::vector<DetailRow> decode_details(const uint8_t* p, const uint8_t* end) {
    std::vector<DetailRow> rows; uint32_t prev = 0; bool first = true;
    while (p < end) {
        DetailRow r{}; uint64_t dv = get_varint(p, end); r.d = first ? uint32_t(dv) : uint32_t(prev + dv); first = false; prev = r.d;
        need(p < end, "graph detail"); uint8_t kf = *p++; r.kind = kf & 15; r.flags = kf & 0xf0;
        if (r.flags & df_asset_raw) { need(end - p >= 20, "graph detail asset"); std::memcpy(r.asset_raw.b, p, 20); p += 20; r.asset = none; }
        else if (r.flags & df_asset_none) r.asset = none;
        else r.asset = uint32_t(get_varint(p, end) - 1);
        r.n = get_varint(p, end); r.fb = get_varint(p, end); r.lb = r.fb + get_varint(p, end);
        if (!(r.flags & df_amount_null)) { need(p < end, "graph detail amount"); size_t L = *p++; need(size_t(end - p) >= L && L <= 32, "graph detail amount bytes"); r.amount_le.assign(reinterpret_cast<const char*>(p), L); p += L; }
        rows.push_back(std::move(r));
    }
    return rows;
}
// Original address row (cold): first/last block and time, tx and token transfer counters, map degrees.
struct NodeDetail { uint64_t fb = 0, ft = 0, lb = 0, lt = 0, tx_sent = 0, tx_recv = 0, tt_in = 0, tt_out = 0, map_out = 0, map_in = 0; bool contract = false; };
inline void encode_node_detail(std::string& o, const NodeDetail& d) {
    o += char(d.contract ? 1 : 0);
    for (uint64_t v : {d.fb, d.ft, d.lb, d.lt, d.tx_sent, d.tx_recv, d.tt_in, d.tt_out, d.map_out, d.map_in}) put_varint(o, v);
}
inline NodeDetail decode_node_detail(const uint8_t* p, const uint8_t* end) {
    NodeDetail d; need(p < end, "graph node detail"); d.contract = *p++ & 1;
    for (uint64_t* v : {&d.fb, &d.ft, &d.lb, &d.lt, &d.tx_sent, &d.tx_recv, &d.tt_in, &d.tt_out, &d.map_out, &d.map_in}) *v = get_varint(p, end);
    return d;
}

// ------------------------------------------------------------------ reader
// Opens an image directory read-only (mmap).  Cold segments are local files here; the server may swap
// them for Blob range reads (same byte layout).
class GraphV2 {
    std::vector<std::pair<std::string, Segment>> segs_;
    template <class T> std::span<const T> map(const fs::path& d, const std::string& name, int advice) {
        segs_.emplace_back(name, Segment(d / (name + ".seg"), sizeof(T), false, advice));
        return segs_.back().second.as<T>();
    }
    const Segment& seg(std::string_view name) const { for (auto& [n, s] : segs_) if (n == name) return s; throw std::runtime_error("graph segment missing"); }
    struct Table {
        std::span<const uint64_t> blk; std::span<const uint32_t> rel32; std::span<const uint64_t> rel64; std::span<const uint8_t> bytes;
        uint64_t rel(uint32_t v) const { return rel64.empty() ? rel32[v] : rel64[v]; }
    };
    Table open_table(const fs::path& d, const std::string& base) {
        Table t; t.blk = map<uint64_t>(d, base + "_blk", MADV_RANDOM);
        bool wide = base == "det" || base == "ndet";
        if (wide) t.rel64 = map<uint64_t>(d, base + "_rel", MADV_RANDOM); else t.rel32 = map<uint32_t>(d, base + "_rel", MADV_RANDOM);
        t.bytes = map<uint8_t>(d, wide ? base : base + "_adj", MADV_RANDOM);
        need((wide ? t.rel64.size() : t.rel32.size()) == size_t(n) + 1 && t.blk.size() == size_t(n) / block_nodes + 2, "graph table shape: " + base);
        return t;
    }
    static std::pair<const uint8_t*, const uint8_t*> slice(const Table& t, uint32_t v) {
        uint64_t b = t.blk[v / block_nodes] + t.rel(v);
        uint64_t e = ((v + 1) % block_nodes ? t.blk[v / block_nodes] : t.blk[(v + 1) / block_nodes]) + t.rel(v + 1);
        need(b <= e && e <= t.bytes.size(), "graph offsets");
        return {t.bytes.data() + b, t.bytes.data() + e};
    }
public:
    fs::path dir; json manifest; uint32_t n = 0; uint32_t prefix_bits = 0;
    std::span<const Addr> addr; std::span<const uint32_t> prefix; std::span<const NodeV2> node; std::span<const uint64_t> day_start; uint32_t day0 = 0;
    Table adj[2], det, ndet;
    std::span<const ColdIdx> cidx[2]; std::span<const uint8_t> cold[2];

    void open(const fs::path& d, bool verify_sha = false) {
        dir = d; manifest = json::parse(load_text(d / "manifest.json"));
        need(manifest.value("schema", "") == schema, "graph manifest schema");
        for (auto& [name, f] : manifest.at("files").items()) {
            auto p = d / name; need(fs::exists(p) && fs::file_size(p) == f.at("bytes").get<uint64_t>(), "graph file size: " + name);
            if (verify_sha) {
                CryptoPP::SHA256 h; int fd = ::open(p.c_str(), O_RDONLY | O_CLOEXEC); need(fd >= 0, "graph verify open");
                std::vector<uint8_t> buf(size_t(1) << 22); ssize_t r;
                while ((r = ::read(fd, buf.data(), buf.size())) > 0) h.Update(buf.data(), size_t(r));
                ::close(fd); Hash dg; h.Final(dg.data());
                need(hex(View(dg.data(), 32)) == f.at("sha256").get<std::string>(), "graph file sha256: " + name);
            }
        }
        addr = map<Addr>(d, "addr", MADV_RANDOM); n = uint32_t(addr.size());
        prefix = map<uint32_t>(d, "prefix", 0); prefix_bits = uint32_t(seg("prefix").aux());
        node = map<NodeV2>(d, "node", MADV_RANDOM); day_start = map<uint64_t>(d, "day", 0); day0 = uint32_t(seg("day").aux());
        need(node.size() == n, "graph node shape");
        adj[out] = open_table(d, "out"); adj[in] = open_table(d, "in");
        det = open_table(d, "det"); ndet = open_table(d, "ndet");
        for (int k = 0; k < 2; k++) { cidx[k] = map<ColdIdx>(d, std::string(dir_name[k]) + "_cidx", MADV_RANDOM); cold[k] = map<uint8_t>(d, std::string(dir_name[k]) + "_cold", MADV_RANDOM); }
    }
    uint32_t find(const Addr& a) const {
        uint32_t p = prefix_bits ? uint32_t(((uint32_t(a.b[0]) << 24) | (uint32_t(a.b[1]) << 16) | (uint32_t(a.b[2]) << 8) | a.b[3]) >> (32 - prefix_bits)) : 0;
        uint32_t lo = prefix[p], hi = prefix[p + 1];
        while (lo < hi) { uint32_t mid = lo + (hi - lo) / 2; int c = std::memcmp(addr[mid].b, a.b, 20); if (c == 0) return mid; if (c < 0) lo = mid + 1; else hi = mid; }
        return none;
    }
    uint32_t find(std::string_view hexaddr) const { Addr a{}; return parse_addr(hexaddr, a) ? find(a) : none; }
    // Hot list (hubs: top-256 by weight + the full count in lv.total).
    void hot(uint32_t v, Dir dr, ListView& lv) const { auto [b, e] = slice(adj[dr], v); decode_list(b, e, dr == out, true, lv); }
    bool is_hub(uint32_t v, Dir dr) const { return node[v].flags & (dr == out ? nv_out_hub : nv_in_hub); }
    // Full list: the cold record for hubs, the hot record otherwise.
    void full(uint32_t v, Dir dr, ListView& lv) const {
        if (!is_hub(v, dr)) { hot(v, dr, lv); return; }
        auto& ix = cidx[dr];
        auto it = std::lower_bound(ix.begin(), ix.end() - 1, v, [](const ColdIdx& c, uint32_t x) { return c.node < x; });
        need(it != ix.end() - 1 && it->node == v, "graph cold index");
        const uint8_t* p = cold[dr].data() + it->off; const uint8_t* e = cold[dr].data() + (it + 1)->off;
        need(it->off <= (it + 1)->off && (it + 1)->off <= cold[dr].size(), "graph cold offsets");
        lv.entries.clear(); lv.top.clear(); lv.total = lv.stored = it->count; lv.entries.reserve(it->count);
        while (lv.entries.size() < it->count) p = decode_block(p, e, dr == out, lv.entries);
        need(lv.entries.size() == it->count && p == e, "graph cold list");
    }
    std::vector<DetailRow> details(uint32_t v) const { auto [b, e] = slice(det, v); return decode_details(b, e); }
    std::optional<NodeDetail> node_detail(uint32_t v) const { auto [b, e] = slice(ndet, v); if (b == e) return std::nullopt; return decode_node_detail(b, e); }
    // Day number of a block (0 = unknown / before the first known day).
    uint16_t day_of_block(uint64_t b) const {
        auto it = std::upper_bound(day_start.begin(), day_start.end(), b);
        return it == day_start.begin() ? 0 : uint16_t(day0 + uint32_t(it - day_start.begin() - 1));
    }
};
} // namespace machine::engine::graph
