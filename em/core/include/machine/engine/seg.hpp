#pragma once
// Sealed V2 segments and build manifests (EM-ENGINE-V2 §5, §6).
//
// Segment file = payload bytes, then a 64-byte trailer ("EMV2SEG1"). The trailer-at-end layout lets
// builders stream payloads of unknown length while hashing the whole file in one pass, and keeps
// the payload page-aligned at offset 0 for mmap / io_uring reads. sha256 covers the whole file
// (payload + trailer) and is what the manifest and the Blob object name (<chain>/emv2/objects/<sha>)
// pin. Writes go tmp -> fdatasync -> rename -> directory fsync (graphloop durable.hpp helpers).
//
// A build directory holds the segments plus one manifest.<module>.json per builder module; the
// `emv2-manifest merge` tool (or merge_manifests()) verifies every file and writes manifest.json.
#include "machine/engine/api.hpp"
#include "machine/engine/coldmap.hpp"
#include "machine/graphloop/durable.hpp"
#include <cryptopp/sha.h>
#include <nlohmann/json.hpp>
#include <map>
#include <memory>

namespace machine::engine {
namespace fs = std::filesystem;
using json = nlohmann::json;
using machine::graphloop::need;

struct SegTrailer {
    uint64_t payload_bytes;   // bytes before the trailer
    uint64_t count;           // logical element count (records, values, events)
    uint64_t aux;             // kind-specific (documented with the Kind)
    uint64_t aux2;            // kind-specific
    uint32_t kind;            // Kind
    uint32_t elem;            // fixed record size in bytes, 0 = variable / codec stream
    uint32_t codec;           // Codec
    uint32_t version;         // seg_version
    char magic[8];            // "EMV2SEG1"
    uint8_t reserved[8];      // zero
};
static_assert(sizeof(SegTrailer) == 64);
inline constexpr char seg2_magic[8] = {'E', 'M', 'V', '2', 'S', 'E', 'G', '1'};

struct SegResult { std::string name; uint64_t bytes{}; std::string sha256; Kind kind{}; uint64_t count{}; uint32_t elem{}; Codec codec{}; };

// Streaming writer. Name is the file name inside the build directory (segment_file_name(kind[, part])).
class SegWriter {
    fs::path final_; std::string tmp_; int fd_{-1}; CryptoPP::SHA256 sha_; uint64_t payload_{};
    Kind kind_; uint32_t elem_; Codec codec_; Bytes buf_;
    void flush() { if (!buf_.empty()) { machine::graphloop::write_all(fd_, buf_.data(), buf_.size()); buf_.clear(); } }
    void raw(const void* p, size_t n) {
        sha_.Update(static_cast<const uint8_t*>(p), n);
        if (n >= (size_t(1) << 20)) { flush(); machine::graphloop::write_all(fd_, p, n); return; }
        auto b = static_cast<const uint8_t*>(p); buf_.insert(buf_.end(), b, b + n);
        if (buf_.size() >= (size_t(1) << 22)) flush();
    }
public:
    SegWriter(const fs::path& path, Kind kind, uint32_t elem = 0, Codec codec = Codec::raw)
        : final_(path), kind_(kind), elem_(elem), codec_(codec) {
        tmp_ = path.string() + ".tmp-XXXXXX";
        fd_ = ::mkostemp(tmp_.data(), O_CLOEXEC);
        need(fd_ >= 0, "segment temp open: " + path.filename().string());
        buf_.reserve(size_t(1) << 22);
    }
    ~SegWriter() { if (fd_ >= 0) { ::close(fd_); ::unlink(tmp_.c_str()); } }
    SegWriter(const SegWriter&) = delete;
    SegWriter& operator=(const SegWriter&) = delete;
    void put(const void* p, size_t n) { raw(p, n); payload_ += n; }
    void put(std::span<const uint8_t> s) { put(s.data(), s.size()); }
    template <class T> void put_records(std::span<const T> s) {
        need(elem_ == sizeof(T), "segment record size mismatch");
        put(s.data(), s.size_bytes());
    }
    uint64_t payload_bytes() const noexcept { return payload_; }
    // count = logical elements; for fixed-size records it must equal payload / elem.
    SegResult finish(uint64_t count, uint64_t aux = 0, uint64_t aux2 = 0) {
        need(!elem_ || payload_ == count * elem_, "segment length mismatch: " + final_.filename().string());
        SegTrailer t{}; t.payload_bytes = payload_; t.count = count; t.aux = aux; t.aux2 = aux2;
        t.kind = uint32_t(kind_); t.elem = elem_; t.codec = uint32_t(codec_); t.version = seg_version;
        std::memcpy(t.magic, seg2_magic, 8);
        raw(&t, sizeof t);
        flush();
        need(::fdatasync(fd_) == 0, "segment sync");
        ::close(fd_); fd_ = -1;
        need(::rename(tmp_.c_str(), final_.c_str()) == 0, "segment rename");
        machine::graphloop::sync_directory(final_.parent_path());
        uint8_t d[32]; sha_.Final(d);
        return {final_.filename().string(), payload_ + sizeof t, machine::graphloop::hex(View(d, 32)), kind_, count, elem_, codec_};
    }
};

// Read-only mmap of one segment. populate pre-faults the mapping (MAP_POPULATE).
class SegView {
    void* base_{MAP_FAILED}; size_t bytes_{}; SegTrailer t_{}; bool cold_{false};
    void release() noexcept {
        if (base_ == MAP_FAILED) return;
        if (cold_) cold::Mapper::get().unmap(base_); else ::munmap(base_, bytes_);
        base_ = MAP_FAILED;
    }
public:
    SegView() = default;
    SegView(const fs::path& p, Kind expect, bool populate = false, int advice = 0) {
        int fd = ::open(p.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        need(fd >= 0, "segment open: " + p.filename().string());
        struct stat st{};
        if (::fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < 64) { ::close(fd); throw std::runtime_error("segment type: " + p.filename().string()); }
        bytes_ = size_t(st.st_size);
        std::error_code ec;
        if (fs::exists(p.string() + ".cold", ec)) {   // cold tier (coldmap.hpp): sparse local cache filled by the fetcher on demand
            ::close(fd);
            base_ = cold::Mapper::get().map(p, bytes_); cold_ = true; populate = false; advice = 0;
        } else {
            base_ = ::mmap(nullptr, bytes_, PROT_READ, MAP_SHARED | (populate ? MAP_POPULATE : 0), fd, 0);
            ::close(fd);
        }
        need(base_ != MAP_FAILED, "segment mmap: " + p.filename().string());
        std::memcpy(&t_, static_cast<const uint8_t*>(base_) + bytes_ - 64, 64);
        need(std::memcmp(t_.magic, seg2_magic, 8) == 0 && t_.version == seg_version, "segment magic: " + p.filename().string());
        need(t_.payload_bytes + 64 == bytes_, "segment size: " + p.filename().string());
        need(t_.kind == uint32_t(expect), "segment kind: " + p.filename().string());
        need(!t_.elem || t_.payload_bytes == t_.count * t_.elem, "segment shape: " + p.filename().string());
        if (advice) ::madvise(base_, bytes_, advice);
    }
    ~SegView() { release(); }
    SegView(SegView&& o) noexcept : base_(o.base_), bytes_(o.bytes_), t_(o.t_), cold_(o.cold_) { o.base_ = MAP_FAILED; }
    SegView& operator=(SegView&& o) noexcept {
        if (this != &o) { release(); base_ = o.base_; bytes_ = o.bytes_; t_ = o.t_; cold_ = o.cold_; o.base_ = MAP_FAILED; }
        return *this;
    }
    bool cold() const noexcept { return cold_; }
    SegView(const SegView&) = delete;
    bool valid() const noexcept { return base_ != MAP_FAILED; }
    std::span<const uint8_t> payload() const noexcept { return {static_cast<const uint8_t*>(base_), size_t(t_.payload_bytes)}; }
    template <class T> std::span<const T> as() const {
        need(t_.elem == sizeof(T), "segment record type mismatch");
        return {reinterpret_cast<const T*>(base_), size_t(t_.count)};
    }
    const SegTrailer& trailer() const noexcept { return t_; }
    uint64_t count() const noexcept { return t_.count; }
    uint64_t aux() const noexcept { return t_.aux; }
    uint64_t aux2() const noexcept { return t_.aux2; }
    // Hint the kernel about a byte range (MADV_WILLNEED before a batch of reads, MADV_RANDOM, ...).
    void advise(size_t off, size_t len, int advice) const noexcept {
        if (cold_) return;
        size_t pg = 4096, a = off & ~(pg - 1);
        if (a < bytes_) ::madvise(static_cast<uint8_t*>(base_) + a, std::min(bytes_ - a, len + (off - a)), advice);
    }
};

inline std::string sha256_file(const fs::path& p) {
    int fd = ::open(p.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    need(fd >= 0, "hash open: " + p.filename().string());
    CryptoPP::SHA256 h; std::vector<uint8_t> buf(size_t(1) << 22);
    for (;;) {
        auto r = ::read(fd, buf.data(), buf.size());
        if (r < 0 && errno == EINTR) continue;
        if (r < 0) { ::close(fd); throw std::runtime_error("hash read"); }
        if (r == 0) break;
        h.Update(buf.data(), size_t(r));
    }
    ::close(fd);
    uint8_t d[32]; h.Final(d);
    return machine::graphloop::hex(View(d, 32));
}

// Inputs that determine a build (content signatures, not paths): the same inputs + tool version
// give the same build_id, and a rebuild must reproduce every file byte for byte.
struct BuildInput { std::string name; std::string sha256; uint64_t bytes{}; uint64_t rows{}; };

inline std::string build_id_of(const std::string& chain, const std::vector<BuildInput>& inputs) {
    json j; j["schema"] = std::string(manifest_schema); j["format_version"] = format_version; j["chain"] = chain;
    j["layouts"] = {{"seg", seg_version}, {"graph", std::string(layout_graph)}, {"ledger", layout_ledger}};
    json in = json::array();
    auto sorted = inputs; std::sort(sorted.begin(), sorted.end(), [](auto& a, auto& b) { return a.name < b.name; });
    for (auto& i : sorted) in.push_back({{"name", i.name}, {"sha256", i.sha256}, {"bytes", i.bytes}, {"rows", i.rows}});
    j["inputs"] = in;
    auto s = j.dump();
    return machine::graphloop::hex(View(machine::graphloop::sha256(s).data(), 32)).substr(0, 16);
}

// One builder module's part of a build: manifest.<module>.json in the build directory.
class PartManifest {
    std::string chain_, module_, build_id_; std::vector<BuildInput> inputs_; std::vector<SegResult> files_; json extra_ = json::object();
public:
    PartManifest(std::string chain, std::string module, std::string build_id) : chain_(std::move(chain)), module_(std::move(module)), build_id_(std::move(build_id)) {}
    void input(BuildInput i) { inputs_.push_back(std::move(i)); }
    void file(const SegResult& r) { files_.push_back(r); }
    json& extra() { return extra_; }   // module facts (row counts, check values); must be deterministic
    json to_json() const {
        json j; j["schema"] = std::string(manifest_schema); j["format_version"] = format_version; j["chain"] = chain_;
        j["module"] = module_; j["build_id"] = build_id_;
        json in = json::array(); for (auto& i : inputs_) in.push_back({{"name", i.name}, {"sha256", i.sha256}, {"bytes", i.bytes}, {"rows", i.rows}});
        j["inputs"] = in;
        json f = json::object();
        for (auto& r : files_) f[r.name] = {{"bytes", r.bytes}, {"sha256", r.sha256}, {"kind", std::string(kind_name(r.kind))}, {"count", r.count}, {"elem", r.elem}, {"codec", std::string(codec_name(r.codec))}};
        j["files"] = f; j["facts"] = extra_;
        return j;
    }
    void write(const fs::path& dir) const { machine::graphloop::replace_file(dir / ("manifest." + module_ + ".json"), to_json().dump(1) + "\n"); }
};

// Merge every manifest.<module>.json in dir into manifest.json after re-hashing each file.
// All parts must agree on chain and build_id; a file listed twice must have the same sha256.
inline json merge_manifests(const fs::path& dir, bool verify_hashes = true) {
    json out; out["schema"] = std::string(manifest_schema); out["format_version"] = format_version;
    json files = json::object(), modules = json::object(), inputs = json::array();
    std::vector<fs::path> parts;
    for (auto& e : fs::directory_iterator(dir)) {
        auto n = e.path().filename().string();
        if (n.rfind("manifest.", 0) == 0 && n != "manifest.json" && n.size() > 14 && n.substr(n.size() - 5) == ".json") parts.push_back(e.path());
    }
    std::sort(parts.begin(), parts.end());
    need(!parts.empty(), "no manifest parts");
    for (auto& p : parts) {
        json j = json::parse(machine::graphloop::load_text(p));
        need(j.value("schema", "") == manifest_schema, "manifest part schema");
        if (out.contains("chain")) need(out["chain"] == j["chain"] && out["build_id"] == j["build_id"], "manifest parts disagree on chain/build_id");
        out["chain"] = j["chain"]; out["build_id"] = j["build_id"];
        modules[j["module"].get<std::string>()] = j["facts"];
        for (auto& i : j["inputs"]) inputs.push_back(i);
        for (auto& [name, f] : j["files"].items()) {
            if (files.contains(name)) need(files[name]["sha256"] == f["sha256"], "file listed twice with different sha256: " + name);
            if (verify_hashes) {
                need(fs::file_size(dir / name) == f["bytes"].get<uint64_t>(), "file size mismatch: " + name);
                need(sha256_file(dir / name) == f["sha256"].get<std::string>(), "file sha256 mismatch: " + name);
            }
            files[name] = f;
        }
    }
    out["inputs"] = inputs; out["files"] = files; out["modules"] = modules;
    machine::graphloop::replace_file(dir / "manifest.json", out.dump(1) + "\n");
    return out;
}

// A sealed build opened for reading (serve, dump, verify). Finds segments by file name.
class BuildDir {
    fs::path dir_; json m_;
public:
    BuildDir() = default;
    explicit BuildDir(const fs::path& dir, bool verify_hashes = false) : dir_(dir) {
        m_ = json::parse(machine::graphloop::load_text(dir / "manifest.json"));
        need(m_.value("schema", "") == manifest_schema && m_.value("format_version", 0u) == format_version, "manifest schema/version");
        if (verify_hashes)
            for (auto& [name, f] : m_["files"].items()) need(sha256_file(dir / name) == f["sha256"].get<std::string>(), "file sha256 mismatch: " + name);
    }
    const fs::path& dir() const noexcept { return dir_; }
    const json& manifest() const noexcept { return m_; }
    std::string chain() const { return m_.value("chain", ""); }
    std::string build_id() const { return m_.value("build_id", ""); }
    bool has(const std::string& file) const { return m_["files"].contains(file); }
    bool has(Kind k, uint32_t part = 0) const { return has(segment_file_name(k, part)); }
    // Any part of a kind (part 0 "<name>.seg" or partitioned "<name>.NNN.seg"): modules that partition a kind (ledger p+1)
    // are found by this, not by has(kind) which looks for part 0 only.
    bool has_any(Kind k) const {
        std::string base(kind_name(k));
        for (auto& [name, f] : m_["files"].items()) {
            (void)f;
            if (name.size() > base.size() && name.compare(0, base.size(), base) == 0 && name[base.size()] == '.' && name.size() >= 4 &&
                name.compare(name.size() - 4, 4, ".seg") == 0) return true;
        }
        return false;
    }
    SegView open(Kind k, uint32_t part = 0, bool populate = false, int advice = 0) const {
        auto name = segment_file_name(k, part);
        need(has(name), "segment not in manifest: " + name);
        return SegView(dir_ / name, k, populate, advice);
    }
    // Facts a module recorded in its part manifest (row counts, check values).
    const json& facts(const std::string& module) const { static const json empty = json::object(); return m_["modules"].contains(module) ? m_["modules"][module] : empty; }
};

} // namespace machine::engine
