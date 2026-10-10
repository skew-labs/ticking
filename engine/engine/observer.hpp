#pragma once
// Our observation-only client. No Firedancer code, execution, voting or RPC.
#include <array>
#include <atomic>
#include <cstdint>
#include <memory>
#include <span>
#include <stdexcept>
#include <vector>
namespace slotline::observer {
using Bytes = std::span<const uint8_t>;
using Hash = std::array<uint8_t,32>;
using Signature = std::array<uint8_t,64>;
struct Error : std::runtime_error { using std::runtime_error::runtime_error; };
uint16_t u16(const uint8_t*);
uint32_t u32(const uint8_t*);
uint64_t u64(const uint8_t*);
Hash hash(Bytes);
struct Wire {
 size_t size{};
 Signature signature{};
 bool versioned{}, has_lookups{}, selected{};
 size_t message_size{};
};
// Legacy/v0 and signature-checked V1; unknown future versions fail closed.
Wire parse_wire(Bytes, std::span<const Hash> watched = {});
struct ProtoWire {
 std::array<uint8_t,4096> bytes{};
 size_t size{};
 Signature signature{};
 uint64_t index{};
 bool failed{}, selected{};
};
// Reads original SubscribeUpdateTransactionInfo bytes without JSON/protobuf heap trees.
ProtoWire parse_result(Bytes, std::span<const Hash> watched = {});
struct AmmConfig {Hash pool,program,base_vault,quote_vault,base_mint,quote_mint;uint32_t base_decimals,quote_decimals;};
struct Fill {double price{},volume{},base{};uint64_t index{};};
// Raydium v4/CPMM/CLMM, PumpSwap, DLMM and Whirlpool vault-flow adapters.
int decode_amm(Bytes, const AmmConfig&, Fill&);
struct Shred {
 uint64_t slot{};
 uint32_t index{}, fec{}, code_first{};
 uint16_t version{}, parent{}, data_size{};
 uint8_t shard{}, flags{};
 bool data{}, resigned{};
 size_t size{}, proof_offset{}, erasure_size{};
 Hash root{}, chain{};
};
Shred parse_shred(Bytes, uint16_t version, uint32_t max_index);
bool verify_signature(const Signature&, const Hash&, const Hash& leader);
// 32:32 systematic Vandermonde GF(256), polynomial 0x11d, per Agave.
// AVX2 is runtime-dispatched; scalar implementation is kept as an oracle/fallback.
class ReedSolomon {
 std::array<std::array<uint8_t,32>,64> rows_{};
 struct Plan {uint64_t mask{};std::array<std::array<uint8_t,32>,32> inverse{};};
 mutable std::array<Plan,16> plans_{};
 mutable size_t next_plan_{};
 public:
 ReedSolomon();
 void restore(std::array<std::array<uint8_t,1232>,64>&, uint64_t present,
              size_t shard_size, bool scalar = false) const;
 const auto& rows() const { return rows_; }
};
struct alignas(64) Fec {
 bool used{}, complete{}, resigned{}, have_code{};
 uint64_t slot{}, present{}, stamp{};
 uint32_t index{}, code_first{}, max_index{};
 uint16_t version{};
 Hash root{}, chain{}, leader{};
 Signature signature{};
 std::array<std::array<uint8_t,1232>,64> packets{};
};
class FecArena {
 struct alignas(64) Index {Hash root{};uint64_t slot{};uint32_t fec{},entry{};uint8_t state{};};
 std::unique_ptr<Fec[]> entries_;
 std::unique_ptr<Index[]> index_;
 std::unique_ptr<uint32_t[]> free_;
 size_t capacity_, table_size_, free_count_;
 uint64_t retired_{};
 ReedSolomon rs_;
 public:
 uint64_t received{}, rejected{}, duplicate{}, completed{};
 explicit FecArena(size_t capacity=512);
 // Inputs must have been retained durably by the caller before output admission.
 const Fec* add(Bytes, const Hash& leader, uint16_t version, uint32_t max_index,
                uint64_t receive_ns);
 // Only an explicit verified downstream/recovery barrier may reclaim state.
 void retire(uint64_t through_slot);
};
struct alignas(64) JoinRecord {
 uint64_t slot{};
 Signature signature{};
 Hash wire{}, branch{};
 uint8_t state{}; // 0 empty, 1 occupied, 2 tombstone
};
class JoinCache {
 std::unique_ptr<JoinRecord[]> records_;
 size_t capacity_, table_size_, count_{};
 uint64_t retired_{};
 size_t bucket(uint64_t, const Signature&) const;
 public:
 explicit JoinCache(size_t capacity);
 // 1 insert; 0 duplicate; -1 conflict; -2 full; -3 retired; -4 invalid.
 int observe(uint64_t, Bytes wire, const Hash& branch);
 // Exact source-attested branch binding is mandatory, not inferred from signature.
 int match(uint64_t, Bytes result, const Hash& branch);
 int match_any(uint64_t, Bytes result, std::span<const Hash> branches);
 void retire(uint64_t through_slot);
 size_t count() const { return count_; }
};
// Bounded single-producer/single-consumer rings, separated cache lines.
template<class T, size_t N> class Spsc {
 static_assert((N&(N-1))==0);
 alignas(64) std::atomic<uint64_t> write_{0};
 alignas(64) std::atomic<uint64_t> read_{0};
 alignas(64) std::array<T,N> data_{};
 public:
 T* producer() { auto w=write_.load(std::memory_order_relaxed);
   return w-read_.load(std::memory_order_acquire)<N ? &data_[w&(N-1)] : nullptr; }
 void publish() { write_.fetch_add(1,std::memory_order_release); }
 T* consumer() { auto r=read_.load(std::memory_order_relaxed);
   return r<write_.load(std::memory_order_acquire) ? &data_[r&(N-1)] : nullptr; }
 void release() { read_.fetch_add(1,std::memory_order_release); }
};
}
extern "C" {
void* sl_observer_new(uint32_t capacity);
void sl_observer_delete(void*);
int sl_observer_observe(void*, uint64_t, const uint8_t*, uint32_t, const uint8_t*);
int sl_observer_match(void*, uint64_t, const uint8_t*, uint32_t, const uint8_t*);
int sl_observer_match_any(void*,uint64_t,const uint8_t*,uint32_t,const uint8_t*,uint32_t);
uint64_t sl_observer_count(void*);
int sl_observer_retire(void*, uint64_t);
int sl_observer_result(const uint8_t*,uint32_t,uint8_t*,uint32_t*,uint64_t*);
const char* sl_observer_last_error();
int sl_observer_selected(const uint8_t*,uint32_t,const uint8_t*,uint32_t);
int sl_observer_candidates(const uint8_t*,uint32_t,const uint8_t*,uint32_t,uint8_t*);
int sl_observer_amm_batch(const uint8_t*,uint32_t,const slotline::observer::AmmConfig*,uint32_t,const uint32_t*,uint32_t,slotline::observer::Fill*,int32_t*);
// Immutable cold-market address index. Build on the control path; lookup visits
// transaction accounts only and emits at most 256 matching registry offsets.
void* sl_pool_index_new(const uint8_t*,uint32_t);
void* sl_pool_index_reserved_new(const uint8_t*,uint32_t,uint32_t);
int sl_pool_index_append(void*,const uint8_t*,uint32_t);
void sl_pool_index_delete(void*);
int sl_pool_index_candidates(void*,const uint8_t*,uint32_t,uint32_t*,uint32_t*);
// Metadata-only observer selection: all verified pool addresses in the account
// list, independent of the supported instruction set. Decoders decide execution.
int sl_pool_index_keys(void*,const uint8_t*,uint32_t,uint32_t*,uint32_t*);
int sl_pool_index_batch(void*,const uint8_t*,uint32_t,const uint32_t*,uint32_t,uint32_t*,uint32_t*);
int sl_observer_amm(const uint8_t*,uint32_t,const slotline::observer::AmmConfig*,slotline::observer::Fill*);
}
