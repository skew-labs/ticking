#include "native_archive.hpp"
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <openssl/sha.h>
#include <sstream>
#include <stdexcept>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <fcntl.h>
#include <unistd.h>

namespace tick::archive {
using namespace std::chrono;
uint64_t wall_ns(){return duration_cast<nanoseconds>(system_clock::now().time_since_epoch()).count();}
uint64_t mono_ns(){return duration_cast<nanoseconds>(steady_clock::now().time_since_epoch()).count();}
void require(bool v,const char*m){if(!v)throw std::runtime_error(m);}
struct FD {int fd;explicit FD(int v):fd(v){require(fd>=0,"file open failed");}~FD(){if(fd>=0)::close(fd);}operator int()const{return fd;}};
void write_all(int fd,const char*p,size_t n){while(n){ssize_t w=::write(fd,p,n);if(w<0&&errno==EINTR)continue;require(w>0,"original write failed");p+=w;n-=w;}}
void sync_dir(const fs::path&p){FD fd(::open(p.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC));require(::fsync(fd)==0,"directory sync failed");}
Json read_json(const fs::path&p,size_t max){require(fs::file_size(p)<=max,"JSON exceeds bound");std::ifstream f(p);require(f.good(),"JSON read failed");Json v;f>>v;return v;}
void atomic_json(const fs::path&p,const Json&v,bool durable){auto tmp=p;tmp+=".native-next";auto data=v.dump();{FD fd(::open(tmp.c_str(),O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC|O_NOFOLLOW,0600));write_all(fd,data.data(),data.size());if(durable)require(::fsync(fd)==0,"JSON sync failed");}require(::rename(tmp.c_str(),p.c_str())==0,"JSON rename failed");if(durable)sync_dir(p.parent_path());}
std::string hex(const unsigned char*p,size_t n){static const char*x="0123456789abcdef";std::string r(n*2,'0');for(size_t i=0;i<n;i++){r[2*i]=x[p[i]>>4];r[2*i+1]=x[p[i]&15];}return r;}
std::string sha256(const std::string&s){unsigned char out[SHA256_DIGEST_LENGTH];SHA256(reinterpret_cast<const unsigned char*>(s.data()),s.size(),out);return hex(out,sizeof out);}
Json identity(const fs::path&p){struct stat s{};require(::stat(p.c_str(),&s)==0,"original stat failed");return Json::array({uint64_t(s.st_dev),uint64_t(s.st_ino),uint64_t(s.st_size),uint64_t(s.st_mtim.tv_sec)*1000000000+s.st_mtim.tv_nsec,uint64_t(s.st_ctim.tv_sec)*1000000000+s.st_ctim.tv_nsec});}
void Counts::add(const Counts&v){retained+=v.retained;executed+=v.executed;successful+=v.successful;failed+=v.failed;bank+=v.bank;if(!first)first=v.first;last=std::max(last,v.last);finalized=std::max(finalized,v.finalized);}
Json Counts::json()const{return {{"retained",retained},{"executedResults",executed},{"successful",successful},{"failed",failed},{"exactBankIdentities",bank},{"firstObservedSlot",first},{"lastSlot",last},{"lastFinalizedSlot",finalized}};}
Counts Counts::from_json(const Json&j){for(auto key:{"retained","executedResults","successful","failed","exactBankIdentities","lastSlot","lastFinalizedSlot"})require(j.at(key).is_number_unsigned(),"invalid cached counter type");Counts v;v.retained=j.at("retained").get<uint64_t>();v.executed=j.at("executedResults").get<uint64_t>();v.successful=j.at("successful").get<uint64_t>();v.failed=j.at("failed").get<uint64_t>();v.bank=j.at("exactBankIdentities").get<uint64_t>();v.first=j.value("firstObservedSlot",uint64_t(0));v.last=j.at("lastSlot").get<uint64_t>();v.finalized=j.at("lastFinalizedSlot").get<uint64_t>();require(v.failed<=v.executed&&v.successful==v.executed-v.failed&&v.executed<=v.retained&&v.bank<=v.retained&&v.first<=v.last&&v.finalized<=v.last,"invalid cached counters");return v;}
Counts inspect(geyser::SubscribeUpdate&u,const std::string&raw){require(!raw.empty()&&raw.size()<=max_frame,"original frame bound");require(u.ParseFromString(raw),"invalid original protobuf");Counts c;c.retained=1;
 if(u.has_transaction()){auto&v=u.transaction();c.last=v.slot();c.bank=v.bank_id()!=0;if(v.transaction().has_meta()){c.executed=1;c.failed=v.transaction().meta().has_err();c.successful=!c.failed;}}
 else if(u.has_slot()){c.last=u.slot().slot();if(u.slot().status()==geyser::SLOT_FINALIZED)c.finalized=c.last;}
 else if(u.has_block_meta())c.last=u.block_meta().slot();
 else {c.retained=0;return c;}
 c.first=c.last;return c;
}
uint64_t read_le(const char*p,size_t n){uint64_t v=0;for(size_t i=0;i<n;i++)v|=uint64_t(uint8_t(p[i]))<<(8*i);return v;}
void append_le(std::string&s,uint64_t v,size_t n){for(size_t i=0;i<n;i++)s.push_back(char(v>>(8*i)));}
Json gap_state(const fs::path&p){Json result={{"unrepairedGapWitnesses",0}};if(!fs::exists(p))return result;require(fs::file_size(p)<=16*1024*1024,"gap journal bound");std::ifstream f(p,std::ios::binary);uint64_t count=0;for(;;){char h[header_size];f.read(h,sizeof h);if(!f.gcount())break;require(size_t(f.gcount())==sizeof h&&!memcmp(h,"SLI1",4),"gap witness header corrupt");size_t n=read_le(h+4,4);require(n>0&&n<16384&&read_le(h+8,8)==++count,"gap witness sequence bound");std::string raw(n,'\0');f.read(raw.data(),n);require(size_t(f.gcount())==n&&crc32(0,reinterpret_cast<const Bytef*>(raw.data()),n)==read_le(h+24,4),"gap witness CRC mismatch");auto witness=Json::parse(raw);require(witness.at("completeRecoveryVerified")==false&&witness.at("requestedSlot").get<uint64_t>()>0&&witness.at("requestedSlot").get<uint64_t>()<witness.at("firstAvailableSlot").get<uint64_t>(),"invalid retained gap witness");result["lastGapWitness"]=witness;result["gapRecoveryRequired"]=true;}result["unrepairedGapWitnesses"]=count;return result;}
Scan scan(const fs::path&p,const std::function<void(Frame&&)>&consume){if(!fs::exists(p))return scan_capsule(p.parent_path(),consume);std::ifstream f(p,std::ios::binary);require(f.good(),"original read failed");Scan out;std::string stored,raw;std::vector<char>inflated(max_frame);stored.reserve(16384);raw.reserve(32768);std::unique_ptr<libdeflate_decompressor,decltype(&libdeflate_free_decompressor)>decoder(libdeflate_alloc_decompressor(),libdeflate_free_decompressor);require(bool(decoder),"decoder allocation failed");geyser::SubscribeUpdate u;SHA256_CTX hash;SHA256_Init(&hash);
 for(;;){char h[header_size];f.read(h,sizeof h);size_t got=f.gcount();if(!got){require(f.eof(),"original read failed before EOF");break;}require(got==sizeof h,"torn original header; preserve and reconcile");size_t n=read_le(h+4,4);uint64_t seq=read_le(h+8,8),stamp=read_le(h+16,8);uint32_t crc=read_le(h+24,4);require(n>0&&n<=max_frame&&seq==out.frames+1&&stamp>0,"invalid original header");stored.resize(n);f.read(stored.data(),n);require(size_t(f.gcount())==n,"torn original body; preserve and reconcile");SHA256_Update(&hash,h,sizeof h);SHA256_Update(&hash,stored.data(),stored.size());
 if(!memcmp(h,"SLI1",4))raw=stored;
 else if(!memcmp(h,"SLZ1",4)){size_t used=0,len=0;auto rc=libdeflate_zlib_decompress_ex(decoder.get(),stored.data(),stored.size(),inflated.data(),inflated.size(),&used,&len);require(rc==LIBDEFLATE_SUCCESS&&used==stored.size()&&len>0&&len<=max_frame,"compressed original corrupt");raw.assign(inflated.data(),len);}
 else require(false,"unsupported original magic");
 require(libdeflate_crc32(0,raw.data(),raw.size())==crc,"original CRC mismatch");auto c=inspect(u,raw);require(c.retained==1,"unsupported retained original");out.counts.add(c);out.frames++;out.bytes+=sizeof h+n;if(consume)consume(Frame{raw,stamp,c});
 }require(out.bytes==fs::file_size(p),"original size changed during scan");unsigned char digest[32];SHA256_Final(digest,&hash);out.digest=hex(digest,32);return out;
}
void Samples::add(double v){values[next]=v;next=(next+1)%values.size();size_=std::min(size_+1,values.size());}
Json Samples::json()const{if(!size_)return nullptr;auto v=values;std::sort(v.begin(),v.begin()+size_);return {{"samples",size_},{"p50",v[size_/2]},{"p95",v[std::min(size_-1,size_t(size_*.95))]},{"max",v[size_-1]}};}
bool FrameQueue::push(Frame&&v){require(!v.raw.empty()&&v.raw.size()<=max_frame,"queue frame bound");std::unique_lock lock(mutex);if(count==slots.size()||bytes+v.raw.size()>queue_bytes)waits++;changed.wait(lock,[&]{return closed||(count<slots.size()&&bytes+v.raw.size()<=queue_bytes);});if(closed)return false;bytes+=v.raw.size();enqueued[tail]=mono_ns();std::swap(slots[tail],v);tail=(tail+1)%slots.size();count++;accepted++;peak=std::max(peak,uint64_t(bytes));changed.notify_all();return true;}
bool FrameQueue::take(std::vector<Frame>&batch){
 std::unique_lock lock(mutex);
 // Idle input wakes on arrival, shutdown or the one-second status heartbeat.
 // Loaded batches use the OLDEST queued frame's monotonic deadline: time spent
 // behind a disk commit never starts a second batching delay.
 changed.wait_for(lock,seconds(1),[&]{return closed||count;});
 if(!count)return !closed;
 auto deadline=steady_clock::time_point(nanoseconds(enqueued[head]))+milliseconds(batch_wait_ms);
 changed.wait_until(lock,deadline,[&]{return closed||count>=128||bytes>=2*1024*1024;});
 size_t n=0;while(count&&batch.size()<128&&n<2*1024*1024){auto&v=slots[head];n+=v.raw.size();bytes-=v.raw.size();batch.emplace_back();std::swap(batch.back(),v);head=(head+1)%slots.size();count--;}
 changed.notify_all();return true;
}
void FrameQueue::commit(size_t n){std::lock_guard lock(mutex);committed+=n;changed.notify_all();}
void FrameQueue::drain(){std::unique_lock lock(mutex);changed.wait(lock,[&]{return closed||committed==accepted;});require(committed==accepted,"writer failed before draining received originals");}
void FrameQueue::close(){std::lock_guard lock(mutex);closed=true;changed.notify_all();}
Json FrameQueue::status(){std::lock_guard lock(mutex);return {{"capacity",256},{"byteLimit",queue_bytes},{"pendingFrames",accepted-committed},{"queuedBytes",bytes},{"peakBytes",peak},{"backpressureWaits",waits},{"acceptedThisRun",accepted},{"committedThisRun",committed}};}
bool Checkpoints::load(const fs::path&folder,Counts&c,uint64_t&bytes){if(!fs::exists(folder/"inputs.sli"))return capsule_summary(folder,c,bytes);try{auto v=read_json(folder/"verified-summary.json",16384);auto&j=v.at("value");double age=wall_ns()/1e9-j.at("validatedAt").get<double>();if(v.at("sha256")!=sha256(j.dump())||j.at("schema")!=1||j.at("identity")!=identity(folder/"inputs.sli")||age<0||age>86400)return false;c=Counts::from_json(j.at("state"));if(c.retained!=j.at("frames"))return false;bytes=j.at("identity").at(2).get<uint64_t>();return true;}catch(...){return false;}}
void Checkpoints::build(const fs::path&folder){auto source=folder/"inputs.sli";auto before=identity(source);auto result=scan(source);require(identity(source)==before,"closed original changed during validation");Json v={{"schema",1},{"identity",before},{"sourceSHA256",result.digest},{"frames",result.frames},{"state",result.counts.json()},{"validatedAt",wall_ns()/1e9}};atomic_json(folder/"verified-summary.json",{{"value",v},{"sha256",sha256(v.dump())}},false);}
Checkpoints::Checkpoints():worker([this]{run();}){}
Checkpoints::~Checkpoints(){{std::lock_guard lock(mutex);stop=true;changed.notify_all();}worker.join();}
void Checkpoints::submit(const fs::path&p){std::lock_guard lock(mutex);if(pending.size()>=2){skipped++;return;}pending.push_back(p);changed.notify_all();}
void Checkpoints::check(){require(!integrity_failed,"closed original validation failed; preserve and reconcile");}
Json Checkpoints::status(){std::lock_guard lock(mutex);return {{"mode","native_background_thread"},{"queueLimit",2},{"pending",pending.size()},{"completed",completed},{"cacheJobsSkipped",skipped},{"failed",errors},{"active",active},{"lastBuildMs",last_ms},{"maxBuildMs",max_ms},{"integrityFailure",integrity_failed.load()}};}
void Checkpoints::run(){for(;;){fs::path p;{std::unique_lock lock(mutex);changed.wait(lock,[&]{return stop||!pending.empty();});if(stop&&pending.empty())return;p=std::move(pending.front());pending.pop_front();active=true;}auto start=mono_ns();try{build(p);std::lock_guard lock(mutex);completed++;}catch(...){integrity_failed=true;std::lock_guard lock(mutex);errors++;}{std::lock_guard lock(mutex);active=false;last_ms=(mono_ns()-start)/1e6;max_ms=std::max(max_ms,last_ms);}}}
Store::Store(const fs::path&dir,uint64_t reserve_bytes,uint64_t segment_bytes):root(dir),originals(dir/"originals"),segment_limit(segment_bytes),reserve(reserve_bytes){
 fs::create_directories(originals);FD retention(::open((originals/"retention.lock").c_str(),O_RDWR|O_CREAT|O_CLOEXEC|O_NOFOLLOW,0600));require(::flock(retention,LOCK_SH)==0,"retention startup lock failed");lock_fd=::open((originals/"writer.lock").c_str(),O_RDWR|O_CREAT|O_CLOEXEC|O_NOFOLLOW,0600);require(lock_fd>=0,"writer lock open failed");try{require(::flock(lock_fd,LOCK_EX|LOCK_NB)==0,"another original writer owns archive");if(fs::exists(root/"status.json"))extra=read_json(root/"status.json");else extra=Json::object();
 std::vector<fs::path> paths;for(auto&e:fs::directory_iterator(originals))if(e.is_directory())paths.push_back(e.path());std::sort(paths.begin(),paths.end());for(size_t i=0;i<paths.size();i++){char name[24];snprintf(name,sizeof name,"%08zu",i+1);require(paths[i].filename()==name,"original segment sequence incomplete");Counts c;uint64_t bytes=0;if(i+1<paths.size()&&Checkpoints::load(paths[i],c,bytes))cached++;else{auto r=scan(paths[i]/"inputs.sli");c=r.counts;bytes=r.bytes;}totals.add(c);total_bytes+=bytes;if(i+1==paths.size()){segment=i+1;offset=bytes;sequence=c.retained;active_path=paths[i];}}
 if(segment){fd=::open((active_path/"inputs.sli").c_str(),O_RDWR|O_APPEND|O_CLOEXEC|O_NOFOLLOW);require(fd>=0&&::flock(fd,LOCK_EX|LOCK_NB)==0,"active original lock failed");require(::fdatasync(fd)==0,"restart active original durability failed");sync_dir(active_path);}
 compressor.reset(libdeflate_alloc_compressor(1));require(bool(compressor),"compressor allocation failed");packed.resize(libdeflate_zlib_compress_bound(compressor.get(),max_frame));batch_bytes.reserve(4*1024*1024);
 extra["restartValidation"]={{"cachedClosedSegments",cached},{"completeArchiveScrubThisStart",cached==0},{"policy","unchanged identity/hash cache, 24h expiry, active segment fully validated"}};
 extra["runtime"]="cpp_native_metadata_recorder";extra["connected"]=false;extra["failure"]=nullptr;extra["shredRequired"]=false;extra["directShredDeliveryConfigured"]=false;extra["completeGapRecoveryVerified"]=false;extra["integratedAcceptancePassed"]=false;extra["clockSynchronizationVerified"]=false;extra["providerCreatedToCanadaMs"]=nullptr;extra["oldestQueuedToDurableMs"]=nullptr;
 if(fs::is_directory("/dev/shm"))memory_tail=fs::path("/dev/shm")/("tick-tail-"+std::to_string(geteuid())+"-"+sha256(fs::canonical(root).string())+".json");
 extra.update(gap_state(root/"gap-events/inputs.sli"));publish(true);
 }catch(...){if(fd>=0)::close(fd);fd=-1;if(lock_fd>=0)::close(lock_fd);lock_fd=-1;throw;}
}
Store::~Store(){if(fd>=0)::close(fd);if(!memory_tail.empty()){std::error_code ignored;fs::remove(memory_tail,ignored);fs::remove(memory_tail.string()+".native-next",ignored);}if(lock_fd>=0)::close(lock_fd);}
void Store::open_segment(){if(fd>=0){require(::fdatasync(fd)==0,"rotation sync failed");::close(fd);fd=-1;caches.submit(active_path);}segment++;require(segment<=99999999,"segment namespace exhausted");char name[24];snprintf(name,sizeof name,"%08llu",(unsigned long long)segment);active_path=originals/name;require(fs::create_directory(active_path),"segment unexpectedly exists");sync_dir(originals);fd=::open((active_path/"inputs.sli").c_str(),O_RDWR|O_CREAT|O_EXCL|O_APPEND|O_CLOEXEC|O_NOFOLLOW,0600);require(fd>=0&&::flock(fd,LOCK_EX|LOCK_NB)==0,"new original lock failed");sync_dir(active_path);offset=0;sequence=0;}
void Store::publish(bool force_disk){if(!segment)return;Json head={{"segment",active_path.filename().string()},{"offset",offset},{"records",totals.retained}};bool in_memory=false;if(!memory_tail.empty()){try{atomic_json(memory_tail,head,false);in_memory=true;}catch(const std::exception&){/* Disk fallback is slower, but remains correct. */}}
 auto now=mono_ns();if(force_disk||!in_memory||now-last_disk_tail>=1000000000){atomic_json(root/"durable-tail.json",head,false);last_disk_tail=now;}extra["tailPublicationBackend"]=in_memory?"private_tmpfs_with_disk_fallback":"disk_fallback";
}
void Store::append(const std::vector<Frame>&batch){if(batch.empty())return;caches.check();require(batch.size()<=128,"batch frame bound");auto started=mono_ns();size_t raw_size=0;for(auto&v:batch)raw_size+=header_size+v.raw.size();require(raw_size<=queue_bytes,"batch bytes bound");struct statvfs sv{};require(::statvfs(root.c_str(),&sv)==0,"free-space inspection failed");auto available=uint64_t(sv.f_bavail)*sv.f_frsize;require(available>=reserve&&available-reserve>=raw_size,"original free-space reserve reached; preserve originals");bool rotated=fd<0||offset+raw_size>segment_limit;if(rotated)open_segment();auto compress_start=mono_ns();batch_bytes.clear();Counts delta;
 for(auto&v:batch){require(v.stamp>0&&v.counts.retained==1&&!v.raw.empty()&&v.raw.size()<=max_frame,"invalid received frame");const char*p=v.raw.data();size_t n=v.raw.size();bool zipped=false;if(n>=256){auto compressed=libdeflate_zlib_compress(compressor.get(),p,n,packed.data(),packed.size());require(compressed>0,"compression bound failed");if(compressed<n){p=reinterpret_cast<const char*>(packed.data());n=compressed;zipped=true;}}
 batch_bytes.append(zipped?"SLZ1":"SLI1",4);append_le(batch_bytes,n,4);append_le(batch_bytes,sequence+delta.retained+1,8);append_le(batch_bytes,v.stamp,8);append_le(batch_bytes,libdeflate_crc32(0,v.raw.data(),v.raw.size()),4);batch_bytes.append(p,n);delta.add(v.counts);}
 auto write_start=mono_ns();write_all(fd,batch_bytes.data(),batch_bytes.size());auto sync_start=mono_ns();require(::fdatasync(fd)==0,"original durability failed");auto barrier=mono_ns(),durable_ns=wall_ns();{std::lock_guard lock(state_mutex);sequence+=batch.size();offset+=batch_bytes.size();total_bytes+=batch_bytes.size();totals.add(delta);durability.add((barrier-started)/1e6);compression_ms.add((write_start-compress_start)/1e6);write_ms.add((sync_start-write_start)/1e6);sync_ms.add((barrier-sync_start)/1e6);if(rotated)rotation_ms.add((compress_start-started)/1e6);for(auto&v:batch)if(durable_ns>=v.stamp)ingress.add((durable_ns-v.stamp)/1e6);publish();tail_ms.add((mono_ns()-barrier)/1e6);}
}
void Store::set(const Json&v){std::lock_guard lock(state_mutex);extra.update(v);}
Json Store::snapshot()const{std::lock_guard lock(state_mutex);Json v=extra;v.update(totals.json());v["received"]=totals.retained;v["originalFrames"]=totals.retained;v["originalBytes"]=total_bytes;v["originalSegments"]=segment;v["originalBatchDurabilityMs"]=durability.json();v["receivedToOriginalDurableMs"]=ingress.json();v["compression"]="libdeflate level 1, unchanged zlib wire format";v["limits"]={{"totalOriginalBytes",nullptr},{"freeSpaceReserveBytes",reserve},{"segmentBytes",segment_limit},{"batchFrames",128},{"batchTargetBytes",2*1024*1024},{"batchWaitMs",batch_wait_ms}};time_t t=time(nullptr);tm utc{};gmtime_r(&t,&utc);char date[32];strftime(date,sizeof date,"%Y-%m-%dT%H:%M:%SZ",&utc);v["updatedUTC"]=date;return v;}
uint64_t Store::cursor()const{std::lock_guard lock(state_mutex);auto s=totals.finalized?totals.finalized:totals.last;return s?std::max(totals.first,s>64?s-64:0):0;}
void Store::report(const Json&q,bool force){auto now=mono_ns();if(!force&&now-last_report<1000000000)return;auto v=snapshot();v["queue"]=q;v["pendingFrames"]=q.value("pendingFrames",0);v["restartCacheWorker"]=caches.status();{std::lock_guard lock(state_mutex);v["stagesMs"]={{"compression",compression_ms.json()},{"write",write_ms.json()},{"fdatasync",sync_ms.json()},{"rotation",rotation_ms.json()},{"tailPublication",tail_ms.json()},{"statusPublication",status_ms.json()}};}
 // Status and restart summaries are rebuildable. Keep their metadata fsyncs
 // off the hot path; original payload fdatasync remains mandatory. Persist
 // shutdown and provider-wide cooldown state separately from routine telemetry.
 atomic_json(root/"status.json",v,force||v.value("sharedQuotaCooldownSeconds",0.0)>0);last_report=now;{std::lock_guard lock(state_mutex);status_ms.add((mono_ns()-now)/1e6);}}
void Store::gap(uint64_t requested,uint64_t available,const std::string&replay){require(requested>0&&available>requested,"invalid gap witness");auto folder=root/"gap-events";fs::create_directories(folder);FD log(::open((folder/"inputs.sli").c_str(),O_RDWR|O_CREAT|O_APPEND|O_CLOEXEC|O_NOFOLLOW,0600));require(::flock(log,LOCK_EX|LOCK_NB)==0,"gap witness lock failed");std::ifstream f(folder/"inputs.sli",std::ios::binary);uint64_t count=0;for(;;){char h[header_size];f.read(h,sizeof h);if(!f.gcount())break;require(size_t(f.gcount())==sizeof h&&!memcmp(h,"SLI1",4),"gap witness header corrupt");size_t n=read_le(h+4,4);require(n>0&&n<16384&&read_le(h+8,8)==++count,"gap witness bound");std::string raw(n,'\0');f.read(raw.data(),n);require(size_t(f.gcount())==n&&crc32(0,reinterpret_cast<const Bytef*>(raw.data()),n)==read_le(h+24,4),"gap witness CRC mismatch");}
 auto snap=snapshot();Json w={{"requestedSlot",requested},{"firstAvailableSlot",available},{"retainedHighWaterSlot",snap.at("lastSlot")},{"reason","upstream replay retention expired"},{"completeRecoveryVerified",false},{"replayInfoProtobufHex",hex(reinterpret_cast<const unsigned char*>(replay.data()),replay.size())}};auto raw=w.dump();std::string bytes="SLI1";append_le(bytes,raw.size(),4);append_le(bytes,count+1,8);append_le(bytes,wall_ns(),8);append_le(bytes,crc32(0,reinterpret_cast<const Bytef*>(raw.data()),raw.size()),4);bytes+=raw;require(fs::file_size(folder/"inputs.sli")+bytes.size()<=16*1024*1024,"gap witness storage bound");write_all(log,bytes.data(),bytes.size());require(fdatasync(log)==0,"gap witness sync failed");sync_dir(folder);set({{"unrepairedGapWitnesses",count+1},{"lastGapWitness",w},{"gapRecoveryRequired",true}});
}
void Store::finish(){if(fd>=0)require(fdatasync(fd)==0,"shutdown sync failed");caches.check();publish(true);}
}
