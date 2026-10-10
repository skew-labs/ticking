#include "native_archive.hpp"
#include <fstream>
#include <iostream>
#include <csignal>
#include <sys/wait.h>
#include <unistd.h>
using namespace tick::archive;
int checks=0;
void check(bool v,const char*m){require(v,m);checks++;}
template<class F>void rejects(F fn,const char*m){bool bad=false;try{fn();}catch(...){bad=true;}check(bad,m);}
Frame input(unsigned n){geyser::SubscribeUpdate u;auto*t=u.mutable_transaction();t->set_slot(100+n);auto*info=t->mutable_transaction();info->set_signature(std::string(64,char(n%250)));auto*m=info->mutable_meta();if(n%3==0)m->mutable_err()->set_err("failed");for(int i=0;i<8;i++)m->add_log_messages("Program log shared invocation metadata with stable accounts "+std::to_string(n%13)+std::string(60,'A'));auto raw=u.SerializeAsString();raw.append("\xa0\x9c\x01\x01",4);auto c=inspect(u,raw);return {raw,wall_ns(),c};}
std::string bytes(const fs::path&p){std::ifstream f(p,std::ios::binary);return std::string(std::istreambuf_iterator<char>(f),{});}
void write(const fs::path&p,const std::string&s){std::ofstream f(p,std::ios::binary|std::ios::trunc);f<<s;}
int main(int argc,char**argv){try{
 require(argc==2&&!fs::exists(argv[1]),"fresh isolated test directory required");fs::path root=argv[1];std::vector<Frame> frames;for(unsigned i=0;i<2400;i++)frames.push_back(input(i));
 {Store s(root/"archive",0,32768);for(size_t i=0;i<frames.size();i+=8)s.append({frames.begin()+i,frames.begin()+i+8});s.finish();}
 auto checkpoint_folder=root/"archive/originals/00000002";
 auto refreshed=refresh_raw_checkpoint(checkpoint_folder);check(refreshed.at("verified")==true,"checkpoint refresh failed");
 auto checkpoint=read_json(checkpoint_folder/"verified-summary.json",16384);
 auto forged=checkpoint;forged["value"]["sourceSHA256"]=std::string(64,'0');forged["sha256"]=sha256(forged["value"].dump());
 atomic_json(checkpoint_folder/"verified-summary.json",forged,true);
 rejects([&]{refresh_raw_checkpoint(checkpoint_folder);},"checkpoint refresh skipped full content hash");
 atomic_json(checkpoint_folder/"verified-summary.json",checkpoint,true);
 auto folder=root/"archive/originals/00000001",source=folder/"inputs.sli";auto original=bytes(source);std::vector<Frame> expected;auto scan_before=scan(source,[&](Frame&&f){expected.push_back(std::move(f));});
 auto packed=pack_capsule(folder);check(fs::exists(source),"packing deleted source");check(packed.at("capsuleIdentity").at(2)<packed.at("sourceBytes"),"no compression savings");check(pack_capsule(folder)==packed,"sealed packing not idempotent");
 size_t i=0;auto replay=scan_capsule(folder,[&](Frame&&f){check(f.raw==expected.at(i).raw,"input wire mismatch");check(f.stamp==expected.at(i).stamp,"ingress timestamp mismatch");i++;});check(i==expected.size()&&replay.counts.json()==scan_before.counts.json(),"replay accounting mismatch");
 fs::remove(source);{Store s(root/"archive",0,32768);check(s.snapshot()["retained"]==2400,"retired-prefix restart counters");s.append({input(2401)});s.finish();check(s.snapshot()["retained"]==2401,"append after retirement");}
 i=0;scan(source,[&](Frame&&f){check(f.raw==expected.at(i++).raw,"transparent native replay");});check(i==expected.size(),"transparent native replay incomplete");
 auto capsule=folder/"inputs.slc",manifest=folder/"capsule.json";auto good=bytes(capsule),meta=bytes(manifest);
 auto bad=good;bad[bad.size()/2]^=1;write(capsule,bad);rejects([&]{scan_capsule(folder);},"corrupt capsule accepted");rejects([&]{Store s(root/"archive",0);},"changed capsule identity accepted at restart");write(capsule,good);
 // Restoring bytes still requires explicitly rebinding its physical identity.
 write(source,original);fs::remove(capsule);fs::remove(manifest);packed=pack_capsule(folder);fs::remove(source);
 meta=bytes(manifest);write(manifest,"{}");rejects([&]{Store s(root/"archive",0);},"missing manifest fields accepted");write(manifest,meta);
 auto missing=root/"missing";fs::create_directories(missing/"originals/00000002");write(missing/"originals/00000002/inputs.sli",original);rejects([&]{Store s(missing,0);},"unrecorded prefix deletion accepted");
 auto malformed=root/"malformed";fs::create_directories(malformed);write(malformed/"inputs.sli",original.substr(0,original.size()-1));rejects([&]{pack_capsule(malformed);},"torn source packed");check(fs::exists(malformed/"inputs.sli"),"invalid source removed");
 // A process killed after the verified capsule is sealed but BEFORE unlink
 // leaves two valid sources. Restart must prefer and validate the hot source.
 auto crash=root/"crash";{Store s(crash,0,32768);for(size_t n=0;n<frames.size();n+=8)s.append({frames.begin()+n,frames.begin()+n+8});}
 int pipefd[2];require(pipe(pipefd)==0,"pipe");pid_t child=fork();require(child>=0,"fork");if(child==0){close(pipefd[0]);try{pack_capsule(crash/"originals/00000001");char ready=1;::write(pipefd[1],&ready,1);for(;;)pause();}catch(...){_exit(3);}}
 close(pipefd[1]);char ready=0;check(read(pipefd[0],&ready,1)==1,"sealed child boundary");close(pipefd[0]);kill(child,SIGKILL);int status;waitpid(child,&status,0);{Store s(crash,0,32768);check(s.snapshot()["retained"]==2400,"crash before unlink restart");}
 auto report=Json{{"passed",true},{"assertions",checks},{"sampleFrames",frames.size()},{"capsuleFrames",scan_before.frames},{"sourceBytes",scan_before.bytes},{"capsuleBytes",packed["capsuleIdentity"][2]}};std::cout<<report.dump()<<'\n';return 0;
 }catch(const std::exception&e){std::cerr<<e.what()<<'\n';return 1;}}
