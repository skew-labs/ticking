// serve_v2store: store_v2.cpp over a real (small) sealed build written with core's writers: dictionary, tokens and block time.
// Without graph or ledger segments the store reports only kDict|kBlocks and the routes that need more answer 501.
#include <machine/engine/dict.hpp>
#include <machine/engine/serve_routes.hpp>
#include <nlohmann/json.hpp>
#include <cstdio>

using namespace machine::engine;
using namespace machine::engine::serve;

static int fails = 0;
#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static Addr ad(int i) { Addr a{}; a.b[18] = uint8_t(i >> 8); a.b[19] = uint8_t(i); return a; }

int main() {
    fs::path dir = fs::temp_directory_path() / ("emv2-serve-v2store-" + std::to_string(::getpid()));
    fs::remove_all(dir); fs::create_directories(dir);
    PartManifest pm("arc", "core", "test-build-0001");
    {
        AddrDictWriter w(dir, 64);
        for (int i = 1; i <= 40; i++) w.push(ad(i));        // node id = i - 1
        auto [r1, r2] = w.finish(); pm.file(r1); pm.file(r2);
    }
    {
        TokenDictWriter tw(dir);
        tw.push(9, TokenKind::erc20, 6, 0, 1010, "USDC", "USD Coin");      // token contract = ad(10)
        tw.push(19, TokenKind::erc20, 255, tf_no_decimals, 1020, "ODD", "");
        auto [a, b] = tw.finish(); pm.file(a); pm.file(b);
    }
    {
        BlockTimeWriter bw(dir, 1000);
        for (int i = 0; i < 3000; i++) bw.push(1700000000u + uint32_t(i) * 2);
        pm.file(bw.finish());
    }
    pm.write(dir);
    merge_manifests(dir);

    StoreRef s = open_store(dir.string());
    CHECK(s->chain() == "arc" && s->chain_id() == 5042 && s->build_id() == "test-build-0001");
    CHECK((s->caps() & (ChainStore::kDict | ChainStore::kBlocks)) == (ChainStore::kDict | ChainStore::kBlocks));
    CHECK(!(s->caps() & ChainStore::kGraph) && !(s->caps() & ChainStore::kHolders));
    CHECK(s->head_block() == 3999 && s->head_time() == 1700000000u + 2999 * 2);
    CHECK(s->node_of(ad(10)) == 9 && s->node_of(ad(77)) == kNone);
    NodeAttr na; CHECK(s->node_attr(9, na) && na.addr == ad(10) && na.known);
    TokenId t = s->token_of(ad(10)); TokenAttr ta;
    CHECK(t == 0 && s->token_attr(t, ta) && ta.symbol == "USDC" && ta.decimals == 6 && ta.first_block == 1010 && ta.first_time == 1700000020u);
    TokenAttr tb; CHECK(s->token_attr(s->token_of(ad(20)), tb) && tb.decimals == 18);   // unknown decimals read as 18, like invscreen
    CHECK(s->block_at(1700000021u) == 1010 && s->block_at(1699999999u) == 0 && s->time_at(5000) == s->head_time());

    ChainMap chains; chains["arc"] = s;
    auto get = [&](const std::string& target) {
        machine::graphloop::HttpRequest rq; rq.method = "GET";
        auto q = target.find('?'); rq.path = target.substr(0, q); if (q != std::string::npos) rq.query = target.substr(q + 1);
        Budget b; b.deadline_ns = machine::graphloop::mono_ns() + 2'000'000'000ULL;
        return handle(chains, rq, b);
    };
    char a10[43]; std::snprintf(a10, sizeof a10, "0x%040x", 10);
    auto r = get(std::string("/api/public/em/holders-at?market=arc:") + a10 + "&snaps=1");
    CHECK(r.status == 501 && nlohmann::json::parse(r.body)["as_of"]["block"] == 3999);
    CHECK(get(std::string("/api/public/em/trace-graph?chain=arc&address=") + a10).status == 501);
    r = get(std::string("/api/public/em/address-chains?address=") + a10);
    auto j = nlohmann::json::parse(r.body);
    CHECK(r.status == 200 && j["chains"].size() == 1 && j["chains"][0]["chain"] == "arc" && j["as_of"]["arc"]["build"] == "test-build-0001");
    fs::remove_all(dir);
    if (fails) { std::fprintf(stderr, "%d failures\n", fails); return 1; }
    std::puts("serve_v2store ok");
    return 0;
}
