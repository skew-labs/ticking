// ledger_read.cpp: queries over a finished ledger build (read-only mmaps).
#include "machine/engine/ledger.hpp"
#include "ledger_io.hpp"

#include <filesystem>
#include <unordered_map>

namespace machine::engine::ledger::fmt {

struct Part {
    io::In tok_dir, tok_ev, tok_grp, ckpt_dir, ckpt_top, ckpt_full, run, addr_ev, addr_grp, bal;
};

struct Reader::Impl {
    io::Dict dict;
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
    // Random access to one token's events, one decoded group cached.
    struct TokRandom {
        const Impl* im; const TokDir* d; uint64_t g = ~uint64_t(0); std::vector<Event> buf;
        const Event& get(uint64_t idx) {
            uint64_t gg = idx / group_every;
            if (gg != g) {
                const Part& pt = im->part(d->tid);
                const Group& G = pt.tok_grp.as<Group>()[d->grp_begin + gg];
                const uint8_t* p = pt.tok_ev.data() + G.ev_off; uint32_t prev = G.prev_block;
                uint64_t end = std::min<uint64_t>(d->n_events, (gg + 1) * group_every);
                buf.clear();
                for (uint64_t k = gg * group_every; k < end; ++k) buf.push_back(decode_tok(p, prev));
                g = gg;
            }
            return buf[idx % group_every];
        }
    };
    // by_addr cursor over one run, positioned at group g (0 = run start)
    struct AddrCursor {
        TokRandom tr; const uint8_t* p; uint64_t prev; uint32_t addr;
        AddrEvent next() {
            uint64_t idx = prev + get_varint(p); prev = idx;
            const Event& e = tr.get(idx);
            AddrEvent a{}; a.block = e.block; a.tx = e.tx; a.log = e.log; a.batch = e.batch;
            a.in = e.to == addr; a.out = e.from == addr; a.other = a.in && a.out ? addr : a.in ? e.from : e.to;
            std::memcpy(a.amt, e.amt, 32);
            return a;
        }
    };
    AddrCursor cursor(const Run& r, uint64_t g) const {
        const Part& pt = part(r.tid);
        AddrCursor c{TokRandom{this, tok(r.tid)}, pt.addr_ev.data() + r.ev_off, 0, r.addr};
        if (g) { const AddrGroup& G = pt.addr_grp.as<AddrGroup>()[r.grp_begin + g - 1]; c.p = pt.addr_ev.data() + G.ev_off; c.prev = G.prev_idx; }
        return c;
    }

    // Token stream cursor positioned at event index `k` (0-based) of token d.
    struct TokCursor { const uint8_t* p; uint32_t prev; uint64_t k; };
    TokCursor tok_at(const TokDir& d, uint64_t k) const {
        const Part& pt = part(d.tid);
        auto gs = pt.tok_grp.as<Group>();
        uint64_t g = k / group_every;
        const Group& G = gs[d.grp_begin + g];
        TokCursor c{pt.tok_ev.data() + G.ev_off, G.prev_block, g * group_every};
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
        const uint8_t* p = pt.tok_ev.data() + G.ev_off; uint32_t prev = G.prev_block;
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
        std::filesystem::path d = dir;
        if (std::filesystem::exists(d / "manifest.ledger.json")) {   // standalone builds; merged builds are checked by ledger::open
            auto m = json::parse(machine::graphloop::load_text(d / "manifest.ledger.json"));
            uint32_t v = m.value("facts", json::object()).value("ledger_format", 1u);
            if (v != ledger_format) throw std::runtime_error("ledger format " + std::to_string(v) + ", reader " + std::to_string(ledger_format) + ": rebuild");
        }
        p_->dict = io::Dict(d);
        for (uint32_t i = 1; std::filesystem::exists(d / segment_file_name(Kind::tok_stats, i)); ++i) {
            Part pt;
            pt.tok_dir = io::In(d, Kind::tok_stats, i); pt.tok_ev = io::In(d, Kind::tt_tok_data, i);
            pt.tok_grp = io::In(d, Kind::tt_tok_index, i); pt.ckpt_dir = io::In(d, Kind::bal_ckpt_index, i);
            pt.ckpt_top = io::In(d, Kind::bal_top, i); pt.ckpt_full = io::In(d, Kind::bal_ckpt_data, i);
            pt.run = io::In(d, Kind::bal_head_index, i); pt.addr_ev = io::In(d, Kind::tt_addr_data, i);
            pt.addr_grp = io::In(d, Kind::tt_addr_index, i); pt.bal = io::In(d, Kind::bal_head_data, i);
            p_->parts.push_back(std::move(pt));
        }
        if (p_->parts.empty()) throw std::runtime_error("no ledger partitions in " + dir);
        p_->zero_id = addr_id(zero_addr).value_or(no_id);
        p_->dead_id = addr_id(dead_addr).value_or(no_id);
    } catch (...) { delete p_; throw; }
}
Reader::~Reader() { delete p_; }

uint32_t Reader::n_addr() const noexcept { return uint32_t(p_->dict.size()); }
uint32_t Reader::n_tok() const noexcept { return uint32_t(p_->dict.tokens().size()); }
uint32_t Reader::parts() const noexcept { return uint32_t(p_->parts.size()); }
std::optional<uint32_t> Reader::addr_id(const Addr& a) const noexcept { return p_->dict.find(a.data()); }
Addr Reader::addr(uint32_t id) const noexcept { Addr a; p_->dict.get(id, a.data()); return a; }
std::optional<uint32_t> Reader::tid(const Addr& token, TokenKind k) const noexcept {
    auto aid = p_->dict.find(token.data());
    if (!aid) return std::nullopt;
    auto ts = p_->dict.tokens();
    auto key = std::pair{*aid, uint8_t(k)};
    auto it = std::lower_bound(ts.begin(), ts.end(), key, [](const TokenRec& r, const std::pair<uint32_t, uint8_t>& q) { return std::pair{r.addr_id, r.kind} < q; });
    return it != ts.end() && it->addr_id == *aid && it->kind == uint8_t(k) ? std::optional<uint32_t>(uint32_t(it - ts.begin())) : std::nullopt;
}
const TokenRec& Reader::token(uint32_t tid) const noexcept { return p_->dict.tokens()[tid]; }

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
    Acc320 v; uint64_t g = uint64_t(it - gs.begin()), k = g * group_every;
    if (g) std::memcpy(v.w, gs[g - 1].bal, 40);
    auto cur = p_->cursor(*r, g);
    uint64_t end = std::min<uint64_t>(r->n_events, (g + 1) * group_every);
    for (; k < end; ++k) {
        AddrEvent e = cur.next();
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
    uint64_t g = it == gs.begin() ? 0 : uint64_t(it - gs.begin()), k = g * group_every;
    auto cur = p_->cursor(*r, g);
    for (; k < r->n_events && out.size() < limit; ++k) {
        AddrEvent e = cur.next();
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
    if (s.k == 0 || n == 0) return t;
    const Part& pt = p_->part(tid);
    auto cs = pt.ckpt_dir.as<CkptDir>().subspan(d->ckpt_begin, d->ckpt_end - d->ckpt_begin);
    auto it = std::upper_bound(cs.begin(), cs.end(), s.k, [](uint64_t v, const CkptDir& c) { return v < c.n_applied; });
    const CkptDir* c = it == cs.begin() ? nullptr : &*(it - 1);
    // Checkpoint top list (exact balances).  When it was cut at top_keep, every other holder had at most `threshold`;
    // when it was not cut, every other address had a balance <= 0 at the checkpoint.
    std::unordered_map<uint32_t, Acc320> base;
    std::optional<Acc320> threshold;
    uint64_t from = 0;
    if (c) {
        from = c->n_applied;
        const uint8_t* q = pt.ckpt_top.data() + c->top_off;
        Acc320 last;
        for (uint32_t i = 0; i < c->n_top; ++i) { uint32_t id = get_u32(q); last = get_acc(q); base[id] = last; }
        if (c->n_top >= top_keep) threshold = last;
    }
    std::unordered_map<uint32_t, Acc320> delta;   // net movement between the checkpoint and block b
    if (from < s.k) {
        auto cur = p_->tok_at(*d, from);
        for (uint64_t k = from; k < s.k; ++k) {
            Event e = Impl::decode_tok(cur.p, cur.prev);
            if (e.from == e.to) continue;
            delta[e.from].sub_u256(e.amt); delta[e.to].add_u256(e.amt);
        }
    }
    auto by_bal = [](const Holder& x, const Holder& y) { int k = cmp(x.balance, y.balance); return k ? k > 0 : x.addr < y.addr; };
    std::vector<Holder> rows;
    for (auto& [id, v] : base) {
        Acc320 f = v; if (auto q = delta.find(id); q != delta.end()) f += q->second;
        if (f.sign() > 0 && !p_->excluded(id)) rows.push_back({id, f});
    }
    struct Pend { Acc320 upper; uint32_t id; Acc320 dl; };
    std::vector<Pend> pend;
    for (auto& [id, dl] : delta) {
        if (dl.sign() <= 0 || p_->excluded(id) || base.count(id)) continue;
        if (!c) { rows.push_back({id, dl}); continue; }   // replay from the start: delta is the balance
        Acc320 up = dl; if (threshold) up += *threshold;
        pend.push_back({up, id, dl});
    }
    std::sort(rows.begin(), rows.end(), by_bal);
    std::sort(pend.begin(), pend.end(), [](const Pend& x, const Pend& y) { return cmp(x.upper, y.upper) > 0; });
    for (auto& p : pend) {   // exact checkpoint balance only for addresses that could still rank
        if (rows.size() >= n) {   // ties rank by address id, so an equal bound still counts when its id is smaller
            int k = cmp(p.upper, rows[n - 1].balance);
            if (k < 0) break;
            if (k == 0 && p.id > rows[n - 1].addr) continue;
        }
        Acc320 f = balance_at(p.id, tid, c->block); f += p.dl;
        if (f.sign() <= 0) continue;
        Holder h{p.id, f};
        rows.insert(std::upper_bound(rows.begin(), rows.end(), h, by_bal), h);
        if (rows.size() > n) rows.pop_back();
    }
    if (rows.size() > n) rows.resize(n);
    t.rows = std::move(rows);
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

const TokDir* Reader::tok_dir(uint32_t tid) const noexcept { return p_->tok(tid); }
uint32_t Reader::zero_id() const noexcept { return p_->zero_id; }

// Decodes group g of a run (events [64g, 64g + 64)) into `out`.
static void run_group(const Reader::Impl& im, const Run& r, uint64_t g, std::vector<AddrEvent>& out) {
    out.clear();
    auto cur = im.cursor(r, g);
    uint64_t end = std::min<uint64_t>(r.n_events, (g + 1) * group_every);
    for (uint64_t k = g * group_every; k < end; ++k) out.push_back(cur.next());
}

std::vector<AddrEvent> Reader::addr_events_rev(uint32_t addr, uint32_t tid, uint32_t b0, uint32_t b1, size_t limit) const {
    std::vector<AddrEvent> out, buf;
    const Run* r = p_->run(addr, tid);
    if (!r || b1 < r->first_block || b0 > r->last_block || !limit) return out;
    const Part& pt = p_->part(tid);
    uint64_t ng = (r->n_events - 1) / group_every;
    auto gs = pt.addr_grp.as<AddrGroup>().subspan(r->grp_begin, ng);
    // last group whose first block is <= b1
    uint64_t g = uint64_t(std::upper_bound(gs.begin(), gs.end(), b1, [](uint32_t v, const AddrGroup& x) { return v < x.block; }) - gs.begin());
    for (;; --g) {
        run_group(*p_, *r, g, buf);
        for (auto it = buf.rbegin(); it != buf.rend(); ++it) {
            if (it->block > b1) continue;
            if (it->block < b0) return out;
            out.push_back(*it);
            if (out.size() >= limit) return out;
        }
        if (g == 0) break;
    }
    return out;
}

std::vector<Event> Reader::token_events_rev(uint32_t tid, uint32_t b0, uint32_t b1, size_t limit) const {
    std::vector<Event> out, buf;
    const TokDir* d = p_->tok(tid);
    if (!d || b1 < d->first_block || b0 > d->last_block || !limit) return out;
    uint64_t K = p_->state_at(*d, b1).k;  // events with block <= b1
    if (!K) return out;
    const Part& pt = p_->part(tid);
    auto gs = pt.tok_grp.as<Group>();
    for (uint64_t g = (K - 1) / group_every;; --g) {
        const Group& G = gs[d->grp_begin + g];
        const uint8_t* p = pt.tok_ev.data() + G.ev_off; uint32_t prev = G.prev_block;
        buf.clear();
        uint64_t end = std::min<uint64_t>(K, (g + 1) * group_every);
        for (uint64_t k = g * group_every; k < end; ++k) buf.push_back(Impl::decode_tok(p, prev));
        for (auto it = buf.rbegin(); it != buf.rend(); ++it) {
            if (it->block < b0) return out;
            out.push_back(*it);
            if (out.size() >= limit) return out;
        }
        if (g == 0) break;
    }
    return out;
}

Reader::At Reader::at(uint32_t addr, uint32_t tid, uint32_t b) const {
    At a{{}, 0, 0};
    const Run* r = p_->run(addr, tid);
    if (!r || b < r->first_block) return a;
    if (b >= r->last_block) { const uint8_t* q = p_->part(tid).bal.data() + r->bal_off; return {get_acc(q), r->n_in, r->n_out}; }
    auto cur = p_->cursor(*r, 0);
    for (uint64_t k = 0; k < r->n_events; ++k) {
        AddrEvent e = cur.next();
        if (e.block > b) break;
        a.balance += e.delta(); a.n_in += e.in; a.n_out += e.out;
    }
    return a;
}

void Reader::each_run(const std::function<void(const Run&, const Acc320&)>& f) const {
    for (auto& pt : p_->parts)
        for (const Run& r : pt.run.as<Run>()) { const uint8_t* q = pt.bal.data() + r.bal_off; f(r, get_acc(q)); }
}
void Reader::each_token(const std::function<void(const TokDir&)>& f) const {
    for (auto& pt : p_->parts) for (const TokDir& d : pt.tok_dir.as<TokDir>()) f(d);
}

}  // namespace machine::engine::ledger::fmt
