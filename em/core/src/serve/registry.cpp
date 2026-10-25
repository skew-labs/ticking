// Chain discovery and SIGHUP reload for emv2-serve. Layout (ops, emv2_fetch.py): <root>/<chain>/current -> builds/<build_id>/ with
// manifest.json. A build is opened completely before it replaces the previous one; a chain whose new build fails keeps serving
// the old build, and a chain whose current link disappeared is dropped.
#include <machine/engine/serve_json.hpp>
#include <machine/engine/serve_routes.hpp>
#include <machine/graphloop/durable.hpp>
#include <nlohmann/json.hpp>
#include <chrono>
#include <filesystem>

namespace machine::engine::serve {
namespace fs = std::filesystem;

StoreRef open_store(const std::string& dir) {
    auto m = nlohmann::json::parse(graphloop::load_text(fs::path(dir) / "manifest.json"));
    if (m.is_object() && m.value("kind", "") == "synth") return open_synth((fs::path(dir) / "manifest.json").string());
    return open_v2(dir);
}

std::string Registry::reload() {
    std::lock_guard<std::mutex> lk(reload_mu_);
    auto prev = snap_.load();
    auto next = std::make_shared<ChainMap>();
    Json log; log.ob().k("chains").ab();
    std::error_code ec;
    for (auto& ent : fs::directory_iterator(root_, ec)) {
        std::string chain = ent.path().filename().string();
        if (chain.empty() || chain[0] == '.' || !ent.is_directory()) continue;
        fs::path cur = ent.path() / link_;   // "current" in production; a staging instance reads "staged" (--link)
        std::error_code fe;
        if (!fs::exists(cur / "manifest.json", fe)) continue;
        auto old = prev->find(chain);
        log.ob().ks("chain", chain);
        try {
            StoreRef s;
            // same build id as the loaded one: keep the mapped store (SIGHUP for another chain must not remap this one)
            auto mj = nlohmann::json::parse(graphloop::load_text(cur / "manifest.json"));
            std::string bid = mj.value("build_id", "");
            int64_t stamp = 0;
            {
                std::error_code oe; auto ov = ent.path() / "overlay" / "overlay.json";
                if (fs::exists(ov, oe)) stamp = std::chrono::duration_cast<std::chrono::nanoseconds>(fs::last_write_time(ov, oe).time_since_epoch()).count();
            }
            if (old != prev->end() && old->second->build_id() == bid && old->second->overlay_stamp() == stamp) s = old->second;
            else { s = open_store(cur.string()); std::vector<TokenId> warm; s->find_tokens({}, 0, warm); }   // builds the symbol index
            graphloop::need(s->chain() == chain, "manifest chain " + s->chain() + " under " + chain);
            (*next)[chain] = s;
            log.ks("build", s->build_id()).ku("caps", s->caps()).kb("reused", old != prev->end() && old->second == s);
        } catch (const std::exception& e) {
            log.ks("error", e.what());
            if (old != prev->end()) { (*next)[chain] = old->second; log.ks("kept", old->second->build_id()); }
        }
        log.oe();
    }
    if (ec) log.ae().ks("error", ec.message()).oe(); else log.ae().oe();
    snap_.store(std::shared_ptr<const ChainMap>(std::move(next)));
    return log.s;
}
} // namespace machine::engine::serve
