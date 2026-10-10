#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "observer.hpp"
#include <algorithm>
#include <arpa/inet.h>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <poll.h>
#include <mutex>
#include <sched.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <thread>
#include <unistd.h>
#include <zlib.h>
using namespace slotline::observer;
static volatile sig_atomic_t stopping;
static void stop(int){stopping=1;}
static uint64_t now(){timespec t;clock_gettime(CLOCK_REALTIME,&t);return uint64_t(t.tv_sec)*1000000000+t.tv_nsec;}
static void write_all(int fd,const void*p,size_t n){size_t at=0;while(at<n){auto k=write(fd,(const uint8_t*)p+at,n-at);if(k<0&&errno==EINTR)continue;if(k<=0)throw Error("durable/output write failed");at+=k;}}
template<class T> static void out(int fd,const T&v){write_all(fd,&v,sizeof(v));}
static void frame(int fd,const char*magic,Bytes b){write_all(fd,magic,4);out(fd,uint32_t(b.size()));write_all(fd,b.data(),b.size());}
static void emit(int fd,const Fec&e){
 std::array<uint8_t,40000> b{};size_t at=0;auto copy=[&](const void*p,size_t n){if(at+n>b.size())throw Error("FEC output bound");std::memcpy(b.data()+at,p,n);at+=n;};
 auto value=[&](const auto&v){copy(&v,sizeof(v));};value(e.stamp);value(e.slot);value(e.index);value(uint32_t(e.version));value(uint32_t(u16(e.packets[0].data()+83)));copy(e.root.data(),32);copy(e.chain.data(),32);
 for(unsigned i=0;i<32;i++){const auto&p=e.packets[i];value(u32(p.data()+73));value(uint16_t(p[85]));auto size=uint16_t(u16(p.data()+86)-88);value(size);copy(p.data()+88,size);}
 frame(fd,"SLF1",Bytes(b.data(),at));
}
static void parse_batch(int fd,Bytes raw,uint64_t slot,uint32_t first,std::span<const Hash>watched,const Hash*branch=nullptr,uint32_t last=0,uint16_t parent=0,uint16_t flags=0,const Hash*previous=nullptr,const Hash*last_root=nullptr){
 if(raw.size()<8||!u64(raw.data())||u64(raw.data())>65536)throw Error("entry header");
 // Validate the complete batch before publishing any observation, including
 // the suffix. A malformed later entry cannot leave a partially published batch.
 size_t check=8;uint64_t total_transactions=0;for(uint64_t entry=0;entry<u64(raw.data());entry++){
  if(raw.size()-check<48)throw Error("entry truncated");auto n=u64(raw.data()+check+40);check+=48;if(n>32768)throw Error("entry transactions");
  total_transactions+=n;if(branch&&total_transactions>32768)throw Error("batch proof transaction bound");
  for(uint64_t i=0;i<n;i++){auto w=parse_wire(raw.subspan(check),watched);check+=w.size;}
 }for(size_t i=check;i<raw.size();i++)if(raw[i])throw Error("entry nonzero suffix");
 size_t pos=8;uint32_t ordinal=0,selected=0,dependencies=0;std::vector<Hash> digests;Hash terminal{};
 for(uint64_t entry=0;entry<u64(raw.data());entry++){
  if(raw.size()-pos<48)throw Error("entry truncated");std::copy_n(raw.data()+pos+8,32,terminal.begin());auto count=u64(raw.data()+pos+40);pos+=48;if(count>32768)throw Error("entry transactions");
  for(uint64_t within=0;within<count;within++){
   auto w=parse_wire(raw.subspan(pos),watched);if(w.selected||w.has_lookups){
    std::array<uint8_t,4352>b{};size_t at=0;auto cp=[&](const auto&v){std::memcpy(b.data()+at,&v,sizeof(v));at+=sizeof(v);};
    cp(slot);cp(first);cp(uint32_t(entry));cp(uint32_t(within));cp(ordinal);std::memcpy(b.data()+at,w.signature.data(),64);at+=64;
    if(branch){std::memcpy(b.data()+at,branch->data(),32);at+=32;}
    std::memcpy(b.data()+at,raw.data()+pos,w.size);at+=w.size;frame(fd,branch?"SLW2":"SLT1",Bytes(b.data(),at));selected+=w.selected;dependencies+=!w.selected;
   }if(branch){if(digests.size()==32768)throw Error("batch proof transaction bound");digests.push_back(hash(raw.subspan(pos,w.size)));}ordinal++;pos+=w.size;
  }
 }
 for(size_t i=pos;i<raw.size();i++)if(raw[i])throw Error("entry nonzero suffix");
 if(branch&&previous&&last_root){constexpr size_t proof_header=8+4+4+2+2+32*4+4;std::vector<uint8_t> proof(proof_header+32*digests.size());size_t at=0;auto put=[&](const void*p,size_t n){if(n>proof.size()-at)throw Error("batch proof serialization bound");std::memcpy(proof.data()+at,p,n);at+=n;};auto value=[&](const auto&v){put(&v,sizeof(v));};
  value(slot);value(first);value(last);value(parent);value(flags);put(branch->data(),32);put(previous->data(),32);put(last_root->data(),32);put(terminal.data(),32);value(uint32_t(digests.size()));for(auto&h:digests)put(h.data(),32);if(at!=proof.size())throw Error("batch proof serialization size");frame(fd,"SLB2",proof);
 }else frame(fd,"SLB1",Bytes((const uint8_t*)&slot,8));
 std::fprintf(stderr,"{\"transactions\":%u,\"selected\":%u,\"lookupDependencies\":%u,\"executionKnown\":false}\n",ordinal,selected,dependencies);
}
class Batches {
 struct Ref {const Fec*f{};bool consumed{},started{};};
 struct Chain {bool active{};uint64_t slot{};uint32_t first{},next{};uint16_t parent{};Hash previous{},anchor{},batch_previous{};size_t size{};std::unique_ptr<uint8_t[]> bytes;};
 std::array<Ref,512> refs_{};size_t count_{};std::array<Chain,8> chains_{};
 int output_;std::span<const Hash>watched_;
 public:
 Batches(int output,std::span<const Hash>watched):output_(output),watched_(watched){for(auto&c:chains_)c.bytes=std::make_unique<uint8_t[]>(4<<20);}
 void add(const Fec&fec){if(count_==refs_.size())throw Error("batch chain arena full; repair/retirement barrier required");refs_[count_++]={&fec};
  bool progress=true;while(progress){progress=false;
   for(size_t i=0;i<count_;i++){auto&r=refs_[i];if(r.consumed||r.started)continue;auto&f=*r.f;bool start=f.index==0;
    for(size_t j=0;j<count_&&!start;j++){auto&p=*refs_[j].f;start=p.slot==f.slot&&p.root==f.chain&&p.index+32==f.index&&(p.packets[31][85]&64);}
    if(!start)continue;Chain*free=nullptr;for(auto&c:chains_)if(!c.active){free=&c;break;}if(!free)throw Error("entry chain arena full; retain and repair");
    auto&c=*free;c.active=true;c.slot=f.slot;c.first=c.next=f.index;c.previous=f.chain;c.batch_previous=f.chain;c.anchor=f.root;c.parent=u16(f.packets[0].data()+83);c.size=0;r.started=true;progress=true;
   }
   for(auto&c:chains_)if(c.active){Ref*next=nullptr;
    for(size_t i=0;i<count_;i++){auto&r=refs_[i];auto&f=*r.f;if(!r.consumed&&f.slot==c.slot&&f.index==c.next&&f.chain==c.previous){if(next)throw Error("equivocating signed chain; authoritative fork resolution required");next=&r;}}
    if(!next)continue;auto&f=*next->f;if(u16(f.packets[0].data()+83)!=c.parent)throw Error("chain parent changed");
    for(unsigned i=0;i<32;i++){auto&p=f.packets[i];size_t n=u16(p.data()+86)-88;if(n>(4<<20)-c.size)throw Error("entry batch capacity");std::copy_n(p.begin()+88,n,c.bytes.get()+c.size);c.size+=n;
     if(p[85]&64){parse_batch(output_,Bytes(c.bytes.get(),c.size),c.slot,c.first,watched_,&c.anchor,f.index+i,c.parent,p[85],&c.batch_previous,&f.root);c.size=0;c.first=f.index+i+1;c.anchor=f.root;c.batch_previous=f.root;}}
    c.next+=32;c.previous=f.root;next->consumed=true;next->started=true;progress=true;
    if(f.packets[31][85]&64){if(c.size)throw Error("batch suffix");c.active=false;}
   }
  }
 }
 void retire(uint64_t slot){for(auto&c:chains_)if(c.active&&c.slot<=slot)throw Error("cannot retire incomplete entry chain");size_t n=0;for(size_t i=0;i<count_;i++)if(refs_[i].f->slot>slot)refs_[n++]=refs_[i];count_=n;}
};
#pragma pack(push,1)
struct Leader {uint64_t slot;Hash key;uint16_t version,reserved;uint32_t max_index;};
struct Record {char magic[4];uint16_t size,version;uint64_t stamp,slot;Hash leader;uint32_t peer;uint16_t port,reserved;uint32_t crc;};
#pragma pack(pop)
static_assert(sizeof(Leader)==48&&sizeof(Record)==68);
struct alignas(64) Packet {Record header{};uint32_t max_index{};std::array<uint8_t,2048> bytes{};};
static void exact(int fd,void*p,size_t n){size_t at=0;while(at<n){auto k=read(fd,(uint8_t*)p+at,n-at);if(k<0&&errno==EINTR)continue;if(k<=0)throw Error("torn input; retain and repair");at+=k;}}
static std::vector<Leader> leaders(const char*path){int fd=open(path,O_RDONLY|O_CLOEXEC);if(fd<0)throw Error("verified leader schedule required");char magic[4];uint32_t n;exact(fd,magic,4);exact(fd,&n,4);
 if(std::memcmp(magic,"SLL1",4)||!n||n>32768)throw Error("leader schedule header");std::vector<Leader> rows(n);exact(fd,rows.data(),n*sizeof(Leader));uint8_t suffix;if(read(fd,&suffix,1)!=0)throw Error("leader suffix");close(fd);
 for(size_t i=0;i<n;i++)if(!rows[i].version||rows[i].reserved||rows[i].max_index<32||rows[i].max_index>32768||(i&&rows[i-1].slot>=rows[i].slot))throw Error("leader bounds/order");return rows;
}
static const Leader* find(const std::vector<Leader>&rows,uint64_t slot){auto p=std::lower_bound(rows.begin(),rows.end(),slot,[](auto&l,auto s){return l.slot<s;});return p!=rows.end()&&p->slot==slot?&*p:nullptr;}
static void cpu(int id){if(id<0)return;if(id>=CPU_SETSIZE)throw Error("CPU affinity bound");cpu_set_t available,set;CPU_ZERO(&set);
 if(sched_getaffinity(0,sizeof(available),&available)||!CPU_ISSET(id,&available))throw Error("CPU outside authorized process affinity");CPU_SET(id,&set);if(sched_setaffinity(0,sizeof(set),&set))throw Error("CPU affinity failed");}
static uint64_t retirement(const char*path,uint64_t previous){
 if(!path)return previous;int fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0){if(errno==ENOENT)return previous;throw Error("retirement checkpoint unavailable");}
 struct stat s;if(fstat(fd,&s)||!S_ISREG(s.st_mode)||s.st_uid!=getuid()||(s.st_mode&077)||s.st_size!=12){close(fd);throw Error("retirement checkpoint permissions/length");}
 uint8_t raw[12];exact(fd,raw,12);close(fd);if(std::memcmp(raw,"SLC1",4)||u64(raw+4)<previous)throw Error("retirement checkpoint regressed");return u64(raw+4);
}
static void udp(FecArena&arena,int output,Batches*batches,const char*address,const char*peer,const char*schedule,const char*journal,const char*retire_path,int rx_cpu,int disk_cpu,int verify_cpu){
 auto rows=leaders(schedule);char host[64];unsigned port;if(std::sscanf(address,"%63[^:]:%u",host,&port)!=2||!port||port>65535)throw Error("numeric IPv4:port required");
 sockaddr_in bindaddr{};bindaddr.sin_family=AF_INET;bindaddr.sin_port=htons(port);in_addr allowed{};
 if(inet_pton(AF_INET,host,&bindaddr.sin_addr)!=1||inet_pton(AF_INET,peer,&allowed)!=1)throw Error("numeric peer required");
 int log=open(journal,O_RDWR|O_CREAT|O_APPEND|O_CLOEXEC,0600);if(log<0||flock(log,LOCK_EX|LOCK_NB))throw Error("journal ownership");
 auto parent_dir=std::filesystem::path(journal).parent_path();if(parent_dir.empty())parent_dir=".";
 int parent_fd=open(parent_dir.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC);if(parent_fd<0||fsync(parent_fd))throw Error("original journal directory durability");close(parent_fd);
 // Replay originals without rewriting/truncating. The downstream journal makes
 // replay idempotent. A torn/checksum-invalid original stops admission.
 uint64_t floor=retirement(retire_path,0);if(floor)arena.retire(floor);
 lseek(log,0,SEEK_SET);Record record;for(;;){auto n=read(log,&record,sizeof(record));if(!n)break;if(n!=(ssize_t)sizeof(record)||std::memcmp(record.magic,"SLP1",4)||record.size>2048||!record.size)throw Error("original header integrity");
  std::array<uint8_t,2048>b{};exact(log,b.data(),record.size);auto crc=crc32(0,(const Bytef*)&record,sizeof(record)-4);crc=crc32(crc,b.data(),record.size);if(crc!=record.crc)throw Error("original CRC");
  auto leader=find(rows,record.slot);if(!leader||leader->key!=record.leader||leader->version!=record.version)throw Error("historical leader missing");
  if(record.slot>floor)if(auto f=arena.add(Bytes(b.data(),record.size),leader->key,leader->version,leader->max_index,record.stamp)){if(batches)batches->add(*f);else emit(output,*f);}
 }lseek(log,0,SEEK_END);
 int sock=socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);if(sock<0)throw Error("UDP socket");int buffer=8<<20,overflow=1;
 if(setsockopt(sock,SOL_SOCKET,SO_RCVBUF,&buffer,sizeof(buffer))||setsockopt(sock,SOL_SOCKET,SO_RXQ_OVFL,&overflow,sizeof(overflow))||bind(sock,(sockaddr*)&bindaddr,sizeof(bindaddr)))throw Error("UDP setup");
 socklen_t blen=sizeof(buffer);getsockopt(sock,SOL_SOCKET,SO_RCVBUF,&buffer,&blen);
 auto inbound=std::make_unique<Spsc<Packet,1024>>(),durable=std::make_unique<Spsc<Packet,1024>>();
 std::atomic<bool> rx_done=false,disk_done=false,failed=false;std::atomic<uint64_t> packets=0,peer_rejects=0;
 std::string failure;std::mutex error_lock;auto fail=[&](const char*why){std::lock_guard<std::mutex>lock(error_lock);if(!failed.exchange(true))failure=why;};
 std::thread receiver([&]{try{cpu(rx_cpu);constexpr unsigned burst=32;std::array<mmsghdr,burst>msgs{};std::array<iovec,burst>vec{};std::array<sockaddr_in,burst>peers{};std::array<std::array<uint8_t,2048>,burst>data{};alignas(cmsghdr) std::array<std::array<uint8_t,128>,burst>controls{};
  while(!stopping&&!failed){pollfd p{sock,POLLIN,0};auto ready=poll(&p,1,100);if(ready<0&&errno==EINTR)continue;if(ready<0)throw Error("UDP poll");if(!ready)continue;
   for(unsigned i=0;i<burst;i++){msgs[i]={};vec[i]={data[i].data(),2048};auto&h=msgs[i].msg_hdr;h.msg_name=&peers[i];h.msg_namelen=sizeof(sockaddr_in);h.msg_iov=&vec[i];h.msg_iovlen=1;h.msg_control=controls[i].data();h.msg_controllen=128;}
   auto n=recvmmsg(sock,msgs.data(),burst,MSG_DONTWAIT,nullptr);if(n<0&&(errno==EAGAIN||errno==EINTR))continue;if(n<0)throw Error("batched UDP receive");
   for(int i=0;i<n;i++){
    for(auto c=CMSG_FIRSTHDR(&msgs[i].msg_hdr);c;c=CMSG_NXTHDR(&msgs[i].msg_hdr,c))if(c->cmsg_level==SOL_SOCKET&&c->cmsg_type==SO_RXQ_OVFL){uint32_t lost;std::memcpy(&lost,CMSG_DATA(c),4);if(lost)throw Error("kernel drop; repair retained range before admission");}
    if(peers[i].sin_addr.s_addr!=allowed.s_addr||msgs[i].msg_hdr.msg_flags&(MSG_TRUNC|MSG_CTRUNC)||msgs[i].msg_len<83){peer_rejects++;continue;}
    auto slot=u64(data[i].data()+65);auto l=find(rows,slot);if(!l){peer_rejects++;continue;}auto packet=inbound->producer();if(!packet)throw Error("receive ring saturated; upstream repair required");
    *packet=Packet{};auto&h=packet->header;std::memcpy(h.magic,"SLP1",4);h.size=msgs[i].msg_len;h.version=l->version;h.stamp=now();h.slot=slot;h.leader=l->key;h.peer=peers[i].sin_addr.s_addr;h.port=peers[i].sin_port;packet->max_index=l->max_index;
    std::copy_n(data[i].begin(),h.size,packet->bytes.begin());auto crc=crc32(0,(Bytef*)&h,sizeof(h)-4);h.crc=crc32(crc,packet->bytes.data(),h.size);inbound->publish();packets++;
   }
  }
 }catch(const std::exception&e){fail(e.what());}rx_done=true;});
 std::thread storage([&]{try{cpu(disk_cpu);std::array<Packet,64> batch{};
  while((!rx_done||inbound->consumer())&&!failed){unsigned n=0;while(n<batch.size()){auto p=inbound->consumer();if(!p)break;batch[n++]=*p;inbound->release();}
   if(!n){std::this_thread::sleep_for(std::chrono::microseconds(100));continue;}
   struct stat info;struct statvfs disk;if(fstat(log,&info)||fstatvfs(log,&disk)||uint64_t(info.st_size)+(uint64_t(n)*2116)>(8ULL<<30)||uint64_t(disk.f_bavail)*disk.f_frsize<(200ULL<<30))throw Error("storage reserve; retain originals and pause");
   for(unsigned i=0;i<n;i++){write_all(log,&batch[i].header,sizeof(Record));write_all(log,batch[i].bytes.data(),batch[i].header.size);}if(fdatasync(log))throw Error("original fdatasync");
   for(unsigned i=0;i<n;i++){auto p=durable->producer();if(!p)throw Error("verified input ring saturated; replay originals");*p=batch[i];durable->publish();}
  }
 }catch(const std::exception&e){fail(e.what());}disk_done=true;});
 std::fprintf(stderr,"{\"source\":\"slotline_cpp_observer\",\"receiveBufferBytes\":%d,\"ringPackets\":1024,\"workers\":3,\"executionKnown\":false}\n",buffer);
 try{cpu(verify_cpu);auto next_control=std::chrono::steady_clock::now();while((!disk_done||durable->consumer())&&!failed){
   if(std::chrono::steady_clock::now()>=next_control){auto updated=retirement(retire_path,floor);if(updated>floor){if(batches)batches->retire(updated);arena.retire(updated);floor=updated;}next_control=std::chrono::steady_clock::now()+std::chrono::milliseconds(100);}
   auto p=durable->consumer();if(!p){std::this_thread::sleep_for(std::chrono::microseconds(100));continue;}
   if(auto f=arena.add(Bytes(p->bytes.data(),p->header.size),p->header.leader,p->header.version,p->max_index,p->header.stamp)){if(batches)batches->add(*f);else emit(output,*f);}durable->release();
 }}catch(const std::exception&e){fail(e.what());}
 receiver.join();storage.join();close(sock);close(log);if(failed)throw Error(failure);
 std::fprintf(stderr,"{\"retainedPackets\":%lu,\"peerRejected\":%lu}\n",packets.load(),peer_rejects.load());
}
int main(int argc,char**argv){try{
 struct sigaction action{};action.sa_handler=stop;sigaction(SIGTERM,&action,nullptr);sigaction(SIGINT,&action,nullptr);
 const char*bindaddr=nullptr,*peer=nullptr,*schedule=nullptr,*journal=nullptr,*retire_path=nullptr;int output=STDOUT_FILENO,rx=-1,disk=-1,verify=-1;uint16_t version=0;uint64_t slot=0;uint32_t first=0;bool batch=false,stream=false,wire=false;std::vector<Hash> watched;
 for(int i=1;i<argc;i++){std::string a=argv[i];auto arg=[&](){if(++i==argc)throw Error("missing option value");return argv[i];};
  if(a=="--version")version=std::stoul(arg());else if(a=="--udp")bindaddr=arg();else if(a=="--peer")peer=arg();else if(a=="--leaders")schedule=arg();else if(a=="--journal")journal=arg();
  else if(a=="--output"){output=open(arg(),O_WRONLY|O_CREAT|O_APPEND|O_CLOEXEC,0600);if(output<0)throw Error("output path");}
  else if(a=="--parse-batch")batch=true;else if(a=="--parse-stream")stream=true;else if(a=="--slot")slot=std::stoull(arg());else if(a=="--first-index")first=std::stoul(arg());
  else if(a=="--rx-cpu")rx=std::stoi(arg());else if(a=="--disk-cpu")disk=std::stoi(arg());else if(a=="--verify-cpu")verify=std::stoi(arg());
  else if(a=="--wire")wire=true;
  else if(a=="--retire-path")retire_path=arg();
  else if(a=="--watch-hex"){std::string s=arg();if(s.size()!=64)throw Error("watch key");Hash h;for(unsigned j=0;j<32;j++){auto b=s.substr(j*2,2);size_t used;auto v=std::stoul(b,&used,16);if(used!=2)throw Error("watch hex");h[j]=v;}if(watched.size()==256)throw Error("watch bound");watched.push_back(h);}
  else throw Error("unknown option");
 }
 if(batch){std::vector<uint8_t>b(4<<20);auto n=fread(b.data(),1,b.size(),stdin);if(!feof(stdin))throw Error("batch bound");parse_batch(output,Bytes(b.data(),n),slot,first,watched);return 0;}
 if(stream){uint32_t n;while(fread(&slot,8,1,stdin)==1){if(fread(&first,4,1,stdin)!=1||fread(&n,4,1,stdin)!=1||!n||n>4<<20)throw Error("parse frame");std::vector<uint8_t>b(n);if(fread(b.data(),1,n,stdin)!=n)throw Error("parse frame truncated");parse_batch(output,b,slot,first,watched);}if(!feof(stdin))throw Error("parse input");return 0;}
 FecArena arena;std::unique_ptr<Batches>batches;if(wire)batches=std::make_unique<Batches>(output,watched);
 if(bindaddr){if(!peer||!schedule||!journal)throw Error("authorized peer, leaders and original journal required");udp(arena,output,batches.get(),bindaddr,peer,schedule,journal,retire_path,rx,disk,verify);}
 else{if(!version)throw Error("version required");uint32_t n;Hash leader;std::array<uint8_t,2048>b{};while(fread(&n,4,1,stdin)==1){if(!n||n>b.size()||fread(leader.data(),1,32,stdin)!=32||fread(b.data(),1,n,stdin)!=n)throw Error("fixture frame");if(auto f=arena.add(Bytes(b.data(),n),leader,version,32768,now())){if(batches)batches->add(*f);else emit(output,*f);}}if(!feof(stdin))throw Error("fixture input");}
 if(output!=STDOUT_FILENO){if(fdatasync(output))throw Error("output sync");close(output);}
 std::fprintf(stderr,"{\"received\":%lu,\"rejected\":%lu,\"duplicates\":%lu,\"completedFECs\":%lu,\"executionKnown\":false,\"firedancerLinked\":false}\n",arena.received,arena.rejected,arena.duplicate,arena.completed);return 0;
 }catch(const std::exception&e){std::fprintf(stderr,"SLOTLINE observer stopped: %s\n",e.what());return 2;}}
