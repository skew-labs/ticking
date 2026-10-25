#pragma once
// Apache Arrow / Parquet access for glbuild and glexport.  Columns are consumed straight from the
// Arrow buffers (no per-row materialisation); only projected columns are decoded, one row group at
// a time so memory stays bounded per file.
#include "durable.hpp"
#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/reader.h>
#include <parquet/arrow/writer.h>
#include <parquet/properties.h>
#include <filesystem>
#include <memory>

namespace machine::graphloop {

struct Col {
    std::shared_ptr<arrow::Array> hold; const arrow::Array* a{}; arrow::Type::type t{arrow::Type::NA}; int32_t scale{}; int64_t ts_div{1};
    explicit operator bool() const { return a != nullptr; }
    bool null(int64_t i) const { return !a || a->IsNull(i); }
    uint64_t u64(int64_t i) const {
        if (null(i)) return 0;
        switch (t) {
        case arrow::Type::UINT64: return static_cast<const arrow::UInt64Array*>(a)->Value(i);
        case arrow::Type::INT64: { auto v = static_cast<const arrow::Int64Array*>(a)->Value(i); return v < 0 ? 0 : uint64_t(v); }
        case arrow::Type::UINT32: return static_cast<const arrow::UInt32Array*>(a)->Value(i);
        case arrow::Type::INT32: { auto v = static_cast<const arrow::Int32Array*>(a)->Value(i); return v < 0 ? 0 : uint64_t(v); }
        case arrow::Type::UINT16: return static_cast<const arrow::UInt16Array*>(a)->Value(i);
        case arrow::Type::INT16: { auto v = static_cast<const arrow::Int16Array*>(a)->Value(i); return v < 0 ? 0 : uint64_t(v); }
        case arrow::Type::UINT8: return static_cast<const arrow::UInt8Array*>(a)->Value(i);
        case arrow::Type::INT8: { auto v = static_cast<const arrow::Int8Array*>(a)->Value(i); return v < 0 ? 0 : uint64_t(v); }
        case arrow::Type::BOOL: return static_cast<const arrow::BooleanArray*>(a)->Value(i);
        default: { double d = f64(i); return d <= 0 ? 0 : d >= 1.8e19 ? UINT64_MAX : uint64_t(d); }
        }
    }
    double f64(int64_t i) const {
        if (null(i)) return 0;
        switch (t) {
        case arrow::Type::DOUBLE: return static_cast<const arrow::DoubleArray*>(a)->Value(i);
        case arrow::Type::FLOAT: return static_cast<const arrow::FloatArray*>(a)->Value(i);
        case arrow::Type::DECIMAL128: return arrow::Decimal128(static_cast<const arrow::Decimal128Array*>(a)->GetValue(i)).ToDouble(scale);
        case arrow::Type::DECIMAL256: return arrow::Decimal256(static_cast<const arrow::Decimal256Array*>(a)->GetValue(i)).ToDouble(scale);
        case arrow::Type::STRING: case arrow::Type::LARGE_STRING: { auto s = bytes(i); return std::strtod(std::string(s).c_str(), nullptr); }
        default: return double(u64(i));
        }
    }
    std::string_view bytes(int64_t i) const {
        if (null(i)) return {};
        switch (t) {
        case arrow::Type::BINARY: return static_cast<const arrow::BinaryArray*>(a)->GetView(i);
        case arrow::Type::LARGE_BINARY: return static_cast<const arrow::LargeBinaryArray*>(a)->GetView(i);
        case arrow::Type::FIXED_SIZE_BINARY: return static_cast<const arrow::FixedSizeBinaryArray*>(a)->GetView(i);
        case arrow::Type::STRING: return static_cast<const arrow::StringArray*>(a)->GetView(i);
        case arrow::Type::LARGE_STRING: return static_cast<const arrow::LargeStringArray*>(a)->GetView(i);
        case arrow::Type::BINARY_VIEW: return static_cast<const arrow::BinaryViewArray*>(a)->GetView(i);
        case arrow::Type::STRING_VIEW: return static_cast<const arrow::StringViewArray*>(a)->GetView(i);
        default: return {};
        }
    }
    bool b(int64_t i) const { return u64(i) != 0; }
    // timestamp / integer seconds -> unix seconds
    uint64_t ts_s(int64_t i) const {
        if (null(i)) return 0;
        if (t == arrow::Type::TIMESTAMP) { auto v = static_cast<const arrow::TimestampArray*>(a)->Value(i); return v <= 0 ? 0 : uint64_t(v / ts_div); }
        uint64_t v = u64(i); return v > 100000000000ULL ? v / 1000 : v;
    }
    // 20-byte address from binary(20) or "0x" hex text; false when absent/malformed.
    bool addr(int64_t i, Addr& out) const {
        auto s = bytes(i);
        if (s.size() == 20) { std::memcpy(out.b, s.data(), 20); return true; }
        if (s.size() == 42 || s.size() == 40) return parse_addr(s, out);
        return false;
    }
};

class PqReader {
    std::unique_ptr<parquet::arrow::FileReader> r_; std::shared_ptr<arrow::Schema> schema_;
public:
    explicit PqReader(const std::filesystem::path& p) {
        auto in = arrow::io::ReadableFile::Open(p.string());
        need(in.ok(), "parquet open: " + p.filename().string());
        auto rr = parquet::arrow::OpenFile(*in, arrow::default_memory_pool());
        need(rr.ok(), "parquet reader: " + p.filename().string());
        r_ = std::move(*rr);
        need(r_->GetSchema(&schema_).ok(), "parquet schema");
    }
    int col(std::string_view name) const { return schema_->GetFieldIndex(std::string(name)); }
    int row_groups() const { return r_->num_row_groups(); }
    // Reads the given (possibly missing = -1) columns of one row group; returns rows and Col views.
    int64_t read(int rg, const std::vector<int>& cols, std::vector<Col>& out) {
        std::vector<int> present; for (int c : cols) if (c >= 0) present.push_back(c);
        out.assign(cols.size(), Col{});
        if (present.empty()) return 0;
        auto t = r_->ReadRowGroup(rg, present);
        need(t.ok(), "parquet row group read");
        auto table = (*t)->CombineChunks();
        need(table.ok(), "parquet combine");
        size_t k = 0;
        for (size_t j = 0; j < cols.size(); j++) {
            if (cols[j] < 0) continue;
            auto ch = (*table)->column(int(k++));
            Col c; if (ch->num_chunks() == 0) { out[j] = c; continue; }
            c.hold = ch->chunk(0); c.a = c.hold.get(); c.t = c.a->type_id();
            if (c.t == arrow::Type::DECIMAL128 || c.t == arrow::Type::DECIMAL256) c.scale = static_cast<const arrow::DecimalType&>(*c.a->type()).scale();
            if (c.t == arrow::Type::TIMESTAMP) {
                auto u = static_cast<const arrow::TimestampType&>(*c.a->type()).unit();
                c.ts_div = u == arrow::TimeUnit::SECOND ? 1 : u == arrow::TimeUnit::MILLI ? 1000 : u == arrow::TimeUnit::MICRO ? 1000000 : 1000000000;
            }
            out[j] = std::move(c);
        }
        return (*table)->num_rows();
    }
};

// Write an Arrow table to Parquet (zstd) atomically: tmp file, fdatasync, rename, directory fsync.
inline std::pair<uint64_t, std::string> write_parquet_atomic(const std::shared_ptr<arrow::Table>& table, const std::filesystem::path& p) {
    std::string tmp = p.string() + ".tmp-" + std::to_string(::getpid()) + "-" + std::to_string(mono_ns());
    {
        auto os = arrow::io::FileOutputStream::Open(tmp);
        need(os.ok(), "parquet out open");
        auto props = parquet::WriterProperties::Builder().compression(parquet::Compression::ZSTD)->build();
        auto st = parquet::arrow::WriteTable(*table, arrow::default_memory_pool(), *os, 1 << 20, props);
        need(st.ok(), "parquet write: " + st.ToString());
        need((*os)->Close().ok(), "parquet close");
    }
    int fd = ::open(tmp.c_str(), O_RDONLY | O_CLOEXEC); need(fd >= 0, "parquet reopen");
    int rc = ::fdatasync(fd); ::close(fd); need(rc == 0, "parquet sync");
    auto bytes = load_file(tmp, size_t(64) << 30);
    auto h = sha256(View(bytes.data(), bytes.size()));
    need(::rename(tmp.c_str(), p.c_str()) == 0, "parquet rename");
    sync_directory(p.parent_path());
    return {bytes.size(), hex(View(h.data(), 32))};
}
} // namespace machine::graphloop
