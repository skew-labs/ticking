#pragma once
// Deterministic validators, ported from skew:/srv/skew/em-labeling/validate.py and protocol.py.
// They run on the server-side pack (the exact facts the model saw, already de-aliased) and on
// graph facts.  Hard reasons reject; soft reasons are recorded as warnings.  Reason codes keep the
// Python names so dashboards and the dispatcher read the same vocabulary.
#include "graph.hpp"
#include "classify.hpp"
#include <functional>
#include <regex>

namespace machine::graphloop {

struct Verdict { std::vector<std::string> hard, soft; bool ok() const { return hard.empty(); } };

// Facts the validator needs about the subject (filled from the graph by the pack builder).
struct SubjectFacts {
    uint32_t node = none; char type = 'R'; uint64_t topics = 0; uint32_t sent = 0, recv = 0, cp = 0, outdeg = 0;
    double top2_out = 0, eth_in = 0, eth_out = 0, usd = 0; bool registry = false; uint8_t base_cat = unknown; double base_conf = 0;
    std::vector<std::string> keys;    // evidence vocabulary of this subject (fact keys + aliases it mentions)
    std::vector<std::string> names;   // entity/symbol strings present in the pack
};

struct ValidateParams { double deposit_share = 0.80; uint32_t hot_min_sent = 50, hot_min_cp = 20, mev_min = 100; double whale_eth_soft = 100; };

inline Verdict validate_address(uint8_t cat, double conf, const std::vector<std::string>& evidence, const std::string& entity,
                                const SubjectFacts& f, const ValidateParams& p) {
    Verdict v;
    if (cat >= category_count) { v.hard.push_back("CATEGORY_NOT_IN_ENUM"); return v; }
    if (!(conf >= 0 && conf <= 1)) v.hard.push_back("CONFIDENCE_RANGE");
    for (auto& k : evidence)
        if (std::find(f.keys.begin(), f.keys.end(), k) == f.keys.end()) v.hard.push_back("EVIDENCE_KEY_NOT_IN_PACK:" + k.substr(0, 40));
    if (evidence.empty() && cat != unknown) v.hard.push_back("EVIDENCE_EMPTY");
    if (!entity.empty()) {
        std::string e = entity; for (auto& ch : e) ch = char(std::tolower(uint8_t(ch)));
        bool okn = e.size() >= 2 && std::any_of(f.names.begin(), f.names.end(), [&](const std::string& s) {
            std::string t = s; for (auto& ch : t) ch = char(std::tolower(uint8_t(ch))); return t == e || t.find(e) != std::string::npos; });
        if (!okn) v.hard.push_back("ENTITY_NAME_NOT_IN_PACK");
    }
    const bool is_c = f.type == 'C';
    const uint64_t K = f.topics;
    if (contract_only(cat) && !is_c) v.hard.push_back("CONTRACT_CATEGORY_BUT_NOT_CONTRACT");
    if (eoa_only(cat) && is_c) v.hard.push_back("EOA_CATEGORY_BUT_IS_CONTRACT");
    if ((cat == cex_hot_wallet || cat == cex_deposit) && is_c) v.soft.push_back(cat == cex_deposit ? "DEPOSIT_IS_CONTRACT_FORWARDER" : "HOT_WALLET_IS_CONTRACT");
    switch (cat) {
    case token_contract:
        if (!(K & tf_transfer)) v.hard.push_back("TOKEN_WITHOUT_EMITTED_TRANSFER");
        if (K & (tf_sync_v2 | tf_swap_v2 | tf_swap_v3)) v.soft.push_back("TOKEN_ALSO_EMITS_POOL_EVENTS");
        break;
    case nft_contract:
        if (!(K & (tf_transfer | tf_erc1155))) v.hard.push_back("NFT_WITHOUT_TRANSFER_EVENTS");
        if (K && !(K & (tf_approval_all | tf_erc1155))) v.soft.push_back("NFT_NO_APPROVAL_FOR_ALL");
        break;
    case dex_pool:
        if (!(K & tf_swap_any)) v.hard.push_back("POOL_WITHOUT_SWAP_EVENT");
        if ((K & tf_swap_v2) && !(K & tf_sync_v2) && !(K & (tf_swap_v3 | tf_swap_v4 | tf_swap_curve | tf_swap_balancer))) v.hard.push_back("V2_POOL_WITHOUT_SYNC");
        break;
    case dex_router:
        if (K & (tf_sync_v2 | tf_swap_any)) v.hard.push_back("ROUTER_EMITS_POOL_EVENTS");
        if (f.cp < 10) v.soft.push_back("ROUTER_FEW_COUNTERPARTIES");
        break;
    case lending: if (!(K & (tf_lending | tf_lending_token))) v.soft.push_back("NO_KNOWN_LENDING_EVENT"); break;
    case bridge: if (!(K & tf_bridge)) v.soft.push_back("NO_KNOWN_BRIDGE_EVENT"); break;
    case multisig_or_safe: if (!(K & tf_safe)) v.soft.push_back("NO_SAFE_EVENT"); break;
    case cex_deposit:
        if (f.sent < 1) v.hard.push_back("DEPOSIT_NEVER_SENDS");
        else if (f.top2_out < p.deposit_share) { char b[64]; std::snprintf(b, sizeof b, "DEPOSIT_OUTFLOW_NOT_CONCENTRATED:top2_out_share=%.2f", f.top2_out); v.hard.push_back(b); }
        if (f.sent && double(f.sent) > std::max(500.0, 2.0 * f.recv)) v.hard.push_back("DEPOSIT_SENDS_MORE_THAN_RECEIVES");
        break;
    case cex_hot_wallet:
        if (f.sent < p.hot_min_sent || f.cp < p.hot_min_cp) v.hard.push_back("HOT_WALLET_LOW_FANOUT");
        break;
    case mev_bot: {
        uint32_t act = f.type == 'E' ? f.sent : f.type == 'C' ? f.recv : std::max(f.sent, f.recv);
        if (act < p.mev_min) v.hard.push_back("MEV_LOW_ACTIVITY");
        break;
    }
    case eoa_whale: if (f.eth_in + f.eth_out < p.whale_eth_soft) v.soft.push_back("WHALE_NATIVE_VOLUME_BELOW_100_ETH"); break;
    case unknown: v.soft.push_back("UNKNOWN_LABEL"); break;
    default: break;
    }
    // Graph-level consistency: registry-pinned contracts cannot be relabelled by an agent.
    if (f.registry && cat != f.base_cat) v.hard.push_back("REGISTRY_CONFLICT:" + std::string(category_names[f.base_cat]));
    return v;
}

// ------------------------------------------------------------------ protocol discovery
struct SampleLog { uint8_t n_topics = 0; Hash topics[4]{}; uint32_t data_words = 0; std::vector<Hash> data; bool misaligned = false; };

inline std::string trim(std::string s) { size_t a = s.find_first_not_of(" \t\r\n"), b = s.find_last_not_of(" \t\r\n"); return a == std::string::npos ? "" : s.substr(a, b - a + 1); }
inline std::vector<std::string> split_params(const std::string& inner) {
    std::vector<std::string> out; int depth = 0; std::string cur;
    for (char ch : inner) {
        if (ch == '(') depth++; else if (ch == ')') depth--;
        if (ch == ',' && depth == 0) { out.push_back(cur); cur.clear(); } else cur += ch;
    }
    if (!trim(cur).empty()) out.push_back(cur);
    need(depth == 0, "SIG_PARENS");
    return out;
}
inline std::string canon_type(std::string t) {
    t = trim(t);
    need(!t.empty(), "SIG_EMPTY_PARAM");
    auto sp = t.find_first_of(" \t"); if (sp != std::string::npos) t = t.substr(0, sp);   // drop 'indexed' / names
    if (t.rfind("tuple(", 0) == 0) t = t.substr(5);
    if (!t.empty() && t[0] == '(') {
        auto close = t.rfind(')'); need(close != std::string::npos, "SIG_PARENS");
        std::string inner, out = "(";
        auto ps = split_params(t.substr(1, close - 1));
        for (size_t i = 0; i < ps.size(); i++) { if (i) out += ','; out += canon_type(ps[i]); }
        return out + ")" + t.substr(close + 1);
    }
    size_t i = 0; while (i < t.size() && std::isalpha(uint8_t(t[i]))) i++;
    std::string base = t.substr(0, i), suffix = t.substr(i);
    bool digit = !suffix.empty() && std::isdigit(uint8_t(suffix[0]));
    if (!digit) { if (base == "uint") base = "uint256"; else if (base == "int") base = "int256"; else if (base == "byte") base = "bytes1"; else if (base == "fixed") base = "fixed128x18"; else if (base == "ufixed") base = "ufixed128x18"; }
    t = base + suffix;
    static const std::regex elem(R"(^(address|bool|string|bytes([1-9]|[12][0-9]|3[0-2])?|u?int(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?|u?fixed[0-9]*x?[0-9]*)((\[[0-9]*\])*)$)");
    need(std::regex_match(t, elem), "SIG_TYPE:" + t.substr(0, 40));
    return t;
}
// "event Supply(address indexed reserve, uint amount)" -> "Supply(address,uint256)"
inline std::pair<std::string, std::vector<std::string>> canonical_signature(std::string s) {
    s = trim(s);
    if (s.rfind("event ", 0) == 0) s = trim(s.substr(6));
    if (!s.empty() && s.back() == ';') s.pop_back();
    static const std::regex shape(R"(^([A-Za-z_$][A-Za-z0-9_$]*)\s*\(([\s\S]*)\)\s*$)");
    std::smatch m; need(std::regex_match(s, m, shape), "SIG_SHAPE");
    std::vector<std::string> params;
    std::string inner = m[2].str();
    if (!trim(inner).empty()) for (auto& p : split_params(inner)) params.push_back(canon_type(p));
    std::string out = m[1].str() + "(";
    for (size_t i = 0; i < params.size(); i++) { if (i) out += ','; out += params[i]; }
    return {out + ")", params};
}
inline bool is_dynamic(const std::string& t) {
    if (t == "string" || t == "bytes" || (t.size() >= 2 && t.substr(t.size() - 2) == "[]")) return true;
    if (!t.empty() && t[0] == '(') { auto c = t.rfind(')'); for (auto& p : split_params(t.substr(1, c - 1))) if (is_dynamic(p)) return true; }
    return false;
}
inline uint64_t static_words(const std::string& t) {
    static const std::regex arr(R"(^(.*)\[([0-9]+)\]$)"); std::smatch m;
    if (std::regex_match(t, m, arr)) return std::stoull(m[2].str()) * static_words(m[1].str());
    if (!t.empty() && t[0] == '(') { uint64_t s = 0; auto c = t.rfind(')'); for (auto& p : split_params(t.substr(1, c - 1))) s += static_words(p); return s; }
    return 1;
}
// 12 zero bytes then a 160-bit value >= 2^120 (or the zero address).
inline bool address_word_loose(const Hash& w) {   // >= 2^120 exactly as protocol.py (top 5 bytes of the address)
    for (int i = 0; i < 12; i++) if (w[i]) return false;
    bool zero = true; for (int i = 12; i < 32; i++) zero &= w[i] == 0;
    if (zero) return true;
    for (int i = 12; i < 17; i++) if (w[i]) return true;   // bytes 12..16 hold bits 159..120
    return false;
}

struct Mapping { std::string topic_ref; Hash topic0{}; std::string signature; int action = -1; std::string wallet, asset, amount; };

inline void index_sets_check(size_t n, size_t k, const std::vector<std::string>& params, uint32_t data_words, bool& hit) {
    // exists subset of k indexed params such that the remaining static words == data_words
    std::vector<size_t> idx(k);
    std::function<void(size_t, size_t)> rec = [&](size_t start, size_t depth) {
        if (hit) return;
        if (depth == k) {
            uint64_t words = 0;
            for (size_t i = 0; i < n; i++) if (std::find(idx.begin(), idx.begin() + long(k), i) == idx.begin() + long(k)) words += static_words(params[i]);
            if (words == data_words) hit = true;
            return;
        }
        for (size_t i = start; i < n; i++) { idx[depth] = i; rec(i + 1, depth + 1); }
    };
    rec(0, 0);
}

// One mapping vs. the retained sample log (nullptr when the pack had none).
inline std::vector<std::string> check_mapping(const Mapping& m, const SampleLog* log) {
    std::vector<std::string> r;
    std::string t10 = hex0x(View(m.topic0.data(), 32)).substr(0, 10);
    std::pair<std::string, std::vector<std::string>> sig;
    try { sig = canonical_signature(m.signature); } catch (const std::exception& e) { r.push_back("SIGNATURE_UNPARSEABLE:" + t10 + ":" + e.what()); return r; }
    if (keccak256(sig.first) != m.topic0) { r.push_back("SIGNATURE_HASH_MISMATCH:" + t10 + ":" + sig.first.substr(0, 60)); return r; }
    if (m.action < 0 || m.action >= int(protocol_action_count)) r.push_back("MAPPING_SHAPE_OR_ACTION");
    const auto& params = sig.second;
    if (!log) {
        if (!m.wallet.empty() || !m.asset.empty() || !m.amount.empty()) r.push_back("NO_SAMPLE_LOG_FOR_ROLES:" + t10);
        return r;
    }
    size_t n_indexed = log->n_topics ? log->n_topics - 1u : 0;
    if (n_indexed > params.size()) r.push_back("LAYOUT_TOO_MANY_TOPICS:" + t10);
    else if (std::any_of(params.begin(), params.end(), is_dynamic)) { if (log->data_words < params.size() - n_indexed) r.push_back("LAYOUT_DATA_WORDS_MISMATCH:" + t10); }
    else { bool hit = false; index_sets_check(params.size(), n_indexed, params, log->data_words, hit); if (!hit) r.push_back("LAYOUT_DATA_WORDS_MISMATCH:" + t10); }
    static const std::regex tre(R"(^t(?:opic)?([0-9]+)$)"), dre(R"(^d(?:ata)?\[?([0-9]+)\]?$)");
    for (auto [role, ref] : {std::pair<std::string, std::string>{"wallet", m.wallet}, {"asset", m.asset}, {"amount", m.amount}}) {
        if (ref.empty() || (ref == "em" && role == "asset") || (ref == "emitter" && role == "asset")) continue;
        std::smatch mt; const Hash* word = nullptr;
        if (std::regex_match(ref, mt, tre)) {
            size_t k = std::stoul(mt[1].str());
            if (k < 1 || k >= log->n_topics) { r.push_back("FIELD_ROLE_OUT_OF_RANGE:" + t10 + ":" + role + "=" + ref); continue; }
            word = &log->topics[k];
        } else if (std::regex_match(ref, mt, dre)) {
            size_t k = std::stoul(mt[1].str());
            if (k >= log->data_words) { r.push_back("FIELD_ROLE_OUT_OF_RANGE:" + t10 + ":" + role + "=" + ref); continue; }
            if (k < log->data.size()) word = &log->data[k];
        } else { r.push_back("FIELD_ROLE_SYNTAX:" + t10 + ":" + role + "=" + ref.substr(0, 20)); continue; }
        if ((role == "wallet" || role == "asset") && word && !address_word_loose(*word)) r.push_back("FIELD_ROLE_NOT_ADDRESS_WORD:" + t10 + ":" + role + "=" + ref);
    }
    return r;
}
} // namespace machine::graphloop
