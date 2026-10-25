#pragma once
// emv2-serve routes: the bridge's public paths (/api/public/em/...) answered from EMV2 builds in the JSON shape the Python services
// give today, so the bridge's shadow mode can compare field by field. A path, chain or field this phase cannot produce answers 501
// {"error":"unsupported"} (the shadow log counts it as unsupported, not as a difference).
#include <machine/engine/serve_store.hpp>
#include <machine/graphloop/http.hpp>
#include <atomic>
#include <map>
#include <memory>
#include <mutex>
#include <string>

namespace machine::engine::serve {

using ChainMap = std::map<std::string, StoreRef>;

// The loaded builds, swapped as a whole on SIGHUP. Readers take one snapshot per request.
class Registry {
public:
    explicit Registry(std::string root) : root_(std::move(root)) { snap_.store(std::make_shared<const ChainMap>()); }
    // Scans <root>/<chain>/current/manifest.json. A chain whose build fails to open keeps its previous build.
    // Returns a one-line JSON summary for the log.
    std::string reload();
    std::shared_ptr<const ChainMap> get() const { return snap_.load(); }
    const std::string& root() const { return root_; }
private:
    std::string root_;
    std::atomic<std::shared_ptr<const ChainMap>> snap_;
    std::mutex reload_mu_;
};

// Per-request limits (design §8 safety): the bridge gives up after 1.5 s, so a request stops itself at `deadline_ns`.
struct Budget {
    uint64_t deadline_ns = 0;
    size_t max_nodes = 4096;
    bool expired() const { return deadline_ns && graphloop::mono_ns() > deadline_ns; }
};

graphloop::HttpResponse handle(const ChainMap& chains, const graphloop::HttpRequest& rq, Budget& budget);

// Exposed for tests: Python-compatible pieces of the invscreen answers.
std::string kind_of(const std::string& category, const std::string& cls);
} // namespace machine::engine::serve
