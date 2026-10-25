// emv2-build-ledger: token_transfers Parquet (em-extract schema) -> EMV2 ledger segments (VM only: Arrow/Parquet).
//
//   emv2-build-ledger full   --chain C --in TT_DIR --out BUILD_DIR --build-id ID [--blocks BLK_DIR] [--max-block N]
//                            [--extra WETH_DIR] [--pass-hook CMD]
//                            [--parts 32] [--per-pass 4] [--threads 12] [--ckpt 256] [--own-dict] [--dump]
//   emv2-build-ledger focus  --in TT_DIR --pairs PAIRS.tsv --out OUT.tsv [--max-block N] [--threads 12] [--stream N]
//   emv2-build-ledger counts --in TT_DIR [--max-block N] [--threads 12] [--stream N]
//
// TT_DIR holds token_transfers_<first>-<last>.parquet; *.tmp* / *.part files are ignored and block ranges must not
// overlap (lexical order = block order).  full needs graph's dict.addr.* in BUILD_DIR (--own-dict writes one for a
// standalone build), writes dict.tok{,.str}, blk.time (with --blocks), the ledger partitions and manifest.ledger.json;
// --dump also writes dump/{bal,token_stats}.tsv.zst and dump/counts.tsv.  focus accumulates exact heads for the
// listed (address, token, kind) pairs in one pass.  --stream N: N input files are expected (names from TT_DIR/
// files.txt, one per line in block order); each is processed when it appears and deleted afterwards.
#include "machine/engine/ledger.hpp"
#include "../lib/ledger_io.hpp"

#include <arrow/api.h>
#include <arrow/io/file.h>
#include <cryptopp/sha.h>
#include <parquet/arrow/reader.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <regex>
#include <set>
#include <thread>
#include <unordered_map>

namespace fs = std::filesystem;
namespace E = machine::engine;
using namespace machine::engine::ledger::fmt;
using E::TokenKind;

namespace {

[[noreturn]] void die(const std::string& m) { std::fprintf(stderr, "emv2-build-ledger: %s\n", m.c_str()); std::exit(2); }
double now_s() { return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count(); }
void logf(const char* fmt, auto... a) {
    auto t = std::time(nullptr); char ts[16]; std::strftime(ts, sizeof ts, "%H:%M:%S", std::gmtime(&t));
    std::fprintf(stderr, "%s ", ts); std::fprintf(stderr, fmt, a...); std::fputc('\n', stderr);
}
std::string hex(const uint8_t* p, size_t n) {
    static const char* d = "0123456789abcdef"; std::string s = "0x";
    for (size_t i = 0; i < n; ++i) { s.push_back(d[p[i] >> 4]); s.push_back(d[p[i] & 15]); }
    return s;
}
bool unhex20(std::string_view s, Addr& a) {
    if (s.size() >= 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) s.remove_prefix(2);
    if (s.size() != 40) return false;
    auto v = [](char c) { return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1; };
    for (int i = 0; i < 20; ++i) { int h = v(s[2 * i]), l = v(s[2 * i + 1]); if (h < 0 || l < 0) return false; a[i] = uint8_t(h << 4 | l); }
    return true;
}

// ------------------------------------------------------------------ inputs
struct InFile { std::string path, name; uint64_t first, last, bytes; bool extra = false; };

bool parse_range(const std::string& n, uint64_t& a, uint64_t& b) {
    static const std::regex re(R"(_(\d+)-(\d+)\.parquet$)"); std::smatch m;
    if (!std::regex_search(n, m, re)) return false;
    a = std::stoull(m[1]); b = std::stoull(m[2]); return true;
}
std::vector<InFile> order_inputs(std::vector<InFile> v, uint64_t max_block) {
    std::sort(v.begin(), v.end(), [](auto& a, auto& b) { return a.first < b.first; });
    for (size_t i = 1; i < v.size(); ++i) if (v[i].first <= v[i - 1].last) die("overlapping block ranges: " + v[i - 1].name + " / " + v[i].name);
    std::erase_if(v, [&](const InFile& f) { return f.first > max_block; });
    if (v.empty()) die("no parquet inputs");
    if (std::min(v.back().last, max_block) >= 0xffffffffull) die("block numbers above u32");
    return v;
}
std::vector<InFile> list_inputs(const std::string& dir, uint64_t max_block) {
    std::vector<InFile> v;
    for (auto& e : fs::directory_iterator(dir)) {
        auto n = e.path().filename().string();
        // partial downloads (azcopy .azDownload-*, *.tmp*, *.part) are never inputs
        if (n.find(".tmp") != std::string::npos || n.find(".azDownload") != std::string::npos || n.find(".part") != std::string::npos ||
            e.path().extension() != ".parquet") continue;
        InFile f{e.path().string(), n, 0, 0, uint64_t(e.file_size())};
        if (!parse_range(n, f.first, f.last)) die("unexpected file name " + n);
        v.push_back(f);
    }
    return order_inputs(std::move(v), max_block);
}
// --stream: names from files.txt; sizes are unknown until each file lands
std::vector<InFile> list_stream(const std::string& dir, size_t n, uint64_t max_block) {
    std::ifstream in(dir + "/files.txt"); if (!in) die("--stream needs " + dir + "/files.txt");
    std::vector<InFile> v; std::string line;
    while (std::getline(in, line)) {
        if (line.empty()) continue;
        auto name = line.substr(line.find_last_of('/') + 1);
        InFile f{dir + "/" + name, name, 0, 0, 0};
        if (!parse_range(name, f.first, f.last)) die("unexpected file name " + name);
        v.push_back(f);
    }
    if (v.size() != n) die("--stream count does not match files.txt");
    return order_inputs(std::move(v), max_block);
}
void wait_for(const InFile& f) {
    for (int i = 0; !fs::exists(f.path); ++i) {
        if (i % 600 == 599) logf("waiting for %s", f.name.c_str());
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
}
std::string input_sig(const std::vector<InFile>& fs_) {
    CryptoPP::SHA256 h;
    for (auto& f : fs_) { std::string l = f.name + "\n"; h.Update(reinterpret_cast<const uint8_t*>(l.data()), l.size()); }
    uint8_t d[32]; h.Final(d); return hex(d, 32).substr(2);
}

uint64_t u64at(const arrow::Array* a, int64_t i) {
    switch (a->type_id()) {
    case arrow::Type::UINT64: return static_cast<const arrow::UInt64Array*>(a)->Value(i);
    case arrow::Type::INT64: return uint64_t(static_cast<const arrow::Int64Array*>(a)->Value(i));
    case arrow::Type::UINT32: return static_cast<const arrow::UInt32Array*>(a)->Value(i);
    case arrow::Type::INT32: return uint64_t(uint32_t(static_cast<const arrow::Int32Array*>(a)->Value(i)));
    default: die("unexpected integer column type " + a->type()->ToString());
    }
}
const uint8_t* fixed(const arrow::Array* a, int64_t i, int width) {
    if (a->type_id() != arrow::Type::FIXED_SIZE_BINARY) die("expected fixed_size_binary, got " + a->type()->ToString());
    auto f = static_cast<const arrow::FixedSizeBinaryArray*>(a);
    if (f->byte_width() != width) die("unexpected byte width");
    return f->GetValue(i);
}
// standard column -> kind (0 = unknown, 1..3 = TokenKind, bit 7 = WETH row); plain or dictionary strings
uint8_t std_code(std::string_view s) {
    auto k = kind_of(s); if (!k) return 0;
    return uint8_t(*k) | (s.starts_with("WETH_") ? 0x80 : 0);
}
struct KindCol {
    const arrow::Array* a; std::vector<uint8_t> dict_kind; const arrow::Array* idx{};
    explicit KindCol(const arrow::Array* x) : a(x) {
        if (a->type_id() == arrow::Type::DICTIONARY) {
            auto d = static_cast<const arrow::DictionaryArray*>(a);
            auto dv = std::static_pointer_cast<arrow::StringArray>(d->dictionary());
            for (int64_t i = 0; i < dv->length(); ++i) dict_kind.push_back(std_code(dv->GetView(i)));
            idx = d->indices().get();
        } else if (a->type_id() != arrow::Type::STRING) die("unexpected standard type " + a->type()->ToString());
    }
    uint8_t at(int64_t i) const {
        if (idx) return dict_kind[size_t(u64at(idx, i))];
        return std_code(static_cast<const arrow::StringArray*>(a)->GetView(i));
    }
};

struct Cols {
    std::shared_ptr<arrow::Table> t;
    const arrow::Array *block, *tx, *log, *batch, *standard, *token, *from, *to, *amount;
    int64_t rows;
};
template <class F> void read_tt(const InFile& in, F&& f) {
    auto fin = arrow::io::ReadableFile::Open(in.path);
    if (!fin.ok()) die("open " + in.path);
    auto rr = parquet::arrow::OpenFile(*fin, arrow::default_memory_pool());
    if (!rr.ok()) die("parquet open " + in.path + ": " + rr.status().ToString());
    auto r = std::move(*rr);
    std::shared_ptr<arrow::Schema> schema; if (!r->GetSchema(&schema).ok()) die("schema " + in.path);
    const char* names[] = {"block_number", "tx_index", "log_index", "batch_index", "standard", "token", "from", "to", "amount"};
    std::vector<int> idx; for (auto n : names) { int c = schema->GetFieldIndex(n); if (c < 0) die(std::string("missing column ") + n + " in " + in.name); idx.push_back(c); }
    for (int rg = 0; rg < r->num_row_groups(); ++rg) {
        auto t = r->ReadRowGroup(rg, idx);
        if (!t.ok()) die("read " + in.path + ": " + t.status().ToString());
        auto tc = (*t)->CombineChunks();
        if (!tc.ok()) die("combine " + in.path);
        Cols c; c.t = *tc; c.rows = c.t->num_rows();
        if (c.rows == 0) continue;
        const arrow::Array** dst[] = {&c.block, &c.tx, &c.log, &c.batch, &c.standard, &c.token, &c.from, &c.to, &c.amount};
        for (int j = 0; j < 9; ++j) *dst[j] = c.t->column(j)->chunk(0).get();
        for (int j = 0; j < 9; ++j) if ((*dst[j])->null_count()) die(std::string("nulls in ") + names[j] + " " + in.name);
        f(c);
    }
}

template <class F> void parallel_files(size_t n, unsigned threads, F&& fn) {
    std::atomic<size_t> next{0}; std::vector<std::thread> ws;
    for (unsigned t = 0; t < threads; ++t) ws.emplace_back([&] { for (size_t i; (i = next++) < n;) fn(i); });
    for (auto& w : ws) w.join();
}

struct Counts { std::atomic<uint64_t> kind[4]{}, unknown{}, rows{}, cut{}, max_block{}; };
void note_block(Counts& c, uint64_t b) { uint64_t m = c.max_block.load(); while (b > m && !c.max_block.compare_exchange_weak(m, b)) {} }

// ------------------------------------------------------------------ address dictionary
struct AddrSet {  // 256 shards by first byte, open addressing; the zero address is tracked separately
    struct Shard { std::mutex m; std::vector<Addr> slot; size_t used = 0; };
    std::array<Shard, 256> sh; std::atomic<bool> has_zero{false};
    static size_t h(const Addr& a) { uint64_t v; std::memcpy(&v, a.data() + 4, 8); return size_t(v * 0x9E3779B97F4A7C15ULL); }
    static bool put(std::vector<Addr>& s, const Addr& a) {
        size_t m = s.size() - 1;
        for (size_t i = h(a) & m;; i = (i + 1) & m) { if (s[i] == a) return false; if (s[i] == zero_addr) { s[i] = a; return true; } }
    }
    void insert_sorted_unique(const std::vector<Addr>& v) {
        size_t i = 0;
        while (i < v.size()) {
            size_t j = i; uint8_t b = v[i][0]; while (j < v.size() && v[j][0] == b) ++j;
            Shard& s = sh[b]; std::lock_guard g(s.m);
            for (size_t k = i; k < j; ++k) {
                if (v[k] == zero_addr) { has_zero = true; continue; }
                if ((s.used + 1) * 2 > s.slot.size()) {
                    std::vector<Addr> n(std::max<size_t>(1024, s.slot.size() * 2)); for (auto& a : s.slot) if (a != zero_addr) put(n, a); s.slot.swap(n);
                }
                if (put(s.slot, v[k])) ++s.used;
            }
            i = j;
        }
    }
    std::vector<Addr> sorted() {
        std::vector<Addr> out; if (has_zero) out.push_back(zero_addr);
        for (auto& s : sh) {
            size_t b = out.size();
            for (auto& a : s.slot) if (a != zero_addr) out.push_back(a);
            std::sort(out.begin() + b, out.end());
            std::vector<Addr>().swap(s.slot);
        }
        return out;
    }
};
struct AddrIndex {  // sorted dictionary in memory + 16-bit prefix index
    std::vector<Addr> v; std::vector<uint32_t> pre;
    void build() {
        pre.assign(65537, 0);
        for (auto& a : v) ++pre[(size_t(a[0]) << 8 | a[1]) + 1];
        for (size_t i = 1; i < pre.size(); ++i) pre[i] += pre[i - 1];
    }
    uint32_t find(const uint8_t* p) const {
        size_t k = size_t(p[0]) << 8 | p[1];
        auto b = v.begin() + pre[k], e = v.begin() + pre[k + 1];
        auto it = std::lower_bound(b, e, p, [](const Addr& a, const uint8_t* q) { return std::memcmp(a.data(), q, 20) < 0; });
        if (it == e || std::memcmp(it->data(), p, 20)) return no_id;
        return uint32_t(it - v.begin());
    }
    // graph's dict.addr.{sfx,pfx} (core AddrDict format) expanded into memory
    void load(const fs::path& dir) {
        E::SegView sfx(dir / E::segment_file_name(E::Kind::dict_addr_sfx), E::Kind::dict_addr_sfx);
        E::SegView pfx(dir / E::segment_file_name(E::Kind::dict_addr_pfx), E::Kind::dict_addr_pfx);
        unsigned k = unsigned(sfx.aux()); uint32_t w = 20 - k; auto p = pfx.as<uint32_t>(); const uint8_t* s = sfx.payload().data();
        v.resize(sfx.count());
        for (uint64_t b = 0; b + 1 < p.size(); ++b)
            for (uint32_t id = p[b]; id < p[b + 1]; ++id) {
                for (unsigned i = 0; i < k; ++i) v[id][i] = uint8_t(b >> (8 * (k - 1 - i)));
                std::memcpy(v[id].data() + k, s + size_t(id) * w, w);
            }
        for (size_t i = 1; i < v.size(); ++i) if (!(v[i - 1] < v[i])) die("graph dictionary is not sorted");
    }
};

struct Opts {
    std::string mode, chain, in, out, build_id, blocks, extra, pass_hook;
    std::vector<std::string> pair_files;   // focus: union of every --pairs file
    uint64_t max_block = ~0ull; size_t stream = 0;
    uint32_t parts = 32, per_pass = 4, ckpt = 256; unsigned threads = 12; bool dump = false, own_dict = false, full_ckpt = false, allow_missing = false;
};

void write_counts(const std::string& path, const std::vector<std::pair<std::string, std::string>>& kv) {
    std::ofstream o(path); o << "key\tvalue\n"; for (auto& [k, v] : kv) o << k << '\t' << v << '\n';
    if (!o) die("write " + path);
}
void zstd_file(const std::string& p) {
    std::string cmd = "zstd -q -f -T8 --rm '" + p + "'";
    if (std::system(cmd.c_str()) != 0) die("zstd failed for " + p);
}

// blk.time from extraction blocks (number, timestamp), first..max_block.  Blocks missing from the extraction (gaps)
// repeat the previous timestamp; their count is returned in `gap_blocks` (no transfers can sit in a missing block).
E::SegResult write_block_time(const std::string& dir, const std::string& out, uint64_t max_block, unsigned threads, uint64_t& gap_blocks) {
    std::vector<InFile> files;
    for (auto& e : fs::directory_iterator(dir)) {
        auto n = e.path().filename().string();
        if (n.find(".tmp") != std::string::npos || e.path().extension() != ".parquet") continue;
        InFile f{e.path().string(), n, 0, 0, uint64_t(e.file_size())};
        if (!parse_range(n, f.first, f.last)) die("unexpected blocks file " + n);
        files.push_back(f);
    }
    files = order_inputs(std::move(files), max_block);
    std::vector<std::vector<std::pair<uint64_t, uint32_t>>> per(files.size());
    parallel_files(files.size(), threads, [&](size_t i) {
        auto fin = arrow::io::ReadableFile::Open(files[i].path); if (!fin.ok()) die("open " + files[i].path);
        auto rr = parquet::arrow::OpenFile(*fin, arrow::default_memory_pool()); if (!rr.ok()) die("parquet open " + files[i].path);
        auto r = std::move(*rr); std::shared_ptr<arrow::Schema> sc; if (!r->GetSchema(&sc).ok()) die("schema");
        int cn = sc->GetFieldIndex("number"), ct = sc->GetFieldIndex("timestamp");
        if (cn < 0 || ct < 0) die("blocks: missing number/timestamp in " + files[i].name);
        for (int rg = 0; rg < r->num_row_groups(); ++rg) {
            auto t = r->ReadRowGroup(rg, {cn, ct}); if (!t.ok()) die("read " + files[i].path);
            auto tc = (*t)->CombineChunks(); if (!tc.ok()) die("combine");
            auto a = (*tc)->column(0)->chunk(0).get(), b = (*tc)->column(1)->chunk(0).get();
            for (int64_t k = 0; k < (*tc)->num_rows(); ++k) {
                uint64_t n = u64at(a, k); if (n > max_block) continue;
                per[i].push_back({n, uint32_t(u64at(b, k))});
            }
        }
        std::sort(per[i].begin(), per[i].end());
    });
    uint64_t first = ~0ull; for (auto& p : per) if (!p.empty()) { first = p.front().first; break; }
    if (first == ~0ull) die("no blocks");
    E::BlockTimeWriter w(out, first);
    uint64_t expect = first; uint32_t last_t = 0; gap_blocks = 0;
    for (auto& p : per)
        for (auto& [n, t] : p) {
            if (n < expect) die("duplicate block " + std::to_string(n));
            for (; expect < n; ++expect, ++gap_blocks) w.push(last_t);
            w.push(std::max(t, last_t)); last_t = std::max(t, last_t); ++expect;
        }
    logf("blk.time: blocks %llu..%llu, %llu missing blocks filled", (unsigned long long)first, (unsigned long long)(expect - 1), (unsigned long long)gap_blocks);
    return w.finish();
}

// ------------------------------------------------------------------ full build
int run_full(const Opts& o) {
    auto files = list_inputs(o.in, o.max_block);
    size_t n_main = files.size();
    if (!o.extra.empty()) {   // --extra: WETH9 Deposit/Withdrawal rows from emv2-ledger-weth (token_transfers schema, own block ranges)
        auto ex = list_inputs(o.extra, o.max_block);
        for (auto& f : ex) { f.extra = true; f.name = "extra/" + f.name; files.push_back(f); }
    }
    std::string sig = input_sig(files);
    fs::create_directories(o.out);
    const fs::path out = o.out;
    double t0 = now_s(), gb = 0; for (auto& f : files) gb += double(f.bytes) / 1e9;
    logf("full %s: %zu files, %.1f GB, parts=%u per_pass=%u threads=%u", o.chain.c_str(), files.size(), gb, o.parts, o.per_pass, o.threads);
    bool graph_dict = fs::exists(out / E::segment_file_name(E::Kind::dict_addr_sfx));
    if (!graph_dict && !o.own_dict) die("dict.addr.* not in the build dir (graph writes it first); --own-dict for a standalone build");
    if (graph_dict && o.own_dict) die("--own-dict but the build dir already has dict.addr.*");

    // pass 0: (token, kind) with first block and WETH flag; addresses when we write our own dictionary
    AddrSet aset; std::mutex tm; std::map<std::pair<Addr, uint8_t>, std::pair<uint32_t, bool>> toks;
    Counts cnt;
    // Tokens whose WETH_ rows already come from the extractor (allowlisted emitters): --extra rows for them are skipped, so a
    // Deposit/Withdrawal is never counted twice. Filled after the main inputs' pass 0, read by every later pass.
    std::set<Addr> main_weth; std::atomic<uint64_t> extra_rows{0}, extra_skipped{0};
    auto skip_extra = [&](const InFile& f, const uint8_t* token) { if (!f.extra) return false; Addr a; std::memcpy(a.data(), token, 20); return main_weth.count(a) > 0; };
    auto pass0 = [&](size_t i) {
        std::vector<Addr> local; std::map<std::pair<Addr, uint8_t>, std::pair<uint32_t, bool>> lt;
        read_tt(files[i], [&](Cols& c) {
            KindCol kc(c.standard);
            for (int64_t r = 0; r < c.rows; ++r) {
                ++cnt.rows; uint64_t b = u64at(c.block, r);
                if (b > o.max_block) { ++cnt.cut; continue; }
                uint8_t code = kc.at(r), k = code & 0x7f;
                if (!k) { ++cnt.unknown; continue; }
                if (skip_extra(files[i], fixed(c.token, r, 20))) { ++extra_skipped; continue; }
                if (files[i].extra) ++extra_rows;
                ++cnt.kind[k]; note_block(cnt, b);
                Addr a; std::memcpy(a.data(), fixed(c.token, r, 20), 20);
                auto [it, fresh] = lt.try_emplace({a, k}, std::pair{uint32_t(b), bool(code & 0x80)});
                if (!fresh) { it->second.first = std::min(it->second.first, uint32_t(b)); it->second.second |= bool(code & 0x80); }
                if (!graph_dict) {
                    local.push_back(a);
                    std::memcpy(a.data(), fixed(c.from, r, 20), 20); local.push_back(a);
                    std::memcpy(a.data(), fixed(c.to, r, 20), 20); local.push_back(a);
                }
            }
        });
        if (!graph_dict) { std::sort(local.begin(), local.end()); local.erase(std::unique(local.begin(), local.end()), local.end()); aset.insert_sorted_unique(local); }
        std::lock_guard g(tm);
        for (auto& [k, v] : lt) {
            auto [it, fresh] = toks.try_emplace(k, v);
            if (!fresh) { it->second.first = std::min(it->second.first, v.first); it->second.second |= v.second; }
        }
    };
    {
        std::vector<size_t> mains, extras;
        for (size_t i = 0; i < files.size(); ++i) (files[i].extra ? extras : mains).push_back(i);
        parallel_files(mains.size(), o.threads, [&](size_t j) { pass0(mains[j]); });
        for (auto& [k, v] : toks) if (v.second) main_weth.insert(k.first);
        parallel_files(extras.size(), o.threads, [&](size_t j) { pass0(extras[j]); });
        if (!extras.empty()) logf("extra inputs: %llu WETH9 rows added, %llu skipped (token already has extractor WETH rows)",
                                  (unsigned long long)extra_rows.load(), (unsigned long long)extra_skipped.load());
    }
    E::PartManifest man(o.chain, "ledger", o.build_id);
    for (auto& f : files) man.input({f.name, "", f.bytes, 0});
    AddrIndex dict;
    if (graph_dict) dict.load(out);
    else { dict.v = aset.sorted(); for (auto& r : write_addr_dict(o.out, dict.v)) man.file(r); }
    dict.build();
    // token dictionary in (addr_id, kind) order; token contracts must be in the address dictionary
    std::vector<std::tuple<uint32_t, uint8_t, uint32_t, bool>> trows;
    uint64_t tok_missing = 0;
    for (auto& [k, v] : toks) { uint32_t id = dict.find(k.first.data()); if (id == no_id) { ++tok_missing; continue; } trows.push_back({id, k.second, v.first, v.second}); }
    if (tok_missing) die(std::to_string(tok_missing) + " token contracts missing from the address dictionary (n_addr_missing)");
    std::sort(trows.begin(), trows.end());
    struct TokHash { size_t operator()(const std::pair<Addr, uint8_t>& k) const { uint64_t v; std::memcpy(&v, k.first.data() + 4, 8); return size_t(v * 0x9E3779B97F4A7C15ULL ^ k.second); } };
    std::unordered_map<std::pair<Addr, uint8_t>, uint32_t, TokHash> tid_h; tid_h.reserve(trows.size() * 2);
    {
        E::TokenDictWriter tw(out);
        for (auto& [id, k, fb, weth] : trows) {
            uint32_t tid = tw.push(id, TokenKind(k), 255, uint16_t(weth ? E::tf_weth : 0), fb, "", "");
            tid_h[{dict.v[id], k}] = tid;
        }
        auto [a, b] = tw.finish(); man.file(a); man.file(b);
    }
    toks.clear();
    uint32_t n_addr = uint32_t(dict.v.size()), n_tok = uint32_t(trows.size());
    logf("pass 0 in %.0fs: rows=%llu addresses=%u tokens=%u unknown=%llu cut=%llu", now_s() - t0, (unsigned long long)cnt.rows.load(), n_addr, n_tok,
         (unsigned long long)cnt.unknown.load(), (unsigned long long)cnt.cut.load());
    uint64_t gap_blocks = 0;
    if (!o.blocks.empty()) man.file(write_block_time(o.blocks, o.out, o.max_block, o.threads, gap_blocks));

    PartOptions po; po.parts = o.parts; po.ckpt_min_events = o.ckpt; po.threads = o.threads; po.full_ckpt = o.full_ckpt;
    po.zero_id = dict.find(zero_addr.data()); po.dead_id = dict.find(dead_addr.data());
    BuildStats st; std::mutex stm; std::atomic<uint64_t> addr_missing{0};
    for (uint32_t p0 = 0; p0 < o.parts; p0 += o.per_pass) {
        uint32_t p1 = std::min(o.parts, p0 + o.per_pass);
        double tp = now_s();
        std::vector<std::vector<std::vector<Xfer>>> per_file(files.size());
        parallel_files(files.size(), o.threads, [&](size_t i) {
            auto& pf = per_file[i]; pf.resize(p1 - p0);
            read_tt(files[i], [&](Cols& c) {
                KindCol kc(c.standard);
                for (int64_t r = 0; r < c.rows; ++r) {
                    uint8_t k = kc.at(r) & 0x7f; if (!k) continue;
                    uint64_t b = u64at(c.block, r); if (b > o.max_block) continue;
                    if (skip_extra(files[i], fixed(c.token, r, 20))) continue;
                    Addr ta; std::memcpy(ta.data(), fixed(c.token, r, 20), 20);
                    uint32_t tid = tid_h.find({ta, k})->second;
                    uint32_t p = tid % o.parts; if (p < p0 || p >= p1) continue;
                    Xfer x{}; x.block = uint32_t(b); x.tx = uint32_t(u64at(c.tx, r)); x.log = uint32_t(u64at(c.log, r));
                    x.batch = uint32_t(u64at(c.batch, r)); x.tid = tid;
                    x.from = dict.find(fixed(c.from, r, 20)); x.to = dict.find(fixed(c.to, r, 20));
                    if (x.from == no_id || x.to == no_id) {
                        if (++addr_missing <= 5) logf("missing address in %s block %u", files[i].name.c_str(), x.block);
                        continue;
                    }
                    if (k == uint8_t(TokenKind::erc721)) x.amt[0] = 1; else be32_to_limbs(fixed(c.amount, r, 32), x.amt);
                    pf[p - p0].push_back(x);
                }
            });
            for (auto& v : pf) v.shrink_to_fit();   // drop growth slack: peak memory is per-file chunks + one partition copy
        });
        if (addr_missing && !o.allow_missing) die(std::to_string(addr_missing.load()) + " transfer addresses missing from the address dictionary (n_addr_missing)");
        double tr = now_s();
        std::vector<std::thread> ws;
        for (uint32_t p = p0; p < p1; ++p)
            ws.emplace_back([&, p] {
                std::vector<Xfer> xs; size_t n = 0;
                for (auto& f : per_file) n += f[p - p0].size();
                xs.reserve(n);
                for (auto& f : per_file) { xs.insert(xs.end(), f[p - p0].begin(), f[p - p0].end()); std::vector<Xfer>().swap(f[p - p0]); }
                BuildStats local;
                auto d = build_partition(o.out, p, n_addr, n_tok, xs, po, local);
                std::lock_guard g(stm);
                for (auto& r : d) man.file(r);
                st.sum_n_in += local.sum_n_in; st.sum_n_out += local.sum_n_out; st.pairs += local.pairs; st.checkpoints += local.checkpoints;
                logf("partition %u: %zu events, %llu pairs", p, n, (unsigned long long)local.pairs);
            });
        for (auto& w : ws) w.join();
        logf("parts %u-%u: read %.0fs build %.0fs", p0, p1 - 1, tr - tp, now_s() - tr);
        if (!o.pass_hook.empty()) {   // e.g. upload this pass's part files to Blob and delete them locally (disk-bound chains)
            std::string cmd = o.pass_hook + " '" + o.out + "' " + std::to_string(p0 + 1) + " " + std::to_string(p1);   // part numbers p+1
            int rc = std::system(cmd.c_str());
            if (rc != 0) die("--pass-hook failed for parts " + std::to_string(p0 + 1) + "-" + std::to_string(p1));
        }
    }
    uint64_t total = cnt.kind[1] + cnt.kind[2] + cnt.kind[3] - addr_missing.load();   // --allow-missing drops those rows
    if (st.sum_n_in != total || st.sum_n_out != total) die("n_in/n_out sums do not match the event count");
    auto& f = man.extra();
    f["ledger_format"] = ledger_format; f["parts"] = o.parts; f["rows"] = cnt.rows.load(); f["events"] = total;
    f["events_erc20"] = cnt.kind[1].load(); f["events_erc721"] = cnt.kind[2].load(); f["events_erc1155"] = cnt.kind[3].load();
    f["n_tt_unknown"] = cnt.unknown.load(); f["rows_after_max_block"] = cnt.cut.load(); f["sum_n_in"] = st.sum_n_in; f["sum_n_out"] = st.sum_n_out;
    f["pairs"] = st.pairs; f["addresses"] = n_addr; f["tokens"] = n_tok; f["checkpoints"] = st.checkpoints; f["n_addr_missing"] = addr_missing.load(); f["allow_missing"] = o.allow_missing;
    f["input_end_block"] = cnt.max_block.load(); f["max_block"] = o.max_block == ~0ull ? nlohmann::json() : nlohmann::json(o.max_block);
    f["input_sig"] = sig; f["dict"] = graph_dict ? "graph" : "own"; f["blk_missing_blocks"] = gap_blocks;
    f["weth9_extra_rows"] = extra_rows.load(); f["weth9_extra_skipped"] = extra_skipped.load(); f["main_inputs"] = n_main;
    f["pass_hook"] = !o.pass_hook.empty();
    man.write(out);
    logf("built in %.0fs: events=%llu pairs=%llu checkpoints=%llu", now_s() - t0, (unsigned long long)total, (unsigned long long)st.pairs,
         (unsigned long long)st.checkpoints);

    if (o.dump) {
        fs::create_directories(out / "dump");
        Reader R(o.out);
        {
            std::string p = (out / "dump/bal.tsv").string(); FILE* fp = std::fopen(p.c_str(), "w"); if (!fp) die("open " + p);
            std::fputs("address\ttoken\tkind\tn_in\tn_out\tbalance\tfirst_block\tlast_block\n", fp);
            R.each_run([&](const Run& r, const Acc320& b) {
                const E::TokenRec& t = R.token(r.tid);
                std::fprintf(fp, "%s\t%s\t%s\t%llu\t%llu\t%s\t%u\t%u\n", hex(R.addr(r.addr).data(), 20).c_str(), hex(R.addr(t.addr_id).data(), 20).c_str(),
                             kind_name(TokenKind(t.kind)), (unsigned long long)r.n_in, (unsigned long long)r.n_out, b.to_dec().c_str(), r.first_block, r.last_block);
            });
            if (std::fclose(fp)) die("close " + p);
            zstd_file(p);
        }
        {
            std::string p = (out / "dump/token_stats.tsv").string(); FILE* fp = std::fopen(p.c_str(), "w"); if (!fp) die("open " + p);
            std::fputs("token\tkind\tn_transfers\tn_holders\tsupply\tirregular\n", fp);
            R.each_token([&](const TokDir& d) {
                const E::TokenRec& t = R.token(d.tid);
                std::fprintf(fp, "%s\t%s\t%llu\t%lld\t%s\t%d\n", hex(R.addr(t.addr_id).data(), 20).c_str(), kind_name(TokenKind(t.kind)), (unsigned long long)d.n_events,
                             (long long)d.holders_end, R.supply_at(d.tid, d.last_block).to_dec().c_str(), int(d.flags & tok_irregular ? 1 : 0));
            });
            if (std::fclose(fp)) die("close " + p);
            zstd_file(p);
        }
        std::vector<std::pair<std::string, std::string>> kv;
        for (auto& [k, v] : f.items()) kv.push_back({k, v.is_string() ? v.get<std::string>() : v.dump()});
        kv.push_back({"build_id", o.build_id});
        write_counts((out / "dump/counts.tsv").string(), kv);
        logf("dumps written, %.0fs total", now_s() - t0);
    }
    return 0;
}

// ------------------------------------------------------------------ one-pass modes (focus, counts), optional streaming
template <class F> void scan_once(const Opts& o, const std::vector<InFile>& files, F&& per_file) {
    std::atomic<size_t> done{0}; double t0 = now_s();
    parallel_files(files.size(), o.threads, [&](size_t i) {
        if (o.stream) wait_for(files[i]);
        per_file(files[i]);
        if (o.stream) fs::remove(files[i].path);
        size_t d = ++done; if (d % 50 == 0 || d == files.size()) logf("%zu/%zu files, %.0fs", d, files.size(), now_s() - t0);
    });
}

int run_focus(const Opts& o) {
    auto files = o.stream ? list_stream(o.in, o.stream, o.max_block) : list_inputs(o.in, o.max_block);
    struct Key { Addr a, t; uint8_t k; };
    struct Slot { Key key; bool used; Acc320 bal; uint64_t n_in, n_out; uint32_t first, last; };
    std::vector<Slot> tab; size_t mask;
    auto hk = [](const uint8_t* a, const uint8_t* t, uint8_t k) { uint64_t x, y; std::memcpy(&x, a + 4, 8); std::memcpy(&y, t + 4, 8); return size_t((x * 0x9E3779B97F4A7C15ULL) ^ (y + k)); };
    {
        std::vector<Key> keys;
        for (const std::string& pairs : o.pair_files) {
        size_t before = keys.size();
        if (pairs.ends_with(".parquet")) {   // columns wallet|address, token, optional kind (default erc20)
            auto fin = arrow::io::ReadableFile::Open(pairs); if (!fin.ok()) die("open " + pairs);
            auto rr = parquet::arrow::OpenFile(*fin, arrow::default_memory_pool()); if (!rr.ok()) die("parquet open " + pairs);
            auto r = std::move(*rr); std::shared_ptr<arrow::Schema> sc; if (!r->GetSchema(&sc).ok()) die("schema");
            int ca = sc->GetFieldIndex("wallet"); if (ca < 0) ca = sc->GetFieldIndex("address");
            int ct = sc->GetFieldIndex("token"), ck = sc->GetFieldIndex("kind");
            if (ca < 0 || ct < 0) die("pairs parquet needs wallet|address and token");
            std::vector<int> cols{ca, ct}; if (ck >= 0) cols.push_back(ck);
            for (int rg = 0; rg < r->num_row_groups(); ++rg) {
                auto t = r->ReadRowGroup(rg, cols); if (!t.ok()) die("read pairs");
                auto tc = (*t)->CombineChunks(); if (!tc.ok()) die("combine pairs");
                auto str = [&](int j, int64_t i) -> std::string_view {
                    auto a = (*tc)->column(j)->chunk(0);
                    if (a->type_id() == arrow::Type::STRING) return std::static_pointer_cast<arrow::StringArray>(a)->GetView(i);
                    if (a->type_id() == arrow::Type::LARGE_STRING) return std::static_pointer_cast<arrow::LargeStringArray>(a)->GetView(i);
                    die("pairs: string columns expected");
                };
                for (int64_t i = 0; i < (*tc)->num_rows(); ++i) {
                    Key k{}; k.k = 1;
                    if (!unhex20(str(0, i), k.a) || !unhex20(str(1, i), k.t)) die("pairs: bad address");
                    if (ck >= 0) { auto kn = str(2, i); k.k = kn == "erc721" || kn == "ERC721" ? 2 : kn == "erc1155" || kn == "ERC1155" ? 3 : 1; }
                    keys.push_back(k);
                }
            }
        } else {
        std::ifstream in(pairs); if (!in) die("open " + pairs);
        std::string line; bool first = true;
        while (std::getline(in, line)) {
            if (line.empty()) continue;
            for (auto& ch : line) if (ch == ',') ch = '\t';
            size_t p1 = line.find('\t'), p2 = p1 == std::string::npos ? p1 : line.find('\t', p1 + 1);
            if (p2 == std::string::npos) die("pairs: need address, token, kind");
            Key k{}; std::string kn = line.substr(p2 + 1); kn = kn.substr(0, kn.find('\t')); while (!kn.empty() && (kn.back() == '\r' || kn.back() == ' ')) kn.pop_back();
            bool ok = unhex20(std::string_view(line).substr(0, p1), k.a) && unhex20(std::string_view(line).substr(p1 + 1, p2 - p1 - 1), k.t);
            k.k = kn == "erc20" || kn == "1" ? 1 : kn == "erc721" || kn == "2" ? 2 : kn == "erc1155" || kn == "3" ? 3 : 0;
            if (!ok || !k.k) { if (first) { first = false; continue; } die("pairs: bad line " + line); }
            first = false; keys.push_back(k);
        }
        }
        logf("pairs %s: %zu", pairs.c_str(), keys.size() - before);
        }
        size_t cap = 1; while (cap < keys.size() * 2 + 16) cap <<= 1;
        tab.assign(cap, Slot{}); mask = cap - 1;
        size_t unique = 0;
        for (auto& k : keys)
            for (size_t i = hk(k.a.data(), k.t.data(), k.k) & mask;; i = (i + 1) & mask) {
                if (!tab[i].used) { tab[i].used = true; tab[i].key = k; tab[i].first = 0xffffffffu; ++unique; break; }
                if (tab[i].key.a == k.a && tab[i].key.t == k.t && tab[i].key.k == k.k) break;
            }
        logf("focus: %zu pairs (%zu unique), %zu files", keys.size(), unique, files.size());
    }
    auto find = [&](const uint8_t* a, const uint8_t* t, uint8_t k) -> Slot* {
        for (size_t i = hk(a, t, k) & mask;; i = (i + 1) & mask) {
            if (!tab[i].used) return nullptr;
            if (tab[i].key.k == k && !std::memcmp(tab[i].key.a.data(), a, 20) && !std::memcmp(tab[i].key.t.data(), t, 20)) return &tab[i];
        }
    };
    std::vector<std::mutex> locks(4096);
    Counts cnt; double t0 = now_s();
    scan_once(o, files, [&](const InFile& in) {
        read_tt(in, [&](Cols& c) {
            KindCol kc(c.standard);
            for (int64_t r = 0; r < c.rows; ++r) {
                ++cnt.rows; uint64_t b = u64at(c.block, r);
                if (b > o.max_block) { ++cnt.cut; continue; }
                uint8_t k = kc.at(r) & 0x7f;
                if (!k) { ++cnt.unknown; continue; }
                ++cnt.kind[k]; note_block(cnt, b);
                const uint8_t* t = fixed(c.token, r, 20); const uint8_t* f = fixed(c.from, r, 20); const uint8_t* to = fixed(c.to, r, 20);
                bool self = std::memcmp(f, to, 20) == 0;
                uint64_t amt[4] = {1, 0, 0, 0}; bool have_amt = false;
                for (int side = 0; side < 2; ++side) {
                    if (side == 1 && self) break;
                    Slot* s = find(side ? to : f, t, k); if (!s) continue;
                    if (!have_amt) { if (k != uint8_t(TokenKind::erc721)) be32_to_limbs(fixed(c.amount, r, 32), amt); have_amt = true; }
                    std::lock_guard g(locks[size_t(s - tab.data()) & 4095]);
                    bool in_ = side == 1 || self, out_ = side == 0;
                    if (in_) ++s->n_in;
                    if (out_) ++s->n_out;
                    if (!self) { if (in_) s->bal.add_u256(amt); else s->bal.sub_u256(amt); }
                    s->first = std::min(s->first, uint32_t(b)); s->last = std::max(s->last, uint32_t(b));
                }
            }
        });
    });
    FILE* fp = std::fopen(o.out.c_str(), "w"); if (!fp) die("open " + o.out);
    std::fputs("address\ttoken\tkind\tn_in\tn_out\tbalance\tfirst_block\tlast_block\n", fp);
    uint64_t seen = 0;
    for (auto& s : tab) {
        if (!s.used) continue;
        bool any = s.n_in + s.n_out > 0; seen += any;
        std::fprintf(fp, "%s\t%s\t%s\t%llu\t%llu\t%s\t%u\t%u\n", hex(s.key.a.data(), 20).c_str(), hex(s.key.t.data(), 20).c_str(), kind_name(TokenKind(s.key.k)),
                     (unsigned long long)s.n_in, (unsigned long long)s.n_out, s.bal.to_dec().c_str(), any ? s.first : 0, s.last);
    }
    if (std::fclose(fp)) die("close " + o.out);
    write_counts(o.out + ".counts.tsv", {{"rows", std::to_string(cnt.rows)}, {"events_erc20", std::to_string(cnt.kind[1])},
        {"events_erc721", std::to_string(cnt.kind[2])}, {"events_erc1155", std::to_string(cnt.kind[3])}, {"n_tt_unknown", std::to_string(cnt.unknown)},
        {"rows_after_max_block", std::to_string(cnt.cut)}, {"input_end_block", std::to_string(cnt.max_block)}, {"pairs_seen", std::to_string(seen)},
        {"input_files", std::to_string(files.size())}, {"input_sig", input_sig(files)}});
    logf("focus done in %.0fs: rows=%llu pairs_seen=%llu", now_s() - t0, (unsigned long long)cnt.rows.load(), (unsigned long long)seen);
    return 0;
}

int run_counts(const Opts& o) {
    auto files = o.stream ? list_stream(o.in, o.stream, o.max_block) : list_inputs(o.in, o.max_block);
    Counts cnt;
    scan_once(o, files, [&](const InFile& in) {
        read_tt(in, [&](Cols& c) {
            KindCol kc(c.standard);
            for (int64_t r = 0; r < c.rows; ++r) {
                ++cnt.rows; uint64_t b = u64at(c.block, r);
                if (b > o.max_block) { ++cnt.cut; continue; }
                uint8_t k = kc.at(r) & 0x7f; if (k) { ++cnt.kind[k]; note_block(cnt, b); } else ++cnt.unknown;
            }
        });
    });
    uint64_t total = cnt.kind[1] + cnt.kind[2] + cnt.kind[3];
    std::printf("key\tvalue\nrows\t%llu\nevents\t%llu\nevents_erc20\t%llu\nevents_erc721\t%llu\nevents_erc1155\t%llu\nn_tt_unknown\t%llu\nrows_after_max_block\t%llu\n"
                "sum_n_in\t%llu\nsum_n_out\t%llu\ninput_end_block\t%llu\ninput_files\t%zu\ninput_sig\t%s\n",
                (unsigned long long)cnt.rows.load(), (unsigned long long)total, (unsigned long long)cnt.kind[1].load(), (unsigned long long)cnt.kind[2].load(),
                (unsigned long long)cnt.kind[3].load(), (unsigned long long)cnt.unknown.load(), (unsigned long long)cnt.cut.load(), (unsigned long long)total,
                (unsigned long long)total, (unsigned long long)cnt.max_block.load(), files.size(), input_sig(files).c_str());
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) die("usage: emv2-build-ledger full|focus|counts --in DIR ...");
    Opts o; o.mode = argv[1];
    for (int i = 2; i < argc; ++i) {
        std::string a = argv[i];
        auto val = [&]() -> std::string { if (i + 1 >= argc) die("missing value for " + a); return argv[++i]; };
        if (a == "--in") o.in = val(); else if (a == "--out") o.out = val(); else if (a == "--pairs") o.pair_files.push_back(val());
        else if (a == "--chain") o.chain = val(); else if (a == "--blocks") o.blocks = val();
        else if (a == "--build-id") o.build_id = val(); else if (a == "--parts") o.parts = uint32_t(std::stoul(val()));
        else if (a == "--per-pass") o.per_pass = uint32_t(std::stoul(val())); else if (a == "--ckpt") o.ckpt = uint32_t(std::stoul(val()));
        else if (a == "--threads") o.threads = unsigned(std::stoul(val())); else if (a == "--max-block") o.max_block = std::stoull(val());
        else if (a == "--stream") o.stream = std::stoul(val());
        else if (a == "--dump") o.dump = true; else if (a == "--own-dict") o.own_dict = true; else if (a == "--full-ckpt") o.full_ckpt = true;
        else if (a == "--extra") o.extra = val();          // WETH9 rows from emv2-ledger-weth (semantics: api.hpp SEMANTICS)
        else if (a == "--pass-hook") o.pass_hook = val();  // run "<cmd> <out> <first part> <last part>" after each pass
        else if (a == "--allow-missing") o.allow_missing = true;   // skip transfers whose address is not in graph's dictionary (facts.n_addr_missing)
        else die("unknown option " + a);
    }
    if (o.in.empty()) die("--in is required");
    if (o.parts == 0 || o.per_pass == 0 || o.parts > 999) die("--parts must be 1..999 and --per-pass > 0");
    if (o.mode == "full") {
        if (o.out.empty() || o.chain.empty() || o.build_id.empty()) die("full needs --out, --chain and --build-id");
        if (o.stream) die("--stream is for focus/counts");
        if (!o.pass_hook.empty() && o.dump) die("--dump reads the part files that --pass-hook removes");
        return run_full(o);
    }
    if (o.mode == "focus") { if (o.out.empty() || o.pair_files.empty()) die("--pairs and --out are required"); return run_focus(o); }
    if (o.mode == "counts") return run_counts(o);
    die("unknown mode " + o.mode);
}
