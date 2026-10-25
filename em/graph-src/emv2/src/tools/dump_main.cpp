// emv2-dump <build_dir> <table> [--addresses FILE] [--tokens FILE] [--all] --out FILE.csv
// Verify contract (api.hpp "dump"): tables edges, nodes (graph), bal, token_stats (ledger), counts (both).
#include "machine/engine/dict.hpp"
#include <fstream>
#include <iostream>

using namespace machine::engine;

static std::vector<uint32_t> load_ids(const std::string& path, const AddrDict& d, bool tokens, const BuildDir& b) {
    std::vector<uint32_t> out; std::ifstream f(path); need(bool(f), "cannot read " + path);
    std::unique_ptr<TokenDict> td; if (tokens) td = std::make_unique<TokenDict>(b);
    std::string line;
    while (std::getline(f, line)) {
        while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
        if (line.empty()) continue;
        Addr a{}; need(machine::graphloop::parse_addr(line, a), "bad address line: " + line);
        auto id = d.find(a);
        if (!id) continue;
        if (!tokens) { out.push_back(*id); continue; }
        uint32_t first = 0; auto recs = td->by_addr(*id, first);
        for (size_t i = 0; i < recs.size(); i++) out.push_back(first + uint32_t(i));
    }
    std::sort(out.begin(), out.end()); out.erase(std::unique(out.begin(), out.end()), out.end());
    return out;
}

int main(int argc, char** argv) {
    try {
        std::vector<std::string> a(argv + 1, argv + argc);
        need(a.size() >= 2, "usage: emv2-dump <build_dir> <table> [--addresses F] [--tokens F] [--all] --out F.csv");
        BuildDir b(a[0]); std::string table = a[1], out_path, addr_file, tok_file; DumpFilter flt;
        for (size_t i = 2; i < a.size(); i++) {
            if (a[i] == "--all") flt.all = true;
            else if (a[i] == "--out" && i + 1 < a.size()) out_path = a[++i];
            else if (a[i] == "--addresses" && i + 1 < a.size()) addr_file = a[++i];
            else if (a[i] == "--tokens" && i + 1 < a.size()) tok_file = a[++i];
            else throw std::runtime_error("unknown option " + a[i]);
        }
        need(!out_path.empty(), "--out is required");
        need(out_path.size() > 4 && out_path.substr(out_path.size() - 4) == ".csv", "only .csv output (DuckDB reads it directly)");
        need(flt.all || !addr_file.empty() || !tok_file.empty() || table == "counts", "give --addresses, --tokens or --all");
        AddrDict d(b);
        if (!addr_file.empty()) flt.addrs = load_ids(addr_file, d, false, b);
        if (!tok_file.empty()) flt.tids = load_ids(tok_file, d, true, b);
        std::FILE* f = std::fopen(out_path.c_str(), "w"); need(f != nullptr, "cannot write " + out_path);
        CsvOut out(f);
        bool graph_table = table == "edges" || table == "nodes", ledger_table = table == "bal" || table == "token_stats";
        need(graph_table || ledger_table || table == "counts", "unknown table " + table);
        if (table == "counts") out.row({"key", "value"});
#ifdef HAVE_EMV2_GRAPH
        if (graph_table || table == "counts") graph::dump(b, table, flt, out);
#else
        need(!graph_table, "emv2-dump built without the graph reader");
#endif
#ifdef HAVE_EMV2_LEDGER
        if (ledger_table || table == "counts") ledger::dump(b, table, flt, out);
#else
        need(!ledger_table, "emv2-dump built without the ledger reader");
#endif
        if (table == "counts") { out.row({"build_id", b.build_id()}); out.row({"chain", b.chain()}); }
        need(std::fclose(f) == 0, "write " + out_path);
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "emv2-dump: " << e.what() << "\n";
        return 1;
    }
}
