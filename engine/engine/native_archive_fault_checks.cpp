#include "native_archive.hpp"
#include <cerrno>
#include <fstream>
#include <iostream>
#include <unistd.h>
using namespace tick::archive;
static std::atomic<int>write_mode{0},sync_mode{0},rename_mode{0};
extern "C" ssize_t __real_write(int,const void*,size_t);
extern "C" int __real_fdatasync(int);
extern "C" int __real_rename(const char*,const char*);
extern "C" ssize_t __wrap_write(int fd,const void*p,size_t n){auto mode=write_mode.exchange(0);if(mode==1){errno=EIO;return -1;}if(mode==2){write_mode=1;return __real_write(fd,p,n/2);}return __real_write(fd,p,n);}
extern "C" int __wrap_fdatasync(int fd){if(sync_mode.exchange(0)){errno=EIO;return -1;}return __real_fdatasync(fd);}
extern "C" int __wrap_rename(const char*a,const char*b){if(rename_mode.load()>0){rename_mode--;errno=EIO;return -1;}return __real_rename(a,b);}
Frame sample(uint64_t slot){geyser::SubscribeUpdate u;u.mutable_slot()->set_slot(slot);u.mutable_slot()->set_status(geyser::SLOT_FINALIZED);auto raw=u.SerializeAsString();return {raw,wall_ns(),inspect(u,raw)};}
template<class F>void fails(F f){bool caught=false;try{f();}catch(...){caught=true;}require(caught,"injected failure was ignored");}
std::string bytes(const fs::path&p){std::ifstream f(p,std::ios::binary);return std::string(std::istreambuf_iterator<char>(f),{});}
int main(int argc,char**argv){try{require(argc==2&&!fs::exists(argv[1]),"new isolated fault directory required");fs::path root=argv[1];fs::create_directories(root);
 for(int mode=0;mode<4;mode++){auto path=root/std::to_string(mode);Json before;{Store s(path,0);s.append({sample(100)});before=read_json(path/"durable-tail.json");if(mode==0)write_mode=1;if(mode==1)write_mode=2;if(mode==2)sync_mode=1;if(mode==3)rename_mode=2;fails([&]{s.append({sample(101)});});require(read_json(path/"durable-tail.json")==before,"failed publication advanced durable marker");write_mode=0;sync_mode=0;rename_mode=0;}
 auto original=path/"originals/00000001/inputs.sli";auto preserved=bytes(original);if(mode==1){fails([&]{Store s(path,0);});require(bytes(original)==preserved,"torn original destructively repaired");}else{Store s(path,0);auto expected=mode==0?1:2;require(s.snapshot()["retained"]==expected,"restart did not validate actual retained prefix");require(read_json(path/"durable-tail.json")["records"]==expected,"restart did not republish validated durable prefix");}}
 Json result={{"passed",true},{"injectedFailures",Json::array({"write EIO","partial write then EIO","fdatasync EIO","tail marker rename EIO"})},{"unpublishedTornTailPreserved",true},{"restartResyncsValidatedPrefix",true}};atomic_json(root/"report.json",result,true);std::cout<<result.dump()<<'\n';return 0;
 }catch(const std::exception&e){std::cerr<<"fault checks failed: "<<e.what()<<'\n';return 1;}}
