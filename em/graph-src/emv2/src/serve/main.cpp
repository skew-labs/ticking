// emv2-serve: one process for every chain under --root (design §8). graphloop's epoll HTTP server, loopback only, workers pinned to
// --cpus; segments are mmapped read-only. SIGHUP re-reads <root>/<chain>/current/manifest.json; SIGTERM stops. After the listener
// is bound and the first builds are open, Landlock (read-only --root) and seccomp (serve_sandbox.hpp) are installed before the
// workers start.
//   emv2-serve --root /srv/skew/emv2 --host 127.0.0.1 --port 8740 --workers 4 --cpus 4,5,6,7 [--budget-ms 1000] [--no-sandbox]
#include <machine/engine/serve_routes.hpp>
#include <machine/engine/serve_sandbox.hpp>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

using namespace machine::engine::serve;
using machine::graphloop::HttpRequest;
using machine::graphloop::HttpResponse;
using machine::graphloop::HttpServer;
using machine::graphloop::jstr;
using machine::graphloop::log_event;
using machine::graphloop::mono_ns;

namespace {
struct Args {
    std::string root = "/srv/skew/emv2", host = "127.0.0.1";
    int port = 8740, workers = 4;
    std::vector<int> cpus;
    uint64_t budget_ms = 1000;
    bool sandbox = true;
};
Args parse(int argc, char** argv) {
    Args a;
    for (int i = 1; i < argc; i++) {
        std::string k = argv[i];
        auto val = [&]() -> std::string { if (i + 1 >= argc) { std::fprintf(stderr, "missing value for %s\n", k.c_str()); std::exit(2); } return argv[++i]; };
        if (k == "--root") a.root = val();
        else if (k == "--host") a.host = val();
        else if (k == "--port") a.port = std::stoi(val());
        else if (k == "--workers") a.workers = std::max(1, std::min(32, std::stoi(val())));
        else if (k == "--cpus") { std::string s = val(); size_t p = 0; while (p < s.size()) { size_t z = s.find(',', p); if (z == std::string::npos) z = s.size(); a.cpus.push_back(std::stoi(s.substr(p, z - p))); p = z + 1; } }
        else if (k == "--budget-ms") a.budget_ms = std::stoull(val());
        else if (k == "--no-sandbox") a.sandbox = false;
        else { std::fprintf(stderr, "unknown argument %s\n", k.c_str()); std::exit(2); }
    }
    if (a.host != "127.0.0.1" && a.host != "::1") { std::fprintf(stderr, "emv2-serve listens on loopback only\n"); std::exit(2); }
    return a;
}
// invscreen adds elapsed_ms to every object it answers
void add_elapsed(HttpResponse& r, uint64_t t0) {
    if (r.body.size() < 2 || r.body.front() != '{' || r.body.back() != '}') return;
    std::string tail = "\"elapsed_ms\":" + std::to_string((mono_ns() - t0) / 1000000) + "}";
    r.body.pop_back();
    if (r.body.size() > 1) r.body += ',';
    r.body += tail;
}
} // namespace

int main(int argc, char** argv) {
    Args a = parse(argc, argv);
    sigset_t set; sigemptyset(&set); sigaddset(&set, SIGHUP); sigaddset(&set, SIGTERM); sigaddset(&set, SIGINT);
    pthread_sigmask(SIG_BLOCK, &set, nullptr);   // before any thread: workers inherit the mask, main waits for the signals
    std::signal(SIGPIPE, SIG_IGN);
    Registry reg(a.root);
    log_event("loaded", "\"root\":" + jstr(a.root) + ",\"result\":" + reg.reload());
    const uint64_t budget_ns = a.budget_ms * 1000000ULL;
    HttpServer srv(a.host, a.port, "", a.workers, a.cpus, [&](HttpRequest& rq, HttpServer::Reply rep) {
        uint64_t t0 = mono_ns();
        Budget b; b.deadline_ns = t0 + budget_ns;
        auto snap = reg.get();
        HttpResponse r = handle(*snap, rq, b);
        add_elapsed(r, t0);
        uint64_t ms = (mono_ns() - t0) / 1000000;
        if (r.status >= 500 && r.status != 501) log_event("error", "\"path\":" + jstr(rq.path) + ",\"status\":" + std::to_string(r.status));
        else if (ms >= 100) log_event("slow", "\"path\":" + jstr(rq.path) + ",\"ms\":" + std::to_string(ms));
        rep(std::move(r));
    });
    if (a.sandbox) {
        int abi = landlock_read_only(a.root);
        bool sc = seccomp_serve();
        log_event("sandbox", "\"landlock_abi\":" + std::to_string(abi) + ",\"seccomp\":" + (sc ? "true" : "false"));
        if (!sc) { std::fprintf(stderr, "seccomp could not be installed; refusing to serve without it (use --no-sandbox for tests)\n"); return 3; }
    }
    srv.start();
    log_event("listening", "\"host\":" + jstr(a.host) + ",\"port\":" + std::to_string(a.port) + ",\"workers\":" + std::to_string(a.workers));
    for (;;) {
        siginfo_t si;
        timespec ts{1, 0};
        int s = sigtimedwait(&set, &si, &ts);
        if (s == SIGHUP) log_event("reload", "\"result\":" + reg.reload());
        else if (s == SIGTERM || s == SIGINT) break;
    }
    srv.stop();
    log_event("stopped");
    return 0;
}
