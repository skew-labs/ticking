// serve_basic: Python-compatible JSON numbers, invscreen-shaped answers from a synthetic chain, path search and route errors.
#include <machine/engine/serve_json.hpp>
#include <machine/engine/serve_routes.hpp>
#include <nlohmann/json.hpp>
#include <cassert>
#include <cstdio>
#include <fstream>
#include <string>

using namespace machine::engine::serve;
using nlohmann::json;

static int fails = 0;
#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static std::string pyf(double v) { std::string s; py_float(s, v); return s; }

static machine::graphloop::HttpResponse get(const ChainMap& m, const std::string& target) {
    machine::graphloop::HttpRequest rq; rq.method = "GET";
    auto q = target.find('?'); rq.path = target.substr(0, q); if (q != std::string::npos) rq.query = target.substr(q + 1);
    Budget b; b.deadline_ns = machine::graphloop::mono_ns() + 2'000'000'000ULL;
    return handle(m, rq, b);
}

static std::string a(int i) { char b[43]; std::snprintf(b, sizeof b, "0x%040x", i); return b; }

int main() {
    // repr(float) as CPython prints it
    CHECK(pyf(1e16) == "1e+16");
    CHECK(pyf(1234567890123456.0) == "1234567890123456.0");
    CHECK(pyf(0.0001) == "0.0001");
    CHECK(pyf(0.00001) == "1e-05");
    CHECK(pyf(1000000000.0) == "1000000000.0");
    CHECK(pyf(14395207.591280462) == "14395207.591280462");
    CHECK(pyf(1.4395) == "1.4395");
    CHECK(pyf(0.1 + 0.2) == "0.30000000000000004");
    CHECK(pyf(-2.5) == "-2.5");
    CHECK(pyf(1e22) == "1e+22");
    CHECK(pyf(123456789012345678.0) == "1.2345678901234568e+17");
    CHECK(pyf(0.0) == "0.0");
    CHECK(pyf(100.0) == "100.0");
    CHECK(py_round(1.23456789, 4) == 1.2346);
    CHECK(kind_of("dex_pool", "") == "pool" && kind_of("cex_deposit", "") == "exchange" && kind_of("multisig_or_safe", "") == "safe" &&
          kind_of("contract_other", "") == "contract" && kind_of("eoa_user", "") == "wallet" && kind_of("eoa_whale", "") == "whale" &&
          kind_of("mev_bot", "") == "mev" && kind_of("unknown", "dex") == "pool");

    // synthetic chain: 0 = zero address (mint source), 1 creator, 2 pool, 3 token, 4..9 wallets, 10 exchange hub
    json nodes = json::array();
    nodes.push_back({{"addr", a(0)}, {"category", "unknown"}});
    nodes.push_back({{"addr", a(1)}, {"category", "eoa_user"}, {"tx_sent", 5}, {"tx_received", 2}, {"first_block", 1}, {"last_block", 90}});
    nodes.push_back({{"addr", a(2)}, {"category", "dex_pool"}, {"entity", "uniswap:pool"}, {"is_contract", true}});
    nodes.push_back({{"addr", a(3)}, {"category", "token_contract"}, {"is_contract", true}});
    for (int i = 4; i < 10; i++) nodes.push_back({{"addr", a(i)}, {"category", "eoa_user"}, {"tx_sent", i}, {"tx_received", 1}, {"first_block", 10}, {"last_block", 50}});
    nodes.push_back({{"addr", a(10)}, {"category", "cex_hot_wallet"}, {"entity", "okx"}, {"out_degree", 90000}, {"in_degree", 90000}});
    json edges = json::array();
    edges.push_back({{"from", 4}, {"to", 1}, {"n", 3}, {"amount", "3000000000000000000"}, {"first_block", 10}, {"last_block", 20}});
    edges.push_back({{"from", 5}, {"to", 1}, {"n", 7}, {"asset", 0}, {"amount", "5000"}, {"first_block", 11}, {"last_block", 40}});
    edges.push_back({{"from", 10}, {"to", 1}, {"n", 9}, {"amount", "1"}, {"first_block", 5}, {"last_block", 60}});
    edges.push_back({{"from", 6}, {"to", 4}, {"n", 2}, {"amount", "2"}, {"first_block", 12}, {"last_block", 13}});
    edges.push_back({{"from", 1}, {"to", 7}, {"n", 1}, {"amount", "1"}, {"first_block", 30}, {"last_block", 30}});
    edges.push_back({{"from", 7}, {"to", 8}, {"n", 1}, {"amount", "1"}, {"first_block", 31}, {"last_block", 31}});
    edges.push_back({{"from", 8}, {"to", 9}, {"n", 1}, {"amount", "1"}, {"first_block", 32}, {"last_block", 32}});
    json transfers = json::array();
    transfers.push_back({{"token", 0}, {"block", 10}, {"to", 2}, {"amount", "1000000000000000000000000000"}});   // mint 1e9 * 1e18 to the pool
    transfers.push_back({{"token", 0}, {"block", 10}, {"from", 2}, {"to", 1}, {"amount", "14395207591280462000000000"}});
    transfers.push_back({{"token", 0}, {"block", 20}, {"from", 2}, {"to", 4}, {"amount", "5000000000000000000000000"}});
    transfers.push_back({{"token", 0}, {"block", 30}, {"from", 4}, {"to", 5}, {"amount", "1000000000000000000000000"}});
    json m = {{"kind", "synth"}, {"chain", "robinhood"}, {"chain_id", 4663}, {"build_id", "synth-1"}, {"head_block", 100},
              {"blocks", {{0, 1781812800}, {100, 1781812800 + 100 * 60}}}, {"nodes", nodes},
              {"tokens", {{{"node", 3}, {"symbol", "CAT"}, {"decimals", 18}, {"creator", 1}}}}, {"edges", edges}, {"transfers", transfers}};
    std::string path = "/tmp/emv2-serve-test-synth.json";
    { std::ofstream f(path); f << m.dump(); }
    ChainMap chains; chains["robinhood"] = open_synth(path);

    // holders-at, snaps
    auto r = get(chains, "/api/public/em/holders-at?market=robinhood:" + a(3) + "&snaps=1&limit=5");
    CHECK(r.status == 200);
    CHECK(r.ctype.find("X-EMV2-Build: robinhood:synth-1") != std::string::npos);
    auto j = json::parse(r.body);
    CHECK(j["schema"] == "holders-at/1" && j["symbol"] == "CAT" && j["available"] == true && j["complete"] == true);
    CHECK(j["launch"]["block"] == 10 && j["head"] == 100 && j["snaps"].size() == 6);
    CHECK(j["as_of"]["chain"] == "robinhood" && j["as_of"]["block"] == 100 && j["as_of"]["build"] == "synth-1");
    CHECK(r.ctype.find("X-EMV2-As-Of: 100:") != std::string::npos);
    auto& s0 = j["snaps"][0];
    CHECK(s0["key"] == "launch" && s0["holders"] == 2 && s0["supplyBasis"] == "totalSupply()");
    CHECK(s0["top"][0]["address"] == a(2) && s0["top"][0]["kind"] == "pool" && s0["top"][0]["entity"] == "uniswap:pool" && s0["top"][0]["class"].is_null());
    CHECK(s0["top"][1]["address"] == a(1) && s0["top"][1]["isCreator"] == true);
    CHECK(std::abs(s0["top"][1]["amount"].get<double>() - 14395207.591280462) < 1e-6);
    CHECK(s0["top"][1]["pct"] == 1.4395);
    auto& now = j["snaps"][5];
    CHECK(now["key"] == "now" && now["holders"] == 4 && now["block"] == 100);
    // a block past the map end is answered at the map end, and as_of says so
    j = json::parse(get(chains, "/api/public/em/holders-at?market=robinhood:" + a(3) + "&block=5000").body);
    CHECK(j["snaps"][0]["block"] == 100 && j["as_of"]["block"] == 100);
    j = json::parse(get(chains, "/api/public/em/holders-at?market=robinhood:" + a(3) + "&block=30").body);
    CHECK(j["as_of"]["block"] == 30 && j["snaps"][0]["holders"] == 4);
    // `to` pins "now": block 30 (time t0 + 30 min) -> the 4th transfer (block 30) is in, holders 4; block 25 -> 3
    j = json::parse(get(chains, "/api/public/em/holders-at?market=robinhood:" + a(3) + "&snaps=1&to=" + std::to_string(1781812800 + 25 * 60)).body);
    CHECK(j["head"] == 25 && j["snaps"][5]["holders"] == 3);
    // a block before the first transfer
    r = get(chains, "/api/public/em/holders-at?market=robinhood:" + a(3) + "&block=5");
    j = json::parse(r.body);
    CHECK(j["snaps"][0]["note"] == "before the token's first transfer" && j["snaps"][0]["holders"] == 0);
    // unknown token, unknown chain, missing moment
    CHECK(json::parse(get(chains, "/api/public/em/holders-at?market=robinhood:" + a(77) + "&snaps=1").body)["available"] == false);
    CHECK(get(chains, "/api/public/em/holders-at?market=base:" + a(3) + "&snaps=1").status == 501);
    CHECK(get(chains, "/api/public/em/holders-at?market=robinhood:" + a(3)).status == 400);
    CHECK(get(chains, "/api/public/em/holders-at?market=robinhood:" + a(3) + "&snaps=1&bogus=1").status == 400);

    // address-chains
    r = get(chains, "/api/public/em/address-chains?address=" + a(1));
    j = json::parse(r.body);
    CHECK(r.status == 200 && j["available"] == true && j["chains"].size() == 1 && j["chains"][0]["chainId"] == 4663);
    CHECK(j["chains"][0]["txSent"] == 5 && j["chains"][0]["kind"] == "eoa" && j["chains"][0]["why"] == "seen on one chain only" && j["chains"][0]["txs"] == 7);

    // trace-graph in: peers ranked by count, the exchange is a stop, the native link carries ETH
    r = get(chains, "/api/public/em/trace-graph?chain=robinhood&address=" + a(1) + "&direction=in&hops=2");
    j = json::parse(r.body);
    CHECK(r.status == 200 && j["schema"] == "trace/1" && j["inputHash"].get<std::string>().size() == 16);
    CHECK(j["nodes"][0]["address"] == a(1) && j["nodes"][0]["hop"] == 0);
    bool saw_stop = false, saw_eth = false, saw_hop2 = false;
    for (auto& n : j["nodes"]) { if (n["address"] == a(10)) saw_stop = n["stop"] == "cex hot wallet (okx)"; if (n["address"] == a(6)) saw_hop2 = n["hop"] == 2; }
    for (auto& l : j["links"]) if (l["a"] == a(4) && l["b"] == a(1)) saw_eth = l["symbol"] == "ETH" && l["asset"].is_null() && l["amount"] == 3.0 && l["why"] == "3 direct transfer(s), mostly ETH";
    CHECK(saw_stop && saw_eth && saw_hop2);
    CHECK(j["stops"].size() == 1 && j["stops"][0]["hop"] == 1);

    // trace (path) and 3-hop outflow
    r = get(chains, "/api/public/em/trace?chain=robinhood&from=" + a(1) + "&to=" + a(9));
    j = json::parse(r.body);
    CHECK(r.status == 200 && j["found"] == true && j["hops"] == 3 && j["path"][3]["ref"] == a(9) && j["path"][1]["edge_in"] == "native");
    r = get(chains, "/api/public/em/trace?chain=robinhood&from=" + a(1));
    j = json::parse(r.body);
    CHECK(r.status == 200 && j["params"]["direction"] == "out" && j["params"]["hops"] == 3);

    // phase-1 gaps and unknown paths
    CHECK(get(chains, "/api/public/em/wallet-trades?address=" + a(1) + "&market=robinhood:" + a(3)).status == 501);
    CHECK(get(chains, "/api/public/em/tools/wallet?address=" + a(1)).status == 501);
    CHECK(get(chains, "/api/public/em/nope").status == 404);
    CHECK(get(chains, "/health").status == 200);

    std::remove(path.c_str());
    if (fails) { std::fprintf(stderr, "%d failures\n", fails); return 1; }
    std::puts("serve_basic ok");
    return 0;
}
