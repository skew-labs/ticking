// ledger_build.cpp: one ledger partition from decoded transfers (no Arrow; the CLI feeds it).
#include "machine/engine/ledger.hpp"
#include "ledger_io.hpp"

#include <cstdio>
#include <filesystem>
#include <memory>
#include <numeric>
#include <stdexcept>
#include <tuple>

namespace machine::engine::ledger::fmt {
namespace {

// Open-addressing map u32 -> Acc320 for one token's balances (keys are ledger address ids).
class Balances {
    struct Slot { uint32_t key; uint32_t pad; Acc320 v; };
    std::vector<Slot> s_; size_t mask_{}, used_{};
    static size_t h(uint32_t k) noexcept { return size_t(k) * 0x9E3779B97F4A7C15ULL >> 20; }
    void grow() {
        std::vector<Slot> old; old.swap(s_);
        s_.assign(old.empty() ? 1024 : old.size() * 2, Slot{no_id, 0, {}}); mask_ = s_.size() - 1; used_ = 0;
        for (auto& e : old) if (e.key != no_id) slot(e.key).v = e.v;
    }
public:
    Balances() { grow(); }
    void clear() { if (s_.size() > 1024) { s_.clear(); grow(); } else { for (auto& e : s_) e.key = no_id; used_ = 0; } }
    Slot& slot(uint32_t k) {
        if ((used_ + 1) * 4 > s_.size() * 3) grow();
        for (size_t i = h(k) & mask_;; i = (i + 1) & mask_) {
            if (s_[i].key == k) return s_[i];
            if (s_[i].key == no_id) { s_[i].key = k; s_[i].v = Acc320{}; ++used_; return s_[i]; }
        }
    }
    Acc320& operator[](uint32_t k) { return slot(k).v; }
    template <class F> void each(F f) const { for (auto& e : s_) if (e.key != no_id && !e.v.is_zero()) f(e.key, e.v); }
};

// by_addr record: everything the by_addr stream needs, so the transfers themselves can be dropped before the sort
struct AeRec { uint32_t addr, tid; uint64_t idx; uint32_t block, flags; uint64_t amt[4]; };  // flags: bit 0 in, bit 1 out
static_assert(sizeof(AeRec) == 56);
inline bool ae_less(const AeRec& a, const AeRec& b) noexcept {
    return a.addr != b.addr ? a.addr < b.addr : a.tid != b.tid ? a.tid < b.tid : a.idx < b.idx;   // keys are unique
}

// sorted run of AeRec on disk, read back in blocks
class RunReader {
    std::FILE* f_{}; std::vector<AeRec> b_; size_t i_ = 0, n_ = 0;
public:
    RunReader(const std::filesystem::path& p, size_t block) : f_(std::fopen(p.c_str(), "rb")), b_(block) {
        if (!f_) throw std::runtime_error("open " + p.string());
        refill();
    }
    ~RunReader() { if (f_) std::fclose(f_); }
    RunReader(const RunReader&) = delete;
    void refill() { n_ = std::fread(b_.data(), sizeof(AeRec), b_.size(), f_); i_ = 0; }
    const AeRec* peek() { if (i_ == n_) { if (n_ < b_.size()) return nullptr; refill(); if (n_ == 0) return nullptr; } return &b_[i_]; }
    void pop() { ++i_; }
};

}  // namespace

struct PartitionWriter::Impl {
    std::filesystem::path pd; uint32_t part, fp, n_tok; PartOptions opt; uint64_t ae_cap;
    io::Out tok_dir, tok_ev, tok_grp, ckpt_dir, ckpt_top, ckpt_full;
    std::vector<uint8_t> buf, tbuf, fbuf;
    Balances bal;
    uint64_t n_grp = 0, n_ckpt = 0;
    // current token
    bool open = false; uint32_t tid = 0; TokDir d{}; int64_t holders = 0; Acc320 supply;
    uint64_t live = 0, since = 0, since_top = 0, k = 0; uint32_t prev = 0, last_block = 0;
    // by_addr records
    std::vector<AeRec> ae; std::vector<std::filesystem::path> runs;

    Impl(const std::string& dir, uint32_t part_, uint32_t n_tok_, const PartOptions& o, uint64_t ae_budget_bytes)
        : pd(dir), part(part_), fp(part_ + 1), n_tok(n_tok_), opt(o),
          ae_cap(std::max<uint64_t>(1, ae_budget_bytes / sizeof(AeRec))),
          tok_dir((std::filesystem::create_directories(pd), pd), Kind::tok_stats, fp, sizeof(TokDir)), tok_ev(pd, Kind::tt_tok_data, fp, 1),
          tok_grp(pd, Kind::tt_tok_index, fp, sizeof(Group)), ckpt_dir(pd, Kind::bal_ckpt_index, fp, sizeof(CkptDir)),
          ckpt_top(pd, Kind::bal_top, fp, 1), ckpt_full(pd, Kind::bal_ckpt_data, fp, 1) {}

    bool excluded(uint32_t id) const noexcept { return id == opt.zero_id || id == opt.dead_id; }

    // Checkpoints sit on block boundaries (state after a whole block), so balance_at(addr, tid, ckpt.block) equals the
    // checkpoint balance.  full = every nonzero balance (cold) + top list; otherwise top list only (hot, frequent).
    void checkpoint(uint64_t n_applied, uint32_t block, bool full) {
        std::vector<Holder> all; all.reserve(live);
        bal.each([&](uint32_t key, const Acc320& v) { all.push_back({key, v}); });
        CkptDir c{}; c.n_applied = n_applied; c.block = block; c.tid = tid; c.holders = holders;
        c.full_off = ~uint64_t(0); c.n_full = 0;
        since = 0;
        if (full) {
            std::sort(all.begin(), all.end(), [](const Holder& x, const Holder& y) { return x.addr < y.addr; });
            c.full_off = ckpt_full.size(); c.n_full = all.size();
            // full list: header, sparse index, entries
            fbuf.clear(); std::vector<std::pair<uint32_t, uint64_t>> idx;
            for (size_t i = 0; i < all.size(); ++i) {
                if (i % full_index_every == 0) idx.push_back({all[i].addr, fbuf.size()});
                put_u32(fbuf, all[i].addr); put_acc(fbuf, all[i].balance);
            }
            uint64_t hdr[2] = {all.size(), idx.size()}; ckpt_full.put(hdr, sizeof hdr);
            for (auto& [id, off] : idx) { uint32_t w[2] = {id, 0}; ckpt_full.put(w, 8); ckpt_full.put_one(off); }
            ckpt_full.put_bytes(fbuf);
            since = 0;
        }
        since_top = 0;
        // top list: positive, not 0x0 / dEaD, balance desc then id
        std::vector<Holder> top;
        for (auto& h : all) if (h.balance.sign() > 0 && !excluded(h.addr)) top.push_back(h);
        auto by_bal = [](const Holder& x, const Holder& y) { int cc = cmp(x.balance, y.balance); return cc ? cc > 0 : x.addr < y.addr; };
        if (top.size() > top_keep) { std::partial_sort(top.begin(), top.begin() + top_keep, top.end(), by_bal); top.resize(top_keep); }
        else std::sort(top.begin(), top.end(), by_bal);
        c.top_off = ckpt_top.size(); c.n_top = uint32_t(top.size());
        tbuf.clear(); for (auto& h : top) { put_u32(tbuf, h.addr); put_acc(tbuf, h.balance); }
        ckpt_top.put_bytes(tbuf);
        ckpt_dir.put_one(c); ++n_ckpt;
    }

    void spill() {
        if (ae.empty()) return;
        std::sort(ae.begin(), ae.end(), ae_less);
        auto p = pd / (".ae-spill-" + std::to_string(fp) + "-" + std::to_string(runs.size()));
        std::FILE* f = std::fopen(p.c_str(), "wb");
        if (!f) throw std::runtime_error("open " + p.string());
        if (std::fwrite(ae.data(), sizeof(AeRec), ae.size(), f) != ae.size() || std::fclose(f) != 0) throw std::runtime_error("write " + p.string());
        runs.push_back(p);
        std::vector<AeRec>().swap(ae);
    }
    void push_ae(uint32_t addr, uint64_t idx, uint32_t block, uint32_t flags, const uint64_t amt[4]) {
        AeRec r{addr, tid, idx, block, flags, {amt[0], amt[1], amt[2], amt[3]}};
        ae.push_back(r);
        if (ae.size() >= ae_cap) spill();
    }

    void event(const Xfer& x) {
        if (x.tid != tid) throw std::runtime_error("event of another token");
        if (k == 0) { d.first_block = x.block; prev = x.block; }
        else if (x.block != last_block) {   // the previous event closed its block and was not the token's last event
            if (since >= std::max<uint64_t>(opt.ckpt_min_events, live)) checkpoint(k, last_block, opt.full_ckpt);
            else if (since_top >= std::max<uint64_t>(top_every, live / 8)) checkpoint(k, last_block, false);
        }
        if (k % group_every == 0) {
            Group g{}; g.block = x.block; g.prev_block = prev; g.holders = holders; std::memcpy(g.supply, supply.w, 40);
            tok_ev.put_bytes(buf); buf.clear(); g.ev_off = tok_ev.size();
            tok_grp.put_one(g); ++n_grp;
        }
        int dh = 0;
        if (x.from != x.to) {
            Acc320& bf = bal[x.from];
            bool f_was = bf.sign() > 0, f_nz = !bf.is_zero();
            bf.sub_u256(x.amt);
            if (!excluded(x.from) && f_was && bf.sign() <= 0) --dh;
            if (x.from != opt.zero_id && bf.negative()) d.flags |= tok_irregular;
            if (f_nz && bf.is_zero()) --live; else if (!f_nz && !bf.is_zero()) ++live;
            Acc320& bt = bal[x.to];
            bool t_was = bt.sign() > 0, t_nz = !bt.is_zero();
            bt.add_u256(x.amt);
            if (!excluded(x.to) && !t_was && bt.sign() > 0) ++dh;
            if (t_nz && bt.is_zero()) --live; else if (!t_nz && !bt.is_zero()) ++live;
            if (x.from == opt.zero_id) supply.add_u256(x.amt);
            if (x.to == opt.zero_id) supply.sub_u256(x.amt);
        }
        holders += dh;
        put_varint(buf, x.block - prev); prev = x.block;
        put_varint(buf, x.tx); put_varint(buf, x.log); put_varint(buf, x.batch);
        put_u32(buf, x.from); put_u32(buf, x.to);
        buf.push_back(uint8_t(dh > 0 ? 1 : dh < 0 ? 2 : 0));
        put_u256(buf, x.amt);
        if (x.from == x.to) push_ae(x.from, k, x.block, 3, x.amt);
        else { push_ae(x.from, k, x.block, 2, x.amt); push_ae(x.to, k, x.block, 1, x.amt); }
        ++since; ++since_top;
        last_block = x.block; ++k;
    }

    // by_addr: runs of (addr, tid) in (addr, tid, token event index) order
    template <class Next> std::vector<SegResult> emit_by_addr(Next&& next, BuildStats& st) {
        io::Out run(pd, Kind::bal_head_index, fp, sizeof(Run)), addr_ev(pd, Kind::tt_addr_data, fp, 1), addr_grp(pd, Kind::tt_addr_index, fp, sizeof(AddrGroup)),
            bal_out(pd, Kind::bal_head_data, fp, 1);
        uint64_t n_agrp = 0; bool have = false; Run r{}; Acc320 v; uint64_t pv = 0, q = 0;
        auto close = [&] {
            r.n_events = q;
            addr_ev.put_bytes(buf); buf.clear();
            r.bal_off = bal_out.size(); buf.clear(); put_acc(buf, v); bal_out.put_bytes(buf); buf.clear();
            run.put_one(r);
            st.sum_n_in += r.n_in; st.sum_n_out += r.n_out; ++st.pairs;
        };
        buf.clear();
        while (const AeRec* e = next()) {
            if (!have || e->addr != r.addr || e->tid != r.tid) {
                if (have) close();
                have = true;
                r = Run{}; r.addr = e->addr; r.tid = e->tid; r.ev_off = addr_ev.size(); r.grp_begin = uint32_t(n_agrp);
                if (n_agrp > 0xffffffffu) throw std::runtime_error("addr groups exceed u32");
                r.first_in_block = 0xffffffffu; v = Acc320{}; pv = 0; q = 0; r.first_block = e->block;
            }
            if (q % group_every == 0 && q > 0) {
                AddrGroup g{}; addr_ev.put_bytes(buf); buf.clear(); g.ev_off = addr_ev.size(); g.block = e->block; g.prev_idx = pv; std::memcpy(g.bal, v.w, 40);
                addr_grp.put_one(g); ++n_agrp;
            }
            bool in = e->flags & 1, out = e->flags & 2;
            put_varint(buf, e->idx - pv); pv = e->idx;
            if (in) { ++r.n_in; if (r.first_in_block == 0xffffffffu) r.first_in_block = e->block; }
            if (out) ++r.n_out;
            if (in && !out) v.add_u256(e->amt); else if (out && !in) v.sub_u256(e->amt);
            r.last_block = e->block; ++q;
        }
        if (have) close();
        std::vector<SegResult> res;
        for (io::Out* o : {&tok_dir, &tok_ev, &tok_grp, &ckpt_dir, &ckpt_top, &ckpt_full, &run, &addr_ev, &addr_grp, &bal_out}) res.push_back(o->finish());
        return res;
    }
};

PartitionWriter::PartitionWriter(const std::string& dir, uint32_t part, uint32_t n_tok, const PartOptions& opt, uint64_t ae_budget_bytes)
    : p_(std::make_unique<Impl>(dir, part, n_tok, opt, ae_budget_bytes)) {}
PartitionWriter::~PartitionWriter() { if (p_) for (auto& r : p_->runs) { std::error_code ec; std::filesystem::remove(r, ec); } }
uint64_t PartitionWriter::spilled_runs() const noexcept { return p_->runs.size(); }

void PartitionWriter::begin_token(uint32_t tid) {
    Impl& m = *p_;
    if (m.open) throw std::runtime_error("begin_token inside a token");
    if (tid >= m.n_tok || tid % m.opt.parts != m.part) throw std::runtime_error("tid outside partition");
    m.open = true; m.tid = tid; m.bal.clear();
    m.d = TokDir{}; m.d.tid = tid; m.d.ev_off = m.tok_ev.size(); m.d.grp_begin = m.n_grp; m.d.ckpt_begin = m.n_ckpt;
    m.holders = 0; m.supply = Acc320{}; m.live = m.since = m.since_top = m.k = 0; m.prev = m.last_block = 0;
}
void PartitionWriter::add(std::span<const Xfer> events) {
    Impl& m = *p_;
    if (!m.open) throw std::runtime_error("add outside a token");
    for (const Xfer& x : events) m.event(x);
}
void PartitionWriter::end_token() {
    Impl& m = *p_;
    if (!m.open) throw std::runtime_error("end_token outside a token");
    m.open = false;
    if (m.k == 0) return;                     // a token without events writes nothing
    m.tok_ev.put_bytes(m.buf); m.buf.clear();
    m.d.n_events = m.k; m.d.last_block = m.last_block;
    // final top list so "now" needs no replay
    if (m.since_top > 0 && m.d.n_events >= m.opt.ckpt_min_events) m.checkpoint(m.d.n_events, m.d.last_block, false);
    m.d.ckpt_end = m.n_ckpt; m.d.holders_end = m.holders;
    m.tok_dir.put_one(m.d);
}
std::vector<SegResult> PartitionWriter::finish(BuildStats& st) {
    Impl& m = *p_;
    if (m.open) throw std::runtime_error("finish inside a token");
    st.checkpoints += m.n_ckpt;
    if (m.runs.empty()) {                      // everything fit in memory
        std::sort(m.ae.begin(), m.ae.end(), ae_less);
        size_t i = 0;
        auto res = m.emit_by_addr([&]() -> const AeRec* { return i < m.ae.size() ? &m.ae[i++] : nullptr; }, st);
        std::vector<AeRec>().swap(m.ae);
        return res;
    }
    m.spill();                                 // k-way merge of the sorted runs
    size_t block = std::max<size_t>(1 << 16, size_t(m.ae_cap / (m.runs.size() + 1)));
    std::vector<std::unique_ptr<RunReader>> rd;
    for (auto& p : m.runs) rd.push_back(std::make_unique<RunReader>(p, block));
    auto gt = [&](size_t a, size_t b) { return ae_less(*rd[b]->peek(), *rd[a]->peek()); };
    std::vector<size_t> heap;
    for (size_t i = 0; i < rd.size(); ++i) if (rd[i]->peek()) heap.push_back(i);
    std::make_heap(heap.begin(), heap.end(), gt);
    AeRec cur{};
    auto res = m.emit_by_addr([&]() -> const AeRec* {
        if (heap.empty()) return nullptr;
        std::pop_heap(heap.begin(), heap.end(), gt);
        size_t i = heap.back(); heap.pop_back();
        cur = *rd[i]->peek(); rd[i]->pop();
        if (rd[i]->peek()) { heap.push_back(i); std::push_heap(heap.begin(), heap.end(), gt); }
        return &cur;
    }, st);
    rd.clear();
    for (auto& p : m.runs) { std::error_code ec; std::filesystem::remove(p, ec); }
    m.runs.clear();
    return res;
}

std::vector<SegResult> build_partition(const std::string& dir, uint32_t part, uint32_t n_addr, uint32_t n_tok,
                                       std::vector<Xfer>& xs, const PartOptions& opt, BuildStats& st) {
    (void)n_addr;
    if (xs.size() >= (uint64_t(1) << 62)) throw std::runtime_error("partition too large");
    // ---- order events by tid, keeping block order inside a tid (counting sort on an index)
    std::vector<uint64_t> start(size_t(n_tok) + 1, 0);
    for (auto& x : xs) { if (x.tid >= n_tok || x.tid % opt.parts != part) throw std::runtime_error("tid outside partition"); ++start[x.tid + 1]; }
    for (size_t i = 1; i < start.size(); ++i) start[i] += start[i - 1];
    std::vector<uint64_t> order(xs.size());
    { auto pos = start; for (uint64_t i = 0; i < xs.size(); ++i) order[pos[xs[i].tid]++] = i; }
    // inputs arrive in file (block) order; restore (block, tx, log, batch) order where a source file was not sorted
    auto key = [&](uint64_t i) { const Xfer& x = xs[i]; return std::tuple(x.block, x.tx, x.log, x.batch); };
    PartitionWriter w(dir, part, n_tok, opt, ~uint64_t(0));
    std::vector<Xfer> ev;
    for (uint32_t tid = part; tid < n_tok; tid += opt.parts) {
        uint64_t b = start[tid], e = start[tid + 1];
        if (b == e) continue;
        for (uint64_t i = b + 1; i < e; ++i)
            if (key(order[i]) < key(order[i - 1])) { std::stable_sort(order.begin() + b, order.begin() + e, [&](uint64_t x, uint64_t y) { return key(x) < key(y); }); break; }
        ev.clear(); ev.reserve(e - b);
        for (uint64_t i = b; i < e; ++i) ev.push_back(xs[order[i]]);
        w.begin_token(tid); w.add(ev); w.end_token();
    }
    std::vector<Xfer>().swap(ev); std::vector<uint64_t>().swap(order); std::vector<Xfer>().swap(xs);
    return w.finish(st);
}

std::vector<SegResult> write_addr_dict(const std::string& dir, std::span<const Addr> addrs) {
    std::filesystem::create_directories(dir);
    AddrDictWriter w(dir, addrs.size());
    machine::engine::Addr a{};
    for (auto& x : addrs) { std::memcpy(a.b, x.data(), 20); w.push(a); }
    auto [r1, r2] = w.finish();
    return {r1, r2};
}

}  // namespace machine::engine::ledger::fmt
