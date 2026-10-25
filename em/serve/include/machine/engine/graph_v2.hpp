#pragma once
// EMV2 graph image "CSR-T" (design EM-ENGINE-V2.md 5.3): both directions, time on every edge, Elias-Fano
// neighbour blocks, hubs truncated to their top-256 neighbours in the hot tier with the full list in the
// cold tier, and the original map/edges rows kept verbatim in a cold detail stream (exact n, amounts,
// assets) so the image can be compared row by row with the map.
//
// Segments in the build directory (core seg.hpp: payload + 64-byte trailer; Kind names from api.hpp):
//   dict.addr.{sfx,pfx}  core AddrDict, written by the graph builder from map/addresses; node id = byte-order rank
//   g.node.attr          NodeV2[N] (16 B hot node record)
//   g.meta               u64[] first block of day aux+i (non-decreasing)
//   g.out.off / g.in.off offsets: u32 rel[N+1] (offset inside the node's 256-node block), zero pad to 8, then
//                        u64 blk[N/256+2]; count = N, aux = blk entries, aux2 = byte offset of blk
//   g.out.adj / g.in.adj one list record per node (hot); an empty record = no edges
//   g.out.attr / g.in.attr  ColdIdx[C+1]: hubs of that direction (sorted) + sentinel (hot)
//   g.hub.cold.001 / .002   full lists of out / in hubs (blocks only; count from ColdIdx) (cold)
//   g.edge.exact.001 / .002 offsets (u64 rel) / original map/edges rows per source (cold)
//   g.node.cold.001 / .002  offsets (u64 rel) / original map/addresses rows (cold)
//
// List record: varint (K << 1 | truncated), varint D (full count) only when truncated (hubs), ceil(K/128)
// blocks, then, in hot records with K > 64, 64 x u16 positions of the top-64 entries by weight.  Entries
// are (neighbour, kind) pairs sorted by (neighbour, kind); duplicates of a neighbour carry different kinds.
// Block of c = min(128, K - 128 i) entries: v0 as id_bytes LE (bytes of N-1); when c > 1 varint xmax, low
// bits (c*l), high bits (xmax>>l)+c (Elias-Fano over x_i = v_i - v0, l = floor(log2((xmax+1)/c)) when
// xmax+1 > c); c x wk (weight class << 4 | kind); out direction only: c x zigzag varint (node last_day -
// edge last_day), c x varint (edge last_day - first_day).
// Detail row: varint d (delta to the previous row's d in this source; the first row absolute), u8 kind |
// flags, asset (varint id+1 or 20 raw bytes, see flags), varint n, varint first_block, varint
// (last_block - first_block), amount (u8 length + little-endian bytes) unless the amount is NULL.
#include "machine/engine/dict.hpp"
#include "machine/graphloop/durable.hpp"
#include "machine/graphloop/schema.hpp"
#include <nlohmann/json.hpp>
#include <bit>
#include <optional>
#include <queue>

namespace machine::engine::graph {
using namespace machine::graphloop;
using json = nlohmann::json;
using machine::graphloop::Addr;
using machine::graphloop::View;
using machine::graphloop::need;

inline constexpr const char* graph_layout = "emv2.graph.v2";   // manifest facts.graph.layout; readers refuse others
inline constexpr uint32_t block_nodes = 256;     // offset table granularity
inline constexpr uint32_t ef_block = 128;        // entries per Elias-Fano block
inline constexpr uint32_t hub_degree = 1000;     // more entries than this: hub
inline constexpr uint32_t hub_keep = 256;        // hot entries kept for a hub
inline constexpr uint32_t top_n = 64;            // top-by-weight positions stored for K > 64
inline constexpr uint64_t day_epoch = 1438214400; // 2015-07-30 00:00 UTC (Ethereum genesis day)

enum Side : int { s_out = 0, s_in = 1 };
inline constexpr const char* side_name[2] = {"out", "in"};

// Edge kinds: graphloop EdgeType codes (map/edges uses the first four).
inline const char* edge_kind_text(uint8_t k) { return k == 0 ? "native_tx" : k < edge_type_count ? edge_names[k].data() : "other"; }

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

// Per-list coding context: neighbour ids are fixed id_bytes little-endian (bytes of N-1), and out-edge
// last days are stored relative to the node's own last day (ref_day, NodeV2::last_day).
struct ListFmt { uint32_t id_bytes = 4; uint16_t ref_day = 0; };
inline uint32_t id_bytes_for(uint64_t n) { uint32_t b = 1; while (b < 4 && n > (uint64_t(1) << (8 * b))) b++; return b; }
inline void put_id(std::string& o, uint32_t v, uint32_t nb) { for (uint32_t i = 0; i < nb; i++) o += char(uint8_t(v >> (8 * i))); }
inline uint32_t get_id(const uint8_t*& p, const uint8_t* e, uint32_t nb) { need(uint64_t(e - p) >= nb, "graph id"); uint32_t v = 0; for (uint32_t i = 0; i < nb; i++) v |= uint32_t(p[i]) << (8 * i); p += nb; return v; }
inline uint64_t zigzag(int64_t v) { return (uint64_t(v) << 1) ^ uint64_t(v >> 63); }
inline int64_t unzigzag(uint64_t z) { return int64_t(z >> 1) ^ -int64_t(z & 1); }

// Block of c entries (c is implied by the list: min(128, K - 128 i)).
inline void encode_block(std::string& o, const Entry* e, uint32_t c, bool with_time, ListFmt f) {
    need(c >= 1 && c <= ef_block, "graph block size");
    uint64_t v0 = e[0].nbr, xmax = uint64_t(e[c - 1].nbr) - v0;
    put_id(o, uint32_t(v0), f.id_bytes);
    if (c > 1) {
        uint32_t l = (xmax + 1 > c) ? uint32_t(63 - std::countl_zero((xmax + 1) / c)) : 0;
        put_varint(o, xmax);
        { BitWriter w(o, uint64_t(c) * l); for (uint32_t i = 0; i < c; i++) w.put(uint64_t(i) * l, (e[i].nbr - v0) & ((uint64_t(1) << l) - 1), l); }
        { BitWriter w(o, (xmax >> l) + c); for (uint32_t i = 0; i < c; i++) w.set(((e[i].nbr - v0) >> l) + i); }
    }
    for (uint32_t i = 0; i < c; i++) o += char(uint8_t(e[i].wq << 4 | (e[i].kind & 15)));
    if (with_time) {
        for (uint32_t i = 0; i < c; i++) put_varint(o, zigzag(int64_t(f.ref_day) - int64_t(e[i].last_day)));
        for (uint32_t i = 0; i < c; i++) put_varint(o, e[i].span);
    }
}
// Appends the block's c entries to out; returns the pointer after the block.
inline const uint8_t* decode_block(const uint8_t* p, const uint8_t* end, uint32_t c, bool with_time, ListFmt f, std::vector<Entry>& out) {
    need(c >= 1 && c <= ef_block, "graph block size");
    uint64_t v0 = get_id(p, end, f.id_bytes);
    size_t base = out.size(); out.resize(base + c);
    if (c == 1) out[base].nbr = uint32_t(v0);
    else {
        uint64_t xmax = get_varint(p, end);
        uint32_t l = (xmax + 1 > c) ? uint32_t(63 - std::countl_zero((xmax + 1) / c)) : 0;
        uint64_t low_bytes = (uint64_t(c) * l + 7) / 8, high_bits = (xmax >> l) + c, high_bytes = (high_bits + 7) / 8;
        need(uint64_t(end - p) >= low_bytes + high_bytes, "graph block bounds");
        const uint8_t* low = p; const uint8_t* high = p + low_bytes; p += low_bytes + high_bytes;
        uint64_t pos = 0;
        for (uint32_t i = 0; i < c; i++) {
            while (pos < high_bits && !((high[pos / 8] >> (pos % 8)) & 1)) pos++;
            need(pos < high_bits, "graph block high bits");
            uint64_t hi = pos - i; pos++;
            uint64_t x = (hi << l) | (l ? get_bits(low, uint64_t(i) * l, l) : 0);
            need(x <= xmax, "graph block value");
            out[base + i].nbr = uint32_t(v0 + x);
        }
    }
    need(uint64_t(end - p) >= c, "graph block weights");
    for (uint32_t i = 0; i < c; i++) { uint8_t wk = *p++; out[base + i].kind = wk & 15; out[base + i].wq = wk >> 4; out[base + i].n = weight_floor(wk >> 4); }
    if (with_time) {
        for (uint32_t i = 0; i < c; i++) out[base + i].last_day = uint16_t(int64_t(f.ref_day) - unzigzag(get_varint(p, end)));
        for (uint32_t i = 0; i < c; i++) out[base + i].span = uint32_t(get_varint(p, end));
    } else for (uint32_t i = 0; i < c; i++) { out[base + i].last_day = 0; out[base + i].span = 0; }
    return p;
}
// Decodes count entries stored as consecutive blocks (cold hub lists, and the body of a list record).
inline const uint8_t* decode_blocks(const uint8_t* p, const uint8_t* end, uint64_t count, bool with_time, ListFmt f, std::vector<Entry>& out) {
    for (uint64_t done = 0; done < count; done += ef_block) p = decode_block(p, end, uint32_t(std::min<uint64_t>(ef_block, count - done)), with_time, f, out);
    return p;
}
inline void encode_blocks(std::string& o, std::span<const Entry> e, bool with_time, ListFmt f) {
    for (size_t i = 0; i < e.size(); i += ef_block) encode_block(o, e.data() + i, uint32_t(std::min<size_t>(ef_block, e.size() - i)), with_time, f);
}

// List record: varint (K << 1 | truncated), varint D when truncated, the K entries as blocks, then 64 x u16
// top positions when with_top and K > 64.  e: stored entries sorted by (nbr, kind); total = full count D.
inline void encode_list(std::string& o, std::span<const Entry> e, uint64_t total, bool with_time, bool with_top, ListFmt f) {
    need(total >= e.size(), "graph list total");
    bool trunc = total != e.size();
    put_varint(o, (uint64_t(e.size()) << 1) | (trunc ? 1 : 0)); if (trunc) put_varint(o, total);
    encode_blocks(o, e, with_time, f);
    if (with_top && e.size() > top_n) {
        std::vector<uint32_t> pos(e.size()); for (uint32_t i = 0; i < pos.size(); i++) pos[i] = i;
        std::partial_sort(pos.begin(), pos.begin() + top_n, pos.end(), [&](uint32_t a, uint32_t b) { return entry_weight_less(e[a], e[b]); });
        for (uint32_t i = 0; i < top_n; i++) put_u16(o, uint16_t(pos[i]));
    }
}
struct ListView { uint64_t total = 0, stored = 0; std::vector<Entry> entries; std::vector<uint16_t> top; };
inline void decode_list(const uint8_t* p, const uint8_t* end, bool with_time, bool with_top, ListFmt f, ListView& lv) {
    lv.entries.clear(); lv.top.clear(); lv.total = lv.stored = 0;
    if (p == end) return;
    uint64_t h = get_varint(p, end); lv.stored = h >> 1; lv.total = (h & 1) ? get_varint(p, end) : lv.stored;
    need(lv.stored <= lv.total, "graph list counts");
    lv.entries.reserve(lv.stored);
    p = decode_blocks(p, end, lv.stored, with_time, f, lv.entries);
    if (with_top && lv.stored > top_n) { lv.top.resize(top_n); for (auto& t : lv.top) { t = get_u16(p, end); need(t < lv.stored, "graph top position"); } }
    need(p == end, "graph list trailing bytes");
}

// ------------------------------------------------------------------ amounts (exact, up to 512 bits)
// Map totals can pass 2^256 (spam max-uint transfers summed): rows keep up to 64 magnitude bytes and
// readers report such a total as not representable (no ef_amount) instead of wrapping it.
inline constexpr size_t amount_max_bytes = 64;
// Little-endian magnitude bytes without trailing zeros; "0" -> empty.
inline bool decimal_to_le(std::string_view s, std::string& out) {
    out.clear(); if (s.empty()) return false;
    std::array<uint32_t, 17> w{};   // 544 bits
    for (char ch : s) {
        if (ch < '0' || ch > '9') return false;
        uint64_t carry = uint64_t(ch - '0');
        for (auto& x : w) { uint64_t t = uint64_t(x) * 10 + carry; x = uint32_t(t); carry = t >> 32; }
        if (carry) return false;
    }
    for (int i = 0; i < 68; i++) out += char(uint8_t(w[size_t(i / 4)] >> (8 * (i % 4))));
    while (!out.empty() && out.back() == '\0') out.pop_back();
    return out.size() <= amount_max_bytes;
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
        if (!(r.flags & df_amount_null)) { need(p < end, "graph detail amount"); size_t L = *p++; need(size_t(end - p) >= L && L <= amount_max_bytes, "graph detail amount bytes"); r.amount_le.assign(reinterpret_cast<const char*>(p), L); p += L; }
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
// Offsets segment: rel u32/u64 per node + blk u64 per 256-node block.
struct OffTable {
    std::span<const uint64_t> blk; const uint8_t* rel = nullptr; bool wide = false; std::span<const uint8_t> bytes;
    bool valid() const { return rel != nullptr; }
    uint64_t r(uint32_t v) const { uint64_t x; if (wide) std::memcpy(&x, rel + size_t(v) * 8, 8); else { uint32_t y; std::memcpy(&y, rel + size_t(v) * 4, 4); x = y; } return x; }
    std::pair<const uint8_t*, const uint8_t*> slice(uint32_t v) const {
        uint64_t b = blk[v / block_nodes] + r(v);
        uint64_t e = ((v + 1) % block_nodes ? blk[v / block_nodes] : blk[(v + 1) / block_nodes]) + r(v + 1);
        need(b <= e && e <= bytes.size(), "graph offsets");
        return {bytes.data() + b, bytes.data() + e};
    }
};
inline OffTable off_table(const SegView& off, const SegView& data, uint64_t n, bool wide) {
    OffTable t; auto p = off.payload();
    need(off.count() == n && off.aux2() == ((n + 1) * (wide ? 8 : 4) + 7) / 8 * 8 && off.aux() == n / block_nodes + 2
         && p.size() == off.aux2() + off.aux() * 8, "graph offsets shape");
    t.rel = p.data(); t.wide = wide; t.blk = {reinterpret_cast<const uint64_t*>(p.data() + off.aux2()), size_t(off.aux())}; t.bytes = data.payload();
    return t;
}

// An opened graph build (api.hpp graph::Image). Cold segments are optional locally (cold_missing).
class Image {
    SegView node_s_, meta_s_, off_s_[2], adj_s_[2], attr_s_[2], hub_s_[2], det_off_s_, det_s_, ndet_off_s_, ndet_s_;
    static bool local(const BuildDir& b, Kind k, uint32_t part = 0) { return b.has(k, part) && fs::exists(b.dir() / segment_file_name(k, part)); }
public:
    BuildDir build; AddrDict dict; uint32_t n = 0; uint64_t entries = 0;
    std::unique_ptr<TokenDict> tok;   // ledger's dict.tok when present in the build (asset_tid), else null
    std::span<const NodeV2> node; std::span<const uint64_t> day_start; uint32_t day0 = 0;
    OffTable adj[2], det, ndet; std::span<const ColdIdx> cidx[2]; std::span<const uint8_t> cold[2];

    explicit Image(const BuildDir& b) : build(b), dict(b) {
        need(b.facts("graph").value("layout", "") == graph_layout, "graph layout is not " + std::string(graph_layout) + " (rebuild the graph part)");
        n = uint32_t(dict.size()); id_bytes = id_bytes_for(n);
        node_s_ = b.open(Kind::g_node_attr, 0, false, MADV_RANDOM); node = node_s_.as<NodeV2>(); need(node.size() == n, "graph node shape");
        meta_s_ = b.open(Kind::g_meta); day_start = meta_s_.as<uint64_t>(); day0 = uint32_t(meta_s_.aux());
        const Kind offk[2] = {Kind::g_out_off, Kind::g_in_off}, adjk[2] = {Kind::g_out_adj, Kind::g_in_adj}, attrk[2] = {Kind::g_out_attr, Kind::g_in_attr};
        for (int d = 0; d < 2; d++) {
            off_s_[d] = b.open(offk[d], 0, false, MADV_RANDOM); adj_s_[d] = b.open(adjk[d], 0, false, MADV_RANDOM);
            adj[d] = off_table(off_s_[d], adj_s_[d], n, false);
            attr_s_[d] = b.open(attrk[d]); cidx[d] = attr_s_[d].as<ColdIdx>(); need(!cidx[d].empty() && cidx[d].back().node == none, "graph hub index");
            if (local(b, Kind::g_hub_cold, uint32_t(d + 1))) { hub_s_[d] = b.open(Kind::g_hub_cold, uint32_t(d + 1), false, MADV_RANDOM); cold[d] = hub_s_[d].payload(); }
            entries += d == 0 ? adj_s_[d].count() : 0;
        }
        if (local(b, Kind::g_edge_exact, 1) && local(b, Kind::g_edge_exact, 2)) {
            det_off_s_ = b.open(Kind::g_edge_exact, 1, false, MADV_RANDOM); det_s_ = b.open(Kind::g_edge_exact, 2, false, MADV_RANDOM);
            det = off_table(det_off_s_, det_s_, n, true);
        }
        if (local(b, Kind::dict_tok) && local(b, Kind::dict_tok_str)) tok = std::make_unique<TokenDict>(b);
        if (local(b, Kind::g_node_cold, 1) && local(b, Kind::g_node_cold, 2)) {
            ndet_off_s_ = b.open(Kind::g_node_cold, 1, false, MADV_RANDOM); ndet_s_ = b.open(Kind::g_node_cold, 2, false, MADV_RANDOM);
            ndet = off_table(ndet_off_s_, ndet_s_, n, true);
        }
    }
    Addr addr(uint32_t v) const { return dict.get(v); }
    uint32_t find(const Addr& a) const { auto r = dict.find(a); return r ? *r : none; }
    uint32_t find(std::string_view hexaddr) const { Addr a{}; return parse_addr(hexaddr, a) ? find(a) : none; }
    bool is_hub(uint32_t v, Side s) const { return node[v].flags & (s == s_out ? nv_out_hub : nv_in_hub); }
    bool has_cold_lists(Side s) const { return !cold[s].empty() || cidx[s].size() == 1; }
    bool has_details() const { return det.valid(); }
    bool has_node_details() const { return ndet.valid(); }
    // Hot list (hubs: top-256 by weight; lv.total = full count).
    uint32_t id_bytes = 4;
    ListFmt fmt(uint32_t v) const { return {id_bytes, node[v].last_day}; }
    void hot(uint32_t v, Side s, ListView& lv) const { auto [b, e] = adj[s].slice(v); decode_list(b, e, s == s_out, true, fmt(v), lv); }
    // Full list: cold record for hubs (false when the cold segment is not local), hot record otherwise.
    bool full(uint32_t v, Side s, ListView& lv) const {
        if (!is_hub(v, s)) { hot(v, s, lv); return true; }
        if (!has_cold_lists(s)) { hot(v, s, lv); return false; }
        auto& ix = cidx[s];
        auto it = std::lower_bound(ix.begin(), ix.end() - 1, v, [](const ColdIdx& c, uint32_t x) { return c.node < x; });
        need(it != ix.end() - 1 && it->node == v, "graph cold index");
        need(it->off <= (it + 1)->off && (it + 1)->off <= cold[s].size(), "graph cold offsets");
        const uint8_t* p = cold[s].data() + it->off; const uint8_t* e = cold[s].data() + (it + 1)->off;
        lv.entries.clear(); lv.top.clear(); lv.total = lv.stored = it->count; lv.entries.reserve(it->count);
        p = decode_blocks(p, e, it->count, s == s_out, fmt(v), lv.entries);
        need(lv.entries.size() == it->count && p == e, "graph cold list");
        return true;
    }
    std::vector<DetailRow> details(uint32_t v) const { if (!det.valid()) return {}; auto [b, e] = det.slice(v); return decode_details(b, e); }
    uint64_t details_bytes(uint32_t v) const { if (!det.valid()) return 0; auto [b, e] = det.slice(v); return uint64_t(e - b); }
    // tid of an edge asset (node id + edge kind 1..3), none when unknown or no token dictionary.
    uint32_t asset_tid(uint32_t asset_node, uint8_t kind) const {
        if (!tok || asset_node == none || kind < 1 || kind > 3) return none;
        auto t = tok->find(asset_node, TokenKind(kind)); return t ? *t : none;
    }
    std::optional<NodeDetail> node_detail(uint32_t v) const { if (!ndet.valid()) return std::nullopt; auto [b, e] = ndet.slice(v); if (b == e) return std::nullopt; return decode_node_detail(b, e); }
    // Day number of a block (0 = unknown / before the first known day).
    uint16_t day_of_block(uint64_t b) const {
        auto it = std::upper_bound(day_start.begin(), day_start.end(), b);
        return it == day_start.begin() ? 0 : uint16_t(day0 + uint32_t(it - day_start.begin() - 1));
    }
};
} // namespace machine::engine::graph
