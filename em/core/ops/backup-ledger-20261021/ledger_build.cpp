// ledger_build.cpp: one ledger partition from decoded transfers (no Arrow; the CLI feeds it).
#include "machine/engine/ledger.hpp"
#include "ledger_io.hpp"

#include <filesystem>
#include <numeric>
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

struct AE { uint32_t addr, tid; uint64_t xi_flags; };  // xi << 2 | flags (bit 0 in, bit 1 out)
static_assert(sizeof(AE) == 16);

// stable LSD radix sort by (addr) of entries already ordered by (tid, event)
void sort_by_addr(std::vector<AE>& v) {
    std::vector<AE> tmp(v.size());
    for (int shift = 0; shift < 32; shift += 16) {
        std::vector<uint64_t> cnt(65537, 0);
        for (auto& e : v) ++cnt[((e.addr >> shift) & 0xffff) + 1];
        for (size_t i = 1; i < cnt.size(); ++i) cnt[i] += cnt[i - 1];
        for (auto& e : v) tmp[cnt[(e.addr >> shift) & 0xffff]++] = e;
        v.swap(tmp);
    }
}

}  // namespace

std::vector<SegResult> build_partition(const std::string& dir, uint32_t part, uint32_t n_addr, uint32_t n_tok,
                                       std::vector<Xfer>& xs, const PartOptions& opt, BuildStats& st) {
    (void)n_addr;
    const std::filesystem::path pd = dir;
    const uint32_t fp = part + 1;  // file part number
    std::filesystem::create_directories(pd);
    if (xs.size() >= (uint64_t(1) << 62)) throw std::runtime_error("partition too large");

    // ---- order events by tid, keeping block order inside a tid (counting sort on an index)
    std::vector<uint64_t> start(size_t(n_tok) + 1, 0);
    for (auto& x : xs) { if (x.tid >= n_tok || x.tid % opt.parts != part) throw std::runtime_error("tid outside partition"); ++start[x.tid + 1]; }
    for (size_t i = 1; i < start.size(); ++i) start[i] += start[i - 1];
    std::vector<uint64_t> order(xs.size());
    { auto pos = start; for (uint64_t i = 0; i < xs.size(); ++i) order[pos[xs[i].tid]++] = i; }
    // inputs arrive in file (block) order; restore (block, tx, log, batch) order where a source file was not sorted
    auto key = [&](uint64_t i) { const Xfer& x = xs[i]; return std::tuple(x.block, x.tx, x.log, x.batch); };
    for (uint32_t tid = part; tid < n_tok; tid += opt.parts) {
        uint64_t b = start[tid], e = start[tid + 1];
        for (uint64_t i = b + 1; i < e; ++i)
            if (key(order[i]) < key(order[i - 1])) { std::stable_sort(order.begin() + b, order.begin() + e, [&](uint64_t x, uint64_t y) { return key(x) < key(y); }); break; }
    }

    io::Out tok_dir(pd, Kind::tok_stats, fp, sizeof(TokDir)), tok_ev(pd, Kind::tt_tok_data, fp, 1), tok_grp(pd, Kind::tt_tok_index, fp, sizeof(Group)),
        ckpt_dir(pd, Kind::bal_ckpt_index, fp, sizeof(CkptDir)), ckpt_top(pd, Kind::bal_top, fp, 1), ckpt_full(pd, Kind::bal_ckpt_data, fp, 1);
    std::vector<AE> ae; ae.reserve(xs.size() * 2);
    std::vector<uint8_t> buf, tbuf, fbuf;
    Balances bal;
    uint64_t n_grp = 0, n_ckpt = 0;
    auto excluded = [&](uint32_t id) { return id == opt.zero_id || id == opt.dead_id; };

    for (uint32_t tid = part; tid < n_tok; tid += opt.parts) {
        uint64_t b = start[tid], e = start[tid + 1];
        if (b == e) continue;
        bal.clear();
        TokDir d{}; d.tid = tid; d.ev_off = tok_ev.size(); d.n_events = e - b; d.grp_begin = n_grp; d.ckpt_begin = n_ckpt;
        d.first_block = xs[order[b]].block; d.last_block = xs[order[e - 1]].block;
        int64_t holders = 0; Acc320 supply; uint64_t live = 0, since = 0, since_top = 0; uint32_t prev = d.first_block;
        // Checkpoints sit on block boundaries (state after a whole block), so balance_at(addr, tid, ckpt.block) equals the
        // checkpoint balance.  full = every nonzero balance (cold) + top list; otherwise top list only (hot, frequent).
        auto checkpoint = [&](uint64_t n_applied, uint32_t block, bool full) {
            std::vector<Holder> all; all.reserve(live);
            bal.each([&](uint32_t k, const Acc320& v) { all.push_back({k, v}); });
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
            auto by_bal = [](const Holder& x, const Holder& y) { int c = cmp(x.balance, y.balance); return c ? c > 0 : x.addr < y.addr; };
            if (top.size() > top_keep) { std::partial_sort(top.begin(), top.begin() + top_keep, top.end(), by_bal); top.resize(top_keep); }
            else std::sort(top.begin(), top.end(), by_bal);
            c.top_off = ckpt_top.size(); c.n_top = uint32_t(top.size());
            tbuf.clear(); for (auto& h : top) { put_u32(tbuf, h.addr); put_acc(tbuf, h.balance); }
            ckpt_top.put_bytes(tbuf);
            ckpt_dir.put_one(c); ++n_ckpt;
        };
        for (uint64_t i = b; i < e; ++i) {
            const Xfer& x = xs[order[i]];
            uint64_t k = i - b;
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
            if (x.from == x.to) ae.push_back({x.from, tid, (order[i] << 2) | 3});
            else { ae.push_back({x.from, tid, (order[i] << 2) | 2}); ae.push_back({x.to, tid, (order[i] << 2) | 1}); }
            ++since; ++since_top;
            bool block_end = i + 1 == e || xs[order[i + 1]].block != x.block;
            if (block_end && i + 1 < e) {
                if (since >= std::max<uint64_t>(opt.ckpt_min_events, live)) checkpoint(k + 1, x.block, opt.full_ckpt);
                else if (since_top >= std::max<uint64_t>(top_every, live / 8)) checkpoint(k + 1, x.block, false);
            }
        }
        tok_ev.put_bytes(buf); buf.clear();
        // final top list so "now" needs no replay
        if (since_top > 0 && d.n_events >= opt.ckpt_min_events) checkpoint(d.n_events, d.last_block, false);
        d.ckpt_end = n_ckpt; d.holders_end = holders;
        tok_dir.put_one(d);
    }
    st.checkpoints += n_ckpt;

    // ---- by_addr runs: (addr, tid, event order).  A record is only the token event index (varint delta); the reader
    // takes block, amounts and the counterparty from tt.tok.data.
    std::vector<uint64_t> pos_of(xs.size());
    for (uint64_t q = 0; q < order.size(); ++q) pos_of[order[q]] = q;
    std::vector<uint64_t>().swap(order);
    sort_by_addr(ae);
    io::Out run(pd, Kind::bal_head_index, fp, sizeof(Run)), addr_ev(pd, Kind::tt_addr_data, fp, 1), addr_grp(pd, Kind::tt_addr_index, fp, sizeof(AddrGroup)),
        bal_out(pd, Kind::bal_head_data, fp, 1);
    uint64_t n_agrp = 0;
    for (size_t i = 0; i < ae.size();) {
        size_t j = i; while (j < ae.size() && ae[j].addr == ae[i].addr && ae[j].tid == ae[i].tid) ++j;
        Run r{}; r.addr = ae[i].addr; r.tid = ae[i].tid; r.ev_off = addr_ev.size(); r.grp_begin = uint32_t(n_agrp); r.n_events = j - i;
        if (n_agrp > 0xffffffffu) throw std::runtime_error("addr groups exceed u32");
        r.first_in_block = 0xffffffffu;
        Acc320 v; uint64_t prev = 0; r.first_block = xs[ae[i].xi_flags >> 2].block;
        const uint64_t base = start[r.tid];
        for (size_t k = i; k < j; ++k) {
            uint64_t xi = ae[k].xi_flags >> 2; const Xfer& x = xs[xi]; uint32_t fl = uint32_t(ae[k].xi_flags & 3);
            uint64_t idx = pos_of[xi] - base;   // event index inside the token stream
            if ((k - i) % group_every == 0 && k > i) {
                AddrGroup g{}; addr_ev.put_bytes(buf); buf.clear(); g.ev_off = addr_ev.size(); g.block = x.block; g.prev_idx = prev; std::memcpy(g.bal, v.w, 40);
                addr_grp.put_one(g); ++n_agrp;
            }
            bool in = fl & 1, out = fl & 2;
            put_varint(buf, idx - prev); prev = idx;
            if (in) { ++r.n_in; if (r.first_in_block == 0xffffffffu) r.first_in_block = x.block; }
            if (out) ++r.n_out;
            if (in && !out) v.add_u256(x.amt); else if (out && !in) v.sub_u256(x.amt);
            r.last_block = x.block;
        }
        addr_ev.put_bytes(buf); buf.clear();
        r.bal_off = bal_out.size(); buf.clear(); put_acc(buf, v); bal_out.put_bytes(buf); buf.clear();
        run.put_one(r);
        st.sum_n_in += r.n_in; st.sum_n_out += r.n_out; ++st.pairs;
        i = j;
    }
    std::vector<SegResult> out;
    for (io::Out* o : {&tok_dir, &tok_ev, &tok_grp, &ckpt_dir, &ckpt_top, &ckpt_full, &run, &addr_ev, &addr_grp, &bal_out}) out.push_back(o->finish());
    return out;
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
