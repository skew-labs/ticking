// ledger_read.cpp: queries over a finished ledger build (read-only mmaps).
#include "machine/engine/ledger.hpp"
#include "ledger_io.hpp"

#include <filesystem>
#include <unordered_map>

namespace machine::engine::ledger {

struct Part {
    io::In tok_dir, tok_ev, tok_grp, ckpt_dir, ckpt_top, ckpt_full, run, addr_ev, addr_grp, bal;
};

struct Reader::Impl {
    io::In dict_addr, dict_tok;
    std::vector<Part> parts;
    uint32_t zero_id = no_id, dead_id = no_id;

    const Part& part(uint32_t tid) const { return parts[tid % parts.size()]; }
    const TokDir* tok(uint32_t tid) const {
        auto ds = part(tid).tok_dir.as<TokDir>();
        auto it = std::lower_bound(ds.begin(), ds.end(), tid, [](const TokDir& d, uint32_t t) { return d.tid < t; });
        return it != ds.end() && it->tid == tid ? &*it : nullptr;
    }
    const Run* run(uint32_t addr, uint32_t tid) const {
        auto rs = part(tid).run.as<Run>();
        auto it = std::lower_bound(rs.begin(), rs.end(), std::pair{addr, tid},
                                   [](const Run& r, std::pair<uint32_t, uint32_t> k) { return r.addr != k.first ? r.addr < k.first : r.tid < k.second; });
        return it != rs.end() && it->addr == addr && it->tid == tid ? &*it : nullptr;
    }
    bool excluded(uint32_t id) const noexcept { return id == zero_id || id == dead_id; }

    static Event decode_tok(const uint8_t*& p, uint32_t& prev) {
        Event e{}; e.block = prev + uint32_t(get_varint(p)); prev = e.block;
        e.tx = uint32_t(get_varint(p)); e.log = uint32_t(get_varint(p)); e.batch = uint32_t(get_varint(p));
        e.from = get_u32(p); e.to = get_u32(p);
        uint8_t f = *p++; e.dholders = f == 1 ? 1 : f == 2 ? -1 : 0;
        get_u256(p, e.amt);
        return e;
    }
    static AddrEvent decode_addr(const uint8_t*& p, uint32_t& prev) {
        AddrEvent e{}; e.block = prev + uint32_t(get_varint(p)); prev = e.block;
        e.tx = uint32_t(get_varint(p)); e.log = uint32_t(get_varint(p)); e.batch = uint32_t(get_varint(p));
        e.other = get_u32(p); uint8_t f = *p++; e.in = f & 1; e.out = f & 2;
        get_u256(p, e.amt);
        return e;
    }

    // Token stream cursor positioned at event index `k` (0-based) of token d.
    struct TokCursor { const uint8_t* p; uint32_t prev; uint64_t k; };
    TokCursor tok_at(const TokDir& d, uint64_t k) const {
        const Part& pt = part(d.tid);
        auto gs = pt.tok_grp.as<Group>();
        uint64_t g = k / group_every;
        const Group& G = gs[d.grp_begin + g];
        TokCursor c{pt.tok_ev.data() + G.ev_off, G.block, g * group_every};
        while (c.k < k) { decode_tok(c.p, c.prev); ++c.k; }
        return c;
    }
    // number of events with block <= b, with holder count and supply after them
    struct State { uint64_t k; int64_t holders; Acc320 supply; };
    State state_at(const TokDir& d, uint32_t b) const {
        if (b < d.first_block) return {0, 0, {}};
        const Part& pt = part(d.tid);
        uint64_t ng = (d.n_events + group_every - 1) / group_every;
        auto gs = pt.tok_grp.as<Group>().subspan(d.grp_begin, ng);
        auto it = std::upper_bound(gs.begin(), gs.end(), b, [](uint32_t v, const Group& g) { return v < g.block; });
        const Group& G = *(it - 1);
        uint64_t g = uint64_t(it - 1 - gs.begin());
        State s{g * group_every, G.holders, {}}; std::memcpy(s.supply.w, G.supply, 40);
        const uint8_t* p = pt.tok_ev.data() + G.ev_off; uint32_t prev = G.block;
        uint64_t end = std::min<uint64_t>(d.n_events, (g + 1) * group_every);
        while (s.k < end) {
            const uint8_t* q = p; uint32_t pv = prev;
            Event e = decode_tok(q, pv);
            if (e.block > b) break;
            p = q; prev = pv; ++s.k; s.holders += e.dholders;
            if (e.from != e.to) {
                if (e.from == zero_id) s.supply.add_u256(e.amt);
                if (e.to == zero_id) s.supply.sub_u256(e.amt);
            }
        }
        return s;
    }
    // balance of `addr` in checkpoint c's full list (0 when absent)
    Acc320 full_lookup(const Part& pt, const CkptDir& c, uint32_t addr) const {
        const uint8_t* base = pt.ckpt_full.data() + c.full_off;
        uint64_t n, n_idx; std::memcpy(&n, base, 8); std::memcpy(&n_idx, base + 8, 8);
        if (!n) return {};
        const uint8_t* idx = base + 16; const uint8_t* ents = idx + n_idx * 16;
        uint64_t lo = 0, hi = n_idx;  // last index entry with id <= addr
        while (hi - lo > 1) { uint64_t m = (lo + hi) / 2; uint32_t id; std::memcpy(&id, idx + m * 16, 4); if (id <= addr) lo = m; else hi = m; }
        uint32_t id0; std::memcpy(&id0, idx + lo * 16, 4);
        if (id0 > addr) return {};
        uint64_t off; std::memcpy(&off, idx + lo * 16 + 8, 8);
        const uint8_t* p = ents + off;
        uint64_t left = std::min<uint64_t>(full_index_every, n - lo * full_index_every);
        for (uint64_t i = 0; i < left; ++i) {
            uint32_t id = get_u32(p); Acc320 v = get_acc(p);
            if (id == addr) return v;
            if (id > addr) break;
        }
        return {};
    }
};

Reader::Reader(const std::string& dir) : p_(new Impl) {
    try {
        p_->dict_addr = io::In(dir + "/dict_addr.seg", 20);
        p_->dict_tok = io::In(dir + "/dict_tok.seg", sizeof(TokenRow));
        for (uint32_t i = 0; std::filesystem::exists(dir + "/p" + std::to_string(i)); ++i) {
            std::string pd = dir + "/p" + std::to_string(i) + "/";
            Part pt;
            pt.tok_dir = io::In(pd + "tok_dir.seg", sizeof(TokDir)); pt.tok_ev = io::In(pd + "tok_ev.seg", 1);
            pt.tok_grp = io::In(pd + "tok_grp.seg", sizeof(Group)); pt.ckpt_dir = io::In(pd + "ckpt_dir.seg", sizeof(CkptDir));
            pt.ckpt_top = io::In(pd + "ckpt_top.seg", 1); pt.ckpt_full = io::In(pd + "ckpt_full.seg", 1);
            pt.run = io::In(pd + "run.seg", sizeof(Run)); pt.addr_ev = io::In(pd + "addr_ev.seg", 1);
            pt.addr_grp = io::In(pd + "addr_grp.seg", sizeof(AddrGroup)); pt.bal = io::In(pd + "bal.seg", 1);
            p_->parts.push_back(std::move(pt));
        }
        if (p_->parts.empty()) throw std::runtime_error("no ledger partitions in " + dir);
        p_->zero_id = addr_id(zero_addr).value_or(no_id);
        p_->dead_id = addr_id(dead_addr).value_or(no_id);
    } catch (...) { delete p_; throw; }
}
Reader::~Reader() { delete p_; }

uint32_t Reader::n_addr() const noexcept { return uint32_t(p_->dict_addr.count()); }
uint32_t Reader::n_tok() const noexcept { return uint32_t(p_->dict_tok.count()); }
std::optional<uint32_t> Reader::addr_id(const Addr& a) const noexcept {
    auto as = p_->dict_addr.as<Addr>();
    auto it = std::lower_bound(as.begin(), as.end(), a);
    return it != as.end() && *it == a ? std::optional<uint32_t>(uint32_t(it - as.begin())) : std::nullopt;
}
const Addr& Reader::addr(uint32_t id) const noexcept { return p_->dict_addr.as<Addr>()[id]; }
std::optional<uint32_t> Reader::tid(const Addr& token, Kind k) const noexcept {
    auto ts = p_->dict_tok.as<TokenRow>();
    auto key = std::pair{token, uint8_t(k)};
    auto it = std::lower_bound(ts.begin(), ts.end(), key, [](const TokenRow& r, const std::pair<Addr, uint8_t>& q) {
        return r.addr != q.first ? r.addr < q.first : r.kind < q.second; });
    return it != ts.end() && it->addr == token && it->kind == uint8_t(k) ? std::optional<uint32_t>(uint32_t(it - ts.begin())) : std::nullopt;
}
const TokenRow& Reader::token(uint32_t tid) const noexcept { return p_->dict_tok.as<TokenRow>()[tid]; }

std::optional<Reader::Head> Reader::head(uint32_t addr, uint32_t tid) const {
    const Run* r = p_->run(addr, tid);
    if (!r) return std::nullopt;
    const uint8_t* q = p_->part(tid).bal.data() + r->bal_off;
    return Head{get_acc(q), r->n_in, r->n_out, r->first_block, r->last_block, r->first_in_block};
}

std::vector<std::pair<uint32_t, Reader::Head>> Reader::holdings(uint32_t addr) const {
    std::vector<std::pair<uint32_t, Head>> out;
    for (auto& pt : p_->parts) {
        auto rs = pt.run.as<Run>();
        auto it = std::lower_bound(rs.begin(), rs.end(), addr, [](const Run& r, uint32_t a) { return r.addr < a; });
        for (; it != rs.end() && it->addr == addr; ++it) {
            const uint8_t* q = pt.bal.data() + it->bal_off;
            out.push_back({it->tid, Head{get_acc(q), it->n_in, it->n_out, it->first_block, it->last_block, it->first_in_block}});
        }
    }
    std::sort(out.begin(), out.end(), [](auto& a, auto& b) { return a.first < b.first; });
    return out;
}

Acc320 Reader::balance_at(uint32_t addr, uint32_t tid, uint32_t b) const {
    const Run* r = p_->run(addr, tid);
    if (!r || b < r->first_block) return {};
    const Part& pt = p_->part(tid);
    uint64_t ng = (r->n_events - 1) / group_every;  // groups before events 64, 128, ...
    auto gs = pt.addr_grp.as<AddrGroup>().subspan(r->grp_begin, ng);
    auto it = std::upper_bound(gs.begin(), gs.end(), b, [](uint32_t v, const AddrGroup& g) { return v < g.block; });
    Acc320 v; const uint8_t* p; uint32_t prev; uint64_t k;
    if (it == gs.begin()) { p = pt.addr_ev.data() + r->ev_off; prev = r->first_block; k = 0; }
    else { const AddrGroup& G = *(it - 1); std::memcpy(v.w, G.bal, 40); p = pt.addr_ev.data() + G.ev_off; prev = G.block; k = uint64_t(it - gs.begin()) * group_every; }
    uint64_t end = std::min<uint64_t>(r->n_events, (k / group_every + 1) * group_every);
    for (; k < end; ++k) {
        AddrEvent e = Impl::decode_addr(p, prev);
        if (e.block > b) break;
        v += e.delta();
    }
    return v;
}

std::vector<AddrEvent> Reader::addr_events(uint32_t addr, uint32_t tid, uint32_t b0, uint32_t b1, size_t limit) const {
    std::vector<AddrEvent> out;
    const Run* r = p_->run(addr, tid);
    if (!r || b1 < r->first_block || b0 > r->last_block) return out;
    const Part& pt = p_->part(tid);
    uint64_t ng = (r->n_events - 1) / group_every;
    auto gs = pt.addr_grp.as<AddrGroup>().subspan(r->grp_begin, ng);
    // start at the last group whose first block is < b0 (events of block b0 may begin before it)
    auto it = std::lower_bound(gs.begin(), gs.end(), b0, [](const AddrGroup& g, uint32_t v) { return g.block < v; });
    const uint8_t* p; uint32_t prev; uint64_t k;
    if (it == gs.begin()) { p = pt.addr_ev.data() + r->ev_off; prev = r->first_block; k = 0; }
    else { const AddrGroup& G = *(it - 1); p = pt.addr_ev.data() + G.ev_off; prev = G.block; k = uint64_t(it - gs.begin()) * group_every; }
    for (; k < r->n_events && out.size() < limit; ++k) {
        AddrEvent e = Impl::decode_addr(p, prev);
        if (e.block > b1) break;
        if (e.block >= b0) out.push_back(e);
    }
    return out;
}

Acc320 Reader::supply_at(uint32_t tid, uint32_t b) const {
    const TokDir* d = p_->tok(tid);
    return d ? p_->state_at(*d, b).supply : Acc320{};
}
int64_t Reader::holders_count_at(uint32_t tid, uint32_t b) const {
    const TokDir* d = p_->tok(tid);
    return d ? p_->state_at(*d, b).holders : 0;
}

Reader::Top Reader::holders_at(uint32_t tid, uint32_t b, uint32_t n) const {
    Top t{{}, 0, {}, true};
    const TokDir* d = p_->tok(tid);
    if (!d) return t;
    auto s = p_->state_at(*d, b);
    t.holders = s.holders; t.supply = s.supply;
    if (s.k == 0) return t;
    const Part& pt = p_->part(tid);
    auto cs = pt.ckpt_dir.as<CkptDir>().subspan(d->ckpt_begin, d->ckpt_end - d->ckpt_begin);
    auto it = std::upper_bound(cs.begin(), cs.end(), s.k, [](uint64_t v, const CkptDir& c) { return v < c.n_applied; });
    std::unordered_map<uint32_t, Acc320> cand;
    const CkptDir* c = it == cs.begin() ? nullptr : &*(it - 1);
    std::optional<Acc320> threshold;  // every holder outside the top list had at most this at the checkpoint
    uint64_t from = 0;
    if (c) {
        from = c->n_applied;
        const uint8_t* q = pt.ckpt_top.data() + c->top_off;
        Acc320 last;
        for (uint32_t i = 0; i < c->n_top; ++i) { uint32_t id = get_u32(q); last = get_acc(q); cand[id] = last; }
        if (c->n_top >= top_keep) threshold = last;
    }
    if (from < s.k) {
        auto cur = p_->tok_at(*d, from);
        for (uint64_t k = from; k < s.k; ++k) {
            Event e = Impl::decode_tok(cur.p, cur.prev);
            if (e.from == e.to) continue;
            for (uint32_t a : {e.from, e.to})
                if (!cand.count(a)) cand[a] = c ? p_->full_lookup(pt, *c, a) : Acc320{};
            cand[e.from].sub_u256(e.amt); cand[e.to].add_u256(e.amt);
        }
    }
    for (auto& [id, v] : cand) if (v.sign() > 0 && !p_->excluded(id)) t.rows.push_back({id, v});
    auto by_bal = [](const Holder& x, const Holder& y) { int k = cmp(x.balance, y.balance); return k ? k > 0 : x.addr < y.addr; };
    std::sort(t.rows.begin(), t.rows.end(), by_bal);
    if (t.rows.size() > n) t.rows.resize(n);
    if (threshold) t.exact = t.rows.size() == n && cmp(t.rows.back().balance, *threshold) > 0;
    return t;
}

std::vector<Event> Reader::token_events(uint32_t tid, uint32_t b0, uint32_t b1, size_t limit) const {
    std::vector<Event> out;
    const TokDir* d = p_->tok(tid);
    if (!d || b1 < d->first_block || b0 > d->last_block) return out;
    auto s = p_->state_at(*d, b0 == 0 ? 0 : b0 - 1);  // events before b0
    if (b0 == 0) s.k = 0;
    if (s.k >= d->n_events) return out;
    auto cur = p_->tok_at(*d, s.k);
    for (uint64_t k = s.k; k < d->n_events && out.size() < limit; ++k) {
        Event e = Impl::decode_tok(cur.p, cur.prev);
        if (e.block > b1) break;
        out.push_back(e);
    }
    return out;
}

void Reader::each_run(const std::function<void(const Run&, const Acc320&)>& f) const {
    for (auto& pt : p_->parts)
        for (const Run& r : pt.run.as<Run>()) { const uint8_t* q = pt.bal.data() + r.bal_off; f(r, get_acc(q)); }
}
void Reader::each_token(const std::function<void(const TokDir&)>& f) const {
    for (auto& pt : p_->parts) for (const TokDir& d : pt.tok_dir.as<TokDir>()) f(d);
}

}  // namespace machine::engine::ledger
