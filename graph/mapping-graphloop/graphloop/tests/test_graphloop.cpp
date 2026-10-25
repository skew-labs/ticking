// Conformance tests for the graph-loop native core (validators, hashing, frontier, journal).
// Build: make build/test_graphloop (-O1) and build/test_graphloop-sanitize (ASan+UBSan).
#include "machine/graphloop/engine.hpp"
#include <cassert>
#include <iostream>

using namespace machine::graphloop;

static int failures = 0, checks = 0;
#define CHECK(x) do { checks++; if (!(x)) { failures++; std::cerr << __FILE__ << ":" << __LINE__ << " CHECK failed: " #x "\n"; } } while (0)
static bool has(const std::vector<std::string>& v, std::string_view prefix) { for (auto& x : v) if (x.rfind(std::string(prefix), 0) == 0) return true; return false; }
static Hash h32(std::string_view hx) { Hash h{}; need(unhex_exact(hx, h.data(), 32), "test hex"); return h; }
static Hash addr_word(uint8_t first) { Hash w{}; w[12] = first; w[31] = 0x42; return w; }

static void test_hashing() {
    auto e = keccak256(std::string_view(""));
    CHECK(hex(View(e.data(), 32)) == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470");   // Keccak-256, not SHA3-256 (a7ffc6f8...)
    auto t = keccak256(std::string_view("Transfer(address,address,uint256)"));
    CHECK(hex(View(t.data(), 32)) == "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef");
    const char* s = "123456789"; CHECK(crc32c(reinterpret_cast<const uint8_t*>(s), 9) == 0xE3069283u);
    for (uint64_t n : {0ull, 1ull, 32767ull, 32768ull, 100000ull, 123456789ull, 4000000000ull}) {
        double back = wvalue(wcode(n)); CHECK(std::fabs(back - double(n)) <= std::max(0.0, double(n) * 0.0007));
    }
    CHECK(wcode(5) == 5 && wcount(wcode(32767)) == 32767);
    Addr a = addr_from("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"); CHECK(hex0x(a) == "0x82af49447d8a07e3bd95bd0d56f35241523fbab1");
    Addr bad{}; CHECK(!parse_addr("0x82aF49447D8a07e3bd95BD0d56f35241523fBaZ1", bad) && !parse_addr("0x1234", bad));
}

static void test_signatures() {
    CHECK(canonical_signature("event Supply(address indexed reserve, uint amount)").first == "Supply(address,uint256)");
    CHECK(canonical_signature("Swap(address indexed sender,uint amount0In,uint amount1In,uint amount0Out,uint amount1Out,address indexed to)").first == "Swap(address,uint256,uint256,uint256,uint256,address)");
    CHECK(canonical_signature("Foo((address,uint)[] xs, bytes32 id)").first == "Foo((address,uint256)[],bytes32)");
    CHECK(canonical_signature("Bar()").first == "Bar()");
    bool threw = false; try { canonical_signature("Baz(address,uint7)"); } catch (...) { threw = true; } CHECK(threw);
    threw = false; try { canonical_signature("not a signature"); } catch (...) { threw = true; } CHECK(threw);
    CHECK(is_dynamic("bytes") && is_dynamic("uint256[]") && !is_dynamic("uint256[3]") && static_words("uint256[3]") == 3 && static_words("(address,uint256)") == 2);
}

static void test_mapping() {
    Mapping m; m.topic0 = keccak256(std::string_view("Transfer(address,address,uint256)")); m.signature = "Transfer(address indexed from, address indexed to, uint256 value)";
    m.action = 16; m.wallet = "t1"; m.asset = "em"; m.amount = "d0";
    SampleLog log; log.n_topics = 3; log.topics[0] = m.topic0; log.topics[1] = addr_word(0xab); log.topics[2] = addr_word(0xcd);
    log.data_words = 1; Hash amt{}; amt[31] = 7; log.data.push_back(amt);
    CHECK(check_mapping(m, &log).empty());
    auto m2 = m; m2.amount = "d1"; CHECK(has(check_mapping(m2, &log), "FIELD_ROLE_OUT_OF_RANGE"));
    auto m3 = m; m3.wallet = "d0"; CHECK(has(check_mapping(m3, &log), "FIELD_ROLE_NOT_ADDRESS_WORD"));
    auto m4 = m; m4.wallet = "t3"; CHECK(has(check_mapping(m4, &log), "FIELD_ROLE_OUT_OF_RANGE"));
    auto m5 = m; m5.signature = "Transfer(address,address,uint128)"; CHECK(has(check_mapping(m5, &log), "SIGNATURE_HASH_MISMATCH"));
    auto m6 = m; m6.signature = "Transfer(address,address"; CHECK(has(check_mapping(m6, &log), "SIGNATURE_UNPARSEABLE"));
    auto m7 = m; m7.wallet = "x9"; CHECK(has(check_mapping(m7, &log), "FIELD_ROLE_SYNTAX"));
    SampleLog l2 = log; l2.n_topics = 1; l2.data_words = 3; l2.data = {addr_word(1), addr_word(2), amt};
    auto m8 = m; m8.wallet = ""; m8.asset = ""; m8.amount = "d2"; CHECK(check_mapping(m8, &l2).empty());        // ERC-20 shape, nothing indexed
    SampleLog l3 = l2; l3.data_words = 2; CHECK(has(check_mapping(m8, &l3), "LAYOUT_DATA_WORDS_MISMATCH"));
    SampleLog l4 = log; l4.n_topics = 4; l4.topics[3] = addr_word(9); l4.data_words = 0; l4.data.clear();      // ERC-721 shape (3 indexed)
    auto m9 = m; m9.amount = ""; CHECK(check_mapping(m9, &l4).empty());
    CHECK(has(check_mapping(m, nullptr), "NO_SAMPLE_LOG_FOR_ROLES"));
    auto m10 = m; m10.wallet = m10.asset = m10.amount = ""; CHECK(check_mapping(m10, nullptr).empty());
    Hash z{}; CHECK(address_word_loose(z)); Hash small{}; small[31] = 5; CHECK(!address_word_loose(small));
}

static SubjectFacts facts(char type, uint64_t topics) {
    SubjectFacts f; f.type = type; f.topics = topics; f.sent = 10; f.recv = 50; f.cp = 30; f.outdeg = 1; f.top2_out = 0.95;
    f.keys = {"ty", "tx", "sh2", "o1", "ev", "fam", "X1"}; f.names = {"uniswap_v3:router"}; return f;
}
static void test_validators() {
    ValidateParams p; std::vector<std::string> ev{"ty", "ev"};
    CHECK(validate_address(token_contract, 0.9, ev, "", facts('C', tf_transfer | tf_approval), p).ok());
    CHECK(has(validate_address(token_contract, 0.9, ev, "", facts('C', tf_approval), p).hard, "TOKEN_WITHOUT_EMITTED_TRANSFER"));
    CHECK(has(validate_address(token_contract, 0.9, ev, "", facts('C', tf_transfer | tf_swap_v2 | tf_sync_v2), p).soft, "TOKEN_ALSO_EMITS_POOL_EVENTS"));
    CHECK(validate_address(dex_pool, 0.9, ev, "", facts('C', tf_swap_v2 | tf_sync_v2), p).ok());
    CHECK(has(validate_address(dex_pool, 0.9, ev, "", facts('C', tf_swap_v2), p).hard, "V2_POOL_WITHOUT_SYNC"));
    CHECK(validate_address(dex_pool, 0.9, ev, "", facts('C', tf_swap_v3), p).ok());
    CHECK(has(validate_address(dex_pool, 0.9, ev, "", facts('C', tf_transfer), p).hard, "POOL_WITHOUT_SWAP_EVENT"));
    CHECK(has(validate_address(dex_router, 0.9, ev, "", facts('C', tf_swap_v3), p).hard, "ROUTER_EMITS_POOL_EVENTS"));
    CHECK(has(validate_address(nft_contract, 0.9, ev, "", facts('C', tf_approval), p).hard, "NFT_WITHOUT_TRANSFER_EVENTS"));
    CHECK(has(validate_address(eoa_user, 0.8, ev, "", facts('C', 0), p).hard, "EOA_CATEGORY_BUT_IS_CONTRACT"));
    CHECK(has(validate_address(lending, 0.8, ev, "", facts('E', 0), p).hard, "CONTRACT_CATEGORY_BUT_NOT_CONTRACT"));
    CHECK(has(validate_address(lending, 0.8, ev, "", facts('C', 0), p).soft, "NO_KNOWN_LENDING_EVENT"));
    CHECK(validate_address(cex_deposit, 0.7, {"sh2", "o1"}, "", facts('E', 0), p).ok());
    auto f = facts('E', 0); f.top2_out = 0.5; CHECK(has(validate_address(cex_deposit, 0.7, {"sh2"}, "", f, p).hard, "DEPOSIT_OUTFLOW_NOT_CONCENTRATED"));
    f = facts('E', 0); f.sent = 0; CHECK(has(validate_address(cex_deposit, 0.7, {"sh2"}, "", f, p).hard, "DEPOSIT_NEVER_SENDS"));
    f = facts('E', 0); f.sent = 900; f.recv = 100; CHECK(has(validate_address(cex_deposit, 0.7, {"sh2"}, "", f, p).hard, "DEPOSIT_SENDS_MORE_THAN_RECEIVES"));
    f = facts('E', 0); f.sent = 10; f.cp = 5; CHECK(has(validate_address(cex_hot_wallet, 0.7, {"tx"}, "", f, p).hard, "HOT_WALLET_LOW_FANOUT"));
    f = facts('E', 0); f.sent = 5000; f.cp = 900; CHECK(validate_address(cex_hot_wallet, 0.7, {"tx"}, "", f, p).ok());
    f = facts('E', 0); f.sent = 50; CHECK(has(validate_address(mev_bot, 0.7, {"tx"}, "", f, p).hard, "MEV_LOW_ACTIVITY"));
    CHECK(has(validate_address(eoa_user, 0.8, {"nope"}, "", facts('E', 0), p).hard, "EVIDENCE_KEY_NOT_IN_PACK"));
    CHECK(has(validate_address(eoa_user, 0.8, {}, "", facts('E', 0), p).hard, "EVIDENCE_EMPTY"));
    CHECK(validate_address(unknown, 0.2, {}, "", facts('E', 0), p).ok());
    CHECK(has(validate_address(dex_router, 0.8, {"X1"}, "Binance", facts('C', 0), p).hard, "ENTITY_NAME_NOT_IN_PACK"));
    CHECK(validate_address(dex_router, 0.8, {"X1"}, "uniswap_v3", facts('C', 0), p).ok());
    f = facts('C', tf_lending); f.registry = true; f.base_cat = lending; CHECK(has(validate_address(contract_other, 0.8, {"ty"}, "", f, p).hard, "REGISTRY_CONFLICT"));
    CHECK(has(validate_address(category_count, 0.8, {"ty"}, "", facts('E', 0), p).hard, "CATEGORY_NOT_IN_ENUM"));
    CHECK(has(validate_address(eoa_user, 1.5, {"ty"}, "", facts('E', 0), p).hard, "CONFIDENCE_RANGE"));
}

static void test_output_parse() {
    auto r = Engine::parse_output(json("```json\n{\"r\":[[1,1,6,[\"sh2\",\"o1\"]],{\"s\":\"S2\",\"c\":\"eoa_user\",\"q\":7,\"e\":[\"ty\"],\"n\":\"\"}]}\n```"), t_address);
    CHECK(r.size() == 2 && r[0].s == 1 && r[0].c == 1 && std::fabs(r[0].conf - 0.65) < 1e-9 && r[0].ev.size() == 2);
    CHECK(r[1].s == 2 && r[1].c == eoa_user && std::fabs(r[1].conf - 0.75) < 1e-9);
    auto p = Engine::parse_output(json::parse(R"J({"p":[[1,0,8,[["E1","Supply(address,uint256)",0,"t1","em","d0"]],["ev"]]]})J"), t_protocol);
    CHECK(p.size() == 1 && p[0].fam == 0 && p[0].maps.size() == 1 && p[0].maps[0].action == 0 && p[0].maps[0].wallet == "t1" && p[0].ev.size() == 1);
    bool threw = false; try { Engine::parse_output(json("no json here"), t_address); } catch (...) { threw = true; } CHECK(threw);
    Alias a; auto n1 = a.name(1, 77), n2 = a.name(0, 5), n3 = a.name(1, 77); int c; uint32_t id;
    CHECK(n1 == "X1" && n2 == "A1" && n3 == "X1" && a.resolve("X1", c, id) && c == 1 && id == 77 && !a.resolve("X2", c, id) && !a.resolve("Q1", c, id));
}

static void test_frontier() {
    Frontier f;
    f.push(1, 0.5f); f.push(2, 0.9f); f.push(3, 0.1f); f.push(4, 0.7f);
    std::vector<uint32_t> got;
    f.lease(2, 100, 10, 77, got, [](uint32_t) { return true; });
    CHECK(got.size() == 2 && got[0] == 2 && got[1] == 4);
    CHECK(f.holds(2, 77) && !f.holds(2, 78) && !f.holds(1, 77));
    f.push(2, 0.95f);                         // changed while leased -> dirty
    f.release(2, 77, true, 1234);             // completed, but dirty -> re-queued
    CHECK(f.last_hash(2) == 1234);
    got.clear(); f.lease(1, 101, 10, 78, got, [](uint32_t) { return true; }); CHECK(got.size() == 1 && got[0] == 2);
    CHECK(f.expire(105) == 0);
    CHECK(f.expire(112) == 2);                // nodes 4 (lease 77) and 2 (lease 78) expired -> back in queue
    got.clear(); f.lease(10, 200, 10, 79, got, [](uint32_t v) { return v != 1; });   // veto node 1 (dedupe)
    CHECK(got.size() == 3);
    auto st = f.stats(); CHECK(st.dedup_skips == 1 && st.leased == 3 && st.queued == 0);
    f.release(4, 79, false, 0); st = f.stats(); CHECK(st.queued == 1 && st.leased == 2);
}

static void test_wal_and_snapshot() {
    fs::path dir = fs::temp_directory_path() / ("glwal-test-" + std::to_string(::getpid()));
    fs::remove_all(dir);
    {
        Wal w; Wal::Options o; o.batch_us = 100;
        CHECK(w.open(dir / "wal", "gid", 0, o, [](uint64_t, uint64_t, uint8_t, Dec&) {}) == 0);
        std::atomic<int> done{0};
        for (int i = 0; i < 100; i++) { Enc e; e.u32(uint32_t(i)).str("rec"); w.append(6, e.b, [&] { done++; }); }
        w.drain(); CHECK(done.load() == 100 && w.durable_seq() == 100);
        w.close();
    }
    // torn tail: append a partial frame to the newest file
    std::vector<fs::path> files; for (auto& e : fs::directory_iterator(dir / "wal")) files.push_back(e.path()); std::sort(files.begin(), files.end());
    { int fd = ::open(files.back().c_str(), O_WRONLY | O_APPEND); uint32_t len = 999, crc = 1; ::write(fd, &len, 4); ::write(fd, &crc, 4); ::write(fd, "xx", 2); ::close(fd); }
    uint64_t sum = 0;
    {
        Wal w; Wal::Options o;
        uint64_t n = w.open(dir / "wal", "gid", 0, o, [&](uint64_t seq, uint64_t, uint8_t kind, Dec& d) { CHECK(kind == 6); sum += d.u32(); CHECK(d.str() == "rec"); (void)seq; });
        CHECK(n == 100 && sum == 4950 && w.last_seq() == 100);
        Enc e; e.u32(1000).str("rec"); w.append(6, e.b, nullptr); w.drain(); w.close();
    }
    {
        Wal w; Wal::Options o; uint64_t n = w.open(dir / "wal", "gid", 50, o, [&](uint64_t, uint64_t, uint8_t, Dec&) {});
        CHECK(n == 51);   // seq 51..101 after a snapshot at 50
        w.close();
    }
    // snapshot roundtrip + corruption detection
    std::vector<std::pair<uint8_t, std::string>> recs{{7, "abc"}, {9, std::string(1000, 'z')}};
    write_snapshot(dir / "snap", 42, recs, "gid", json::object());
    auto s = latest_snapshot(dir / "snap"); CHECK(s && s->seq == 42);
    size_t n = 0; read_snapshot(s->file, [&](uint8_t k, Dec& d) { n++; CHECK(k == 7 || k == 9); (void)d; }); CHECK(n == 2);
    { auto b = load_file(s->file); b[20] ^= 1; ::chmod(s->file.c_str(), 0600); int fd = ::open(s->file.c_str(), O_WRONLY | O_TRUNC); ::write(fd, b.data(), b.size()); ::close(fd); }
    CHECK(!latest_snapshot(dir / "snap"));   // sha256 mismatch -> rejected
    fs::remove_all(dir);
}

int main() {
    test_hashing(); test_signatures(); test_mapping(); test_validators(); test_output_parse(); test_frontier(); test_wal_and_snapshot();
    std::cout << "{\"checks\":" << checks << ",\"failures\":" << failures << "}\n";
    if (!failures) std::cout << "native conformance passed\n";
    return failures ? 1 : 0;
}
