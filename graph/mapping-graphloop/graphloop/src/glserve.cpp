// glserve: graph-loop tool API for the LLM dispatcher (HTTP/1.1 JSON on 127.0.0.1 / Unix socket).
//   glserve --data /data/graphloop/data/robinhood --state /data/graphloop/state/robinhood
//           --port 8710 [--unix /run/graphloop.sock] [--workers 4] [--cpus 0-7] [--context agent_context.md]
// Startup: writer lock -> graph (mmap, manifest-verified) -> snapshot + WAL replay (torn tail
// repaired) -> deterministic propagation -> frontier fill -> serve.  SIGTERM: snapshot + exit.
#include "machine/graphloop/engine.hpp"
#include "machine/graphloop/http.hpp"
#include <csignal>
#include <iostream>

using namespace machine::graphloop;

namespace {
std::atomic<bool> g_stop{false};
void on_signal(int) { g_stop = true; }

struct Server {
    fs::path data, graph_dir, state; std::string host = "127.0.0.1", unix_path, verify = "size", context_path; int port = 8710, workers = 4, threads = 8;
    std::vector<int> cpus; bool populate = false, ram = false; ClassifyParams cp; EngineParams ep; Wal::Options wo;
    Graph g; std::unique_ptr<Engine> eng; Wal wal; std::string context; std::mutex snap_mu;
    std::atomic<uint64_t> tool_ns_total{0}; std::array<std::atomic<uint64_t>, 64> lat_hist{};   // log2(ns) histogram for tool calls

    void snapshot(const char* why) {
        std::lock_guard<std::mutex> sl(snap_mu);
        std::vector<std::pair<uint8_t, std::string>> recs; uint64_t seq; std::vector<fs::path> old;
        {   // short critical section: fix S and rotate; records are read afterwards (replay is idempotent per node seq)
            std::lock_guard<std::mutex> l(eng->commit_mutex());
            seq = wal.last_seq(); old = wal.rotate();
        }
        recs = eng->snapshot_records();
        uint64_t t0 = mono_ns();
        write_snapshot(state / "snap", seq, recs, g.graph_id, {{"reason", why}, {"commits_since_last", eng->applied_since_snapshot()}});
        for (auto& p : old) fs::remove(p);
        sync_directory(state / "wal");
        // keep the two newest snapshots
        std::vector<fs::path> snaps; for (auto& e : fs::directory_iterator(state / "snap")) { auto s = e.path().filename().string(); if (s.rfind("snap-", 0) == 0 && s.ends_with(".json")) snaps.push_back(e.path()); }
        std::sort(snaps.begin(), snaps.end());
        for (size_t i = 0; i + 2 < snaps.size(); i++) { auto bin = snaps[i]; bin.replace_extension(".bin"); fs::remove(snaps[i]); fs::remove(bin); }
        eng->last_snapshot_seq = seq; eng->last_snapshot_ms = now_ms(); eng->snapshot_taken();
        log_event("snapshot", "\"seq\":" + std::to_string(seq) + ",\"records\":" + std::to_string(recs.size()) + ",\"reason\":" + jstr(why) + ",\"wal_files_removed\":" + std::to_string(old.size()) + ",\"ms\":" + std::to_string((mono_ns() - t0) / 1000000));
    }

    void open() {
        if (graph_dir.empty()) { auto cur = trim(load_text(data / "CURRENT")); graph_dir = data / cur; }
        fs::create_directories(state);
        uint64_t t0 = mono_ns();
        g.set_ram(ram);
        g.open(graph_dir, verify, populate);
        uint64_t t1 = mono_ns();
        eng = std::make_unique<Engine>(g, cp, ep);
        if (!context_path.empty()) { context = load_text(context_path); auto h = sha256(context); eng->context_sha = hex(View(h.data(), 32)); }
        uint64_t snap_seq = 0, snap_recs = 0;
        if (auto s = latest_snapshot(state / "snap")) { snap_recs = read_snapshot(s->file, [&](uint8_t k, Dec& d) { eng->load_snapshot_record(k, d); }); snap_seq = s->seq; }
        eng->last_snapshot_seq = snap_seq;
        eng->replaying = true;
        uint64_t replayed = wal.open(state / "wal", g.graph_id, snap_seq, wo, [&](uint64_t seq, uint64_t, uint8_t kind, Dec& d) { if (kind == 6) eng->replay_proposal(d, seq); });
        eng->replaying = false;
        eng->wal = &wal;
        uint64_t t2 = mono_ns();
        eng->propagate_all();
        eng->fill_frontier(threads);
        eng->async_propagation = true; eng->start_propagation();
        eng->snapshot_taken();
        if (replayed) { try { snapshot("recovery"); } catch (const std::exception& e) { log_event("snapshot_failed", "\"reason\":" + jstr(e.what())); } }
        uint64_t t3 = mono_ns();
        log_event("ready", "\"graph\":" + jstr(graph_dir.string()) + ",\"nodes\":" + std::to_string(g.n) + ",\"edges\":" + std::to_string(g.m) +
                  ",\"mapped_bytes\":" + std::to_string(g.mapped_bytes()) + ",\"open_ms\":" + std::to_string((t1 - t0) / 1000000) +
                  ",\"snapshot_seq\":" + std::to_string(snap_seq) + ",\"snapshot_records\":" + std::to_string(snap_recs) + ",\"wal_replayed\":" + std::to_string(replayed) +
                  ",\"recover_ms\":" + std::to_string((t2 - t1) / 1000000) + ",\"frontier_ms\":" + std::to_string((t3 - t2) / 1000000));
    }

    static HttpResponse ok(const json& j) { return {200, j.dump(), "application/json"}; }
    static HttpResponse err(int s, const std::string& code) { return {s, json({{"error", code}}).dump(), "application/json"}; }

    std::shared_ptr<Batch> batch_of(const std::string& id) { if (id.empty()) return nullptr; try { return eng->find_batch(std::stoull(id)); } catch (...) { return nullptr; } }

    void handle(HttpRequest& rq, HttpServer::Reply rep) {
        const auto& p = rq.path;
        auto t0 = mono_ns();
        auto tool_done = [&](HttpResponse r) {
            uint64_t ns = mono_ns() - t0; tool_ns_total += ns; int b = 63 - __builtin_clzll(ns | 1); lat_hist[size_t(b)]++; rep(std::move(r));
        };
        if (p == "/health") return rep(ok({{"ok", true}}));
        if (p == "/status") {
            auto s = eng->status();
            uint64_t tot = 0; for (auto& h : lat_hist) tot += h.load();
            auto pct = [&](double q) { uint64_t acc = 0, want = uint64_t(q * double(tot)); for (size_t i = 0; i < lat_hist.size(); i++) { acc += lat_hist[i].load(); if (acc > want) return double(uint64_t(1) << (i + 1)) / 1000.0; } return 0.0; };
            s["tool_latency_us"] = {{"calls", tot}, {"p50_upper", pct(0.5)}, {"p99_upper", pct(0.99)}, {"mean", tot ? double(tool_ns_total.load()) / tot / 1000.0 : 0.0}};
            return rep(ok(s));
        }
        if (p == "/coverage") return rep(ok(eng->coverage(threads)));
        if (p == "/context") return rep({200, context, "text/markdown; charset=utf-8"});
        if (p == "/labels") {
            uint32_t v = g.find(qparam(rq.query, "addr"));
            if (v == none) return rep(err(404, "ADDRESS_NOT_IN_GRAPH"));
            return rep(ok(eng->label_json(v)));
        }
        if (p == "/tasks/lease") {
            if (rq.method != "POST") return rep(err(405, "POST_REQUIRED"));
            auto j = json::parse(rq.body.empty() ? "{}" : rq.body, nullptr, false); if (j.is_discarded()) return rep(err(400, "BODY_NOT_JSON"));
            std::string tt = j.value("task_type", "address_label"); uint8_t type = tt == "protocol_discovery" ? t_protocol : t_address;
            if (tt != "address_label" && tt != "protocol_discovery") return rep(err(400, "TASK_TYPE"));
            size_t n = j.value("n", 15), bs = j.value("batch_size", n);
            return rep(ok(eng->lease(n, type, bs, j.value("ttl_s", 0u), j.value("agent", std::string()))));
        }
        if (p == "/tasks/release") {
            auto j = json::parse(rq.body.empty() ? "{}" : rq.body, nullptr, false); if (j.is_discarded()) return rep(err(400, "BODY_NOT_JSON"));
            try { eng->release_batch(std::stoull(j.value("batch_id", std::string("0")))); } catch (...) { return rep(err(400, "BATCH_ID")); }
            return rep(ok({{"released", true}}));
        }
        if (p == "/proposals") {
            if (rq.method != "POST") return rep(err(405, "POST_REQUIRED"));
            auto j = json::parse(rq.body, nullptr, false); if (j.is_discarded() || !j.is_object()) return rep(err(400, "BODY_NOT_JSON"));
            std::string bid = j.contains("batch_id") ? (j["batch_id"].is_string() ? j["batch_id"].get<std::string>() : std::to_string(j["batch_id"].get<uint64_t>())) : "";
            if (bid.empty() && j.contains("task_id") && j["task_id"].is_string()) bid = j["task_id"].get<std::string>().substr(0, j["task_id"].get<std::string>().find('.'));
            uint64_t id = 0; try { id = std::stoull(bid); } catch (...) { return rep(err(400, "BATCH_ID")); }
            if (!j.contains("output")) return rep(err(400, "OUTPUT_MISSING"));
            eng->propose(id, j.value("provider", std::string("unknown")), j.value("model", std::string("unknown")), j.value("usage", json::object()), j["output"],
                         [rep](int status, json body) { rep({status, body.dump(), "application/json"}); });
            return;
        }
        if (p.rfind("/tools/", 0) == 0) {
            auto b = batch_of(qparam(rq.query, "batch"));
            auto node_of = [&](const char* k) { std::string r = qparam(rq.query, k); if (r.empty() && std::string(k) == "ref") r = qparam(rq.query, "addr"); return eng->resolve_ref(r, b.get()); };
            if (p == "/tools/neighbors") {
                uint32_t v = node_of("ref"); if (v == none) return tool_done(err(404, "REF_NOT_FOUND"));
                return tool_done(ok(eng->neighbors(b.get(), v, qparam(rq.query, "dir", "both"), Engine::types_mask(qparam(rq.query, "types")), size_t(std::stoul(qparam(rq.query, "limit", "16"))))));
            }
            if (p == "/tools/path") {
                uint32_t s = node_of("from"), t = node_of("to"); if (s == none || t == none) return tool_done(err(404, "REF_NOT_FOUND"));
                return tool_done(ok(eng->path(b.get(), s, t, std::stoi(qparam(rq.query, "max_depth", "4")), Engine::types_mask(qparam(rq.query, "types")), qparam(rq.query, "undirected", "0") == "1")));
            }
            if (p == "/tools/feature_pack") {
                uint32_t v = node_of("ref"); if (v == none) return tool_done(err(404, "REF_NOT_FOUND"));
                return tool_done(ok(eng->feature_pack(b.get(), v, qparam(rq.query, "task_type") == "protocol_discovery" ? t_protocol : t_address)));
            }
            return rep(err(404, "NO_SUCH_TOOL"));
        }
        if (p == "/admin/snapshot") { snapshot("admin"); return rep(ok({{"seq", eng->last_snapshot_seq.load()}})); }
        if (p == "/admin/sample") {   // top-k nodes (by importance) whose current label has the given category
            int c = category_code(qparam(rq.query, "category", "unknown")); if (c < 0) return rep(err(400, "CATEGORY"));
            size_t k = size_t(std::stoul(qparam(rq.query, "k", "20")));
            std::vector<std::pair<float, uint32_t>> best;
            for (uint32_t v = 0; v < g.n; v++) if (eng->label(v).cat == c) { best.push_back({g.f32[d_imp][v], v}); if (best.size() > 4 * k + 1024) { std::nth_element(best.begin(), best.begin() + long(k), best.end(), std::greater<>()); best.resize(k); } }
            std::sort(best.begin(), best.end(), std::greater<>()); if (best.size() > k) best.resize(k);
            json a = json::array(); for (auto& [imp, v] : best) { auto j = eng->label_json(v); j["sweep_in"] = g.sparse(v, k_sweep); j["tx_sent"] = g.u32[d_tx_sent][v]; j["tx_recv"] = g.u32[d_tx_recv][v]; j["outdeg"] = g.u32[d_outdeg][v]; j["indeg"] = g.u32[d_indeg][v]; a.push_back(j); }
            return rep(ok(a));
        }
        if (p == "/admin/import_labels") {
            if (rq.method != "POST") return rep(err(405, "POST_REQUIRED"));
            eng->import_labels(rq.body, [rep](int status, json body) { rep({status, body.dump(), "application/json"}); }, qparam(rq.query, "dry_run") == "1");
            return;
        }
        if (p == "/admin/hubs") {   // top-k by distinct counterparties, with code (base) and current labels
            size_t k = size_t(std::stoul(qparam(rq.query, "k", "20")));
            std::vector<std::pair<uint32_t, uint32_t>> best; best.reserve(4 * k + 1024);
            for (uint32_t v = 0; v < g.n; v++) { best.push_back({g.u32[d_cp][v], v}); if (best.size() > 4 * k + 1024) { std::nth_element(best.begin(), best.begin() + long(k), best.end(), std::greater<>()); best.resize(k); } }
            std::sort(best.begin(), best.end(), std::greater<>()); if (best.size() > k) best.resize(k);
            json a = json::array();
            for (auto& [cpn, v] : best) { auto j = eng->label_json(v); j["counterparties"] = cpn; j["tx_sent"] = g.u32[d_tx_sent][v]; j["tx_recv"] = g.u32[d_tx_recv][v];
                j["out_degree"] = g.u32[d_outdeg][v]; j["in_degree"] = g.u32[d_indeg][v]; j["logs"] = g.sparse(v, k_logs); a.push_back(j); }
            return rep(ok(a));
        }
        if (p == "/admin/frontier_top") {
            auto top = eng->fr[qparam(rq.query, "task_type") == "protocol_discovery" ? 1 : 0].top(size_t(std::stoul(qparam(rq.query, "k", "20"))));
            json a = json::array(); for (auto& [v, pr] : top) { auto j = eng->label_json(v); j["priority"] = pr; a.push_back(j); }
            return rep(ok(a));
        }
        return rep(err(404, "NOT_FOUND"));
    }

    int run() {
        fs::create_directories(state);
        WriterLock lock(state / "writer.lock");
        open();
        HttpServer srv(host, port, unix_path, workers, cpus, [this](HttpRequest& rq, HttpServer::Reply r) { handle(rq, r); });
        srv.start();
        log_event("listening", "\"host\":" + jstr(host) + ",\"port\":" + std::to_string(port) + ",\"unix\":" + jstr(unix_path) + ",\"workers\":" + std::to_string(workers));
        uint64_t last_snap = now_ms();
        while (!g_stop.load()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
            uint32_t now_s = uint32_t(now_ms() / 1000);
            eng->fr[0].expire(now_s); eng->fr[1].expire(now_s); eng->expire_batches(now_s);
            uint64_t applied = eng->applied_since_snapshot();
            if (applied >= ep.snapshot_every || (applied > 0 && now_ms() - last_snap >= uint64_t(ep.snapshot_secs) * 1000)) {
                try { snapshot("periodic"); } catch (const std::exception& e) { log_event("snapshot_failed", "\"reason\":" + jstr(e.what())); }
                last_snap = now_ms();
            }
        }
        srv.stop();
        eng->stop_propagation();
        try { snapshot("shutdown"); } catch (const std::exception& e) { log_event("snapshot_failed", "\"reason\":" + jstr(e.what())); }
        wal.close();
        log_event("stopped");
        return 0;
    }
};
} // namespace

int main(int argc, char** argv) {
    try {
        ::umask(0077);
        std::signal(SIGTERM, on_signal); std::signal(SIGINT, on_signal); std::signal(SIGPIPE, SIG_IGN);
        Server s;
        for (int i = 1; i < argc; i++) {
            std::string a = argv[i];
            auto val = [&]() -> std::string { need(i + 1 < argc, "missing value for " + a); return argv[++i]; };
            if (a == "--data") s.data = val();
            else if (a == "--graph") s.graph_dir = val();
            else if (a == "--state") s.state = val();
            else if (a == "--host") s.host = val();
            else if (a == "--port") s.port = std::stoi(val());
            else if (a == "--unix") s.unix_path = val();
            else if (a == "--workers") s.workers = std::stoi(val());
            else if (a == "--threads") s.threads = std::stoi(val());
            else if (a == "--cpus") s.cpus = parse_cpus(val());
            else if (a == "--context") s.context_path = val();
            else if (a == "--verify") s.verify = val();
            else if (a == "--populate") s.populate = true;
            else if (a == "--ram") s.ram = true;
            else if (a == "--ttl") s.ep.ttl_s = uint32_t(std::stoul(val()));
            else if (a == "--resolve") s.ep.resolve = std::stod(val());
            else if (a == "--llm-quantile") s.ep.llm_quantile = std::stod(val());
            else if (a == "--llm-importance-min") s.ep.llm_imp_min = std::stof(val());
            else if (a == "--confirm-quantile") s.ep.confirm_quantile = std::stod(val());
            else if (a == "--confirm-importance-min") s.ep.confirm_imp_min = std::stof(val());
            else if (a == "--protocol-quantile") s.ep.proto_quantile = std::stod(val());
            else if (a == "--max-batch") s.ep.max_batch = uint32_t(std::stoul(val()));
            else if (a == "--snapshot-every") s.ep.snapshot_every = uint32_t(std::stoul(val()));
            else if (a == "--snapshot-secs") s.ep.snapshot_secs = uint32_t(std::stoul(val()));
            else if (a == "--fsync-batch-us") s.wo.batch_us = uint32_t(std::stoul(val()));
            else if (a == "--no-fsync") s.wo.fsync = false;
            else throw std::runtime_error("unknown argument " + a);
        }
        need((!s.data.empty() || !s.graph_dir.empty()) && !s.state.empty(), "usage: glserve --data DIR|--graph DIR --state DIR [--port N] [--unix PATH]");
        need(s.host == "127.0.0.1" || s.host == "::1" || s.host == "localhost" || std::getenv("GRAPHLOOP_ALLOW_NONLOCAL"), "refusing non-loopback listen address");
        if (s.host == "localhost") s.host = "127.0.0.1";
        return s.run();
    } catch (const std::exception& e) { std::cerr << e.what() << "\n"; return 1; }
}
