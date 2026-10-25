// EMV2 graph reader: api.hpp graph::open/node_count/edge_count/node/edges/path/trace/dump over the
// segments described in graph_v2.hpp.  No Arrow.
#include "machine/engine/api.hpp"
#include "machine/engine/graph_v2.hpp"
#include "machine/engine/coldmap.hpp"
#include <deque>
#include <unordered_map>

namespace machine::engine::graph {

std::shared_ptr<const Image> open(const BuildDir& b) { return std::make_shared<const Image>(b); }
uint64_t node_count(const Image& g) { return g.n; }
uint64_t edge_count(const Image& g) { return g.entries; }

NodeAttr node(const Image& g, uint32_t id) {
    need(id < g.n, "graph node id");
    NodeAttr a{}; a.id = id; a.category = uint8_t(machine::graphloop::unknown); a.creator = none32; a.hot = true;
    const NodeV2& v = g.node[id];
    a.is_contract = v.flags & nv_contract; a.is_hub = v.flags & (nv_out_hub | nv_in_hub);
    a.out_degree = v.out_deg; a.in_degree = v.in_deg;
    if (auto d = g.node_detail(id)) {
        a.first_block = uint32_t(std::min<uint64_t>(d->fb, none32)); a.last_block = uint32_t(std::min<uint64_t>(d->lb, none32));
        a.tx_sent = d->tx_sent; a.tx_received = d->tx_recv; a.tt_in = d->tt_in; a.tt_out = d->tt_out;
    }
    return a;
}

namespace {
// false when le does not fit 256 bits
bool u256_le(std::string_view le, U256& r) { r = U256{}; if (le.size() > 32) return false; for (size_t i = 0; i < le.size(); i++) r.w[i / 8] |= uint64_t(uint8_t(le[i])) << (8 * (i % 8)); return true; }
// false on carry out of 256 bits
bool add256(U256& a, const U256& b) { unsigned __int128 c = 0; for (int i = 0; i < 4; i++) { c += (unsigned __int128)a.w[i] + b.w[i]; a.w[i] = uint64_t(c); c >>= 64; } return c == 0; }

bool day_overlap(const Image& g, const Entry& e, Side s, uint64_t b0, uint64_t b1) {
    if (s != s_out || (b0 == 0 && b1 == ~0ull)) return true;
    uint16_t d0 = g.day_of_block(b0), d1 = b1 == ~0ull ? 65535 : g.day_of_block(b1);
    uint16_t first = uint16_t(e.last_day - std::min<uint32_t>(e.span, e.last_day));
    return e.last_day >= d0 && first <= d1;
}
EdgeRec rec_of(const Entry& e, Side s) {
    EdgeRec r{}; r.peer = e.nbr; r.asset_tid = none32; r.type = EdgeType(e.kind); r.dir = s == s_out ? Dir::out : Dir::in;
    r.flags = 0; r.n = e.n; return r;
}

// Exact attributes from the cold detail rows (original map/edges rows, sorted by (to, kind, asset)).
// Rows of one source are decoded once per call; peers whose rows exceed this cap are left inexact
// (no ef_exact_n) rather than decoding a hub's whole history for one edge.
inline constexpr uint64_t peer_detail_cap = uint64_t(8) << 20;
bool block_overlap(uint64_t fb, uint64_t lb, uint64_t b0, uint64_t b1) { return lb >= b0 && fb <= b1; }
template <class Q> bool split_of(const Q& q) { if constexpr (requires { q.split_assets; }) return q.split_assets; else return false; }
// rows of src towards dst with kind, within the block window
std::vector<const DetailRow*> rows_to(const std::vector<DetailRow>& rows, uint32_t dst, uint8_t kind, uint64_t b0, uint64_t b1) {
    std::vector<const DetailRow*> o;
    auto it = std::lower_bound(rows.begin(), rows.end(), dst, [](const DetailRow& r, uint32_t d) { return r.d < d; });
    for (; it != rows.end() && it->d == dst; ++it) if (it->kind == kind && block_overlap(it->fb, it->lb, b0, b1)) o.push_back(&*it);
    return o;
}
EdgeRec exact_rec(const Image& g, const EdgeRec& base, const std::vector<const DetailRow*>& rows) {
    EdgeRec r = base; uint64_t n = 0, fb = ~0ull, lb = 0; U256 amt; bool any_amount = false, too_big = false; int assets = 0; uint32_t last = none32 - 2;
    for (auto* row : rows) {
        n += row->n; fb = std::min(fb, row->fb); lb = std::max(lb, row->lb);
        uint32_t a = (row->flags & df_asset_none) ? none32 : (row->flags & df_asset_raw) ? none32 - 1 : row->asset;
        if (a != last) { assets++; last = a; }
        if (!(row->flags & df_amount_null)) { U256 x; if (!u256_le(row->amount_le, x) || !add256(amt, x)) too_big = true; any_amount = true; }
    }
    r.n = n; r.flags |= ef_exact_n; r.first_block = uint32_t(std::min<uint64_t>(fb, none32)); r.last_block = uint32_t(std::min<uint64_t>(lb, none32));
    if (assets > 1) r.flags |= ef_multi_asset;
    else { if (any_amount && !too_big) { r.amount = amt; r.flags |= ef_amount; }   // > 2^256: left unset (not representable)
           if (!rows.empty() && !(rows[0]->flags & (df_asset_none | df_asset_raw))) { r.asset_tid = g.asset_tid(rows[0]->asset, rows[0]->kind); r.asset_node = rows[0]->asset; } }
    return r;
}
// Neighbours of v on one side, heaviest first (hubs: their hot top-256 unless full is asked and local).
void side_list(const Image& g, uint32_t v, Side s, bool want_full, ListView& lv, bool& from_cold, bool& cold_missing) {
    from_cold = false;
    if (want_full && g.is_hub(v, s)) { if (g.full(v, s, lv)) from_cold = true; else cold_missing = true; }
    else g.hot(v, s, lv);
}
std::vector<size_t> by_weight(const ListView& lv) {
    std::vector<size_t> o(lv.entries.size()); for (size_t i = 0; i < o.size(); i++) o[i] = i;
    std::stable_sort(o.begin(), o.end(), [&](size_t a, size_t b) { return lv.entries[a].wq > lv.entries[b].wq; });
    return o;
}
}

uint64_t hot_degree(const Image& g, uint32_t id) {
    if (id >= g.n) return 0;
    ListView lv; uint64_t d = 0;
    for (int sd = 0; sd < 2; sd++) { g.hot(id, Side(sd), lv); d += lv.total; }
    return d;
}

void prefetch_nodes(const Image& g, const std::vector<uint32_t>& ids) {
    if (!cold::Mapper::get().ready() || ids.empty()) return;
    std::vector<std::pair<const void*, size_t>> rg;
    for (uint32_t v : ids) if (v < g.n) g.node_detail_entry_ranges(v, rg);
    cold::Mapper::get().prefetch(rg);
    rg.clear();
    for (uint32_t v : ids) if (v < g.n) g.node_detail_ranges(v, rg);
    cold::Mapper::get().prefetch(rg);
}

EdgeResult edges(const Image& g, uint32_t id, const EdgeQuery& q) {
    need(id < g.n, "graph node id");
    EdgeResult res; ListView lv;
    for (int sd = 0; sd < 2; sd++) {
        Side s = Side(sd);
        if (!(uint8_t(q.dir) & (s == s_out ? uint8_t(Dir::out) : uint8_t(Dir::in)))) continue;
        bool from_cold = false;
        side_list(g, id, s, q.limit > hub_keep, lv, from_cold, res.cold_missing);
        res.total += lv.total;
        if (lv.stored < lv.total) res.truncated = true;
        for (size_t i : by_weight(lv)) {
            const Entry& e = lv.entries[i];
            if (!day_overlap(g, e, s, q.block0, q.block1)) continue;
            EdgeRec r = rec_of(e, s); if (from_cold) r.flags |= ef_from_cold;
            res.edges.push_back(r);
        }
    }
    // one weight order across both sides, then the limit
    std::stable_sort(res.edges.begin(), res.edges.end(), [](const EdgeRec& a, const EdgeRec& b) { return a.n > b.n; });
    if (res.edges.size() > q.limit) { res.edges.resize(q.limit); res.truncated = true; }
    if (q.want_amount || split_of(q)) {
        if (!g.has_details()) { res.cold_missing = true; return res; }
        // A hub's own detail rows can run to millions (one per (to, kind, asset)): reading them cost seconds to a minute on a
        // cold page cache. Hubs answer from their hot lists (exact n below the weight cap, amounts null), like a hub peer below.
        // cold tier: ask for the offset entries, then the rows, of this node and its in-peers in two batches (two round trips),
        // instead of one fault-and-wait per block while the rows are decoded
        {
            std::vector<std::pair<const void*, size_t>> rg;
            g.detail_entry_ranges(id, rg);
            for (auto& r : res.edges) if (r.dir == Dir::in) g.detail_entry_ranges(r.peer, rg);
            cold::Mapper::get().prefetch(rg);
            rg.clear();
            if (g.details_bytes(id) <= peer_detail_cap) g.detail_ranges(id, rg);
            for (auto& r : res.edges) if (r.dir == Dir::in && g.details_bytes(r.peer) <= peer_detail_cap) g.detail_ranges(r.peer, rg);
            cold::Mapper::get().prefetch(rg);
        }
        if (g.details_bytes(id) > peer_detail_cap) { res.cold_missing = true; return res; }
        // out side: this node's rows towards the kept peers only; in side: each source's rows towards this node (one scan per source)
        std::vector<uint32_t> outs;
        for (auto& r : res.edges) if (r.dir == Dir::out) outs.push_back(r.peer);
        std::sort(outs.begin(), outs.end()); outs.erase(std::unique(outs.begin(), outs.end()), outs.end());
        std::vector<DetailRow> own = g.details_to(id, outs);
        std::unordered_map<uint32_t, std::vector<DetailRow>> src_rows;
        std::vector<EdgeRec> out; bool split = split_of(q);
        for (auto& r : res.edges) {
            const std::vector<DetailRow>* rows = &own; uint32_t dst = r.peer;
            if (r.dir == Dir::in) {
                if (g.details_bytes(r.peer) > peer_detail_cap) { out.push_back(r); continue; }   // hub source: inexact
                auto it = src_rows.find(r.peer);
                if (it == src_rows.end()) it = src_rows.emplace(r.peer, g.details_to(r.peer, {id})).first;
                rows = &it->second; dst = id;
            }
            auto sel = rows_to(*rows, dst, uint8_t(r.type), q.block0, q.block1);
            if (sel.empty()) continue;   // no activity inside the block window
            if (!split) { out.push_back(exact_rec(g, r, sel)); continue; }
            for (size_t i = 0; i < sel.size();) {   // one record per asset (rows are sorted by asset within (to, kind))
                size_t j = i + 1;
                while (j < sel.size() && (sel[j]->flags & 0x30) == (sel[i]->flags & 0x30) && sel[j]->asset == sel[i]->asset &&
                       std::memcmp(sel[j]->asset_raw.b, sel[i]->asset_raw.b, 20) == 0) j++;
                out.push_back(exact_rec(g, r, std::vector<const DetailRow*>(sel.begin() + long(i), sel.begin() + long(j))));
                i = j;
            }
        }
        res.edges = std::move(out);
    }
    return res;
}

// Bidirectional BFS (undirected by default). Hubs are not expanded (the trace stops there), except the
// endpoints themselves, which expand up to endpoint_fanout of their heaviest neighbours.
PathResult path(const Image& g, const PathQuery& q) {
    PathResult res;
    need(q.from < g.n && q.to < g.n, "graph path endpoints");
    if (q.from == q.to) { res.found = true; res.nodes = {q.from}; return res; }
    struct Seen { uint32_t parent; EdgeRec via; uint8_t depth; };
    std::unordered_map<uint32_t, Seen> seen[2];
    std::deque<uint32_t> frontier[2];
    seen[0][q.from] = {none32, {}, 0}; seen[1][q.to] = {none32, {}, 0};
    frontier[0].push_back(q.from); frontier[1].push_back(q.to);
    ListView lv; uint32_t meet = none32;
    uint32_t depth_lim[2] = {(q.max_depth + 1) / 2, q.max_depth / 2};
    while (meet == none32 && (!frontier[0].empty() || !frontier[1].empty())) {
        int side = frontier[0].empty() ? 1 : frontier[1].empty() ? 0 : (frontier[0].size() <= frontier[1].size() ? 0 : 1);
        size_t layer = frontier[side].size();
        for (size_t li = 0; li < layer && meet == none32; li++) {
            uint32_t v = frontier[side].front(); frontier[side].pop_front();
            auto& sv = seen[side][v];
            if (sv.depth >= depth_lim[side]) continue;
            bool endpoint = v == q.from || v == q.to;
            if (!endpoint && g.node[v].flags & (nv_out_hub | nv_in_hub)) continue;
            if (res.expanded >= q.budget || (q.deadline_ns && (res.expanded & 15) == 0 && machine::graphloop::mono_ns() > q.deadline_ns)) {
                res.budget_hit = true; frontier[0].clear(); frontier[1].clear(); break;
            }
            res.expanded++;
            uint32_t fan = endpoint ? q.endpoint_fanout : q.fanout;
            // forward search walks out-edges (and in-edges when undirected); backward search the reverse
            for (int sd = 0; sd < 2 && meet == none32; sd++) {
                Side s = Side(sd);
                bool forward_side = side == 0 ? (s == s_out) : (s == s_in);
                if (!q.undirected && !forward_side) continue;
                g.hot(v, s, lv);
                uint32_t taken = 0;
                for (size_t i : by_weight(lv)) {
                    if (taken >= fan) break;
                    const Entry& e = lv.entries[i];
                    if (!day_overlap(g, e, s, q.block0, q.block1)) continue;
                    taken++;
                    if (seen[side].count(e.nbr)) continue;
                    seen[side][e.nbr] = {v, rec_of(e, s), uint8_t(sv.depth + 1)};
                    if (seen[1 - side].count(e.nbr)) { meet = e.nbr; break; }
                    frontier[side].push_back(e.nbr);
                }
            }
        }
    }
    if (meet == none32) return res;
    res.found = true;
    std::vector<uint32_t> a, b; std::vector<EdgeRec> ea, eb;
    for (uint32_t x = meet; x != none32; x = seen[0][x].parent) { a.push_back(x); if (seen[0][x].parent != none32) ea.push_back(seen[0][x].via); }
    for (uint32_t x = meet; x != none32; x = seen[1][x].parent) { if (x != meet) b.push_back(x); if (seen[1][x].parent != none32) eb.push_back(seen[1][x].via); }
    std::reverse(a.begin(), a.end()); std::reverse(ea.begin(), ea.end());
    res.nodes = a; res.nodes.insert(res.nodes.end(), b.begin(), b.end());
    res.edges = ea; res.edges.insert(res.edges.end(), eb.begin(), eb.end());
    // edges of the backward half point at the node nearer to `to`; flip dir so it is relative to the walk
    for (size_t i = ea.size(); i < res.edges.size(); i++) res.edges[i].dir = res.edges[i].dir == Dir::out ? Dir::in : Dir::out;
    return res;
}

TraceResult trace(const Image& g, const TraceQuery& q) {
    TraceResult res; need(q.root < g.n, "graph trace root");
    std::unordered_map<uint32_t, uint32_t> depth; depth[q.root] = 0; res.nodes.push_back(q.root);
    std::deque<uint32_t> fr{q.root}; ListView lv; uint32_t expanded = 0;
    while (!fr.empty()) {
        uint32_t v = fr.front(); fr.pop_front();
        uint32_t d = depth[v];
        if (d >= q.hops) continue;
        if (v != q.root && g.node[v].flags & (nv_out_hub | nv_in_hub)) { res.stops.push_back(v); continue; }
        if (expanded >= q.budget || (q.deadline_ns && (expanded & 15) == 0 && machine::graphloop::mono_ns() > q.deadline_ns)) { res.budget_hit = true; break; }
        expanded++;
        for (int sd = 0; sd < 2; sd++) {
            Side s = Side(sd);
            if (!(uint8_t(q.dir) & (s == s_out ? uint8_t(Dir::out) : uint8_t(Dir::in)))) continue;
            g.hot(v, s, lv);
            uint32_t taken = 0;
            for (size_t i : by_weight(lv)) {
                if (taken >= q.fanout) break;
                const Entry& e = lv.entries[i];
                if (!day_overlap(g, e, s, q.block0, q.block1)) continue;
                taken++;
                res.edges.push_back({v, rec_of(e, s)});
                if (!depth.count(e.nbr)) { depth[e.nbr] = d + 1; res.nodes.push_back(e.nbr); fr.push_back(e.nbr); }
            }
        }
    }
    return res;
}

// ------------------------------------------------------------------ dump (verify contract)
namespace {
std::string hexa(const Addr& a) { return machine::graphloop::hex0x(a); }
struct Acc288 {   // 640-bit accumulator for exact totals of up to 512-bit rows
    std::array<uint32_t, 20> w{};
    void add_le(std::string_view le) {
        uint64_t carry = 0;
        for (size_t i = 0; i < w.size(); i++) {
            uint32_t x = 0; for (size_t b = 0; b < 4; b++) { size_t k = i * 4 + b; if (k < le.size()) x |= uint32_t(uint8_t(le[k])) << (8 * b); }
            uint64_t t = uint64_t(w[i]) + x + carry; w[i] = uint32_t(t); carry = t >> 32;
        }
        need(carry == 0, "accumulator overflow");
    }
    std::string le() const { std::string s; for (auto x : w) for (int b = 0; b < 4; b++) s += char(uint8_t(x >> (8 * b))); while (!s.empty() && s.back() == '\0') s.pop_back(); return s; }
};
}

void dump(const BuildDir& b, std::string_view table, const DumpFilter& f, CsvOut& out) {
    Image g(b);
    std::vector<uint32_t> nodes;
    if (f.all || table == "counts") { nodes.resize(g.n); for (uint32_t i = 0; i < g.n; i++) nodes[i] = i; }
    else nodes = f.addrs;
    if (table == "edges") {
        need(g.has_details(), "g.edge.exact not local");
        out.row({"from", "to", "asset", "kind", "n", "total_amount", "first_block", "last_block"});
        for (uint32_t v : nodes) {   // --addresses: rows whose "from" is listed (map edges are bucketed by from)
            std::string from = hexa(g.addr(v));
            for (auto& r : g.details(v)) {
                std::string to = hexa(g.addr(r.d)), asset, amount, n = std::to_string(r.n), fb = std::to_string(r.fb), lb = std::to_string(r.lb);
                if (r.flags & df_asset_raw) asset = hexa(r.asset_raw); else if (!(r.flags & df_asset_none)) asset = hexa(g.addr(r.asset));
                if (!(r.flags & df_amount_null)) amount = le_to_decimal(r.amount_le);
                out.row({from, to, asset, edge_kind_text(r.kind), n, amount, fb, lb});
            }
        }
    } else if (table == "nodes") {
        need(g.has_node_details(), "g.node.cold not local");
        out.row({"address", "first_block", "last_block", "tx_sent", "tx_received", "token_transfers_in", "token_transfers_out", "is_contract", "in_deg", "out_deg", "v2_in_entries", "v2_out_entries"});
        for (uint32_t v : nodes) {
            auto d = g.node_detail(v); if (!d) continue;
            std::string c[11] = {std::to_string(d->fb), std::to_string(d->lb), std::to_string(d->tx_sent), std::to_string(d->tx_recv), std::to_string(d->tt_in),
                                 std::to_string(d->tt_out), d->contract ? "true" : "false", std::to_string(d->map_in), std::to_string(d->map_out),
                                 std::to_string(g.node[v].in_deg), std::to_string(g.node[v].out_deg)};
            out.row({hexa(g.addr(v)), c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], c[9], c[10]});
        }
    } else if (table == "counts") {   // totals recomputed from the image (header and build_id rows come from emv2-dump)
        uint64_t rows = 0; std::array<uint64_t, 16> n_kind{}, rows_kind{}; Acc288 native;
        uint64_t nn = 0, tx_s = 0, tx_r = 0, tti = 0, tto = 0, contracts = 0, oe = 0, ie = 0;
        bool det = g.has_details(), nd = g.has_node_details();
        for (uint32_t v = 0; v < g.n; v++) {
            if (det) for (auto& r : g.details(v)) { rows++; n_kind[r.kind] += r.n; rows_kind[r.kind]++; if (r.kind == 0 && !(r.flags & df_amount_null)) native.add_le(r.amount_le); }
            if (nd) if (auto d = g.node_detail(v)) { nn++; tx_s += d->tx_sent; tx_r += d->tx_recv; tti += d->tt_in; tto += d->tt_out; contracts += d->contract; }
            oe += g.node[v].out_deg; ie += g.node[v].in_deg;
        }
        auto kv = [&](const std::string& k, const std::string& val) { out.row({k, val}); };
        if (det) {
            kv("edges_rows", std::to_string(rows));
            for (int k = 0; k < 4; k++) { kv(std::string("edges_n_") + edge_kind_text(uint8_t(k)), std::to_string(n_kind[size_t(k)])); kv(std::string("edges_rows_") + edge_kind_text(uint8_t(k)), std::to_string(rows_kind[size_t(k)])); }
            uint64_t other = 0; for (int k = 4; k < 16; k++) other += rows_kind[size_t(k)]; kv("edges_rows_other_kinds", std::to_string(other));
            kv("native_total_amount", le_to_decimal(native.le()));
        }
        if (nd) { kv("nodes", std::to_string(nn)); kv("sum_tx_sent", std::to_string(tx_s)); kv("sum_tx_received", std::to_string(tx_r));
                  kv("sum_token_transfers_in", std::to_string(tti)); kv("sum_token_transfers_out", std::to_string(tto)); kv("n_is_contract", std::to_string(contracts)); }
        kv("graph_nodes", std::to_string(g.n)); kv("graph_out_entries", std::to_string(oe)); kv("graph_in_entries", std::to_string(ie));
        std::string sig; for (auto& i : b.manifest()["inputs"]) sig += i.dump();
        kv("input_sig", machine::graphloop::hex(View(machine::graphloop::sha256(sig).data(), 32)));
    } else throw std::runtime_error("graph dump table: edges|nodes|counts");
}
} // namespace machine::engine::graph
