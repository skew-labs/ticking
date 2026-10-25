// ledger_api.cpp: the api.hpp ledger reader (Store) and the verify dump over fmt::Reader.
#include "machine/engine/ledger.hpp"

#include <algorithm>
#include <cinttypes>

namespace machine::engine::ledger {
namespace {

I256 to_i256(const fmt::Acc320& a, bool& overflow) {
    I256 r; std::memcpy(r.v.w, a.w, 32);
    if (!a.fits_i256()) overflow = true;
    return r;
}
U256 to_u256(const uint64_t m[4]) { U256 u; std::memcpy(u.w, m, 32); return u; }
uint32_t clamp_block(uint64_t b) { return b > 0xffffffffull ? 0xffffffffu : uint32_t(b); }

Event make_event(const Store& s, uint32_t tid, uint32_t block, uint32_t tx, uint32_t log, uint32_t batch, uint32_t from, uint32_t to, const uint64_t amt[4]) {
    Event e{};
    e.block = block; e.tx_index = tx; e.log_index = log; e.batch_index = batch; e.from = from; e.to = to; e.tid = tid;
    e.kind = TokenKind(s.r.token(tid).kind); e.amount = to_u256(amt); e.has_hash = false;
    e.time = s.times ? s.times->time_at(block).value_or(0) : 0;
    return e;
}
Event from_addr_event(const Store& s, uint32_t addr, uint32_t tid, const fmt::AddrEvent& a) {
    uint32_t from = a.out ? addr : a.other, to = a.in ? addr : a.other;
    return make_event(s, tid, a.block, a.tx, a.log, a.batch, from, to, a.amt);
}
bool key_less(const Event& x, const Event& y) {
    return std::tie(x.block, x.tx_index, x.log_index, x.batch_index, x.tid) < std::tie(y.block, y.tx_index, y.log_index, y.batch_index, y.tid);
}

}  // namespace

std::shared_ptr<const Store> open(const BuildDir& b) {
    uint32_t v = b.facts("ledger").value("ledger_format", 1u);
    if (v != fmt::ledger_format) throw std::runtime_error("ledger format " + std::to_string(v) + ", reader " + std::to_string(fmt::ledger_format) + ": rebuild");
    auto s = std::make_shared<Store>(b.dir().string());
    if (b.has(Kind::blk_time)) s->times = std::make_unique<BlockTime>(b);
    return s;
}

HoldersAt holders_at(const Store& s, uint32_t tid, uint64_t block, uint32_t limit) {
    HoldersAt h; h.tid = tid; h.block = block;
    auto t = s.r.holders_at(tid, clamp_block(block), limit);
    h.holders = t.holders < 0 ? 0 : uint64_t(t.holders);
    h.supply = to_i256(t.supply, h.overflow);
    fmt::Acc320 sum;
    for (auto& r : t.rows) { h.top.push_back({r.addr, to_i256(r.balance, h.overflow)}); sum += r.balance; }
    h.held_top = to_i256(sum, h.overflow);
    h.cold_missing = !t.exact;   // rows are exact balances; ranking beyond the checkpoint top list was not proven
    return h;
}

BalanceAt balance_at(const Store& s, uint32_t addr, uint32_t tid, uint64_t block) {
    BalanceAt b;
    auto a = s.r.at(addr, tid, clamp_block(block));
    b.balance = to_i256(a.balance, b.overflow); b.n_in = a.n_in; b.n_out = a.n_out;
    return b;
}

bool events(const Store& s, const EventQuery& q, const std::function<void(const Event&)>& f) {
    uint32_t b0 = clamp_block(q.block0), b1 = clamp_block(q.block1);
    size_t lim = q.limit ? q.limit : 1000;
    std::vector<Event> out; bool more = false;
    if (q.addr != none32 && q.tid != none32) {
        auto v = q.newest_first ? s.r.addr_events_rev(q.addr, q.tid, b0, b1, lim + 1) : s.r.addr_events(q.addr, q.tid, b0, b1, lim + 1);
        for (auto& a : v) out.push_back(from_addr_event(s, q.addr, q.tid, a));
    } else if (q.tid != none32) {
        auto v = q.newest_first ? s.r.token_events_rev(q.tid, b0, b1, lim + 1) : s.r.token_events(q.tid, b0, b1, lim + 1);
        for (auto& e : v) out.push_back(make_event(s, q.tid, e.block, e.tx, e.log, e.batch, e.from, e.to, e.amt));
    } else if (q.addr != none32) {
        for (auto& [tid, head] : s.r.holdings(q.addr)) {
            if (head.last_block < b0 || head.first_block > b1) continue;
            auto v = q.newest_first ? s.r.addr_events_rev(q.addr, tid, b0, b1, lim + 1) : s.r.addr_events(q.addr, tid, b0, b1, lim + 1);
            for (auto& a : v) out.push_back(from_addr_event(s, q.addr, tid, a));
        }
        std::sort(out.begin(), out.end(), [&](const Event& x, const Event& y) { return q.newest_first ? key_less(y, x) : key_less(x, y); });
    }
    if (out.size() > lim) { out.resize(lim); more = true; }
    for (auto& e : out) f(e);
    return !more;
}

void wallet_tokens(const Store& s, uint32_t addr, std::vector<WalletToken>& out) {
    out.clear();
    uint32_t zero = s.r.zero_id();
    for (auto& [tid, h] : s.r.holdings(addr)) {
        WalletToken w{};
        w.tid = tid; w.overflow = false; w.balance = to_i256(h.balance, w.overflow);
        w.n_in = h.n_in; w.n_out = h.n_out; w.first_block = h.first_block; w.last_block = h.last_block;
        w.first_in_block = h.first_in_block; w.first_buy_block = none32;   // buys need dex trades (phase 2)
        w.acquired = 0;
        if (h.n_in) w.acquired |= acq_received;
        if (h.n_in) {
            auto first = s.r.addr_events(addr, tid, h.first_in_block, h.first_in_block, 64);
            for (auto& e : first) if (e.in && !e.out && e.other == zero) { w.acquired |= acq_minted; break; }
        }
        out.push_back(w);
    }
}

std::optional<TokenStats> token_stats(const Store& s, uint32_t tid) {
    const fmt::TokDir* d = s.r.tok_dir(tid);
    if (!d) return std::nullopt;
    TokenStats t{};
    bool of = false;
    t.tid = tid; t.n_transfers = d->n_events; t.n_holders = d->holders_end < 0 ? 0 : uint64_t(d->holders_end);
    t.supply = to_i256(s.r.supply_at(tid, d->last_block), of);
    t.first_block = d->first_block; t.last_block = d->last_block;
    t.flags = uint16_t((d->flags & fmt::tok_irregular ? tf_irregular : 0) | (of ? tf_overflow : 0));
    auto first = s.r.token_events(tid, d->first_block, d->first_block, 1);
    t.first_from = first.empty() ? none32 : first[0].from;
    return t;
}

void dump(const BuildDir& b, std::string_view table, const DumpFilter& f, CsvOut& out) {
    Store s(b.dir().string());
    auto keep_addr = [&](uint32_t a) { return f.addrs.empty() || std::binary_search(f.addrs.begin(), f.addrs.end(), a); };
    auto keep_tid = [&](uint32_t t) { return f.tids.empty() || std::binary_search(f.tids.begin(), f.tids.end(), t); };
    auto hex = [](const fmt::Addr& a) {
        static const char* d = "0123456789abcdef"; std::string x = "0x";
        for (uint8_t c : a) { x.push_back(d[c >> 4]); x.push_back(d[c & 15]); }
        return x;
    };
    auto tok_addr = [&](uint32_t tid) { return s.r.addr(s.r.token(tid).addr_id); };
    if (table == "bal") {
        s.r.each_run([&](const fmt::Run& r, const fmt::Acc320& bal) {
            if (!keep_addr(r.addr) || !keep_tid(r.tid)) return;
            auto k = kind_text(TokenKind(s.r.token(r.tid).kind));
            out.row({hex(s.r.addr(r.addr)), hex(tok_addr(r.tid)), k, std::to_string(r.n_in), std::to_string(r.n_out), bal.to_dec(),
                     std::to_string(r.first_block), std::to_string(r.last_block)});
        });
    } else if (table == "token_stats") {
        s.r.each_token([&](const fmt::TokDir& d) {
            if (!keep_tid(d.tid)) return;
            out.row({hex(tok_addr(d.tid)), kind_text(TokenKind(s.r.token(d.tid).kind)), std::to_string(d.n_events),
                     std::to_string(d.holders_end < 0 ? 0 : d.holders_end), s.r.supply_at(d.tid, d.last_block).to_dec()});
        });
    } else if (table == "counts") {
        for (auto& [k, v] : b.facts("ledger").items()) out.row({"ledger." + k, v.is_string() ? v.get<std::string>() : v.dump()});
    }
}

}  // namespace machine::engine::ledger
