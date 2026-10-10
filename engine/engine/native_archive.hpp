#pragma once
#include <nlohmann/json.hpp>
#include <atomic>
#include <array>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <functional>
#include <mutex>
#include <memory>
#include <string>
#include <thread>
#include <vector>
#include <zlib.h>
#include <libdeflate.h>
#include "geyser.pb.h"

namespace tick::archive {
using Json=nlohmann::json; namespace fs=std::filesystem;
constexpr size_t max_frame=8*1024*1024, header_size=28, queue_bytes=32*1024*1024;
constexpr unsigned batch_wait_ms=1;
uint64_t wall_ns(); uint64_t mono_ns();
void require(bool,const char*);
Json read_json(const fs::path&,size_t max=65536);
void atomic_json(const fs::path&,const Json&,bool durable);
std::string sha256(const std::string&);
struct Counts {
 uint64_t retained=0,executed=0,successful=0,failed=0,bank=0,first=0,last=0,finalized=0;
 void add(const Counts&); Json json()const; static Counts from_json(const Json&);
};
Counts inspect(geyser::SubscribeUpdate& parsed,const std::string& raw);
struct Frame {std::string raw;uint64_t stamp=0;Counts counts;};
struct Scan {Counts counts;uint64_t bytes=0,frames=0;std::string digest;};
Scan scan(const fs::path&,const std::function<void(Frame&&)>& consume={});
// Cold capsules preserve every received protobuf byte and original cursor offset.
Scan scan_capsule(const fs::path&,const std::function<void(Frame&&)>& consume={});
bool capsule_summary(const fs::path&,Counts&,uint64_t&);
Json pack_capsule(const fs::path&);
Json refresh_raw_checkpoint(const fs::path&);
class Samples {
 std::array<double,2048> values{};size_t size_=0,next=0;
public:void add(double v);Json json()const;
};
class FrameQueue {
 // Fixed slots and aggregate byte admission. No per-frame queue-node allocation.
 alignas(64) std::array<Frame,256> slots;
 std::array<uint64_t,256> enqueued{};
 alignas(64) size_t head=0,tail=0,count=0,bytes=0;
 std::mutex mutex;std::condition_variable changed;bool closed=false;
 uint64_t accepted=0,committed=0,waits=0,peak=0;
public:
 bool push(Frame&&);bool take(std::vector<Frame>&);
 void commit(size_t);void drain();void close();Json status();
};
class Checkpoints {
 std::mutex mutex;std::condition_variable changed;std::deque<fs::path> pending;
 bool stop=false,active=false;std::thread worker;
 std::atomic<bool> integrity_failed{false};uint64_t completed=0,skipped=0,errors=0;double last_ms=0,max_ms=0;
 void run();
public:Checkpoints();~Checkpoints();void submit(const fs::path&);void check();Json status();
 static bool load(const fs::path&,Counts&,uint64_t&);static void build(const fs::path&);
};
class Store {
 fs::path root,originals,active_path,memory_tail;int lock_fd=-1,fd=-1;uint64_t segment=0,offset=0,sequence=0,total_bytes=0,last_disk_tail=0;
 uint64_t segment_limit,reserve,cached=0;Counts totals;
 std::unique_ptr<libdeflate_compressor,decltype(&libdeflate_free_compressor)>compressor{nullptr,libdeflate_free_compressor};
 std::vector<unsigned char> packed;std::string batch_bytes;Checkpoints caches;
 mutable std::mutex state_mutex;Json extra;Samples durability,ingress,compression_ms,write_ms,sync_ms,rotation_ms,tail_ms,status_ms;
 uint64_t last_report=0;
 void open_segment();void publish(bool force_disk=false);
public:
 Store(const fs::path&,uint64_t reserve=32ULL*1024*1024*1024,uint64_t segment_bytes=64ULL*1024*1024);
 ~Store();Store(const Store&)=delete;Store& operator=(const Store&)=delete;
 void append(const std::vector<Frame>&);void report(const Json& queue,bool force=false);
 void set(const Json&);Json snapshot()const;uint64_t cursor()const;
 void gap(uint64_t requested,uint64_t available,const std::string& replay_wire);
 void finish();
};
}
