#pragma once
// EM-ENGINE-V2 public contract (owner: core). Every module codes against this file:
//   * format constants and the segment Kind catalogue (stable numbers, never renumber);
//   * token event semantics (decided by core, §SEMANTICS below);
//   * reader APIs that graph and ledger implement and serve / dump / verify call.
// Implementations live in the owning module's files (graph_v2.hpp + src/lib/graph*.cpp,
// ledger.hpp + src/lib/ledger*.cpp). Changes to this file go through core.
#include "machine/engine/u256.hpp"
#include "machine/graphloop/schema.hpp"
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <functional>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace machine::engine {

using View = std::span<const uint8_t>;
using Bytes = std::vector<uint8_t>;
using machine::graphloop::Category;
using machine::graphloop::EdgeType;

inline constexpr uint32_t format_version = 1;   // build layout; bump = full rebuild
inline constexpr uint32_t seg_version = 1;      // segment trailer
inline constexpr std::string_view manifest_schema = "economic-machine.emv2.build.v1";
inline constexpr uint32_t none32 = 0xffffffffu;
// Module read layouts. Owners request a bump here when their on-disk layout changes; build_id_of()
// folds them in, so a layout change can never reuse a build_id (and overwrite a published build).
inline constexpr std::string_view layout_graph = "emv2.graph.v2";
inline constexpr uint32_t layout_ledger = 2;

// ------------------------------------------------------------------ SEMANTICS (token events)
// Source: extraction token_transfers (receipts with status 1 only), ordered by
// (block_number, tx_index, log_index, batch_index). Identical to mapping/build_map.sql:
//  * TokenKind 1 erc20  = ERC20 + WETH_DEPOSIT (from = 0x0) + WETH_WITHDRAWAL (to = 0x0)
//              2 erc721 = ERC721 (amount = 1 per transfer; balance = n_in - n_out)
//              3 erc1155 = ERC1155_SINGLE + one row per ERC1155_BATCH element
//    Unknown standards are skipped and counted (facts.n_tt_unknown).
//  * tid = rank of (token address, kind); one contract can carry two kinds -> two tids.
//  * Failed transactions emit no logs, so nothing is filtered; native value is graph-only.
//  * Mint: from == 0x0 -> supply += amount. Burn: to == 0x0 -> supply -= amount.
//    supply_prefix therefore equals totalSupply for standard tokens. 0x...dEaD is NOT a burn for
//    supply, but it and 0x0 are excluded from holder counts (burn_sink()).
//  * Balances are exact signed integers. Accumulation may exceed 256 bits (spam max-uint
//    transfers): ledger accumulates in 320 bits; APIs return I256 + overflow = true when it does
//    not fit. Negative balances (rebasing / missing events) stay exact; the token gets
//    TokenFlag::irregular. Holders = addresses with balance > 0 (burn sinks excluded).
//  * Self transfers (from == to) count as events, change no balance and no holder count.
//  * WETH9-family wrapped native tokens (registry/weth9.json: WETH9, WPOL/WMATIC, wrapped native on arc, ...) mint and burn with
//    Deposit(dst, wad) / Withdrawal(src, wad), not Transfer. The extractor decodes them only for allowlisted emitters
//    (WETH_DEPOSIT / WETH_WITHDRAWAL rows); for the others emv2-ledger-weth turns the logs into the same rows (Deposit = 0x0 -> dst,
//    Withdrawal = src -> 0x0) and emv2-build-ledger --extra adds them, skipping a token whose WETH_ rows the extractor already has.
//    Check: contract native balance (RPC) == supply_prefix + sum of ERC20 Transfers *to* 0x0 (WETH sent to 0x0 stays in the
//    contract but is a burn here) + the net of any rows left out by --allow-missing (facts.n_addr_missing).
//  * erc1155: bal, holders and supply are kept per (contract, kind) summed over all token ids --
//    exactly what map address_tokens does (parity for row-level checks). Per-id balances are not
//    kept in phase 1; responses for erc1155 say "aggregated over token ids".
enum class TokenKind : uint8_t { erc20 = 1, erc721 = 2, erc1155 = 3 };
enum TokenFlag : uint16_t { tf_irregular = 1, tf_overflow = 2, tf_weth = 4, tf_spam_suspect = 8, tf_no_decimals = 16 };
inline bool burn_sink(const uint8_t* a20) noexcept {
    static constexpr uint8_t dead[20] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xde, 0xad};
    bool zero = true; for (int i = 0; i < 20; i++) zero &= a20[i] == 0;
    return zero || std::memcmp(a20, dead, 20) == 0;
}

// ------------------------------------------------------------------ segment catalogue
// File name = segment_file_name(kind, part): "<name>.seg", or "<name>.<part 3 digits>.seg" for
// partitioned kinds. Ranges: 1-99 core, 100-199 graph, 200-299 ledger, 300-399 prices/led (later).
enum class Kind : uint32_t {
    // core (dict.hpp)
    dict_addr_sfx = 1,    // elem 17|18, count N, aux = radix bytes k
    dict_addr_pfx = 2,    // elem 4, count 2^(8k)+1
    dict_tok = 3,         // elem sizeof(TokenRec)
    dict_tok_str = 4,     // variable
    blk_time = 5,         // BlockTime stream, aux = first block, aux2 = checkpoints
    dict_addr_xor = 6,    // cold-node xor filter (reserved, phase 1b)
    // overlay (overlay.hpp): mutable, address-keyed facts beside the immutable build (<root>/<chain>/overlay/)
    ovl_label = 7,        // elem sizeof(OvlLabel), sorted by address
    ovl_str = 8,          // variable, strings referenced by ovl_label / ovl_token
    ovl_token = 9,        // elem sizeof(OvlToken), sorted by address
    ovl_entity = 10,      // elem sizeof(OvlEntity), sorted by (fnv1a64(lowercase entity), address)
    // graph (graph_v2.hpp; layout owned by graph)
    g_out_off = 100, g_out_adj = 101, g_out_attr = 102,
    g_in_off = 103, g_in_adj = 104, g_in_attr = 105,
    g_top = 106,          // weight-ordered top-k for high-degree nodes
    g_hub_cold = 107,     // full neighbour lists of hubs (cold tier)
    g_edge_exact = 108,   // exact n, amount, first/last block per (from, to, asset, kind) (cold tier; local for small chains)
    g_node_attr = 109,    // hot node features
    g_node_label = 110,   // 8 B labels (graphloop BaseLabel)
    g_node_cold = 111,    // cold node features
    g_node_str = 112,     // entity names / strings
    g_meta = 113,
    // ledger (ledger.hpp; layout owned by ledger)
    tt_tok_index = 200, tt_tok_data = 201,     // events by (tid, block, tx, log, batch)
    tt_addr_index = 202, tt_addr_data = 203,   // events by (addr, tid, block, ...)
    bal_head_index = 204, bal_head_data = 205, // current (addr, tid) balance + firsts
    bal_ckpt_index = 206, bal_ckpt_data = 207, // per-token checkpoints (full holder lists, cold)
    bal_top = 208,                             // per-checkpoint top-1000 + holders + supply (hot)
    tok_stats = 209,                           // per-tid transfers, holders, supply, first/last, flags
    launch_snaps = 210,                        // precomputed launch .. +1d holder snapshots
    tt_txhash = 211,                           // tx hashes for (block, tx_index) referenced by hot events
    // prices / ledger-v3 (phase 3, reserved)
    px_feed = 300, px_minute = 301, led_actions = 310, led_positions = 311, led_sheets = 312,
};
enum class Codec : uint32_t { raw = 0, varint = 1, elias_fano = 2, bitpacked = 3, mixed = 4, zstd = 5 };

struct KindName { Kind kind; std::string_view name; };
inline constexpr KindName kind_names[] = {
    {Kind::dict_addr_sfx, "dict.addr.sfx"}, {Kind::dict_addr_pfx, "dict.addr.pfx"}, {Kind::dict_tok, "dict.tok"},
    {Kind::dict_tok_str, "dict.tok.str"}, {Kind::blk_time, "blk.time"}, {Kind::dict_addr_xor, "dict.addr.xor"},
    {Kind::ovl_label, "ovl.label"}, {Kind::ovl_str, "ovl.str"}, {Kind::ovl_token, "ovl.token"}, {Kind::ovl_entity, "ovl.entity"},
    {Kind::g_out_off, "g.out.off"}, {Kind::g_out_adj, "g.out.adj"}, {Kind::g_out_attr, "g.out.attr"},
    {Kind::g_in_off, "g.in.off"}, {Kind::g_in_adj, "g.in.adj"}, {Kind::g_in_attr, "g.in.attr"},
    {Kind::g_top, "g.top"}, {Kind::g_hub_cold, "g.hub.cold"}, {Kind::g_edge_exact, "g.edge.exact"},
    {Kind::g_node_attr, "g.node.attr"}, {Kind::g_node_label, "g.node.label"}, {Kind::g_node_cold, "g.node.cold"},
    {Kind::g_node_str, "g.node.str"}, {Kind::g_meta, "g.meta"},
    {Kind::tt_tok_index, "tt.tok.index"}, {Kind::tt_tok_data, "tt.tok.data"}, {Kind::tt_addr_index, "tt.addr.index"},
    {Kind::tt_addr_data, "tt.addr.data"}, {Kind::bal_head_index, "bal.head.index"}, {Kind::bal_head_data, "bal.head.data"},
    {Kind::bal_ckpt_index, "bal.ckpt.index"}, {Kind::bal_ckpt_data, "bal.ckpt.data"}, {Kind::bal_top, "bal.top"},
    {Kind::tok_stats, "tok.stats"}, {Kind::launch_snaps, "launch.snaps"}, {Kind::tt_txhash, "tt.txhash"},
    {Kind::px_feed, "px.feed"}, {Kind::px_minute, "px.minute"}, {Kind::led_actions, "led.actions"},
    {Kind::led_positions, "led.positions"}, {Kind::led_sheets, "led.sheets"},
};
inline constexpr std::string_view kind_name(Kind k) noexcept {
    for (auto& e : kind_names) if (e.kind == k) return e.name;
    return "unknown";
}
inline std::optional<Kind> kind_from_name(std::string_view n) noexcept {
    for (auto& e : kind_names) if (e.name == n) return e.kind;
    return std::nullopt;
}
inline constexpr std::string_view codec_name(Codec c) noexcept {
    switch (c) { case Codec::raw: return "raw"; case Codec::varint: return "varint"; case Codec::elias_fano: return "elias_fano";
                 case Codec::bitpacked: return "bitpacked"; case Codec::mixed: return "mixed"; case Codec::zstd: return "zstd"; }
    return "unknown";
}
// Tier of a kind: hot = needed locally for serving; cold = Blob range reads (local when it fits).
enum class Tier : uint8_t { hot, cold };
inline constexpr Tier kind_tier(Kind k) noexcept {
    switch (k) { case Kind::g_hub_cold: case Kind::g_edge_exact: case Kind::g_node_cold: case Kind::bal_ckpt_data: return Tier::cold; default: return Tier::hot; }
}
inline std::string segment_file_name(Kind k, uint32_t part = 0) {
    std::string n(kind_name(k));
    if (part) { char b[16]; std::snprintf(b, sizeof b, ".%03u", part); n += b; }
    return n + ".seg";
}

class BuildDir;   // seg.hpp

// ------------------------------------------------------------------ SIMD dispatch
// Kernels with AVX-512 variants pick one at runtime: EMV2_SIMD=scalar|avx2|avx512 forces a path
// (verify runs each path twice and compares bytes), default = best the CPU supports. Every path must
// produce identical bytes; x86-64-v3 (AVX2) is the compile baseline, so "scalar" means "no explicit
// vector kernel".
enum class Simd : uint8_t { scalar = 0, avx2 = 1, avx512 = 2 };
inline Simd simd_level() noexcept {
    static const Simd level = [] {
        bool has512 = __builtin_cpu_supports("avx512f") && __builtin_cpu_supports("avx512bw") && __builtin_cpu_supports("avx512vl")
                      && __builtin_cpu_supports("avx512vbmi") && __builtin_cpu_supports("avx512vpopcntdq");
        Simd best = has512 ? Simd::avx512 : (__builtin_cpu_supports("avx2") ? Simd::avx2 : Simd::scalar);
        const char* e = std::getenv("EMV2_SIMD");
        if (!e || !*e || std::string_view(e) == "auto") return best;
        Simd want = std::string_view(e) == "avx512" ? Simd::avx512 : std::string_view(e) == "avx2" ? Simd::avx2 : Simd::scalar;
        return want > best ? best : want;   // never select an unsupported path
    }();
    return level;
}
inline constexpr std::string_view simd_name(Simd s) noexcept { return s == Simd::avx512 ? "avx512" : s == Simd::avx2 ? "avx2" : "scalar"; }

// ------------------------------------------------------------------ build options (all builders)
// One flag set so ops can drive every builder the same way (VM: /data/emv2-out/<chain>/<build_id>/).
struct BuildOptions {
    std::string chain;              // registry id (arbitrum, base, ...)
    std::string out_dir;            // build directory (created by the first module)
    std::string build_id;           // from build_id_of(chain, inputs) -- identical for every module
    uint64_t mem_budget = 64ull << 30;
    unsigned threads = 8;
    unsigned partitions = 256;      // address-hash buckets (auto-split hot keys on top)
    std::string spill_dir;          // default <out_dir>/../.spill-<build_id>
    uint32_t hot_window_days = 90;  // events kept hot by address (ledger); 30 for big chains
    uint64_t event_budget_bytes = 0;// 0 = unlimited; caps hot tt bytes (ops: skew disk)
};

// ------------------------------------------------------------------ graph reader (owner: graph)
namespace graph {
enum class Dir : uint8_t { out = 1, in = 2, both = 3 };
struct EdgeRec {
    uint32_t peer;          // node id of the other endpoint
    uint32_t asset_tid;     // token tid, none32 = native value
    EdgeType type;          // graphloop EdgeType (native, erc20, ...)
    Dir dir;                // out or in, relative to the queried node
    uint8_t flags;          // ef_* below
    uint64_t n;             // transfer count (exact when ef_exact_n)
    U256 amount;            // exact total when ef_amount (cold g.edge.exact present), else zero
    uint32_t first_block, last_block;
    uint32_t asset_node = none32;   // the asset's address id (exact rows) even when the build has no token dictionary
};
enum : uint8_t { ef_exact_n = 1, ef_amount = 2, ef_multi_asset = 4, ef_from_cold = 8 };
struct NodeAttr {
    uint32_t id;
    uint8_t category, subtype, conf, tier;   // graphloop BaseLabel fields
    bool is_contract, is_hub, hot;
    uint32_t first_block, last_block;
    uint64_t tx_sent, tx_received, tt_in, tt_out, contracts_created;
    uint64_t in_degree, out_degree;
    uint32_t creator;                        // node id of the creator, none32 if unknown
    std::string_view entity;                 // registry / label entity name ("" if none)
};
struct EdgeQuery {
    Dir dir = Dir::both;
    uint64_t block0 = 0, block1 = ~0ull;     // keep edges active within [block0, block1]
    uint32_t limit = 256;                    // weight-ordered; hubs return their top list
    bool want_amount = false;                // read cold exact attributes when available
    bool split_assets = false;               // one EdgeRec per (peer, kind, asset) with exact n/amount/blocks/asset_tid
};
struct EdgeResult { std::vector<EdgeRec> edges; uint64_t total = 0; bool truncated = false; bool cold_missing = false; };
struct PathQuery {
    uint32_t from = none32, to = none32;
    uint32_t max_depth = 6, fanout = 128, endpoint_fanout = 1024, budget = 4000;
    bool undirected = true; uint64_t block0 = 0, block1 = ~0ull;
    uint64_t deadline_ns = 0;                // steady-clock ns (graphloop mono_ns); 0 = none. Past it: budget_hit, stop
};
struct PathResult { bool found = false; std::vector<uint32_t> nodes; std::vector<EdgeRec> edges; uint32_t expanded = 0; bool budget_hit = false; };
struct TraceQuery {
    uint32_t root = none32; Dir dir = Dir::in; uint32_t hops = 2, fanout = 32, budget = 2000;
    uint64_t block0 = 0, block1 = ~0ull;
    uint64_t deadline_ns = 0;                // as PathQuery
};
struct TraceResult { std::vector<uint32_t> nodes; std::vector<std::pair<uint32_t, EdgeRec>> edges; std::vector<uint32_t> stops; bool budget_hit = false; };

class Image;   // graph_v2.hpp
std::shared_ptr<const Image> open(const BuildDir& b);
uint64_t node_count(const Image& g);
uint64_t edge_count(const Image& g);
NodeAttr node(const Image& g, uint32_t id);
EdgeResult edges(const Image& g, uint32_t id, const EdgeQuery& q);
// Cold tier: fetch the attribute records of these nodes in one batch before node() reads them one by one (no-op when hot).
void prefetch_nodes(const Image& g, const std::vector<uint32_t>& ids);
// in + out neighbour counts from the hot lists only (no cold read): the hub test of a path search.
uint64_t hot_degree(const Image& g, uint32_t id);
PathResult path(const Image& g, const PathQuery& q);
TraceResult trace(const Image& g, const TraceQuery& q);
} // namespace graph

// ------------------------------------------------------------------ ledger reader (owner: ledger)
namespace ledger {
struct Holder { uint32_t addr; I256 balance; };
struct HoldersAt {
    uint32_t tid = none32; uint64_t block = 0;
    uint64_t holders = 0;          // balance > 0, burn sinks excluded
    I256 supply;                   // mint - burn up to block
    I256 held_top;                 // sum of the returned top
    std::vector<Holder> top;       // balance desc, then addr asc
    bool overflow = false;         // some value exceeded I256
    bool cold_missing = false;     // needed cold data not local (answer still exact for what is returned)
};
struct BalanceAt { I256 balance; uint64_t n_in = 0, n_out = 0; bool overflow = false; };
struct Event {
    uint64_t block; uint32_t tx_index, log_index, batch_index;
    uint32_t from, to, tid; TokenKind kind;
    U256 amount;                   // raw units (erc721 = 1); token_id not kept in phase 1
    bool has_hash; uint8_t tx_hash[32];
    uint32_t time;                 // block timestamp
};
struct EventQuery {
    uint32_t tid = none32;         // none32 = all tokens of addr
    uint32_t addr = none32;        // none32 = all holders of tid
    uint64_t block0 = 0, block1 = ~0ull;
    uint32_t limit = 1000; bool newest_first = true;
};
struct WalletToken {
    uint32_t tid; I256 balance; uint64_t n_in, n_out;
    uint32_t first_block, last_block, first_in_block, first_buy_block;   // none32 when unknown
    uint8_t acquired;              // acq_* bits
    bool overflow;
};
enum : uint8_t { acq_bought = 1, acq_received = 2, acq_minted = 4, acq_same_owner = 8 };
struct TokenStats {
    uint32_t tid; uint64_t n_transfers, n_holders; I256 supply;
    uint32_t first_block, last_block; uint16_t flags; uint32_t first_from;   // first transfer sender (node id)
};

class Store;   // ledger.hpp
std::shared_ptr<const Store> open(const BuildDir& b);
HoldersAt holders_at(const Store& s, uint32_t tid, uint64_t block, uint32_t limit);
BalanceAt balance_at(const Store& s, uint32_t addr, uint32_t tid, uint64_t block);
// Calls f for each event in order; returns false if truncated by limit.
bool events(const Store& s, const EventQuery& q, const std::function<void(const Event&)>& f);
void wallet_tokens(const Store& s, uint32_t addr, std::vector<WalletToken>& out);
std::optional<TokenStats> token_stats(const Store& s, uint32_t tid);
} // namespace ledger

// ------------------------------------------------------------------ dump (verify contract)
// emv2-dump <build_dir> <table> [--addresses FILE] [--tokens FILE] [--all] --out FILE.csv
// CSV, header row, addresses/tokens 0x lowercase hex, integers decimal strings. Tables:
//   edges        from,to,asset,kind,n,total_amount,first_block,last_block      (graph; hot+cold, untruncated)
//   nodes        address,first_block,last_block,tx_sent,tx_received,token_transfers_in,token_transfers_out,is_contract,in_deg,out_deg (graph)
//   bal          address,token,kind,n_in,n_out,balance,first_block,last_block  (ledger)
//   token_stats  token,kind,n_transfers,n_holders,supply                       (ledger)
//   counts       key,value                                                      (graph + ledger rows)
// kind column text: native_tx | erc20 | erc721 | erc1155. Filters: --addresses keeps rows whose
// address is listed (edges: the from address, matching map edges' from-bucket layout); --tokens keeps
// rows whose token/asset is listed.
struct DumpFilter {
    std::vector<uint32_t> addrs;   // sorted node ids, empty = no filter
    std::vector<uint32_t> tids;    // sorted tids, empty = no filter
    bool all = false;
};
class CsvOut {
    std::FILE* f_;
public:
    explicit CsvOut(std::FILE* f) : f_(f) {}
    void row(std::initializer_list<std::string_view> cols) {
        bool first = true;
        for (auto c : cols) { if (!first) std::fputc(',', f_); first = false; std::fwrite(c.data(), 1, c.size(), f_); }
        std::fputc('\n', f_);
    }
};
inline constexpr std::string_view kind_text(TokenKind k) noexcept {
    switch (k) { case TokenKind::erc20: return "erc20"; case TokenKind::erc721: return "erc721"; case TokenKind::erc1155: return "erc1155"; }
    return "unknown";
}
namespace graph { void dump(const BuildDir& b, std::string_view table, const DumpFilter& f, CsvOut& out); }
namespace ledger { void dump(const BuildDir& b, std::string_view table, const DumpFilter& f, CsvOut& out); }

} // namespace machine::engine
