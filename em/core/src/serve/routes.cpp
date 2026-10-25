// emv2-serve routes. Each answer mirrors the Python service the bridge reads today (invscreen.py for holders-at, address-chains and
// trace-graph; glserve /tools/path for trace with `to`), key for key and in the same order, so the bridge's shadow mode compares
// like with like. Differences that come from the data itself (EMV2 reads exact balances from its own ledger, the build head instead
// of eth_getCode at the chain head) are stated in the answer's strings, not hidden.
#include <machine/engine/serve_json.hpp>
#include <machine/engine/serve_routes.hpp>
#include <cryptopp/sha.h>
#include <algorithm>
#include <cstdio>
#include <sys/stat.h>
#include <cmath>
#include <cstring>
#include <deque>
#include <list>
#include <mutex>
#include <map>
#include <optional>
#include <set>
#include <unordered_map>
#include <unordered_set>
#include <thread>

namespace machine::engine::serve {
namespace {
using graphloop::HttpRequest;
using graphloop::HttpResponse;
using graphloop::qparam;

constexpr const char* kPrefix = "/api/public/em";
const std::set<std::string> kVenueCats = {"dex_pool", "dex_router", "token_contract", "bridge", "lending", "nft_contract"};
const std::set<std::string> kExchCats = {"cex_hot_wallet", "cex_deposit"};
const std::set<std::string> kHubCats = {"dex_pool", "dex_router", "token_contract", "bridge", "lending", "nft_contract", "cex_hot_wallet", "cex_deposit"};
constexpr uint32_t kFunderMaxFanout = 60;                      // investigate.FUNDER_MAX_FANOUT
const std::map<int, size_t> kFanout = {{1, 10}, {2, 6}, {3, 4}}; // invscreen FANOUT
constexpr size_t kMaxNodes = 120;                               // invscreen MAX_NODES
const std::map<std::string, uint32_t> kWindows = {{"7d", 7 * 86400}, {"30d", 30 * 86400}, {"90d", 90 * 86400}, {"1y", 365 * 86400}};
const std::pair<const char*, uint32_t> kSnapOffsets[] = {{"launch", 0}, {"+10m", 600}, {"+1h", 3600}, {"+6h", 21600}, {"+1d", 86400}};

const char* native_symbol(const std::string& chain) {
    if (chain == "polygon") return "POL";
    if (chain == "arc") return "USDC";
    if (chain == "xlayer") return "OKB";
    if (chain == "hyperevm") return "HYPE";
    return "ETH";   // invscreen sym_dec: "ETH" for native on every chain it serves
}

std::string lower(std::string s) { for (auto& c : s) c = char(std::tolower(uint8_t(c))); return s; }
bool is_addr(const std::string& s) {
    if (s.size() != 42 || s[0] != '0' || s[1] != 'x') return false;
    for (size_t i = 2; i < 42; i++) if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) return false;
    return true;
}
bool is_digits(const std::string& s) { return !s.empty() && std::all_of(s.begin(), s.end(), [](char c) { return c >= '0' && c <= '9'; }); }
bool burn(const Addr& a) {
    static const Addr dead = graphloop::addr_from("0x000000000000000000000000000000000000dead");
    static const Addr zero{};
    return a == zero || a == dead;
}

std::string build_header(const ChainMap& chains, const std::vector<std::string>& used) {
    std::string h;
    for (auto& c : used) {
        auto it = chains.find(c);
        if (it == chains.end()) continue;
        if (!h.empty()) h += ',';
        h += c + ":" + it->second->build_id();
    }
    return h.empty() ? "none" : h;
}
// graphloop's HttpServer writes "Content-Type: <ctype>\r\n": the build id travels as one more header line after it.
HttpResponse reply(int status, std::string body, const std::string& build) {
    return {status, std::move(body), "application/json\r\nX-EMV2-Build: " + build};
}
// An answer computed at a moment before the build head says so: with_as_of() reports this block/time instead of the head.
HttpResponse reply_at(int status, std::string body, const std::string& build, uint64_t block, uint64_t time) {
    return {status, std::move(body), "application/json\r\nX-EMV2-Build: " + build + "\r\nX-EMV2-As-Of: " + std::to_string(block) + ":" + std::to_string(time)};
}
HttpResponse err(int status, const char* error, const std::string& detail, const std::string& build = "none") {
    Json j; j.ob().ks("error", error).ks("detail", detail).oe();
    return reply(status, j.s, build);
}
HttpResponse unsupported(const std::string& detail, const std::string& build = "none") { return err(501, "unsupported", detail, build); }
HttpResponse bad(const std::string& detail) { Json j; j.ob().ks("detail", detail).oe(); return reply(400, j.s, "none"); }

// Tokens held back after the independent RPC check (tools/rpc_truth.py): <root>/held.tsv, one line per token,
// "<chain>\t0x<token>\t<reason>\t<reason in Korean>". Their balance routes answer available:false with the reason and no numbers.
// Re-read when the file's mtime changes (stat at most once a second); a missing file holds nothing back.
// cls: spam (fake balances / phishing name), revert (balanceOf refuses: look-alike), native (gas token), interest / nonstd
// (balances move without transfer events: balanceKind event_sum).
struct Held { std::string reason, reason_ko, cls; };
class HeldTokens {
    std::mutex mu_;
    std::string path_;
    int64_t stamp_ = -1, checked_ns_ = 0;
    std::map<std::string, Held> held_;   // "<chain>:0x<token>"
    void refresh_locked() {
        int64_t now = int64_t(graphloop::mono_ns());
        if (now - checked_ns_ < 1'000'000'000 && stamp_ != -1) return;
        checked_ns_ = now;
        struct stat st{};
        int64_t stamp = path_.empty() || ::stat(path_.c_str(), &st) != 0 ? 0 : int64_t(st.st_mtim.tv_sec) * 1'000'000'000 + st.st_mtim.tv_nsec;
        if (stamp == stamp_) return;
        stamp_ = stamp; held_.clear();
        if (!stamp) return;
        FILE* f = std::fopen(path_.c_str(), "r");
        if (!f) return;
        char line[2048];
        while (std::fgets(line, sizeof line, f)) {
            std::string l(line);
            while (!l.empty() && (l.back() == '\n' || l.back() == '\r')) l.pop_back();
            if (l.empty() || l[0] == '#') continue;
            std::vector<std::string> c; size_t i = 0;
            for (;;) { size_t z = l.find('\t', i); c.push_back(l.substr(i, z == std::string::npos ? std::string::npos : z - i)); if (z == std::string::npos) break; i = z + 1; }
            if (c.size() < 3) continue;
            held_[lower(c[0]) + ":" + lower(c[1])] = Held{c[2], c.size() > 3 ? c[3] : c[2], c.size() > 4 ? lower(c[4]) : std::string()};
        }
        std::fclose(f);
    }
public:
    void set_path(std::string p) { std::lock_guard g(mu_); path_ = std::move(p); stamp_ = -1; }
    std::optional<Held> find(const std::string& chain, const std::string& token) {
        std::lock_guard g(mu_); refresh_locked();
        auto it = held_.find(chain + ":" + token);
        if (it == held_.end()) return std::nullopt;
        return it->second;
    }
    std::vector<std::pair<std::string, Held>> all() { std::lock_guard g(mu_); refresh_locked(); return {held_.begin(), held_.end()}; }
    int64_t stamp() { std::lock_guard g(mu_); refresh_locked(); return stamp_; }
};
HeldTokens& held_tokens() { static HeldTokens h; return h; }
// Phishing / airdrop-bait names ("UNI | t.me/... visit to claim"): their transfers are fake, so they are spam wherever they appear.
// Look-alike symbols (address poisoning: "USDТ" with a Cyrillic Т, "ꓴꓢꓓꓔ" in Lisu, hair spaces): letters from the confusable
// scripts folded to ASCII; when the folded symbol is a major one and the original is not plain ASCII, the token is a look-alike.
bool lookalike_symbol(std::string_view sym) {
    std::string fold; bool confusable = false;
    for (size_t i = 0; i < sym.size();) {
        uint8_t c = uint8_t(sym[i]); uint32_t cp; size_t n;
        if (c < 0x80) { cp = c; n = 1; }
        else if ((c >> 5) == 6 && i + 1 < sym.size()) { cp = ((c & 0x1f) << 6) | (uint8_t(sym[i + 1]) & 0x3f); n = 2; }
        else if ((c >> 4) == 14 && i + 2 < sym.size()) { cp = ((c & 0x0f) << 12) | ((uint8_t(sym[i + 1]) & 0x3f) << 6) | (uint8_t(sym[i + 2]) & 0x3f); n = 3; }
        else if ((c >> 3) == 30 && i + 3 < sym.size()) { cp = 0x10000; n = 4; }
        else { cp = 0xfffd; n = 1; }
        i += n;
        if (cp < 0x80) {
            if (cp == ' ' || cp == '\t') { confusable = true; continue; }   // padding spaces around a symbol are a poisoning trick too
            fold += char(std::toupper(int(cp))); continue;
        }
        static const std::map<uint32_t, char> m = {
            {0x0410,'A'},{0x0412,'B'},{0x0415,'E'},{0x041A,'K'},{0x041C,'M'},{0x041D,'H'},{0x041E,'O'},{0x0420,'P'},{0x0421,'C'},{0x0422,'T'},
            {0x0425,'X'},{0x0430,'A'},{0x0435,'E'},{0x043E,'O'},{0x0440,'P'},{0x0441,'C'},{0x0443,'Y'},{0x0445,'X'},{0x0405,'S'},{0x0406,'I'},
            {0x0408,'J'},{0x0455,'S'},{0x0456,'I'},{0x0391,'A'},{0x0392,'B'},{0x0395,'E'},{0x0396,'Z'},{0x0397,'H'},{0x0399,'I'},{0x039A,'K'},
            {0x039C,'M'},{0x039D,'N'},{0x039F,'O'},{0x03A1,'P'},{0x03A4,'T'},{0x03A5,'Y'},{0x03A7,'X'},{0x03BF,'O'},
            {0xA4D0,'B'},{0xA4D1,'P'},{0xA4D2,'D'},{0xA4D3,'D'},{0xA4D4,'T'},{0xA4D6,'G'},{0xA4D7,'K'},{0xA4D9,'J'},{0xA4DA,'C'},{0xA4DC,'Z'},
            {0xA4DD,'F'},{0xA4DF,'M'},{0xA4E0,'N'},{0xA4E1,'L'},{0xA4E2,'S'},{0xA4E3,'R'},{0xA4E6,'V'},{0xA4E7,'H'},{0xA4EA,'W'},{0xA4EB,'X'},
            {0xA4EC,'Y'},{0xA4EE,'A'},{0xA4F0,'E'},{0xA4F2,'I'},{0xA4F3,'O'},{0xA4F4,'U'},
            {0x13A0,'D'},{0x13A1,'R'},{0x13A2,'T'},{0x13A5,'I'},{0x13A9,'Y'},{0x13AA,'A'},{0x13AB,'J'},{0x13AC,'E'},{0x13B3,'W'},{0x13B7,'M'},
            {0x13BB,'H'},{0x13C0,'G'},{0x13C3,'Z'},{0x13D2,'R'},{0x13D4,'W'},{0x13DA,'S'},{0x13DE,'L'},{0x13DF,'C'},{0x13E2,'P'},{0x13E6,'K'},
            {0x13F4,'B'},{0x0501,'D'},{0x051B,'Q'},{0x051C,'W'}};
        if (cp == 0x00A0 || cp == 0x00AD || cp == 0x180E || (cp >= 0x2000 && cp <= 0x200F) || (cp >= 0x2028 && cp <= 0x202F) ||
            (cp >= 0x205F && cp <= 0x206F) || cp == 0x2800 || cp == 0x3000 || cp == 0x3164 || cp == 0xFEFF) { confusable = true; continue; }   // spaces, format
        if (cp >= 0xFF21 && cp <= 0xFF3A) { fold += char('A' + (cp - 0xFF21)); confusable = true; continue; }   // fullwidth
        if (cp >= 0xFF41 && cp <= 0xFF5A) { fold += char('A' + (cp - 0xFF41)); confusable = true; continue; }
        auto it = m.find(cp);
        if (it != m.end()) { fold += it->second; confusable = true; continue; }
        if ((cp >= 0x0300 && cp <= 0x036F) || (cp >= 0x1AB0 && cp <= 0x1AFF) || (cp >= 0x1DC0 && cp <= 0x1DFF) || (cp >= 0x20D0 && cp <= 0x20FF) ||
            (cp >= 0xFE20 && cp <= 0xFE2F)) { confusable = true; continue; }   // combining marks ("ET\u0323H")
        if (cp == 0x20AE) { fold += 'T'; continue; }   // ₮ (USD₮0 is the real Tether symbol): not a confusable by itself
        fold += '?'; confusable = true;   // an unmapped letter from another script: matched as a wildcard below
    }
    static const std::set<std::string> major = {"USDC", "USDT", "DAI", "USDE", "USDT0", "USDC.E", "USDCE", "PYUSD", "FDUSD", "TUSD", "USDS", "FRAX",
                                                "ETH", "WETH", "WBTC", "BTC", "ARB", "OP", "POL", "MATIC", "LINK", "UNI", "EURC", "AUSD", "USDG", "GHO"};
    if (!confusable) return false;
    if (major.count(fold)) return true;
    // unmapped letters ('?') match anything, the rest must match a major symbol letter for letter (at least two exact letters)
    for (auto& m : major) {
        if (m.size() != fold.size()) continue;
        size_t exact = 0; bool ok = true;
        for (size_t i = 0; i < m.size() && ok; i++) { if (fold[i] == '?') continue; if (fold[i] != m[i]) ok = false; else exact++; }
        if (ok && exact >= 2) return true;
    }
    return false;
}
bool spam_symbol(std::string_view sym) {
    if (lookalike_symbol(sym)) return true;
    std::string l(sym); for (auto& c : l) c = char(std::tolower(uint8_t(c)));
    static const char* pat[] = {"http", "www.", "t.me", "t.ly", "visit", "claim", "airdrop", "reward", ".com", ".io", ".xyz", ".org", ".net",
                                ".so", ".supply", ".site", ".app", ".top", ".finance", ".cc", ".vip", ".link", ".gift"};
    for (auto* p : pat) if (l.find(p) != std::string::npos) return true;
    return false;
}
const Held& spam_by_name() {
    static const Held h{"Spam token: its name is an airdrop / phishing lure and its transfers do not move real balances.",
                        "스팸으로 분류된 토큰이에요(이름이 에어드롭·피싱 유도라 전송 기록이 실제 잔고가 아니에요).", "spam"};
    return h;
}
// held entry, or the name rule for an unlisted token
// Canonical stablecoin contracts per chain: <root>/stables.tsv, "<chain>\t0x<token>\t<SYMBOL>" (registry/stables). On a chain
// listed there, USD is given for those contracts only, and another token using one of their symbols is an impersonator (spam).
class Stables {
    std::mutex mu_; std::string path_; int64_t stamp_ = -1, checked_ns_ = 0;
    std::set<std::string> addr_;                  // "<chain>:0x<token>"
    std::map<std::string, std::set<std::string>> syms_;   // chain -> upper symbols
    void refresh_locked() {
        int64_t now = int64_t(graphloop::mono_ns());
        if (now - checked_ns_ < 1'000'000'000 && stamp_ != -1) return;
        checked_ns_ = now;
        struct stat st{};
        int64_t stamp = path_.empty() || ::stat(path_.c_str(), &st) != 0 ? 0 : int64_t(st.st_mtim.tv_sec) * 1'000'000'000 + st.st_mtim.tv_nsec;
        if (stamp == stamp_) return;
        stamp_ = stamp; addr_.clear(); syms_.clear();
        FILE* f = stamp ? std::fopen(path_.c_str(), "r") : nullptr;
        if (!f) return;
        char line[512];
        while (std::fgets(line, sizeof line, f)) {
            std::string l(line); while (!l.empty() && (l.back() == '\n' || l.back() == '\r')) l.pop_back();
            if (l.empty() || l[0] == '#') continue;
            size_t a = l.find('\t'), b = a == std::string::npos ? a : l.find('\t', a + 1);
            if (b == std::string::npos) continue;
            std::string c = lower(l.substr(0, a)), t = lower(l.substr(a + 1, b - a - 1)), sym = l.substr(b + 1, l.find('\t', b + 1) == std::string::npos ? std::string::npos : l.find('\t', b + 1) - b - 1);
            for (auto& ch : sym) ch = char(std::toupper(uint8_t(ch)));
            addr_.insert(c + ":" + t); syms_[c].insert(sym);
        }
        std::fclose(f);
    }
public:
    void set_path(std::string p) { std::lock_guard g(mu_); path_ = std::move(p); stamp_ = -1; }
    // 1: canonical stable, 0: not listed, -1: chain not in the registry (fall back to the symbol rule)
    int canonical(const std::string& chain, const std::string& token) { std::lock_guard g(mu_); refresh_locked(); if (!syms_.count(chain)) return -1; return addr_.count(chain + ":" + token) ? 1 : 0; }
    bool impersonates(const std::string& chain, const std::string& token, std::string_view symbol) {
        std::lock_guard g(mu_); refresh_locked();
        auto it = syms_.find(chain); if (it == syms_.end()) return false;
        std::string up(symbol); for (auto& ch : up) ch = char(std::toupper(uint8_t(ch)));
        return it->second.count(up) && !addr_.count(chain + ":" + token);
    }
    int64_t stamp() { std::lock_guard g(mu_); refresh_locked(); return stamp_; }
};
Stables& stables() { static Stables s; return s; }
const Held& spam_impersonator() {
    static const Held h{"Spam token: it uses the symbol of a major stablecoin but is not that stablecoin's contract on this chain.",
                        "스팸으로 분류된 토큰이에요(주요 스테이블코인 이름을 쓰지만 이 체인의 진짜 컨트랙트가 아니에요).", "spam"};
    return h;
}
std::optional<Held> held_or_spam(const std::string& chain, const std::string& token, std::string_view symbol) {
    if (auto h = held_tokens().find(chain, token)) return h;
    if (spam_symbol(symbol)) return spam_by_name();
    if (stables().impersonates(chain, token, symbol)) return spam_impersonator();
    return std::nullopt;
}
const std::map<std::string, std::string> kCategoryName = {
    {"eoa_user", "Wallet"}, {"eoa_whale", "Whale"}, {"mev_bot", "MEV bot"}, {"dex_pool", "DEX pool"}, {"dex_router", "DEX router"},
    {"token_contract", "Token contract"}, {"other_contract", "Contract"}, {"contract_other", "Contract"}, {"lending", "Lending market"},
    {"bridge", "Bridge"}, {"cex_hot_wallet", "Exchange hot wallet"}, {"cex_deposit", "Exchange deposit"}, {"multisig_or_safe", "Multisig / Safe"},
    {"nft_contract", "NFT contract"}, {"unknown", "Unclassified"}};
bool is_spam(const Held& h) { return h.cls == "spam" || h.cls == "revert"; }
const char* balance_kind(const Held& h) { return h.cls == "interest" || h.cls == "nonstd" ? "event_sum" : h.cls == "native" ? "native_partial" : "unverified"; }
// The answer for a held token: 200, available:false, the reason, no numbers (an assistant must not quote a wrong balance).
HttpResponse held_reply(const char* schema, const std::string& market, const Held& h, const std::string& build) {
    Json j; j.ob().ks("schema", schema).ks("market", market).kb("available", false).kb("held", true).kb("spam", is_spam(h))
        .ksn("class", h.cls).ks("balanceKind", balance_kind(h)).ks("reason", h.reason).ks("reasonKo", h.reason_ko)
        .k("status").ob().ks("state", is_spam(h) ? "spam" : "held").ks("detail", "balances failed the independent RPC check").oe().oe();
    return reply(200, j.s, build);
}

// Only the bridge's parameters reach the services; anything else is refused the same way the bridge does.
bool only(const std::string& query, std::initializer_list<const char*> keys) {
    size_t i = 0;
    while (i < query.size()) {
        size_t z = query.find('&', i); if (z == std::string::npos) z = query.size();
        auto kv = std::string_view(query).substr(i, z - i);
        auto key = kv.substr(0, kv.find('='));
        if (!key.empty() && std::none_of(keys.begin(), keys.end(), [&](const char* k) { return key == k; })) return false;
        i = z + 1;
    }
    return true;
}

// sha1(json.dumps(x, sort_keys=True)).hexdigest()[:16] for invscreen's inputHash; the caller hands the already sorted text.
std::string sha1_16(const std::string& text) {
    uint8_t d[20];
    CryptoPP::SHA1().CalculateDigest(d, reinterpret_cast<const uint8_t*>(text.data()), text.size());
    return graphloop::hex(graphloop::View(d, 20)).substr(0, 16);
}

struct Label { std::string category, entity, cls; bool known = false; };
Label label_of(const ChainStore& s, NodeId n) {
    Label l;
    NodeAttr a;
    if (n != kNone && s.node_attr(n, a)) { l.category = a.category; l.entity = a.entity; l.cls = a.cls; l.known = true; }
    return l;
}
} // namespace

// invscreen kind_of(lab, cls)
std::string kind_of(const std::string& cat, const std::string& c) {
    if (cat == "dex_pool" || c == "dex") return "pool";
    if (kExchCats.count(cat) || c == "cex" || c == "exchange") return "exchange";
    if (cat == "bridge" || c == "bridge") return "bridge";
    if (cat == "dex_router") return "router";
    if (cat.find("mev") != std::string::npos || c == "mev") return "mev";
    if (cat.find("whale") != std::string::npos || c == "whale") return "whale";
    if (cat.rfind("contract", 0) == 0 || cat == "token_contract" || cat == "lending" || cat == "nft_contract" || cat == "multisig_or_safe")
        return cat != "multisig_or_safe" ? "contract" : "safe";
    return "wallet";
}

namespace {
// ------------------------------------------------------------------------------------------------ holders-at (invscreen holders_at)
struct Moment { std::string key; uint64_t block; uint64_t time; };

void who(Json& j, const ChainStore& s, NodeId n, const Addr& a) {
    Label l = label_of(s, n);
    j.ks("address", addr_hex(a)).ks("kind", kind_of(l.category, l.cls)).ksn("category", l.category).ksn("entity", l.entity).ksn("class", l.cls);
}

HttpResponse holders_at(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    if (!only(rq.query, {"market", "snaps", "limit", "t", "block", "to"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");
    std::string m = lower(qparam(rq.query, "market"));
    auto colon = m.find(':');
    if (colon == std::string::npos || !is_addr(m.substr(colon + 1))) return bad("market must be <chain>:<0x token address>.");
    std::string chain = m.substr(0, colon), token = m.substr(colon + 1);
    auto it = chains.find(chain);
    if (it == chains.end()) return unsupported("chain " + chain + " is not served by emv2 yet");
    const ChainStore& s = *it->second;
    const std::string build = chain + ":" + s.build_id();
    if (auto h = held_tokens().find(chain, token)) return held_reply("holders-at/1", m, *h, build);
    if (!(s.caps() & ChainStore::kHolders) || !(s.caps() & ChainStore::kBlocks)) {
        std::string why = s.missing_reason(ChainStore::kHolders);
        if (why.empty()) return unsupported("this build carries no holder balances", build);
        Json p; p.ob().ks("schema", "holders-at/1").kb("available", false).ks("reason", "The balances of this build are being prepared.")
                .k("status").ob().ks("state", "preparing").ks("detail", why).oe().oe();
        return reply(200, p.s, build);
    }
    Json j;
    TokenId tid = s.token_of(graphloop::addr_from(token));
    TokenAttr ta;
    if (tid == kNone || !s.token_attr(tid, ta)) {
        j.ob().ks("schema", "holders-at/1").kb("available", false).ks("reason", "This token is not in the map's token table yet.").oe();
        return reply(200, j.s, build);   // elapsed_ms is appended by the caller
    }
    if (spam_symbol(ta.symbol)) return held_reply("holders-at/1", m, spam_by_name(), build);
    std::string limit_s = qparam(rq.query, "limit");
    size_t lim = is_digits(limit_s) ? std::clamp<size_t>(size_t(std::min<uint64_t>(std::stoull(limit_s.substr(0, 9)), 100000)), 1, 100) : 30;
    std::optional<double> supply;
    if (ta.has_supply) supply = ta.supply.scaled(ta.decimals);
    std::optional<Addr> creator;
    if (ta.has_creator) creator = ta.creator;
    uint64_t t0 = ta.first_time, b0 = ta.first_block, head = s.head_block();
    if (std::string pin = qparam(rq.query, "to"); is_digits(pin)) head = std::min(head, s.block_at(uint32_t(std::min<uint64_t>(std::stoull(pin.substr(0, 12)), 0xffffffffu))));   // `to` pins "now" and caps every moment
    std::vector<Moment> moments;
    if (!qparam(rq.query, "snaps").empty()) {   // invscreen: `if qs.get("snaps")` is true for any non-empty value, "0" included
        for (auto& [k, off] : kSnapOffsets) moments.push_back({k, off == 0 ? b0 : s.block_at(uint32_t(t0 + off)), t0 + off});
        moments.push_back({"now", head, s.time_at(head)});
    } else if (is_digits(qparam(rq.query, "block"))) {
        uint64_t b = std::stoull(qparam(rq.query, "block"));
        moments.push_back({"block", b, s.time_at(b)});
    } else if (is_digits(qparam(rq.query, "t"))) {
        uint64_t t = std::stoull(qparam(rq.query, "t"));
        if (t > 100000000000ULL) t /= 1000;
        moments.push_back({"t", s.block_at(uint32_t(t)), t});
    } else {
        return bad("give t (unix seconds), block, or snaps=1.");
    }
    // the moment the answer stands at: the asked block / t (capped at the map end) or, for snaps, "now" (head, or `to`)
    uint64_t pin_block = moments.size() == 1 ? std::min(moments[0].block, head) : head;
    uint64_t pin_time = s.time_at(pin_block);
    bool any = false, all = true;
    // every moment is an independent exact read (checkpoint + replay + exact balances of the candidates): read them in parallel
    std::vector<HoldersAt> hs(moments.size());
    std::vector<Avail> avs(moments.size(), Avail::missing);
    {
        auto one = [&](size_t i) { if (moments[i].block >= b0) avs[i] = s.holders_at(tid, std::min(moments[i].block, head), lim, hs[i]); };
        if (moments.size() == 1) one(0);
        else {
            std::vector<std::thread> th;
            for (size_t i = 0; i < moments.size(); i++) th.emplace_back(one, i);
            for (auto& t : th) t.join();
        }
    }
    Json sn; sn.ab();
    for (size_t mi = 0; mi < moments.size(); mi++) {
        auto& mo = moments[mi];
        if (budget.expired()) return err(504, "budget", "the request ran out of its time budget", build);
        sn.ob();
        if (mo.block < b0) {
            sn.ks("key", mo.key).ku("block", mo.block).ku("time", mo.time).kb("available", true).ku("holders", 0).k("top").ab().ae()
              .ks("note", "before the token's first transfer").oe();
            any = true;
            continue;
        }
        uint64_t blk = std::min(mo.block, head);
        sn.ks("key", mo.key).ku("block", blk).ku("time", mo.time).ksn("timeUtc", utc_iso(mo.time));
        HoldersAt& h = hs[mi];
        Avail av = avs[mi];
        if (av != Avail::ok) {
            sn.kb("available", false).ks("reason", "The token's transfer history up to this moment is being prepared.")
              .k("status").ob().ks("state", av == Avail::preparing ? "preparing" : "missing").oe().oe();
            all = false;
            continue;
        }
        any = true;
        double held = h.held.scaled(ta.decimals);
        bool denom_is_int = false;
        double denom = supply ? *supply : held;
        if (!(denom != 0)) { denom = 1; denom_is_int = !supply && held == 0; }
        sn.kb("available", true).ks("source", "emv2 exact balances (token transfer history)").ku("holders", h.holders);
        if (denom_is_int) sn.ki("supply", 1); else sn.kn("supply", denom);
        sn.ks("supplyBasis", supply ? "totalSupply()" : "sum of balances");
        sn.k("top").ab();
        double top_pct = 0;
        for (auto& r : h.top) {
            NodeAttr na; Addr a{};
            if (s.node_attr(r.holder, na)) a = na.addr;
            double v = r.amount.scaled(ta.decimals);
            double pct = py_round(v / denom * 100, 4);
            top_pct += pct;
            sn.ob(); who(sn, s, r.holder, a);
            sn.kn("amount", v).kn("pct", pct).kb("isCreator", creator && *creator == a).oe();
        }
        sn.ae().kn("topPct", py_round(top_pct, 4));
        if (h.tier == Tier::cold) sn.ks("tier", "cold");
        sn.oe();
    }
    sn.ae();
    j.ob().ks("schema", "holders-at/1").ks("market", chain + ":" + token).ksn("symbol", ta.symbol).kb("available", any).kb("complete", all)
     .k("launch").ob().ku("block", b0).ku("time", t0).ksn("timeUtc", utc_iso(t0)).oe().ku("head", head).k("snaps").raw(sn.s)
     .k("notes").ab()
     .str("pct = share of totalSupply() at the head; holders = addresses with a positive balance (burn addresses excluded).")
     .str("Pools, routers and exchanges are holders too: kind tells them apart (pool, exchange, router, bridge, mev, whale, contract, safe, wallet).")
     .str("Moments before the analytics snapshot come from the token's own transfer history; while it is being prepared that snap is available:false with status.")
     .ae().oe();
    return reply_at(200, j.s, build, pin_block, pin_time);
}

// ------------------------------------------------------------------------------------------------ address-chains (invscreen address_chains)
HttpResponse address_chains(const ChainMap& chains, const HttpRequest& rq, Budget&) {
    if (!only(rq.query, {"address", "to"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");   // `to` accepted; counts are at the build head (as_of)
    std::string a = lower(qparam(rq.query, "address"));
    if (!is_addr(a)) return bad("address must be a 0x address.");
    Addr ad = graphloop::addr_from(a);
    struct Row { std::string chain; uint64_t chain_id; NodeAttr at; };
    std::vector<Row> rows;
    std::vector<std::string> used;
    for (auto& [name, st] : chains) {
        used.push_back(name);
        if (!(st->caps() & ChainStore::kDict)) continue;
        NodeId n = st->node_of(ad);
        NodeAttr at;
        if (n == kNone || !st->node_attr(n, at)) continue;
        rows.push_back({name, st->chain_id(), at});
    }
    std::set<std::string> kinds;
    auto kind = [](const NodeAttr& at) { return at.is_contract ? std::string("contract") : at.delegated ? std::string("eoa_delegated") : std::string("eoa"); };
    for (auto& r : rows) kinds.insert(kind(r.at));
    std::stable_sort(rows.begin(), rows.end(), [](const Row& x, const Row& y) { return x.at.tx_sent + x.at.tx_received > y.at.tx_sent + y.at.tx_received; });
    Json j;
    j.ob().ks("schema", "address-chains/1").kb("available", !rows.empty()).ks("address", a).knull("entity").k("chains").ab();
    for (auto& r : rows) {
        std::string k = kind(r.at);
        j.ob().ks("chain", r.chain).ku("chainId", r.chain_id).ku("txSent", r.at.tx_sent).ku("txReceived", r.at.tx_received)
         .ku("firstSeen", r.at.first_time).ku("lastSeen", r.at.last_time).ku("firstBlock", r.at.first_block).ku("lastBlock", r.at.last_block)
         .ks("kind", k).ku("contractsCreated", r.at.contracts_created).ku("dexTrades", r.at.dex_trades).ks("source", "address index (full map)")
         .ks("kindBasis", "code at the emv2 build head");
        std::string why;
        if (rows.size() < 2) j.knull("sameOwner").knull("grade").ks("why", "seen on one chain only");
        else if (k == "contract" || kinds.count("contract"))
            j.kb("sameOwner", false).knull("grade").ks("why", "a contract wallet (Safe etc.) can have different owners on each chain: the same address is not grouped");
        else {
            why = "same externally owned address = the same private key on every chain";
            if (r.at.tx_sent == 0) why += " (receive only here: no transaction signed on this chain yet)";
            j.kb("sameOwner", true).ks("grade", "proven").ks("why", why);
            if (r.at.tx_sent == 0) j.kb("receiveOnly", true);
        }
        j.ku("txs", r.at.tx_sent + r.at.tx_received).ksn("firstSeenUtc", utc_iso(r.at.first_time)).ksn("lastSeenUtc", utc_iso(r.at.last_time)).oe();
    }
    j.ae().k("notes").ab()
     .str("txSent / txReceived count transactions sent from / to the address (internal transfers excluded).")
     .str("Only EOAs are grouped across chains by address. A delegated EOA (EIP-7702) still has its key, so it stays grouped.")
     .ae().oe();
    auto r = reply(200, j.s, build_header(chains, used));
    r.ctype += "\r\nX-EMV2-Multi: 1";   // a cross-chain answer: as_of per chain even when one chain is loaded
    return r;
}

// ------------------------------------------------------------------------------------------------ trace-graph (invscreen trace)
struct TNode { std::string id, address, chain, kind, category, entity; int hop; std::string stop; };
struct TLink {
    std::string a, b, chain, symbol, asset; bool asset_null = true, sym_null = true, amount_null = true;
    uint64_t n = 0, first_block = 0, last_block = 0, first_time = 0, last_time = 0; double amount = 0; size_t assets = 0;
};

std::string hub_reason(const ChainStore& s, NodeId p) {
    NodeAttr a;
    if (p == kNone || !s.node_attr(p, a)) return {};
    if (kHubCats.count(a.category)) {
        std::string c = a.category; std::replace(c.begin(), c.end(), '_', ' ');
        return (c.empty() ? std::string("hub") : c) + (a.entity.empty() ? "" : " (" + a.entity + ")");
    }
    if (a.out_degree > kFunderMaxFanout * 20 || a.in_degree > 5000)
        return "high fan-out (" + std::to_string(a.out_degree) + " out / " + std::to_string(a.in_degree) + " in)";
    return {};
}

void py_json_str(std::string& o, std::string_view s) { graphloop::json_escape(o, s); }   // ASCII-only inputs here

HttpResponse trace_graph(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    if (!only(rq.query, {"chain", "address", "direction", "hops", "chains", "window", "from", "to"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");
    std::string chain = lower(qparam(rq.query, "chain")), a = lower(qparam(rq.query, "address"));
    std::string direction = lower(qparam(rq.query, "direction", "in")); if (direction.empty()) direction = "in";
    if (!is_addr(a) || (direction != "in" && direction != "out")) return bad("chain must be a served chain, address a 0x address, direction in|out.");
    auto it = chains.find(chain);
    if (it == chains.end()) return unsupported("chain " + chain + " is not served by emv2 yet");
    if (!(it->second->caps() & ChainStore::kGraph)) {
        std::string why = it->second->missing_reason(ChainStore::kGraph);
        if (why.empty()) return unsupported("this build carries no graph", chain + ":" + it->second->build_id());
        Json p; p.ob().ks("schema", "trace/1").kb("available", false).ks("reason", "The graph of this build is being prepared.")
                .k("status").ob().ks("state", "preparing").ks("detail", why).oe().oe();
        return reply(200, p.s, chain + ":" + it->second->build_id());
    }
    std::string hs = qparam(rq.query, "hops");
    int hops = is_digits(hs) ? int(std::clamp<uint64_t>(std::stoull(hs.substr(0, 6)), 1, 3)) : 2;
    std::vector<std::string> cl;
    {
        std::string cs = lower(qparam(rq.query, "chains", chain)); if (cs.empty()) cs = chain;
        size_t i = 0;
        while (i <= cs.size()) {
            size_t z = cs.find(',', i); if (z == std::string::npos) z = cs.size();
            std::string c = cs.substr(i, z - i);
            if (chains.count(c) && std::find(cl.begin(), cl.end(), c) == cl.end()) cl.push_back(c);
            i = z + 1;
        }
        if (cl.empty()) cl.push_back(chain);
    }
    std::string win = lower(qparam(rq.query, "window", "all")); if (win.empty()) win = "all";
    uint64_t ref = 0;
    for (auto& c : cl) { uint64_t t = chains.at(c)->head_time(); if (t && (!ref || t < ref)) ref = t; }
    if (!ref) ref = graphloop::now_ms() / 1000 / 3600 * 3600;
    uint64_t t0 = 0, t1 = ref;
    std::string fs = qparam(rq.query, "from"), ts = qparam(rq.query, "to");
    if (is_digits(fs) || is_digits(ts)) { t0 = is_digits(fs) ? std::stoull(fs) : 0; t1 = is_digits(ts) ? std::stoull(ts) : ref; }
    else if (kWindows.count(win)) { t0 = ref - kWindows.at(win); t1 = ref; }
    else { win = "all"; t0 = 0; t1 = ref; }
    t1 = std::min(t1, ref);   // never past the map end: the bridge re-asks the old service at as_of
    std::map<std::string, std::pair<uint64_t, uint64_t>> blocks;
    for (auto& c : cl) { auto& st = *chains.at(c); blocks[c] = {t0 ? st.block_at(uint32_t(t0)) : 0, st.block_at(uint32_t(std::min<uint64_t>(t1, 0xffffffffu)))}; }

    std::map<std::string, TNode> nodes;            // key chain:address
    std::map<std::tuple<std::string, std::string, std::string>, TLink> links;
    struct Stop { std::string address, chain, why; int hop; };
    std::vector<Stop> stops;
    Json frontier; frontier.ab();
    auto node = [&](const std::string& ch, const std::string& x, NodeId nid, int hop, const std::string& stop) -> TNode& {
        std::string k = ch + ":" + x;
        auto f = nodes.find(k);
        if (f != nodes.end()) return f->second;
        Label l = label_of(*chains.at(ch), nid);
        TNode n{k, x, ch, kind_of(l.category, ""), l.category, l.entity, hop, stop};
        return nodes.emplace(k, std::move(n)).first->second;
    };
    const ChainStore& s0 = *chains.at(chain);
    NodeId root = s0.node_of(graphloop::addr_from(a));
    node(chain, a, root, 0, "");
    std::deque<std::tuple<std::string, NodeId, std::string, int>> q;
    q.emplace_back(chain, root, a, 0);
    Dir d = direction == "in" ? Dir::in : Dir::out;
    bool cold = false;
    while (!q.empty()) {
        if (budget.expired()) return err(504, "budget", "the request ran out of its time budget", build_header(chains, cl));
        auto [ch, xid, x, hop] = q.front(); q.pop_front();
        if (hop >= hops || xid == kNone) continue;
        const ChainStore& s = *chains.at(ch);
        auto [bf, bt] = blocks.count(ch) ? blocks[ch] : std::pair<uint64_t, uint64_t>{0, ~0ULL};
        struct Agg { NodeId peer; std::string addr; uint64_t n = 0, first = ~0ULL, last = 0; std::vector<std::tuple<uint64_t, std::string, Raw256, uint8_t, TokenId>> assets; };
        std::unordered_map<NodeId, Agg> agg;
        bool truncated = false; Tier tier = Tier::hot;
        s.edges(xid, d, bf, bt, [&](const EdgeAgg& e) {
            if (e.last_block < bf || e.first_block > bt || e.peer == xid) return true;
            auto& g = agg[e.peer];
            if (g.addr.empty()) {
                NodeAttr pa; if (!s.node_attr(e.peer, pa)) return true;
                if (burn(pa.addr)) { agg.erase(e.peer); return true; }
                g.peer = e.peer; g.addr = addr_hex(pa.addr);
            }
            g.n += e.n; g.first = std::min(g.first, e.first_block); g.last = std::max(g.last, e.last_block);
            // "native", the token address, or "" when the build does not say which token (asset/symbol/amount null)
            std::string asset = e.asset == kNative ? "native" : [&] { TokenAttr t; return e.asset != kNone && s.token_attr(e.asset, t) ? addr_hex(t.addr) : std::string(); }();
            g.assets.emplace_back(e.n, asset, e.amount, e.has_amount ? e.kind : uint8_t(2), e.asset);   // no exact total read: amount null
            return agg.size() < 200000;
        }, truncated, tier);
        if (tier == Tier::cold) cold = true;
        std::vector<Agg*> ranked;
        for (auto& [k, g] : agg) if (!g.addr.empty()) ranked.push_back(&g);
        std::sort(ranked.begin(), ranked.end(), [](const Agg* p, const Agg* r) { return p->n != r->n ? p->n > r->n : p->addr < r->addr; });
        size_t k = kFanout.count(hop + 1) ? kFanout.at(hop + 1) : 3;
        if (ranked.size() > k)
            frontier.ob().ks("id", ch + ":" + x).ks("address", x).ks("chain", ch).ku("hidden", ranked.size() - k)
                    .ks("why", "showing the top " + std::to_string(k) + " peers by transfer count").oe();
        for (size_t i = 0; i < std::min(k, ranked.size()); i++) {
            Agg& g = *ranked[i];
            if (nodes.size() >= kMaxNodes) {
                frontier.ob().ks("id", ch + ":" + x).ks("address", x).ks("chain", ch).ks("why", "node cap " + std::to_string(kMaxNodes) + " reached").oe();
                break;
            }
            std::string hub = hub_reason(s, g.peer);
            node(ch, g.addr, g.peer, hop + 1, hub);
            std::string src = d == Dir::in ? g.addr : x, dst = d == Dir::in ? x : g.addr;
            auto lk = std::make_tuple(ch, src, dst);
            if (links.count(lk)) continue;
            std::sort(g.assets.begin(), g.assets.end(), [](auto& p, auto& r) { return std::get<0>(p) != std::get<0>(r) ? std::get<0>(p) > std::get<0>(r) : std::get<1>(p) < std::get<1>(r); });
            auto& [n0, asset, amt, kd, tid] = g.assets[0];
            (void)n0;
            std::string sym; int dec = 18;
            if (asset == "native") sym = native_symbol(ch);
            else if (!asset.empty()) { TokenAttr t; if (s.token_attr(tid, t)) { sym = t.symbol; dec = t.decimals; } }
            TLink L;
            L.a = src; L.b = dst; L.chain = ch; L.n = g.n;
            L.asset_null = asset == "native" || asset.empty(); L.asset = asset;
            L.sym_null = sym.empty(); L.symbol = sym;
            L.amount_null = !(kd == 0 || kd == 1) || asset.empty(); L.amount = L.amount_null ? 0 : amt.scaled(dec);
            L.assets = g.assets.size(); L.first_block = g.first; L.last_block = g.last;
            L.first_time = s.time_at(g.first); L.last_time = s.time_at(g.last);
            links.emplace(lk, std::move(L));
            if (!hub.empty()) stops.push_back({g.addr, ch, hub, hop + 1});
            else if (hop + 1 < hops) q.emplace_back(ch, g.peer, g.addr, hop + 1);
        }
    }
    frontier.ae();
    // params / dataVersion / inputHash exactly as invscreen builds them
    Json params; params.ob().ks("address", a).ks("chain", chain).ks("direction", direction).ki("hops", hops).k("chains").ab();
    for (auto& c : cl) params.str(c);
    params.ae().ks("window", win).ku("from", t0).ku("to", t1).oe();
    Json ver; ver.ob(); for (auto& c : cl) ver.ku(c, chains.at(c)->head_block()); ver.oe();
    // json.dumps([params, ver], sort_keys=True): keys sorted, ", " and ": " separators
    std::string hx = "[{";
    {
        std::vector<std::pair<std::string, std::string>> kv = {{"address", ""}, {"chain", ""}, {"chains", ""}, {"direction", ""}, {"from", ""}, {"hops", ""}, {"to", ""}, {"window", ""}};
        bool first = true;
        for (auto& [key, _] : kv) {
            if (!first) hx += ", ";
            first = false;
            py_json_str(hx, key); hx += ": ";
            if (key == "address") py_json_str(hx, a);
            else if (key == "chain") py_json_str(hx, chain);
            else if (key == "chains") { hx += '['; for (size_t i = 0; i < cl.size(); i++) { if (i) hx += ", "; py_json_str(hx, cl[i]); } hx += ']'; }
            else if (key == "direction") py_json_str(hx, direction);
            else if (key == "from") hx += std::to_string(t0);
            else if (key == "hops") hx += std::to_string(hops);
            else if (key == "to") hx += std::to_string(t1);
            else py_json_str(hx, win);
        }
        hx += "}, {";
        std::vector<std::string> sc(cl.begin(), cl.end()); std::sort(sc.begin(), sc.end());
        for (size_t i = 0; i < sc.size(); i++) { if (i) hx += ", "; py_json_str(hx, sc[i]); hx += ": " + std::to_string(chains.at(sc[i])->head_block()); }
        hx += "}]";
    }
    std::vector<const TNode*> nl; for (auto& [k, n] : nodes) nl.push_back(&n);
    std::sort(nl.begin(), nl.end(), [](const TNode* p, const TNode* r) { return std::tie(p->hop, p->chain, p->address) < std::tie(r->hop, r->chain, r->address); });
    std::sort(stops.begin(), stops.end(), [](const Stop& p, const Stop& r) { return std::tie(p.hop, p.address) < std::tie(r.hop, r.address); });
    Json j;
    j.ob().ks("schema", "trace/1").kb("available", true).k("params").raw(params.s).ks("inputHash", sha1_16(hx)).k("dataVersion").raw(ver.s).k("nodes").ab();
    for (auto* n : nl) {
        j.ob().ks("id", n->id).ks("address", n->address).ks("chain", n->chain).ks("kind", n->kind).ksn("label", n->entity).ksn("category", n->category)
         .ksn("entity", n->entity).ki("hop", n->hop).ksn("stop", n->stop).oe();
    }
    j.ae().k("links").ab();
    for (auto& [k, L] : links) {   // std::map order == sorted by (chain, a, b)
        j.ob().ks("a", L.a).ks("b", L.b).ks("chain", L.chain).ks("grade", "proven").ks("kind", "transfer").ku("txCount", L.n)
         .ks("why", std::to_string(L.n) + " direct transfer(s)" + (L.sym_null ? "" : ", mostly " + L.symbol));
        if (L.asset_null) j.knull("asset"); else j.ks("asset", L.asset);
        if (L.sym_null) j.knull("symbol"); else j.ks("symbol", L.symbol);
        if (L.amount_null) j.knull("amount"); else j.kn("amount", L.amount);
        j.ku("assets", L.assets).ku("firstBlock", L.first_block).ku("lastBlock", L.last_block).ku("firstTime", L.first_time).ku("lastTime", L.last_time)
         .ks("evidence", "/api/public/em/edge-evidence?chain=" + L.chain + "&a=" + L.a + "&b=" + L.b).oe();
    }
    j.ae().k("stops").ab();
    for (auto& st : stops) j.ob().ks("address", st.address).ks("chain", st.chain).ks("why", st.why).ki("hop", st.hop).oe();
    j.ae().k("frontier").raw(frontier.s).k("asOf").ob();
    for (auto& c : cl) {
        auto& st = *chains.at(c);
        j.k(c).ob().ku("block", st.head_block()).ku("time", st.head_time()).ksn("timeUtc", utc_iso(st.head_time())).ks("source", "emv2 build " + st.build_id()).oe();
    }
    j.oe();
    if (cold) j.ks("tier", "cold");
    j.k("notes").ab()
     .str("direction=in: where the money came from (links point toward the wallet); out: where it went.")
     .str("Links are direct transfers (proven) and bridge moves with both legs matched (matched; heuristic = inferred). A shared funder never becomes a link between the wallets it funded.")
     .str("Exchanges, routers, pools, bridges and high fan-out addresses are shown and then not expanded (stops).")
     .str("Deterministic: peers are ranked by transfer count, then address; the same input and data version give the same graph.")
     .ae().oe();
    if (cl.size() == 1) return reply_at(200, j.s, build_header(chains, cl), blocks[chain].second, t1);
    return reply(200, j.s, build_header(chains, cl));
}

// ------------------------------------------------------------------------------------------------ path (glserve /tools/path semantics)
// Bounded bidirectional BFS over transfer edges (design §8): hubs are not expanded, fan-out 128 (1,024 at the two ends), budget
// 4,096 visited nodes, at most 3 hops from each side. Answer in glserve's shape with addresses as refs.
HttpResponse path(const ChainMap& chains, const HttpRequest& rq, Budget& budget, const std::string& chain, const std::string& from, const std::string& to,
                  int max_depth, bool via_hub) {
    auto it = chains.find(chain);
    if (it == chains.end()) return unsupported("chain " + chain + " is not served by emv2 yet");
    const ChainStore& s = *it->second;
    const std::string build = chain + ":" + s.build_id();
    if (!(s.caps() & ChainStore::kGraph)) {
        std::string why = s.missing_reason(ChainStore::kGraph);
        if (why.empty()) return unsupported("this build carries no graph", build);
        Json p; p.ob().kb("found", false).kb("available", false).ks("reason", "The graph of this build is being prepared.").ks("detail", why).oe();
        return reply(200, p.s, build);
    }
    NodeId src = s.node_of(graphloop::addr_from(from)), dst = s.node_of(graphloop::addr_from(to));
    if (src == kNone || dst == kNone) return err(404, "REF_NOT_FOUND", "address not in this chain's dictionary", build);
    max_depth = std::clamp(max_depth, 1, 6);
    constexpr size_t fan = 128, fan_end = 1024, budget_nodes = 4096;
    constexpr uint32_t hub_limit = 20000;
    Json j;
    // flowmap/2 nodes and edges (the shape of the public /trace path answer), with V2's own fields beside them
    auto cid = [&](NodeId v) { NodeAttr a; s.node_attr(v, a); return chain + ":w:" + addr_hex(a.addr); };
    auto fnode = [&](Json& o, NodeId v) {
        NodeAttr a; s.node_attr(v, a);
        std::string cat = a.category.empty() ? "unknown" : a.category;
        auto cn = kCategoryName.find(cat);
        o.ob().ks("id", chain + ":w:" + addr_hex(a.addr)).ks("chain", chain).ks("entity_id", "addr:" + addr_hex(a.addr)).ks("kind", "wallet")
         .ks("address", addr_hex(a.addr)).ks("category", cat).ks("name", cn == kCategoryName.end() ? "Wallet" : cn->second)
         .kn("confidence", std::round(double(a.confidence) * 100) / 100).ks("type", a.is_contract ? "c" : "e");
        if (!a.entity.empty()) o.ks("entity", a.entity);
    };
    if (src == dst) {
        Json nd; nd.ab(); fnode(nd, src); nd.oe().ae();
        j.ob().ks("kind", "trace").ks("schema", "flowmap/2").ks("mode", "path").ks("chain", chain).ks("from", from).ks("to", to).kb("found", true).ki("hops", 0)
         .k("nodes").raw(nd.s).k("edges").ab().ae().ku("visited", 0).oe();
        return reply(200, j.s, build);
    }
    std::unordered_map<NodeId, std::pair<NodeId, uint8_t>> pf, pb;
    std::unordered_map<NodeId, int> hf, hb;   // hops from each end: the meeting node with the fewest total hops wins
    pf[src] = {kNone, 0}; pb[dst] = {kNone, 0}; hf[src] = 0; hb[dst] = 0;
    std::vector<NodeId> ff{src}, fb{dst};
    int df = 0, dbk = 0; NodeId meet = kNone; size_t visited = 0;
    std::vector<std::pair<uint64_t, NodeId>> blocked;   // hubs reached (not crossed unless viaHub=1)
    std::set<NodeId> hubs_crossed;
    auto expand = [&](std::vector<NodeId>& front, auto& mine, auto& other, auto& hmine, auto& hother, bool forward) {
        std::vector<NodeId> next;
        int best_total = 1 << 30; NodeId best_w = kNone, best_u = kNone; uint8_t best_kind = 0;
        for (NodeId u : front) {
            if (budget.expired()) break;
            if (u != src && u != dst) {   // hub test on the hot lists only: node_attr would read cold detail rows per expanded node
                uint64_t deg = s.hot_degree(u);
                if (deg != ~0ull && deg > hub_limit) {
                    blocked.push_back({deg, u});
                    if (!via_hub) continue;   // viaHub=1: cross it (hot top 256 neighbours) and mark the path
                    hubs_crossed.insert(u);
                }
            }
            size_t cap = (u == src || u == dst) ? fan_end : fan, taken = 0;
            bool trunc = false; Tier tier = Tier::hot;
            // strongest edges first: collect then keep the top `cap` by transfer count
            std::vector<std::pair<uint64_t, NodeId>> nb;
            std::unordered_map<NodeId, std::pair<uint64_t, uint8_t>> best;
            (void)trunc; (void)tier;
            // hot neighbour lists only (heaviest first): a path needs ids, not exact amounts or cold detail rows
            // endpoints read their full lists (cold tier for hubs); a middle node its hot top 256 (a full list costs ms per node)
            // undirected, like glserve's /tools/path?undirected=1 (the public path answer): money moving either way connects two wallets
            for (Dir d : {Dir::out, Dir::in}) {
                s.peers(u, d, (u == src || u == dst) ? cap * 4 : 256, [&](NodeId p, uint64_t n, uint8_t kind) {
                    auto& b = best[p]; if (n > b.first) b = {n, kind}; else if (!b.first) b.second = kind;
                    return best.size() < 100000;
                });
            }
            (void)forward;
            for (auto& [p, nk] : best) nb.push_back({nk.first, p});
            std::sort(nb.begin(), nb.end(), [](auto& x, auto& y) { return x.first != y.first ? x.first > y.first : x.second < y.second; });
            for (auto& [n, w] : nb) {
                if (taken++ >= cap) break;
                if (other.count(w)) {   // a meeting: keep scanning, a neighbour may meet with fewer hops (a direct edge beats a 2-hop meet)
                    int tot = hmine[u] + 1 + hother[w];
                    if (tot < best_total) { best_total = tot; best_w = w; best_u = u; best_kind = best[w].second; }
                    continue;
                }
                if (mine.count(w)) continue;
                mine[w] = {u, best[w].second}; hmine[w] = hmine[u] + 1;
                visited++;
                next.push_back(w);
                if (visited > budget_nodes) break;
            }
            if (visited > budget_nodes) break;
        }
        if (best_w != kNone) {   // the layer is done: join at the best meeting
            if (!mine.count(best_w)) { mine[best_w] = {best_u, best_kind}; hmine[best_w] = hmine[best_u] + 1; }
            meet = best_w;
        }
        return next;
    };
    while (meet == kNone && df + dbk < max_depth && visited <= budget_nodes && !ff.empty() && !fb.empty() && !budget.expired()) {
        // the smaller frontier first; at a tie (the first layer) the endpoint with fewer neighbours, so a hub endpoint is read last
        bool fwd = ff.size() != fb.size() ? ff.size() < fb.size() : s.hot_degree(ff[0]) <= s.hot_degree(fb[0]);
        if (fwd) { ff = expand(ff, pf, pb, hf, hb, true); df++; } else { fb = expand(fb, pb, pf, hb, hf, false); dbk++; }
    }
    if (meet == kNone) {
        // the search stops at hubs (degree > 20,000): a "path" through one says little (every wallet meets a router). Name the hubs
        // reached from both sides first, so "no path" is not read as "unconnected".
        std::sort(blocked.begin(), blocked.end(), [&](auto& x, auto& y) {
            bool bx = pf.count(x.second) && pb.count(x.second), by = pf.count(y.second) && pb.count(y.second);
            return bx != by ? bx : x.first > y.first; });
        blocked.erase(std::unique(blocked.begin(), blocked.end(), [](auto& x, auto& y) { return x.second == y.second; }), blocked.end());
        j.ob().ks("kind", "trace").ks("schema", "flowmap/2").ks("mode", "path").ks("chain", chain).ks("from", from).ks("to", to)
         .kb("found", false).knull("hops").k("nodes").ab().ae().k("edges").ab().ae().ku("visited", visited).ki("max_depth", max_depth)
         .kb("budget_exhausted", visited > budget_nodes);
        j.kb("onlyViaHubs", !blocked.empty()).k("blockedByHub").ab();
        for (size_t i = 0; i < blocked.size() && i < 8; i++) {
            NodeAttr ha; s.node_attr(blocked[i].second, ha);
            j.ob().ks("address", addr_hex(ha.addr)).ku("degree", blocked[i].first).ksn("entity", ha.entity).ksn("category", ha.category)
             .kb("reachedFromBoth", pf.count(blocked[i].second) && pb.count(blocked[i].second)).oe();
        }
        j.ae();
        if (!blocked.empty()) j.ks("note", "Only paths through hubs (routers, exchanges, degree > 20,000) were found; hubs are not crossed.");
        j.oe();
        return reply(200, j.s, build);
    }
    std::vector<NodeId> ns; std::vector<uint8_t> es;
    for (NodeId x = meet; x != kNone; x = pf[x].first) { ns.push_back(x); es.push_back(pf[x].second); }
    std::reverse(ns.begin(), ns.end()); std::reverse(es.begin(), es.end());
    for (NodeId x = meet; pb.count(x) && pb[x].first != kNone; x = pb[x].first) { ns.push_back(pb[x].first); es.push_back(pb[x].second); }
    bool through_hub = false;
    for (NodeId x : ns) if (hubs_crossed.count(x)) through_hub = true;
    Json nds, eds; nds.ab(); eds.ab();
    for (size_t i = 0; i < ns.size(); i++) {
        fnode(nds, ns[i]);
        if (hubs_crossed.count(ns[i])) nds.kb("hub", true);
        nds.oe();
        if (i) {
            std::string a = cid(ns[i - 1]), b2 = cid(ns[i]);
            const char* et = es[i] == 0 ? "native" : es[i] == 1 ? "erc20" : "other";
            eds.ob().ks("id", a + ">" + b2).ks("source", a).ks("target", b2).ks("src_chain", chain).ks("dst_chain", chain).ks("type", "transfer")
               .ks("edge", et).ku("hop", i).oe();
        }
    }
    nds.ae(); eds.ae();
    std::sort(blocked.begin(), blocked.end(), [](auto& x, auto& y) { return x.first > y.first; });
    blocked.erase(std::unique(blocked.begin(), blocked.end(), [](auto& x, auto& y) { return x.second == y.second; }), blocked.end());
    j.ob().ks("kind", "trace").ks("schema", "flowmap/2").ks("mode", "path").ks("chain", chain).ks("from", from).ks("to", to).kb("found", true)
     .ku("hops", ns.size() - 1).k("nodes").raw(nds.s).k("edges").raw(eds.s).ku("visited", visited).kb("viaHub", through_hub);
    if (!blocked.empty() && !through_hub) {   // hubs the search met and did not cross: a shorter path through one may exist
        j.k("blockedByHub").ab();
        for (size_t i = 0; i < blocked.size() && i < 8; i++) { NodeAttr ha; s.node_attr(blocked[i].second, ha); j.ob().ks("address", addr_hex(ha.addr)).ku("degree", blocked[i].first).oe(); }
        j.ae();
    }
    j.oe();
    return reply(200, j.s, build);
}

HttpResponse trace_public(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    // /api/public/em/trace?chain=&from=[&to=]: with `to` a path; without it the 3-hop outflow of `from` (trace-graph, direction out)
    if (!only(rq.query, {"chain", "from", "to", "max_depth", "viaHub"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");
    std::string chain = lower(qparam(rq.query, "chain")), from = lower(qparam(rq.query, "from")), to = lower(qparam(rq.query, "to"));
    bool via_hub = qparam(rq.query, "viaHub") == "1";
    bool to_time = is_digits(to);   // `to` as unix seconds pins the outflow's moment; as an address it asks for a path
    if (!is_addr(from) || (!to.empty() && !to_time && !is_addr(to))) return bad("from must be a 0x address; to a 0x address or unix seconds.");
    if (!to.empty() && !to_time) {
        std::string md = qparam(rq.query, "max_depth");
        return path(chains, rq, budget, chain, from, to, is_digits(md) ? int(std::min<uint64_t>(std::stoull(md.substr(0, 3)), 6)) : 6, via_hub);
    }
    HttpRequest r2 = rq;
    r2.query = "chain=" + chain + "&address=" + from + "&direction=out&hops=3" + (to_time ? "&to=" + to : "");
    return trace_graph(chains, r2, budget);
}

// ------------------------------------------------------------------------------------------------ emv2 debug: neighbors of one address
HttpResponse neighbors(const ChainMap& chains, const HttpRequest& rq, Budget&) {
    std::string chain = lower(qparam(rq.query, "chain")), a = lower(qparam(rq.query, "address")), dir = lower(qparam(rq.query, "dir", "out"));
    std::string ls = qparam(rq.query, "limit");
    size_t lim = is_digits(ls) ? std::clamp<size_t>(size_t(std::min<uint64_t>(std::stoull(ls.substr(0, 6)), 1000)), 1, 200) : 20;
    if (!is_addr(a) || (dir != "in" && dir != "out")) return bad("chain, address (0x) and dir in|out are required.");
    auto it = chains.find(chain);
    if (it == chains.end()) return unsupported("chain " + chain + " is not served by emv2 yet");
    const ChainStore& s = *it->second;
    const std::string build = chain + ":" + s.build_id();
    NodeId n = s.node_of(graphloop::addr_from(a));
    if (n == kNone) return err(404, "not_found", "address not in this chain's dictionary", build);
    std::vector<EdgeAgg> es;
    bool trunc = false; Tier tier = Tier::hot;
    s.edges(n, dir == "in" ? Dir::in : Dir::out, 0, ~0ULL, [&](const EdgeAgg& e) { es.push_back(e); return es.size() < 1000000; }, trunc, tier);
    std::sort(es.begin(), es.end(), [](auto& x, auto& y) { return x.n != y.n ? x.n > y.n : x.peer < y.peer; });
    Json j;
    j.ob().ks("schema", "emv2-neighbors/1").ks("chain", chain).ks("address", a).ks("dir", dir).ku("edges", es.size()).kb("truncated", trunc);
    if (tier == Tier::cold) j.ks("tier", "cold");
    j.k("rows").ab();
    for (size_t i = 0; i < std::min(lim, es.size()); i++) {
        auto& e = es[i];
        NodeAttr pa; s.node_attr(e.peer, pa);
        j.ob().ks("peer", addr_hex(pa.addr)).ksn("category", pa.category).ksn("entity", pa.entity);
        if (e.asset == kNative) j.ks("asset", "native"); else { TokenAttr t; if (s.token_attr(e.asset, t)) j.ks("asset", addr_hex(t.addr)).ksn("symbol", t.symbol); else j.knull("asset"); }
        j.ku("n", e.n).ks("amountRaw", e.amount.dec()).ku("firstBlock", e.first_block).ku("lastBlock", e.last_block).oe();
    }
    j.ae().oe();
    return reply(200, j.s, build);
}

// ------------------------------------------------------------------------------------------------ tools/holders (bridge card)
// The bridge's assistant card (em_bridge.py tool_holders) for chains the current card cannot cover: top 15 holders at the build
// head with their share of supply and labels. Movers / flows / venues come from Ticking summaries and stay empty here.

HttpResponse tools_holders(const ChainMap& chains, const HttpRequest& rq, Budget&) {
    if (!only(rq.query, {"market", "symbol", "chain"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");
    std::string m = lower(qparam(rq.query, "market"));
    auto colon = m.find(':');
    if (colon == std::string::npos || !is_addr(m.substr(colon + 1))) return err(404, "not_found", "Unknown market.");
    std::string chain = m.substr(0, colon), token = m.substr(colon + 1);
    auto it = chains.find(chain);
    if (it == chains.end()) return unsupported("chain " + chain + " is not served by emv2 yet");
    const ChainStore& s = *it->second;
    const std::string build = chain + ":" + s.build_id();
    TokenId tid = s.token_of(graphloop::addr_from(token));
    TokenAttr ta;
    if (tid == kNone || !s.token_attr(tid, ta)) return err(404, "not_found", "Unknown market.", build);
    if (auto hd = held_or_spam(chain, token, ta.symbol)) {
        Json j; j.ob().ks("kind", "holders").ks("market", chain + ":" + token).ks("chain", chain).ksn("symbol", ta.symbol).ks("token", token)
            .kb("available", false).kb("held", true).kb("spam", is_spam(*hd)).ksn("class", hd->cls).ks("balanceKind", balance_kind(*hd))
            .ks("heldReason", hd->reason).ks("heldReasonKo", hd->reason_ko).knull("holderCount")
            .k("holders").ab().ae().oe();
        return reply(200, j.s, build);
    }
    HoldersAt h;
    uint64_t head = s.head_block();
    bool ok = (s.caps() & ChainStore::kHolders) && s.holders_at(tid, head, 15, h) == Avail::ok;
    double supply = ta.has_supply ? ta.supply.scaled(ta.decimals) : 0;
    Json hs; hs.ab();
    if (ok)
        for (auto& r : h.top) {
            NodeAttr na; Addr a{};
            bool known = s.node_attr(r.holder, na);
            if (known) a = na.addr;
            double v = r.amount.scaled(ta.decimals);
            std::string cat = known && !na.category.empty() ? na.category : "unknown";
            auto cn = kCategoryName.find(cat);
            hs.ob().ks("address", addr_hex(a)).kn("share", py_round(supply > 0 ? v / supply * 100 : 0, 2)).ku("nodeId", r.holder)
              .ks("category", cat).ksn("entity", known ? na.entity : std::string()).ks("categoryName", cn == kCategoryName.end() ? cat : cn->second).oe();
        }
    hs.ae();
    Json j;
    j.ob().ks("kind", "holders").ks("market", chain + ":" + token).ks("chain", chain).ksn("symbol", ta.symbol).ks("token", token).knull("icon");
    if (ok) j.ku("holderCount", h.holders); else j.knull("holderCount");
    j.k("holders").raw(hs.s).k("movers").ab().ae().k("flows").ab().ae().k("venues").ab().ae().k("recent").ab().ae()
     .ku("asOf", s.time_at(head)).kb("ready", ok).ks("source", "emv2 exact balances at the build head").oe();
    return reply_at(200, j.s, build, head, s.time_at(head));
}

// ------------------------------------------------------------------------------------------------ graph (EMV2-only, home map)
// Who a wallet or entity is connected to, 1-2 hops, keyed by address (no galaxy ids). Hop 1 carries exact per-asset totals from the
// build's detail rows; hop 2 comes from the hot neighbour lists (counts, no amounts) so a 2-hop answer stays within tens of ms.
// Hubs (exchanges, routers, pools, bridges, high fan-out) are shown and not expanded (stop). USD is filled for stablecoins only
// in phase 1 (no price engine yet): everything else is usd null with usdReason.
const std::set<std::string> kStableSyms = {"USDC", "USDT", "DAI", "USDG", "USDT0", "USDC.E", "USDCE", "USDE", "PYUSD", "FRAX", "USDBC", "USD₮0",
                                           "AUSD", "USDS", "FDUSD", "TUSD", "USDP", "RLUSD", "USD1", "GHO", "LUSD", "CRVUSD", "USDB", "USDZ"};
const std::map<std::string, uint32_t> kGraphWindows = {{"24h", 86400}, {"7d", 7 * 86400}, {"30d", 30 * 86400}, {"all", 0}};

std::string grade_label(const NodeAttr& a) {
    if (!a.known || a.category.empty() || a.category == "unknown") return {};
    if (a.label_source == "registry" || a.label_source == "manual") return "proven";
    if (a.label_source == "agent") return "judged";
    if (a.label_source == "propagation") return "inferred";
    return a.confidence >= 0.8f ? "inferred" : "judged";
}
std::string mark_key(const std::string& entity) {
    // "binance:hot_wallet_14" / "Binance 14" / "uniswap_v4:pool_manager" -> "binance" / "uniswap"
    std::string k;
    for (char c : entity) {
        if (c == ':' || c == ' ' || c == '_' || c == '-' || c == '.') { if (!k.empty()) break; continue; }
        if (std::isalnum(uint8_t(c))) k += char(std::tolower(uint8_t(c))); else if (!k.empty()) break;
    }
    while (!k.empty() && std::isdigit(uint8_t(k.back()))) k.pop_back();
    return k.size() >= 2 ? k : std::string();
}

HttpResponse graph(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    if (!only(rq.query, {"address", "entity", "chain", "hops", "window", "min_usd", "limit"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");
    std::string addr_s = lower(qparam(rq.query, "address")), entity = qparam(rq.query, "entity"), chain_s = lower(qparam(rq.query, "chain"));
    if (addr_s.empty() == entity.empty()) return bad("give exactly one of address (0x...) or entity.");
    if (!addr_s.empty() && !is_addr(addr_s)) return bad("address must be a 0x address.");
    if (entity.size() > 120) return bad("entity is at most 120 characters.");
    std::string hops_s = qparam(rq.query, "hops"), win = qparam(rq.query, "window"), lim_s = qparam(rq.query, "limit"), mu_s = qparam(rq.query, "min_usd");
    int hops = hops_s.empty() ? 1 : (hops_s == "1" ? 1 : hops_s == "2" ? 2 : 0);
    if (!hops) return bad("hops must be 1 or 2.");
    auto wi = kGraphWindows.find(win.empty() ? "all" : win);
    if (wi == kGraphWindows.end()) return bad("window must be 24h, 7d, 30d or all.");
    size_t lim = is_digits(lim_s) ? std::clamp<size_t>(size_t(std::min<uint64_t>(std::stoull(lim_s.substr(0, 6)), 1000)), 1, 100) : 25;
    double min_usd = 0;
    if (!mu_s.empty()) { char* e = nullptr; min_usd = std::strtod(mu_s.c_str(), &e); if (!e || *e || !(min_usd >= 0) || min_usd > 1e15) return bad("min_usd must be a number >= 0."); }
    const size_t fan2 = hops == 2 ? std::min<size_t>(8, lim) : 0;

    // roots: (chain, node) for every served chain the address / entity appears on (or the asked chain)
    struct Root { std::string chain; const ChainStore* s; NodeId n; };
    std::vector<Root> roots;
    for (auto& [c, st] : chains) {
        if (!chain_s.empty() && c != chain_s) continue;
        if (!(st->caps() & ChainStore::kGraph)) continue;
        if (!addr_s.empty()) { NodeId n = st->node_of(graphloop::addr_from(addr_s)); if (n != kNone) roots.push_back({c, st.get(), n}); }
        else for (NodeId n : st->entity_nodes(entity, 8)) roots.push_back({c, st.get(), n});
    }
    if (!chain_s.empty() && !chains.count(chain_s)) return unsupported("chain " + chain_s + " is not served by emv2 yet");
    Json nodes, edges, stops; nodes.ab(); edges.ab(); stops.ab();
    size_t spam_hidden = 0;   // hop-1 records of spam / look-alike assets left out
    size_t records_hidden = 0;   // hop-1 (peer, asset) records past the limit: the answer shows the heaviest ones
    bool window_exact = false, window_missing = false;   // window answers: ledger chains only
    std::set<std::string> seen_nodes; std::vector<std::string> used; bool truncated = false, cold = false; size_t n_edges = 0, n_nodes = 0;
    Json asof; asof.ob();
    auto node_id = [](const std::string& c, const Addr& a) { return c + ":" + addr_hex(a); };
    auto emit_node = [&](const std::string& c, const ChainStore& s, NodeId n, int hop) -> std::string {
        NodeAttr a; bool known = s.node_attr(n, a);
        std::string id = node_id(c, a.addr);
        if (!seen_nodes.insert(id).second) return id;
        std::string cat = known && !a.category.empty() ? a.category : "unknown";
        bool hub = known && (a.hub || kHubCats.count(cat));
        auto cn = kCategoryName.find(cat);
        std::string key = known ? mark_key(a.entity) : std::string();
        nodes.ob().ks("id", id).ks("address", addr_hex(a.addr)).ks("chain", c).ksn("entity", a.entity).ksn("entityGrade", grade_label(a))
             .ks("category", cat).ks("categoryName", cn == kCategoryName.end() ? cat : cn->second).ks("kind", kind_of(cat, a.cls))
             .kb("isContract", known && a.is_contract).kb("isHub", hub).kb("stop", hub && hop > 0).ki("hop", hop).knull("held")
             .ksn("markKey", key);
        if (!key.empty()) nodes.ks("mark", "/assets/em/marks/brand-" + key + ".svg"); else nodes.knull("mark");
        nodes.ks("chainMark", "/assets/em/marks/chain-" + std::to_string(s.chain_id()) + ".svg");
        if (known && a.first_time) nodes.ku("firstSeen", a.first_time).ku("lastSeen", a.last_time); else nodes.knull("firstSeen").knull("lastSeen");
        nodes.oe(); n_nodes++;
        if (hub && hop > 0) stops.str(id);
        return id;
    };
    for (auto& r : roots) {
        if (budget.expired()) return err(504, "budget", "the request ran out of its time budget", "none");
        const ChainStore& s = *r.s;
        used.push_back(r.chain + ":" + s.build_id());
        uint64_t head = s.head_block(); uint32_t head_t = s.time_at(head);
        uint64_t b0 = wi->second ? s.block_at(head_t > wi->second ? head_t - wi->second : 0) : 0;
        asof.k(r.chain).ob().ku("block", head).ku("time", head_t).ks("build", s.build_id()).ku("fromBlock", b0).oe();
        std::string rid = emit_node(r.chain, s, r.n, 0);
        // hop 1: exact per (peer, asset) records both ways, heaviest first
        struct E1 { EdgeAgg e; Dir d; };
        std::vector<E1> e1;
        // a window: exact token flows inside it from the ledger's address events (native value is not in the ledger)
        if (wi->second) {
            std::vector<EdgeAgg> wo, wn; bool wtr = false;
            if (s.window_flows(r.n, b0, head, 200000, wo, wn, wtr)) {
                truncated |= wtr; window_exact = true;
                for (auto& e : wo) e1.push_back({e, Dir::out});
                for (auto& e : wn) e1.push_back({e, Dir::in});
            } else window_missing = true;
        }
        for (Dir d : {Dir::out, Dir::in}) {
            if (wi->second) break;
            bool tr = false; Tier tier = Tier::hot;
            // the heaviest 2*limit peers per side: exact totals are read for those only (graph cuts before reading detail rows)
            s.edges_top(r.n, d, b0, ~0ULL, lim * 2, [&](const EdgeAgg& e) { e1.push_back({e, d}); return e1.size() < 20000; }, tr, tier);
            truncated |= tr; cold |= tier == Tier::cold;
        }
        std::stable_sort(e1.begin(), e1.end(), [](const E1& x, const E1& y) { return x.e.n > y.e.n; });
        {   // the peers this answer will describe: one cold-tier batch for their attribute records
            std::vector<NodeId> pn; pn.reserve(std::min<size_t>(e1.size(), lim * 4) + 1); pn.push_back(r.n);
            for (size_t i = 0; i < e1.size() && i < lim * 4; i++) pn.push_back(e1[i].e.peer);
            s.prefetch_nodes(pn);
        }
        std::vector<NodeId> hop1;
        std::set<NodeId> hop1_set;
        for (size_t xi = 0; xi < e1.size(); xi++) {
            auto& x = e1[xi];
            if (n_edges >= lim * 4) { truncated = true; records_hidden += e1.size() - xi; break; }
            // asset and USD
            std::string sym, asset_addr; int dec = 18; std::optional<double> usd; const char* why = "price engine not in phase 1";
            bool dec_known = true;
            if (x.e.asset == kNative) { sym = native_symbol(r.chain); asset_addr.clear(); }
            else if (x.e.asset == kNone && x.e.asset_node != kNone) {   // graph-only build: the contract from the map, symbol from the overlay
                NodeAttr na;
                if (s.node_attr(x.e.asset_node, na)) {
                    asset_addr = addr_hex(na.addr); int d = -1;
                    if (!s.token_meta(na.addr, sym, d) || d < 0) dec_known = false; else dec = d;
                    if (auto h = held_or_spam(r.chain, asset_addr, sym); h && is_spam(*h)) { spam_hidden++; continue; }
                }
            }
            else if (x.e.asset != kNone) {
                TokenAttr ta; if (s.token_attr(x.e.asset, ta)) { sym = ta.symbol; dec = ta.decimals; asset_addr = addr_hex(ta.addr); }
                // spam / look-alike assets (held list or phishing name) are left out of the map and never priced as stablecoins
                if (auto h = held_or_spam(r.chain, asset_addr, sym); h && is_spam(*h)) { spam_hidden++; continue; }
            }
            std::optional<double> amount;
            if (x.e.has_amount && x.e.kind != 2 && dec_known) amount = x.e.amount.scaled(dec);
            std::string up = sym; for (auto& c : up) c = char(std::toupper(uint8_t(c)));
            int canon = asset_addr.empty() ? 0 : stables().canonical(r.chain, asset_addr);
            if (amount && (canon == 1 || (canon == -1 && kStableSyms.count(up)))) { usd = *amount; why = "stablecoin at 1 USD"; }
            else if (canon == 0 && kStableSyms.count(up)) why = "not this chain's canonical stablecoin contract";
            if (min_usd > 0 && usd && *usd < min_usd) continue;
            if (!hop1_set.count(x.e.peer) && hop1_set.size() >= lim) { records_hidden++; continue; }
            std::string pid = emit_node(r.chain, s, x.e.peer, 1);
            if (hop1_set.insert(x.e.peer).second) hop1.push_back(x.e.peer);
            const std::string& src = x.d == Dir::out ? rid : pid; const std::string& dst = x.d == Dir::out ? pid : rid;
            edges.ob().ks("source", src).ks("target", dst).ks("chain", r.chain).ks("kind", x.e.kind == 0 ? "native" : x.e.kind == 1 ? "erc20" : "nft")
                 .k("asset").ob().ksn("symbol", sym).ksn("address", asset_addr);
            if (dec_known) edges.ki("decimals", dec); else edges.knull("decimals");
            edges.oe();
            if (x.e.has_amount && x.e.kind != 2) edges.ks("amountRaw", x.e.amount.dec()); else edges.knull("amountRaw");
            if (amount) edges.kn("amount", *amount); else edges.knull("amount");
            if (usd) edges.kn("usd", *usd).knull("usdReason"); else edges.knull("usd").ks("usdReason", why);
            // a hub peer's record can be inexact (its detail rows exceed the read cap): counts from the hot list, blocks unknown
            bool exact = x.e.first_block || x.e.last_block;
            edges.ku("n", x.e.n).kb("exact", exact);
            if (exact) edges.ku("firstTime", s.time_at(x.e.first_block)).ku("lastTime", s.time_at(x.e.last_block)).ku("firstBlock", x.e.first_block).ku("lastBlock", x.e.last_block);
            else edges.knull("firstTime").knull("lastTime").knull("firstBlock").knull("lastBlock");
            edges.ks("grade", "proven").ki("hop", 1).oe();
            n_edges++;
        }
        // hop 2: hot neighbour lists of non-hub hop-1 nodes (ids and counts; amounts on request at hop 1 of that node)
        if (fan2)
            for (NodeId p : hop1) {
                if (budget.expired()) { truncated = true; break; }
                NodeAttr pa; if (!s.node_attr(p, pa) || pa.hub || kHubCats.count(pa.category)) continue;
                std::string pid = node_id(r.chain, pa.addr);
                for (Dir d : {Dir::out, Dir::in}) {
                    size_t k = 0;
                    s.peers(p, d, fan2 * 2, [&](NodeId q, uint64_t n, uint8_t kind) {
                        if (q == r.n || hop1_set.count(q)) return true;
                        if (k++ >= fan2) return false;
                        std::string qid = emit_node(r.chain, s, q, 2);
                        edges.ob().ks("source", d == Dir::out ? pid : qid).ks("target", d == Dir::out ? qid : pid).ks("chain", r.chain)
                             .ks("kind", kind == 0 ? "native" : kind == 1 ? "erc20" : "nft").knull("asset").knull("amountRaw").knull("amount")
                             .knull("usd").ks("usdReason", "hop 2: counts only").ku("n", n).kb("exact", false).knull("firstTime").knull("lastTime")
                             .knull("firstBlock").knull("lastBlock").ks("grade", "proven").ki("hop", 2).oe();
                        n_edges++;
                        return true;
                    });
                }
            }
    }
    nodes.ae(); edges.ae(); stops.ae(); asof.oe();
    Json j;
    j.ob().ks("schema", "graph/1").kb("available", !roots.empty());
    j.k("query").ob().ksn("address", addr_s).ksn("entity", entity).ksn("chain", chain_s).ki("hops", hops).ks("window", win.empty() ? "all" : win)
     .kn("minUsd", min_usd).ku("limit", lim).oe();
    if (window_missing && !window_exact) {   // no ledger on the asked chain(s): the bridge tells the assistant to ask window=all
        Json w; w.ob().ks("detail", "window totals need the chain's ledger; use window=all.").ks("reason", "window_totals_preparing").oe();
        return reply(400, w.s, "none");
    }
    if (roots.empty()) j.ks("reason", addr_s.empty() ? "No served chain has this entity name in its labels." : "This address is not in any served chain's map.");
    j.k("nodes").raw(nodes.s).k("edges").raw(edges.s).k("stops").raw(stops.s).kb("truncated", truncated).ks("tier", cold ? "cold" : "hot")
     .ku("nodeCount", n_nodes).ku("edgeCount", n_edges).ku("spamEdgesHidden", spam_hidden).ku("edgesHidden", records_hidden).ks("totalsScope", window_exact ? "window" : "all_time").k("asOf").raw(asof.s)
     .k("notes").ab()
     .str("Edges are on-chain transfers (grade proven). Hop 1 carries exact per-asset totals; hop 2 carries counts only.")
     .str("Hubs (exchanges, routers, pools, bridges, high fan-out) are shown and not expanded: stop.")
     .str("usd is filled for stablecoins only in phase 1; other assets are usd null (price engine not in phase 1).")
     .str(window_exact ? "window: hop-1 token flows inside the window are exact (ledger events); native value transfers are not in the ledger and are left out."
                       : "window=all: n, amount and first/last are all-time totals (totalsScope).")
     .ae().oe();
    std::string build = used.empty() ? "none" : used[0];
    return reply(200, j.s, build);
}

// ------------------------------------------------------------------------------------------------ accumulators (EMV2-only route)
// Who accumulated / distributed a token over a window: exact net balance change per address from the token's event stream.
// When the window holds more events than the scan budget, the answer falls back to the checkpoint holders at both ends (top 1000
// each) and says so (method "checkpoint_top", complete false). Burn sinks and self transfers are left out.
const std::map<std::string, uint32_t> kAccWindows = {{"1h", 3600}, {"24h", 86400}, {"7d", 7 * 86400}, {"30d", 30 * 86400}, {"90d", 90 * 86400}};
constexpr size_t kAccScan = 500000;

std::string grade_of(const NodeAttr& a) {
    if (!a.known || a.category.empty() || a.category == "unknown") return {};
    if (a.label_source == "registry" || a.label_source == "manual") return "official";
    return a.confidence >= 0.8f ? "strong" : "candidate";
}

HttpResponse accumulators(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    if (!only(rq.query, {"market", "from", "to", "limit", "window"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");
    std::string m = lower(qparam(rq.query, "market"));
    auto colon = m.find(':');
    if (colon == std::string::npos || !is_addr(m.substr(colon + 1))) return bad("market must be <chain>:<0x token address>.");
    std::string chain = m.substr(0, colon), token = m.substr(colon + 1);
    auto it = chains.find(chain);
    if (it == chains.end()) return unsupported("chain " + chain + " is not served by emv2 yet");
    const ChainStore& s = *it->second;
    const std::string build = chain + ":" + s.build_id();
    if (auto h = held_tokens().find(chain, token)) return held_reply("accumulators/1", m, *h, build);
    uint32_t need_caps = ChainStore::kHolders | ChainStore::kEvents | ChainStore::kBlocks;
    if ((s.caps() & need_caps) != need_caps) {
        std::string why = s.missing_reason(ChainStore::kHolders);
        Json p; p.ob().ks("schema", "accumulators/1").kb("available", false).ks("reason", "The balances of this build are being prepared.")
                .k("status").ob().ks("state", "preparing").ksn("detail", why).oe().oe();
        return reply(200, p.s, build);
    }
    TokenId tid = s.token_of(graphloop::addr_from(token));
    TokenAttr ta;
    if (tid == kNone || !s.token_attr(tid, ta)) {
        Json j; j.ob().ks("schema", "accumulators/1").kb("available", false).ks("reason", "This token is not in the map's token table yet.").oe();
        return reply(200, j.s, build);
    }
    if (spam_symbol(ta.symbol)) return held_reply("accumulators/1", m, spam_by_name(), build);
    uint64_t head = s.head_block(); uint32_t head_t = s.time_at(head);
    std::string to_s = qparam(rq.query, "to"), from_s = qparam(rq.query, "from"), win = qparam(rq.query, "window"), lim_s = qparam(rq.query, "limit");
    uint64_t t1 = is_digits(to_s) ? std::stoull(to_s.substr(0, 12)) : head_t;
    if (t1 > 100000000000ULL) t1 /= 1000;
    uint64_t t0;
    if (is_digits(from_s)) { t0 = std::stoull(from_s.substr(0, 12)); if (t0 > 100000000000ULL) t0 /= 1000; }
    else {
        auto w = kAccWindows.find(win.empty() ? "24h" : win);
        if (w == kAccWindows.end()) return bad("window must be 1h, 24h, 7d, 30d or 90d (or give from/to unix seconds).");
        t0 = t1 > w->second ? t1 - w->second : 0;
    }
    if (t0 >= t1) return bad("from must be before to.");
    size_t lim = is_digits(lim_s) ? std::clamp<size_t>(size_t(std::min<uint64_t>(std::stoull(lim_s.substr(0, 6)), 1000)), 1, 50) : 10;
    uint64_t b1 = std::min<uint64_t>(head, s.block_at(uint32_t(std::min<uint64_t>(t1, 0xffffffffu))));
    uint64_t b0 = t0 < ta.first_time ? (ta.first_block ? ta.first_block - 1 : 0) : s.block_at(uint32_t(std::min<uint64_t>(t0, 0xffffffffu)));
    if (b0 > b1) b0 = b1;

    struct Row { NodeId node; machine::engine::I256 delta; uint32_t n_in = 0, n_out = 0; bool has_end = false; machine::engine::I256 end; };
    std::vector<Row> rows; bool truncated = false; uint64_t scanned = 0; std::string method = "events";
    {
        std::vector<NetChange> ch;
        Avail av = s.net_changes(tid, b0, b1, kAccScan, ch, truncated, scanned);
        if (av != Avail::ok) {
            Json p; p.ob().ks("schema", "accumulators/1").kb("available", false).ks("reason", "The token's transfer history is being prepared.").oe();
            return reply(200, p.s, build);
        }
        if (!truncated) for (auto& c : ch) if (!c.delta.is_zero()) rows.push_back({c.node, c.delta, c.n_in, c.n_out, false, {}});
    }
    if (truncated) {   // checkpoint fallback: holders at both ends, exact balance difference for their union
        method = "checkpoint_top"; rows.clear();
        HoldersAt h0, h1;
        if (s.holders_at(tid, b0, 1000, h0) != Avail::ok || s.holders_at(tid, b1, 1000, h1) != Avail::ok)
            return err(503, "preparing", "checkpoint holders are being prepared", build);
        std::set<NodeId> ids;
        for (auto& h : h0.top) ids.insert(h.holder);
        for (auto& h : h1.top) ids.insert(h.holder);
        for (NodeId n : ids) {
            if (budget.expired()) return err(504, "budget", "the request ran out of its time budget", build);
            Raw256 a{}, b{};
            if (s.balance_at(n, tid, b0, a) != Avail::ok || s.balance_at(n, tid, b1, b) != Avail::ok) continue;
            machine::engine::U256 ua, ub; std::memcpy(ua.w, a.w, 32); std::memcpy(ub.w, b.w, 32);
            machine::engine::I256 ia, ib, d;
            auto pa = machine::engine::I256::from_u256(ua), pb = machine::engine::I256::from_u256(ub);
            if (!pa || !pb) continue;
            ia = *pa; ib = *pb;
            if (!machine::engine::sub(ib, ia, d) || d.is_zero()) continue;
            rows.push_back({n, d, 0, 0, true, ib});
        }
    }
    auto by_delta = [](const Row& x, const Row& y) { return x.delta == y.delta ? x.node < y.node : y.delta < x.delta; };
    std::sort(rows.begin(), rows.end(), by_delta);
    double scale = std::pow(10.0, double(ta.decimals));
    std::optional<double> supply; if (ta.has_supply) supply = ta.supply.scaled(ta.decimals);
    auto emit = [&](Json& j, Row& r, const char* side) {
        NodeAttr na; Addr a{};
        bool known = s.node_attr(r.node, na);
        if (known) a = na.addr;
        if (!r.has_end) {
            Raw256 e{};
            if (s.balance_at(r.node, tid, b1, e) == Avail::ok) {
                machine::engine::U256 ue; std::memcpy(ue.w, e.w, 32);
                if (auto pe = machine::engine::I256::from_u256(ue)) { r.end = *pe; r.has_end = true; }
            }
        }
        double delta = machine::engine::to_double(r.delta) / scale;
        j.ob(); who(j, s, r.node, a);
        j.ksn("grade", known ? grade_of(na) : std::string()).ks("side", side).kn("delta", delta).ks("deltaRaw", machine::engine::to_dec(r.delta));
        if (r.has_end) j.kn("balanceEnd", machine::engine::to_double(r.end) / scale); else j.knull("balanceEnd");
        if (supply && *supply != 0) j.kn("pctOfSupply", py_round(delta / *supply * 100, 4)); else j.knull("pctOfSupply");
        if (method == "events") j.ku("nIn", r.n_in).ku("nOut", r.n_out);
        j.kb("isContract", known && na.is_contract).oe();
    };
    Json acc; acc.ab();
    size_t na = 0;
    for (auto& r : rows) { if (na >= lim || r.delta.sign() <= 0) break; emit(acc, r, "accumulate"); na++; }
    acc.ae();
    Json dist; dist.ab();
    size_t nd = 0;
    for (auto r = rows.rbegin(); r != rows.rend(); ++r) { if (nd >= lim || r->delta.sign() >= 0) break; emit(dist, *r, "distribute"); nd++; }
    dist.ae();
    Json j;
    j.ob().ks("schema", "accumulators/1").ks("market", chain + ":" + token).ksn("symbol", ta.symbol).ki("decimals", ta.decimals).kb("available", true)
     .kb("complete", method == "events").ks("method", method).ku("scannedEvents", scanned).ku("wallets", rows.size())
     .k("window").ob().ku("from", t0).ku("to", t1).ksn("fromUtc", utc_iso(t0)).ksn("toUtc", utc_iso(t1)).ku("fromBlock", b0).ku("toBlock", b1).oe()
     .k("accumulators").raw(acc.s).k("distributors").raw(dist.s)
     .k("notes").ab()
     .str("delta = exact net token balance change over (fromBlock, toBlock]; pools, routers and exchanges are included and labelled by kind.")
     .str("method events = every transfer in the window; checkpoint_top = the window had more events than the scan budget, so only the top 1000 holders at both ends are compared.")
     .str("Burn addresses (0x0, 0x...dEaD) and self transfers are left out.")
     .ae().oe();
    return reply_at(200, j.s, build, b1, s.time_at(b1));
}

// /tokens?symbol=EURC[&chain=arc][&limit=10]: symbol -> token contracts, most holders first (an assistant resolves "EURC on Arc").
HttpResponse tokens(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    if (!only(rq.query, {"chain", "symbol", "limit"})) return err(400, "Unexpected parameter.", "Unexpected parameter.");
    std::string chain = lower(qparam(rq.query, "chain")), sym = qparam(rq.query, "symbol"), ls = qparam(rq.query, "limit");
    if (sym.empty() || sym.size() > 40) return bad("give symbol (1-40 characters).");
    size_t lim = is_digits(ls) && !ls.empty() ? std::clamp<size_t>(std::stoul(ls.substr(0, 4)), 1, 50) : 10;
    if (!chain.empty() && !chains.count(chain)) return unsupported("chain " + chain + " is not served by emv2 yet");
    struct Row { std::string chain; TokenAttr a; std::optional<Held> hold; bool spam = false; };
    std::vector<Row> rows; std::vector<std::string> used;
    for (auto& [c, sp] : chains) {
        if (!chain.empty() && c != chain) continue;
        if (budget.expired()) return err(504, "budget", "the request ran out of its time budget");
        std::vector<TokenId> found; sp->find_tokens(sym, 2000, found);
        if (!found.empty()) used.push_back(c);
        for (TokenId t : found) { TokenAttr a; if (sp->token_attr(t, a)) rows.push_back({c, std::move(a), std::nullopt, false}); }
    }
    for (auto& r : rows) { r.hold = held_or_spam(r.chain, addr_hex(r.a.addr), r.a.symbol); r.spam = r.hold && is_spam(*r.hold); }
    std::sort(rows.begin(), rows.end(), [](const Row& x, const Row& y) {   // real tokens first, then by holders
        if (x.spam != y.spam) return !x.spam;
        return x.a.holders_approx != y.a.holders_approx ? x.a.holders_approx > y.a.holders_approx : x.a.transfers > y.a.transfers; });
    Json j; j.ob().ks("schema", "tokens/1").ks("symbol", sym).ku("matches", rows.size()).k("tokens").ab();
    for (size_t i = 0; i < rows.size() && i < lim; i++) {
        auto& r = rows[i]; std::string addr = addr_hex(r.a.addr);
        const auto& hd = r.hold;
        j.ob().ks("chain", r.chain).ks("address", addr).ks("market", r.chain + ":" + addr).ksn("symbol", r.a.symbol).ki("decimals", r.a.decimals)
         .ku("holders", r.a.holders_approx).ku("transfers", r.a.transfers).ku("firstBlock", r.a.first_block).ku("lastBlock", r.a.last_block)
         .kb("held", bool(hd)).kb("spam", r.spam).ksn("class", hd ? hd->cls : std::string())
         .ks("balanceKind", hd ? balance_kind(*hd) : "exact").oe();
    }
    j.ae().k("notes").ab().str("holders = addresses with a positive balance at the build head (burn addresses excluded); 0 when the chain has no balances yet.")
     .str("held = balances failed the independent RPC check: holder and flow routes answer available:false for it.")
     .str("spam = airdrop / phishing name, fake balances or a look-alike contract: listed last, left out of graph assets.").ae().oe();
    if (used.empty() && !chain.empty()) used.push_back(chain);
    return reply(200, j.s, used.empty() ? "none" : build_header(chains, used));
}

// /capabilities: per chain, which assistant tools answer, the data end (as_of) and coverage. Cheap; the bridge may cache it 60 s.
HttpResponse capabilities(const ChainMap& chains) {
    Json j; j.ob().ks("schema", "capabilities/1").k("chains").ob();
    std::vector<std::string> names;
    auto held = held_tokens().all();
    for (auto& [c, s] : chains) {
        names.push_back(c);
        uint32_t k = s->caps();
        bool graph = k & ChainStore::kGraph, blocks = k & ChainStore::kBlocks, hold = (k & ChainStore::kHolders) && blocks;
        bool flows = hold && (k & ChainStore::kEvents);
        j.k(c).ob().ks("build", s->build_id()).ku("caps", k)
         .k("tools").ob().kb("graph", graph).kb("featured", graph).kb("holders", hold).kb("accumulators", flows).kb("tokens", bool(k & ChainStore::kDict))
         .kb("wallet", false).kb("path", false).oe()
         .k("asOf").ob().ku("block", s->head_block()).ku("time", s->head_time()).ksn("timeUtc", utc_iso(s->head_time())).oe()
         .k("coverage").ob().ku("fromBlock", 0).ku("toBlock", s->head_block()).kb("partial", false).oe()
         .k("heldTokens").ab();
        for (auto& [key, h] : held) if (key.rfind(c + ":", 0) == 0) j.ob().ks("address", key.substr(c.size() + 1)).ks("reason", h.reason).oe();
        j.ae().oe();
    }
    j.oe().k("notes").ab().str("tools.graph covers window=all; holders/accumulators need the chain's ledger; wallet and path are not switched to emv2 yet.")
     .str("asOf is the build's data end: every answer of that chain stands at or before it.").ae().oe();
    return reply(200, j.s, names.empty() ? "none" : build_header(chains, names));
}

HttpResponse health(const ChainMap& chains) {
    Json j; j.ob().kb("ok", true).k("chains").ob();
    for (auto& [c, s] : chains)
        j.k(c).ob().ks("build", s->build_id()).ku("head", s->head_block()).ku("headTime", s->head_time()).ku("caps", s->caps()).oe();
    j.oe().oe();
    return reply(200, j.s, build_header(chains, [&] { std::vector<std::string> v; for (auto& [c, s] : chains) v.push_back(c); return v; }()));
}
} // namespace

namespace {
// Every object answer carries as_of: each used chain's data end (the build head), so a comparison can tell a time skew from a
// difference. The used chains are the ones named in X-EMV2-Build.
HttpResponse with_as_of(HttpResponse r, const ChainMap& chains) {
    if (r.body.size() < 2 || r.body.front() != '{' || r.body.back() != '}') return r;
    std::vector<StoreRef> used;
    auto h = r.ctype.find("X-EMV2-Build: ");
    if (h != std::string::npos) {
        size_t e = r.ctype.find("\r\n", h); std::string list = r.ctype.substr(h + 14, e == std::string::npos ? std::string::npos : e - h - 14);
        size_t i = 0;
        while (i < list.size()) {
            size_t z = list.find(',', i); if (z == std::string::npos) z = list.size();
            std::string item = list.substr(i, z - i);
            auto it = chains.find(item.substr(0, item.find(':')));
            if (it != chains.end()) used.push_back(it->second);
            i = z + 1;
        }
    }
    Json a;
    if (used.size() == 1 && r.ctype.find("X-EMV2-Multi: 1") == std::string::npos) {
        auto& st = *used[0];
        uint64_t b = st.head_block(), t = st.head_time();
        auto p = r.ctype.find("X-EMV2-As-Of: ");
        if (p != std::string::npos) { std::sscanf(r.ctype.c_str() + p + 14, "%lu:%lu", &b, &t); }
        else r.ctype += "\r\nX-EMV2-As-Of: " + std::to_string(b) + ":" + std::to_string(t);
        a.ob().ks("chain", st.chain()).ku("block", b).ku("time", t).ksn("timeUtc", utc_iso(t)).ks("build", st.build_id())
         .ku("mapEndBlock", st.head_block()).oe();
    } else {
        a.ob();
        for (auto& sp : used)
            a.k(sp->chain()).ob().ku("block", sp->head_block()).ku("time", sp->head_time()).ksn("timeUtc", utc_iso(sp->head_time())).ks("build", sp->build_id()).oe();
        a.oe();
    }
    r.body.pop_back();
    if (r.body.size() > 1) r.body += ',';
    r.body += "\"as_of\":" + a.s + "}";
    return r;
}
HttpResponse dispatch(const ChainMap& chains, const HttpRequest& rq, Budget& budget);

// Answers of the read routes are pure functions of (path, query, builds, overlays): a repeated question is served from memory.
// Keyed with every chain's build id and overlay stamp, so a reload never serves an old answer. 200s only; bounded by bytes (LRU).
class AnswerCache {
    std::mutex mu_;
    std::list<std::pair<std::string, HttpResponse>> lru_;
    std::unordered_map<std::string, std::list<std::pair<std::string, HttpResponse>>::iterator> at_;
    size_t bytes_ = 0;
    static constexpr size_t cap_bytes = size_t(96) << 20, max_body = size_t(2) << 20;
    static size_t cost(const std::string& k, const HttpResponse& r) { return k.size() * 2 + r.body.size() + r.ctype.size() + 96; }
public:
    bool get(const std::string& k, HttpResponse& out) {
        std::lock_guard g(mu_);
        auto it = at_.find(k);
        if (it == at_.end()) return false;
        lru_.splice(lru_.begin(), lru_, it->second);
        out = it->second->second;
        return true;
    }
    void put(const std::string& k, const HttpResponse& r) {
        if (r.status != 200 || r.body.size() > max_body) return;
        std::lock_guard g(mu_);
        if (at_.count(k)) return;
        lru_.emplace_front(k, r); at_[k] = lru_.begin(); bytes_ += cost(k, r);
        while (bytes_ > cap_bytes && !lru_.empty()) {
            auto& back = lru_.back(); bytes_ -= cost(back.first, back.second); at_.erase(back.first); lru_.pop_back();
        }
    }
};
AnswerCache& answer_cache() { static AnswerCache c; return c; }
bool cacheable(std::string_view p) {
    if (p.rfind(kPrefix, 0) == 0) p.remove_prefix(std::string_view(kPrefix).size());
    return p == "/graph" || p == "/holders-at" || p == "/tools/holders" || p == "/accumulators" || p == "/address-chains" || p == "/tokens";
}
} // namespace

void set_data_root(const std::string& root) { held_tokens().set_path(root + "/held.tsv"); stables().set_path(root + "/stables.tsv"); }

HttpResponse handle(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    if (rq.method != "GET" || !cacheable(rq.path)) return with_as_of(dispatch(chains, rq, budget), chains);
    std::string key = rq.path + "?" + rq.query + "|" + std::to_string(held_tokens().stamp()) + ":" + std::to_string(stables().stamp()) + "|";
    for (auto& [c, sp] : chains) key += c + ":" + sp->build_id() + ":" + std::to_string(sp->overlay_stamp()) + ",";
    HttpResponse r;
    if (answer_cache().get(key, r)) return r;
    r = with_as_of(dispatch(chains, rq, budget), chains);
    answer_cache().put(key, r);
    return r;
}

namespace {
HttpResponse dispatch(const ChainMap& chains, const HttpRequest& rq, Budget& budget) {
    if (rq.method != "GET") return err(405, "method", "GET only");
    std::string p = rq.path;
    if (p == "/health") return health(chains);
    if (p.rfind(kPrefix, 0) == 0) p = p.substr(std::string_view(kPrefix).size());
    if (p == "/holders-at") return holders_at(chains, rq, budget);
    if (p == "/address-chains") return address_chains(chains, rq, budget);
    if (p == "/trace-graph") return trace_graph(chains, rq, budget);
    if (p == "/trace") return trace_public(chains, rq, budget);
    if (p == "/emv2/neighbors") return neighbors(chains, rq, budget);
    if (p == "/accumulators") return accumulators(chains, rq, budget);
    if (p == "/tools/holders") return tools_holders(chains, rq, budget);
    if (p == "/graph") return graph(chains, rq, budget);
    if (p == "/tokens") return tokens(chains, rq, budget);
    if (p == "/capabilities") return capabilities(chains);
    // bridge paths this phase does not answer (wallet-trades / acquisition need DEX fills, tools/* and g/* need the ledger and prices)
    static const char* later[] = {"/wallet-trades", "/acquisition", "/wallet-links", "/token-launch", "/launch-candles", "/edge-evidence",
                                  "/bundle-summary", "/freshness", "/tools/", "/g/", "/wallet-xc", "/search"};
    for (auto* l : later) if (p.rfind(l, 0) == 0) return unsupported(std::string("emv2 phase 1 does not answer ") + l);
    return err(404, "not_found", "unknown route");
}
} // namespace
} // namespace machine::engine::serve
