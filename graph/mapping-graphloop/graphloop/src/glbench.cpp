// glbench: load generator for glserve (keep-alive HTTP/1.1 clients).
//   glbench --port 8710 --mode propose --clients 32 --batch 15 --seconds 30 [--invalid 0.2]
//   glbench --port 8710 --mode tools --graph DIR --clients 8 --seconds 20
//   glbench --port 8710 --mode propose --record acked.jsonl ...   (crash test: acked commits)
//   glbench --port 8710 --mode verify --record acked.jsonl         (after kill -9 + restart)
// Mock answers: the code hint's category (valid), or a deliberately invalid one (--invalid).
#include "machine/graphloop/graph.hpp"
#include "machine/graphloop/util.hpp"
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <fstream>
#include <iostream>
#include <random>

using namespace machine::graphloop;

namespace {
struct Client {
    int fd = -1; int port;
    explicit Client(int p) : port(p) { connect_(); }
    ~Client() { if (fd >= 0) ::close(fd); }
    void connect_() {
        if (fd >= 0) ::close(fd);
        fd = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0); need(fd >= 0, "socket");
        int one = 1; ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
        sockaddr_in a{}; a.sin_family = AF_INET; a.sin_port = htons(uint16_t(port)); ::inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
        need(::connect(fd, reinterpret_cast<sockaddr*>(&a), sizeof a) == 0, "connect");
    }
    std::pair<int, std::string> req(const std::string& method, const std::string& path, const std::string& body = {}) {
        std::string r = method + " " + path + " HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: " + std::to_string(body.size()) + "\r\n\r\n" + body;
        size_t off = 0; while (off < r.size()) { auto n = ::send(fd, r.data() + off, r.size() - off, MSG_NOSIGNAL); need(n > 0, "send"); off += size_t(n); }
        std::string in; char buf[65536]; size_t he = std::string::npos;
        while ((he = in.find("\r\n\r\n")) == std::string::npos) { auto n = ::recv(fd, buf, sizeof buf, 0); need(n > 0, "recv header"); in.append(buf, size_t(n)); }
        int status = std::atoi(in.c_str() + 9);
        auto cl = in.find("Content-Length: "); need(cl != std::string::npos && cl < he, "content-length");
        size_t len = std::strtoull(in.c_str() + cl + 16, nullptr, 10);
        while (in.size() < he + 4 + len) { auto n = ::recv(fd, buf, sizeof buf, 0); need(n > 0, "recv body"); in.append(buf, size_t(n)); }
        return {status, in.substr(he + 4, len)};
    }
};
struct Lat {
    std::vector<uint64_t> ns; void add(uint64_t x) { ns.push_back(x); }
    json summary() { if (ns.empty()) return json::object(); std::sort(ns.begin(), ns.end()); auto p = [&](double q) { return double(ns[std::min(ns.size() - 1, size_t(q * double(ns.size())))]) / 1000.0; };
        return {{"n", ns.size()}, {"p50_us", p(0.5)}, {"p90_us", p(0.9)}, {"p99_us", p(0.99)}, {"max_us", double(ns.back()) / 1000.0}}; }
};
}

int main(int argc, char** argv) {
    try {
        int port = 8710, clients = 16, batch = 15, seconds = 20; std::string mode = "propose", record, graph; double invalid = 0.0; std::vector<int> cpus;
        for (int i = 1; i < argc; i++) {
            std::string a = argv[i]; auto val = [&]() -> std::string { need(i + 1 < argc, "missing value"); return argv[++i]; };
            if (a == "--port") port = std::stoi(val()); else if (a == "--clients") clients = std::stoi(val()); else if (a == "--batch") batch = std::stoi(val());
            else if (a == "--seconds") seconds = std::stoi(val()); else if (a == "--mode") mode = val(); else if (a == "--record") record = val();
            else if (a == "--invalid") invalid = std::stod(val()); else if (a == "--graph") graph = val(); else if (a == "--cpus") cpus = parse_cpus(val());
            else throw std::runtime_error("unknown argument " + a);
        }
        if (mode == "verify") {
            Client c(port); std::ifstream f(record); std::string line; uint64_t ok = 0, bad = 0, total = 0; json misses = json::array();
            while (std::getline(f, line)) {
                if (line.empty()) continue;
                auto j = json::parse(line, nullptr, false);
                if (j.is_discarded()) continue;   // torn last client line
                total++;
                auto [st, body] = c.req("GET", "/labels?addr=" + j["address"].get<std::string>());
                auto l = json::parse(body, nullptr, false);
                bool good = st == 200 && !l.is_discarded() && l.contains("agent") && l["agent"]["wal_seq"].get<uint64_t>() >= 1;
                if (good) ok++; else { bad++; if (misses.size() < 10) misses.push_back(j); }
            }
            std::cout << json({{"mode", "verify"}, {"acked", total}, {"present_after_restart", ok}, {"missing", bad}, {"examples", misses}}).dump() << "\n";
            return bad ? 4 : 0;
        }
        std::atomic<bool> stop{false}; std::atomic<uint64_t> tasks{0}, accepted{0}, rejected{0}, missing{0}, leases{0}, props{0}, empty{0}, tool_calls{0};
        std::mutex mu; Lat lease_lat, prop_lat, tool_lat;
        std::ofstream rec; if (!record.empty()) rec.open(record, std::ios::app);
        Graph g; if (mode == "tools") { g.open(graph, "size", false); }
        auto t0 = mono_ns();
        std::vector<std::thread> ts;
        for (int c = 0; c < clients; c++) ts.emplace_back([&, c] {
            pin_thread(cpus, size_t(c));
            std::mt19937_64 rng(uint64_t(c) * 7919 + 17); Lat ll, pl, tl;
            try {
            Client cl(port);
            while (!stop.load()) {
                if (mode == "tools") {
                    uint32_t v = uint32_t(rng() % g.n), w = uint32_t(rng() % g.n);
                    // prefer active nodes for paths: walk one step from v when possible
                    auto o = g.out(v); if (o.len) { auto o2 = g.out(o.nb[0]); w = o2.len ? o2.nb[rng() % o2.len] : o.nb[0]; }
                    std::string a = hex0x(g.addr[v]), b = hex0x(g.addr[w]);
                    int k = int(rng() % 3); auto s = mono_ns();
                    if (k == 0) cl.req("GET", "/tools/neighbors?addr=" + a + "&dir=both&limit=16");
                    else if (k == 1) cl.req("GET", "/tools/feature_pack?addr=" + a);
                    else cl.req("GET", "/tools/path?from=" + a + "&to=" + b + "&max_depth=4");
                    tl.add(mono_ns() - s); tool_calls++;
                    continue;
                }
                auto s = mono_ns();
                auto [st, body] = cl.req("POST", "/tasks/lease", json({{"n", batch}, {"batch_size", batch}, {"task_type", "address_label"}, {"agent", "bench-" + std::to_string(c)}}).dump());
                ll.add(mono_ns() - s); leases++;
                auto j = json::parse(body, nullptr, false);
                if (st != 200 || j.is_discarded() || j["batches"].empty()) { empty++; std::this_thread::sleep_for(std::chrono::milliseconds(20)); continue; }
                for (auto& b : j["batches"]) {
                    std::string user = b["user"]; json r = json::array(); size_t k = 0;
                    std::vector<std::string> addrs;
                    for (auto& t : b["tasks"]) {
                        // hint=<category>/<conf>/<rule> on line S<k+1>
                        std::string tag = "S" + std::to_string(k + 1) + " "; auto pos = user.find("\n" + tag); pos = pos == std::string::npos ? user.find(tag) : pos + 1;
                        auto h = user.find("hint=", pos); auto sl = user.find('/', h);
                        std::string cat = user.substr(h + 5, sl - h - 5);
                        bool bad = std::uniform_real_distribution<double>(0, 1)(rng) < invalid;
                        json ev = json::array(); auto& keys = t["keys"]; for (size_t i = 0; i < std::min<size_t>(3, keys.size()); i++) ev.push_back(keys[i]);
                        if (bad) ev.push_back("NOPE");
                        r.push_back({int(k + 1), cat, 6, ev});
                        k++;
                    }
                    std::string out = json({{"r", r}}).dump();
                    auto s2 = mono_ns();
                    auto [st2, body2] = cl.req("POST", "/proposals", json({{"batch_id", b["batch_id"]}, {"provider", "bench"}, {"model", "mock-hint"}, {"usage", {{"prompt_tokens", b["est_input_tokens"]}, {"completion_tokens", 12 * k}}}, {"output", out}}).dump());
                    pl.add(mono_ns() - s2); props++;
                    auto v = json::parse(body2, nullptr, false);
                    if (st2 == 200 && !v.is_discarded()) {
                        tasks += k; accepted += v.value("accepted", 0); rejected += v.value("rejected", 0); missing += v.value("missing", 0);
                        if (rec.is_open()) { std::lock_guard<std::mutex> l(mu); for (auto& x : v["results"]) if (x["verdict"] == "accepted") rec << json({{"address", x["address"]}, {"category", x["category"]}}).dump() << "\n"; rec.flush(); }
                    }
                }
            }
            } catch (const std::exception& e) { std::lock_guard<std::mutex> l(mu); std::cerr << "client " << c << ": " << e.what() << "\n"; }
            std::lock_guard<std::mutex> l(mu); lease_lat.ns.insert(lease_lat.ns.end(), ll.ns.begin(), ll.ns.end()); prop_lat.ns.insert(prop_lat.ns.end(), pl.ns.begin(), pl.ns.end()); tool_lat.ns.insert(tool_lat.ns.end(), tl.ns.begin(), tl.ns.end());
        });
        std::this_thread::sleep_for(std::chrono::seconds(seconds)); stop = true;
        for (auto& t : ts) t.join();
        double secs = double(mono_ns() - t0) / 1e9;
        json o = {{"mode", mode}, {"clients", clients}, {"seconds", secs}};
        if (mode == "tools") { o["tool_calls"] = tool_calls.load(); o["calls_per_s"] = double(tool_calls.load()) / secs; o["client_latency"] = tool_lat.summary(); }
        else { o["batch"] = batch; o["tasks"] = tasks.load(); o["tasks_per_s"] = double(tasks.load()) / secs; o["accepted"] = accepted.load(); o["rejected"] = rejected.load(); o["missing"] = missing.load();
               o["leases"] = leases.load(); o["empty_leases"] = empty.load(); o["proposals"] = props.load(); o["lease_latency"] = lease_lat.summary(); o["propose_latency"] = prop_lat.summary(); }
        std::cout << o.dump() << "\n";
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << "\n"; return 1; }
}
