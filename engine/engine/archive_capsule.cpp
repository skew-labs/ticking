#include "native_archive.hpp"
#include <zstd.h>
#include <fstream>
#include <cstring>
#include <cerrno>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <openssl/sha.h>

namespace tick::archive {
namespace {
constexpr size_t limit=256ULL*1024*1024;
uint64_t le(const char*p,size_t n){uint64_t v=0;for(size_t i=0;i<n;i++)v|=uint64_t(uint8_t(p[i]))<<(i*8);return v;}
void put_le(std::string&s,uint64_t v,size_t n){for(size_t i=0;i<n;i++)s.push_back(char(v>>(i*8)));}
Json file_id(const fs::path&p){struct stat s{};require(lstat(p.c_str(),&s)==0&&S_ISREG(s.st_mode)&&s.st_nlink==1,"capsule regular file identity");return Json::array({uint64_t(s.st_dev),uint64_t(s.st_ino),uint64_t(s.st_size),uint64_t(s.st_mtim.tv_sec)*1000000000+s.st_mtim.tv_nsec,uint64_t(s.st_ctim.tv_sec)*1000000000+s.st_ctim.tv_nsec});}
void directory_sync(const fs::path&p){int fd=open(p.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC);require(fd>=0,"capsule directory open");int rc=fsync(fd);close(fd);require(rc==0,"capsule directory sync");}
std::string read_bounded(const fs::path&p,size_t max){auto id=file_id(p);auto n=id[2].get<uint64_t>();require(n>0&&n<=max,"capsule input bound");std::ifstream f(p,std::ios::binary);std::string v(n,'\0');f.read(v.data(),n);require(size_t(f.gcount())==n&&file_id(p)==id,"capsule read changed");return v;}
Json metadata(const fs::path&folder){auto j=read_json(folder/"capsule.json",16384);auto v=j.at("value");require(j.at("sha256")==sha256(v.dump())&&v.at("schema")==1&&v.at("codec")=="zstd-canonical-frames-v1"&&v.at("segment")==folder.filename().string(),"capsule manifest mismatch");require(v.at("canonicalBytes").get<uint64_t>()<=limit&&v.at("sourceBytes").get<uint64_t>()<=96*1024*1024,"capsule manifest bounds");require(v.at("capsuleIdentity")==file_id(folder/"inputs.slc"),"capsule file identity mismatch");return v;}
Scan decode(const std::string&canonical,const Json&v,const std::function<void(Frame&&)>&consume){
 require(sha256(canonical)==v.at("canonicalSHA256").get<std::string>(),"capsule content hash mismatch");Scan out;geyser::SubscribeUpdate u;size_t pos=0;
 while(pos<canonical.size()){
  require(canonical.size()-pos>=32,"capsule torn header");auto*h=canonical.data()+pos;auto n=le(h+28,4),stored=le(h+4,4),seq=le(h+8,8),stamp=le(h+16,8),crc=le(h+24,4);
  require((!memcmp(h,"SLI1",4)||!memcmp(h,"SLZ1",4))&&n>0&&n<=max_frame&&stored>0&&stored<=max_frame&&seq==out.frames+1&&stamp>0&&n<=canonical.size()-pos-32,"capsule invalid frame");
  std::string raw(h+32,n);require(libdeflate_crc32(0,raw.data(),raw.size())==crc,"capsule original CRC mismatch");auto c=inspect(u,raw);require(c.retained==1,"capsule unsupported frame");out.counts.add(c);out.frames++;out.bytes+=28+stored;pos+=32+n;if(consume)consume(Frame{std::move(raw),stamp,c});
 }
 require(out.frames==v.at("frames")&&out.bytes==v.at("sourceBytes")&&out.counts.json()==v.at("state"),"capsule accounting mismatch");out.digest=v.at("sourceSHA256");return out;
}
}
bool capsule_summary(const fs::path&folder,Counts&c,uint64_t&bytes){auto v=metadata(folder);c=Counts::from_json(v.at("state"));bytes=v.at("sourceBytes");require(c.retained==v.at("frames"),"capsule frame count mismatch");return true;}
Json refresh_raw_checkpoint(const fs::path&folder){
 auto j=read_json(folder/"verified-summary.json",16384);auto v=j.at("value");auto source=folder/"inputs.sli";auto before=file_id(source);
 require(j.at("sha256")==sha256(v.dump())&&v.at("schema")==1&&v.at("identity")==before,"checkpoint identity/hash mismatch");
 auto counts=Counts::from_json(v.at("state"));require(counts.retained==v.at("frames"),"checkpoint frame mismatch");
 // Re-hash every byte, not merely the file timestamp. Identical bytes preserve
 // the earlier full protobuf/CRC/count validation without repeated decoding.
 auto digest=sha256(read_bounded(source,96*1024*1024));require(v.at("sourceSHA256")==digest&&before==file_id(source),"checkpoint source checksum mismatch");
 v["validatedAt"]=wall_ns()/1e9;atomic_json(folder/"verified-summary.json",{{"value",v},{"sha256",sha256(v.dump())}},true);return {{"sourceSHA256",digest},{"bytes",before[2]},{"verified",true}};
}
Scan scan_capsule(const fs::path&folder,const std::function<void(Frame&&)>&consume){
 auto v=metadata(folder);auto compressed=read_bounded(folder/"inputs.slc",limit);require(sha256(compressed)==v.at("capsuleSHA256").get<std::string>(),"capsule compressed hash mismatch");auto n=v.at("canonicalBytes").get<size_t>();require(n>0&&n<=limit,"capsule expansion bound");std::string raw(n,'\0');auto rc=ZSTD_decompress(raw.data(),raw.size(),compressed.data(),compressed.size());require(!ZSTD_isError(rc)&&rc==n,"capsule decompression failed");return decode(raw,v,consume);
}
Json pack_capsule(const fs::path&folder){
 auto source=folder/"inputs.sli";auto before=file_id(source);require(before[2].get<uint64_t>()<=96*1024*1024,"closed segment bound");
 // Never replace an existing capsule silently. A crash after sealing can resume
 // using its source identity and a complete integrity check.
 if(fs::exists(folder/"capsule.json")){auto v=metadata(folder);require(v.at("sourceIdentity")==before,"sealed capsule source changed");auto s=scan_capsule(folder);require(s.digest==sha256(read_bounded(source,96*1024*1024)),"sealed capsule source digest mismatch");return v;}
 auto encoded=read_bounded(source,96*1024*1024);std::string canonical;canonical.reserve(std::min(limit,encoded.size()*3));size_t position=0;uint64_t first_stamp=0,last_stamp=0;
 auto scanned=scan(source,[&](Frame&&f){require(position+28<=encoded.size(),"source frame accounting");auto*h=encoded.data()+position;auto n=le(h+4,4);require(n<=encoded.size()-position-28,"source stored frame bound");require(canonical.size()+32+f.raw.size()<=limit,"capsule expansion exceeds configured bound");canonical.append(h,28);put_le(canonical,f.raw.size(),4);canonical+=f.raw;position+=28+n;if(!first_stamp)first_stamp=f.stamp;last_stamp=f.stamp;});
 require(position==encoded.size()&&before==file_id(source)&&scanned.digest==sha256(encoded),"source changed during packing");
 std::string packed(ZSTD_compressBound(canonical.size()),'\0');auto n=ZSTD_compress(packed.data(),packed.size(),canonical.data(),canonical.size(),3);require(!ZSTD_isError(n),"capsule compression failed");packed.resize(n);packed.shrink_to_fit();
 require(packed.size()+16384<encoded.size(),"capsule offers no storage saving");
 // Verify the actual round trip before any filesystem publication.
 std::string restored(canonical.size(),'\0');auto rc=ZSTD_decompress(restored.data(),restored.size(),packed.data(),packed.size());require(!ZSTD_isError(rc)&&restored==canonical,"capsule roundtrip differs");restored.clear();restored.shrink_to_fit();
 auto target=folder/"inputs.slc",tmp=folder/"inputs.slc.pending";
 require(!fs::exists(target)&&!fs::exists(tmp),"uncommitted capsule requires reconciliation");int fd=open(tmp.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);require(fd>=0,"capsule temporary open");size_t done=0;while(done<packed.size()){auto w=write(fd,packed.data()+done,packed.size()-done);if(w<0&&errno==EINTR)continue;if(w<=0){close(fd);throw std::runtime_error("capsule write failed");}done+=w;}int synced=fsync(fd);close(fd);require(synced==0,"capsule sync failed");require(rename(tmp.c_str(),target.c_str())==0,"capsule rename failed");directory_sync(folder);
 Json v={{"schema",1},{"codec","zstd-canonical-frames-v1"},{"segment",folder.filename().string()},{"sourceIdentity",before},{"sourceSHA256",scanned.digest},{"sourceBytes",scanned.bytes},{"canonicalBytes",canonical.size()},{"canonicalSHA256",sha256(canonical)},{"capsuleSHA256",sha256(packed)},{"capsuleIdentity",file_id(target)},{"frames",scanned.frames},{"state",scanned.counts.json()},{"firstIngressNs",first_stamp},{"lastIngressNs",last_stamp},{"createdNs",wall_ns()},{"allInputWireBytesPreserved",true}};
 atomic_json(folder/"capsule.json",{{"value",v},{"sha256",sha256(v.dump())}},true);
 auto verified=scan_capsule(folder);require(verified.digest==scanned.digest&&verified.frames==scanned.frames&&before==file_id(source),"sealed capsule readback mismatch");return v;
}
}
