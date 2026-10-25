// glexport: graph image + durable state (snapshot + WAL, read-only replay) -> Parquet for the UI/Blob.
//   glexport --data DIR --state DIR --out DIR [--all] [--chain-key robinhood]
// Writes (zstd Parquet, tmp + fdatasync + rename): labels.parquet, label_deltas.parquet,
// bridge_legs.parquet, protocol_mappings.parquet, labels.jsonl (Galaxy builder input), and a sealed
// export-<wal_seq>.json manifest with sha256 per file.  Safe to run while glserve is writing.
#include "machine/graphloop/arrow_io.hpp"
#include "machine/graphloop/engine.hpp"
#include <fstream>
#include <iostream>
#include <map>

using namespace machine::graphloop;

namespace {
template <class B, class V> void app(B& b, V v) { need(b.Append(v).ok(), "arrow append"); }
std::shared_ptr<arrow::Array> fin(arrow::ArrayBuilder& b) { std::shared_ptr<arrow::Array> a; need(b.Finish(&a).ok(), "arrow finish"); return a; }
}

int main(int argc, char** argv) {
    try {
        fs::path data, graph_dir, state, out; bool all = false; std::string chain_key;
        for (int i = 1; i < argc; i++) {
            std::string a = argv[i]; auto val = [&]() -> std::string { need(i + 1 < argc, "missing value"); return argv[++i]; };
            if (a == "--data") data = val(); else if (a == "--graph") graph_dir = val(); else if (a == "--state") state = val(); else if (a == "--out") out = val();
            else if (a == "--all") all = true; else if (a == "--chain-key") chain_key = val();
            else throw std::runtime_error("unknown argument " + a);
        }
        need((!data.empty() || !graph_dir.empty()) && !out.empty(), "usage: glexport --data DIR|--graph DIR [--state DIR] --out DIR [--all]");
        if (graph_dir.empty()) graph_dir = data / trim(load_text(data / "CURRENT"));
        Graph g; g.open(graph_dir, "size", false);
        if (chain_key.empty()) chain_key = g.chain;
        Engine eng(g, ClassifyParams{}, EngineParams{});
        uint64_t seq = 0;
        if (!state.empty()) {
            uint64_t snap = 0;
            if (auto s = latest_snapshot(state / "snap")) { read_snapshot(s->file, [&](uint8_t k, Dec& d) { eng.load_snapshot_record(k, d); }); snap = s->seq; }
            eng.replaying = true;
            Wal w; w.open(state / "wal", g.graph_id, snap, Wal::Options{}, [&](uint64_t s2, uint64_t, uint8_t kind, Dec& d) { if (kind == 6) eng.replay_proposal(d, s2); seq = s2; }, true);
            if (seq < snap) seq = snap;
            eng.replaying = false;
            eng.propagate_all();
        }
        fs::create_directories(out);
        json files = json::object();
        // ---- labels.parquet
        arrow::FixedSizeBinaryBuilder ab(arrow::fixed_size_binary(20)); arrow::UInt32Builder nb; arrow::StringBuilder cat, src, st, sub, rule, ent, prov, model, evj;
        arrow::UInt8Builder catc, tier; arrow::FloatBuilder conf, imp; arrow::UInt64Builder wseq;
        std::ofstream jl(out / "labels.jsonl.tmp", std::ios::trunc);
        uint64_t rows = 0;
        std::unordered_map<uint32_t, Detail> det; eng.for_each_detail([&](uint32_t v, const Detail& d) { det[v] = d; });
        for (uint32_t v = 0; v < g.n; v++) {
            auto L = eng.label(v); auto& B = g.base[v];
            bool plain = L.src == s_classifier && L.cat == eoa_user && (B.rule == r_eoa || B.rule == r_recv_only);
            if (!all && plain) continue;
            app(ab, g.addr[v].b); app(nb, v); app(cat, std::string(category_names[L.cat])); app(catc, L.cat); app(conf, float(conf_of(L.conf)));
            app(src, std::string(source_names[L.src])); app(st, std::string(state_names[L.state])); app(sub, std::string(subtype_names[L.sub < subtype_count ? L.sub : 0]));
            app(tier, L.tier); app(rule, std::string(rule_names[B.rule])); auto e = eng.entity(v); app(ent, e); app(imp, g.f32[d_imp][v]);
            auto it = det.find(v);
            json ev = json::array();
            if (it != det.end()) { for (auto& [k, val] : it->second.evidence) ev.push_back({{"key", k}, {"value", val}}); app(prov, it->second.provider); app(model, it->second.model); app(wseq, it->second.seq); }
            else { app(prov, std::string()); app(model, std::string()); app(wseq, uint64_t(0)); ev.push_back({{"key", "rule"}, {"value", std::string(rule_names[B.rule])}}); }
            app(evj, ev.dump());
            if (!plain) {
                json row = {{"address", hex0x(g.addr[v])}, {"chain", chain_key}, {"category", category_names[L.cat]}, {"confidence", std::round(conf_of(L.conf) * 1000) / 1000},
                            {"evidence", ev}, {"label_source", L.src == s_agent ? "label" : std::string(source_names[L.src])}};
                if (!e.empty()) row["entity_name"] = e;
                jl << row.dump() << "\n";
            }
            rows++;
        }
        jl.close();
        { int fd = ::open((out / "labels.jsonl.tmp").c_str(), O_RDONLY); ::fdatasync(fd); ::close(fd); fs::rename(out / "labels.jsonl.tmp", out / "labels.jsonl");
          auto b = load_file(out / "labels.jsonl", size_t(16) << 30); auto h = sha256(View(b.data(), b.size())); files["labels.jsonl"] = {{"bytes", b.size()}, {"sha256", hex(View(h.data(), 32))}}; }
        auto schema = arrow::schema({arrow::field("address", arrow::fixed_size_binary(20)), arrow::field("node_id", arrow::uint32()), arrow::field("category", arrow::utf8()),
            arrow::field("category_code", arrow::uint8()), arrow::field("confidence", arrow::float32()), arrow::field("source", arrow::utf8()), arrow::field("state", arrow::utf8()),
            arrow::field("subtype", arrow::utf8()), arrow::field("tier", arrow::uint8()), arrow::field("rule", arrow::utf8()), arrow::field("entity_name", arrow::utf8()),
            arrow::field("importance", arrow::float32()), arrow::field("provider", arrow::utf8()), arrow::field("model", arrow::utf8()), arrow::field("evidence_json", arrow::utf8()), arrow::field("wal_seq", arrow::uint64())});
        auto t = arrow::Table::Make(schema, {fin(ab), fin(nb), fin(cat), fin(catc), fin(conf), fin(src), fin(st), fin(sub), fin(tier), fin(rule), fin(ent), fin(imp), fin(prov), fin(model), fin(evj), fin(wseq)});
        auto [b1, h1] = write_parquet_atomic(t, out / "labels.parquet"); files["labels.parquet"] = {{"bytes", b1}, {"sha256", h1}, {"rows", rows}};
        // ---- label_deltas.parquet (agent commits with evidence)
        {
            arrow::FixedSizeBinaryBuilder a(arrow::fixed_size_binary(20)); arrow::StringBuilder c, p, m, e, w, en; arrow::FloatBuilder cf; arrow::UInt64Builder sq, ts, bt;
            std::vector<uint32_t> nodes; for (auto& [v, d] : det) nodes.push_back(v); std::sort(nodes.begin(), nodes.end());
            for (auto v : nodes) { auto& d = det[v]; auto L = eng.label(v); json ev = json::array(); for (auto& [k, val] : d.evidence) ev.push_back({{"key", k}, {"value", val}});
                app(a, g.addr[v].b); app(c, std::string(category_names[L.cat])); app(cf, float(conf_of(L.conf))); app(p, d.provider); app(m, d.model); app(e, ev.dump()); app(w, d.warnings); app(en, d.entity); app(sq, d.seq); app(ts, d.ts); app(bt, d.batch); }
            auto sc = arrow::schema({arrow::field("address", arrow::fixed_size_binary(20)), arrow::field("category", arrow::utf8()), arrow::field("confidence", arrow::float32()), arrow::field("provider", arrow::utf8()),
                arrow::field("model", arrow::utf8()), arrow::field("evidence_json", arrow::utf8()), arrow::field("warnings", arrow::utf8()), arrow::field("entity_name", arrow::utf8()), arrow::field("wal_seq", arrow::uint64()),
                arrow::field("ts_ms", arrow::uint64()), arrow::field("batch_id", arrow::uint64())});
            auto tt = arrow::Table::Make(sc, {fin(a), fin(c), fin(cf), fin(p), fin(m), fin(e), fin(w), fin(en), fin(sq), fin(ts), fin(bt)});
            auto [b, h] = write_parquet_atomic(tt, out / "label_deltas.parquet"); files["label_deltas.parquet"] = {{"bytes", b}, {"sha256", h}, {"rows", nodes.size()}};
        }
        // ---- bridge_legs.parquet (wallet <-> contracts labelled bridge, conf >= 0.7)
        {
            arrow::FixedSizeBinaryBuilder wa(arrow::fixed_size_binary(20)), ba(arrow::fixed_size_binary(20)); arrow::StringBuilder dir, et, be; arrow::UInt64Builder n; uint64_t legs = 0;
            for (uint32_t v = 0; v < g.n; v++) {
                auto L = eng.label(v); if (L.cat != bridge || conf_of(L.conf) < 0.7) continue; auto ename = eng.entity(v);
                auto emit = [&](const Graph::Adj& adj, const char* d) { for (uint64_t i = 0; i < std::min<uint64_t>(adj.len, 1000000); i++) { uint32_t w = adj.nb[i]; if (g.is_contract(w)) continue;
                    app(wa, g.addr[w].b); app(ba, g.addr[v].b); app(dir, std::string(d)); app(et, std::string(edge_names[adj.t[i] & et_mask])); app(n, wcount(adj.w[i])); app(be, ename); legs++; } };
                emit(g.in(v), "bridge_out"); emit(g.out(v), "bridge_in");
            }
            auto sc = arrow::schema({arrow::field("wallet", arrow::fixed_size_binary(20)), arrow::field("bridge", arrow::fixed_size_binary(20)), arrow::field("direction", arrow::utf8()),
                arrow::field("edge", arrow::utf8()), arrow::field("n", arrow::uint64()), arrow::field("bridge_entity", arrow::utf8())});
            auto tt = arrow::Table::Make(sc, {fin(wa), fin(ba), fin(dir), fin(et), fin(n), fin(be)});
            auto [b, h] = write_parquet_atomic(tt, out / "bridge_legs.parquet"); files["bridge_legs.parquet"] = {{"bytes", b}, {"sha256", h}, {"rows", legs}};
        }
        // ---- protocol_mappings.parquet (learned topic0 -> signature/action/roles)
        {
            arrow::FixedSizeBinaryBuilder t0(arrow::fixed_size_binary(32)), ca(arrow::fixed_size_binary(20)); arrow::StringBuilder sig, act, fam, w, as, am, p, m; arrow::FloatBuilder cf; uint64_t k = 0;
            eng.for_each_proto([&](const Hash& t, const ProtoMap& pm) { app(t0, t.data()); app(sig, pm.signature); app(act, std::string(protocol_actions[pm.action < protocol_action_count ? pm.action : protocol_action_count - 1]));
                app(fam, std::string(protocol_families[pm.family < protocol_family_count ? pm.family : protocol_family_count - 1])); app(cf, float(conf_of(pm.conf))); app(w, pm.wallet); app(as, pm.asset); app(am, pm.amount);
                app(ca, pm.contract.b); app(p, pm.provider); app(m, pm.model); k++; });
            auto sc = arrow::schema({arrow::field("topic0", arrow::fixed_size_binary(32)), arrow::field("signature", arrow::utf8()), arrow::field("action", arrow::utf8()), arrow::field("family", arrow::utf8()),
                arrow::field("confidence", arrow::float32()), arrow::field("wallet_role", arrow::utf8()), arrow::field("asset_role", arrow::utf8()), arrow::field("amount_role", arrow::utf8()),
                arrow::field("contract", arrow::fixed_size_binary(20)), arrow::field("provider", arrow::utf8()), arrow::field("model", arrow::utf8())});
            auto tt = arrow::Table::Make(sc, {fin(t0), fin(sig), fin(act), fin(fam), fin(cf), fin(w), fin(as), fin(am), fin(ca), fin(p), fin(m)});
            auto [b, h] = write_parquet_atomic(tt, out / "protocol_mappings.parquet"); files["protocol_mappings.parquet"] = {{"bytes", b}, {"sha256", h}, {"rows", k}};
        }
        // ---- protocols.jsonl (Galaxy named structures): entities (registry/agent/propagated) + DEX pool protocols
        {
            auto kind_of = [](uint8_t c) -> const char* {
                switch (c) { case cex_hot_wallet: case cex_deposit: return "cex"; case bridge: return "bridge"; case dex_router: case dex_pool: return "dex";
                             case lending: return "lending"; default: return "other"; }
            };
            auto slug = [](std::string x) { std::string o; for (char ch : x) { unsigned char u = (unsigned char)ch; if (std::isalnum(u)) o += char(std::tolower(u)); else if (!o.empty() && o.back() != '_') o += '_'; } while (!o.empty() && o.back() == '_') o.pop_back(); return o; };
            struct P { std::string name; std::map<std::string, uint64_t> kinds; std::vector<std::pair<float, uint32_t>> nodes; };
            std::map<std::string, P> protos;
            for (uint32_t v = 0; v < g.n; v++) {
                auto L = eng.label(v);
                if (L.cat == cex_deposit || L.cat == eoa_user || L.cat == unknown) continue;
                std::string e = eng.entity(v), pid, name;
                if (!e.empty()) { auto cut = e.find(" (sibling)"); if (cut != std::string::npos) e = e.substr(0, cut); auto c = e.find(':'); name = c == std::string::npos ? e : e.substr(0, c); pid = slug(name); }
                else if (L.cat == dex_pool) { name = std::string(g.str(g.sparse(v, k_pool_proto, none))); pid = slug(name); }
                if (pid.empty()) continue;
                auto& p = protos[pid]; if (p.name.empty()) p.name = name; p.kinds[kind_of(L.cat)]++; p.nodes.push_back({g.f32[d_imp][v], v});
            }
            std::ofstream pj(out / "protocols.jsonl.tmp", std::ios::trunc); uint64_t np = 0;
            for (auto& [pid, p] : protos) {
                std::sort(p.nodes.begin(), p.nodes.end(), [](auto& a, auto& b) { return a.first > b.first || (a.first == b.first && a.second < b.second); });
                if (p.nodes.size() > 2000) p.nodes.resize(2000);
                std::string kind = "other"; uint64_t best = 0; for (auto& [k, c] : p.kinds) if (c > best || (c == best && k != "other")) { best = c; kind = k; }
                json addrs = json::array(); for (auto& [imp, v] : p.nodes) addrs.push_back(hex0x(g.addr[v]));
                pj << json({{"chain", chain_key}, {"protocol", pid}, {"name", p.name}, {"kind", kind}, {"addresses", addrs}}).dump() << "\n"; np++;
            }
            pj.close();
            { int fd = ::open((out / "protocols.jsonl.tmp").c_str(), O_RDONLY); ::fdatasync(fd); ::close(fd); fs::rename(out / "protocols.jsonl.tmp", out / "protocols.jsonl");
              auto b = load_file(out / "protocols.jsonl", size_t(4) << 30); auto h = sha256(View(b.data(), b.size())); files["protocols.jsonl"] = {{"bytes", b.size()}, {"sha256", hex(View(h.data(), 32))}, {"rows", np}}; }
        }
        json man = {{"schema", "economic-machine.graphloop.export.v1"}, {"chain", chain_key}, {"chain_id", g.chain_id}, {"graph_id", g.graph_id}, {"wal_seq", seq}, {"created_at_ms", now_ms()}, {"files", files}};
        char nm[64]; std::snprintf(nm, sizeof nm, "export-%020llu.json", (unsigned long long)seq);
        if (fs::exists(out / nm)) fs::remove(out / nm);
        seal_text(out / nm, man.dump(1));
        replace_file(out / "LATEST", std::string(nm) + "\n");
        std::cout << man.dump() << "\n";
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << "\n"; return 1; }
}
