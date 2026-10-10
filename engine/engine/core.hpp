#pragma once
#include <array>
#include <cstdint>
#include <map>
#include <string>
#include <tuple>
#include <unordered_map>
#include <vector>
namespace slotline {
// Fixed binary ABI v1; little endian, IEEE754. Only chart prices use doubles.
// Original executed receipts/atomic amounts remain in the upstream receipt log.
struct Event {uint64_t slot,time;uint32_t transaction,event,market,action;std::array<uint8_t,32> id,fork;double price,volume,base;};
static_assert(sizeof(Event)==120);
struct Candle {uint64_t time;double open,high,low,close,volume,base;uint64_t count,revision;};
static_assert(sizeof(Candle)==72);
struct Change {uint64_t revision,time;uint32_t market,padding;};
using Order=std::tuple<uint64_t,uint32_t,uint32_t,std::array<uint8_t,32>>;
struct Aggregate {Candle value{};Order first{},last{};void add(const Event &e);};
struct Bucket {
 Aggregate finalized;
 std::vector<uint32_t> pending;
 uint64_t revision=0;
 // Readers hold the containing core's owner lock. A fill updates this cache
 // once; a fork retraction invalidates only its affected minute bucket.
 mutable Aggregate cached;
 mutable bool cache_valid=true;
};
struct IdHash {size_t operator()(const std::array<uint8_t,32>&v)const noexcept;};
class Core {
 std::vector<Event> pool_;std::vector<uint32_t> free_;
 std::unordered_map<std::array<uint8_t,32>,uint32_t,IdHash> identities_;
 std::map<std::pair<uint32_t,uint64_t>,Bucket> buckets_;
 std::unordered_map<uint32_t,uint64_t> latest_;
 uint64_t root_=0,revision_=0;size_t markets_,window_;
 std::vector<Change> changes_;size_t head_=0,change_count_=0;void changed(uint32_t market,uint64_t time);
 void prune(uint32_t market,uint64_t latest);
 public:
 Core(size_t capacity=65536,size_t markets=100,size_t window=2400);
 // 1 accepted; 0 duplicate; -1 invalid; -2 capacity; -3 before root/history window.
 int apply(const Event& e);
 int changes(uint32_t market,uint64_t after,Candle*out,size_t limit)const;
 bool candle(uint32_t market,uint64_t time,Candle &out)const;
 size_t history(uint32_t market,uint64_t before,Candle *out,size_t limit)const;
 uint64_t revision()const{return revision_;}uint64_t root()const{return root_;}
 size_t active()const{return identities_.size();}size_t candles()const{return buckets_.size();}
};
}
