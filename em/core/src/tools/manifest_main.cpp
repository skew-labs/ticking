// emv2-manifest: build-directory manifest tool (core).
//   emv2-manifest build-id <chain> <inputs.json>      print build_id for [{name,sha256,bytes,rows}]
//   emv2-manifest merge <build_dir> [--no-verify]     verify parts, write manifest.json, print summary
//   emv2-manifest verify <build_dir>                  re-hash every file listed in manifest.json
//   emv2-manifest kinds                               print the Kind catalogue (name, number, tier)
#include "machine/engine/seg.hpp"
#include <iostream>

using namespace machine::engine;

int main(int argc, char** argv) {
    try {
        std::vector<std::string> a(argv + 1, argv + argc);
        need(!a.empty(), "usage: emv2-manifest build-id|merge|verify|kinds ...");
        if (a[0] == "kinds") {
            json j = json::array();
            for (auto& k : kind_names) j.push_back({{"name", std::string(k.name)}, {"kind", uint32_t(k.kind)}, {"tier", kind_tier(k.kind) == Tier::hot ? "hot" : "cold"}, {"file", segment_file_name(k.kind)}});
            std::cout << j.dump(1) << "\n";
            return 0;
        }
        if (a[0] == "build-id") {
            need(a.size() == 3, "usage: emv2-manifest build-id <chain> <inputs.json>");
            json in = json::parse(machine::graphloop::load_text(a[2]));
            std::vector<BuildInput> v;
            for (auto& i : in) v.push_back({i.at("name"), i.at("sha256"), i.value("bytes", uint64_t(0)), i.value("rows", uint64_t(0))});
            std::cout << build_id_of(a[1], v) << "\n";
            return 0;
        }
        if (a[0] == "merge") {
            need(a.size() >= 2, "usage: emv2-manifest merge <build_dir> [--no-verify]");
            bool verify = !(a.size() == 3 && a[2] == "--no-verify");
            auto m = merge_manifests(a[1], verify);
            uint64_t bytes = 0; for (auto& [n, f] : m["files"].items()) bytes += f["bytes"].get<uint64_t>();
            std::cout << json{{"chain", m["chain"]}, {"build_id", m["build_id"]}, {"files", m["files"].size()}, {"bytes", bytes}, {"verified", verify}}.dump() << "\n";
            return 0;
        }
        if (a[0] == "verify") {
            need(a.size() == 2, "usage: emv2-manifest verify <build_dir>");
            BuildDir b(a[1], true);
            std::cout << json{{"chain", b.chain()}, {"build_id", b.build_id()}, {"files", b.manifest()["files"].size()}, {"ok", true}}.dump() << "\n";
            return 0;
        }
        throw std::runtime_error("unknown command");
    } catch (const std::exception& e) {
        std::cerr << "emv2-manifest: " << e.what() << "\n";
        return 1;
    }
}
