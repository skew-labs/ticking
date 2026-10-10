#include "native_archive.hpp"
#include "geyser.grpc.pb.h"
#include <grpcpp/grpcpp.h>
#include <atomic>
#include <csignal>
#include <fstream>
#include <iostream>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
extern char**environ;
using namespace tick::archive;
std::string textfile(const fs::path&p){std::ifstream f(p);return std::string(std::istreambuf_iterator<char>(f),{});}
class Feed:public geyser::Geyser::Service{
public:std::atomic<int>calls{0},replays{0},active{0},peak{0};std::atomic<bool>valid{true};std::vector<std::string>expected;std::mutex mutex;
 grpc::Status SubscribeReplayInfo(grpc::ServerContext*,const geyser::SubscribeReplayInfoRequest*,geyser::SubscribeReplayInfoResponse*r)override{replays++;r->set_first_available(1000);return grpc::Status::OK;}
 grpc::Status Subscribe(grpc::ServerContext*ctx,grpc::ServerReaderWriter<geyser::SubscribeUpdate,geyser::SubscribeRequest>*stream)override{
  int now=++active;peak=std::max(peak.load(),now);struct Exit{std::atomic<int>&a;~Exit(){--a;}}exit{active};int n=++calls;geyser::SubscribeRequest request;if(!stream->Read(&request))return grpc::Status(grpc::StatusCode::CANCELLED,"missing request");auto auth=ctx->client_metadata().find("x-token");if(auth==ctx->client_metadata().end()||std::string(auth->second.data(),auth->second.length())!="test-private-token")valid=false;
  if(request.commitment()!=geyser::PROCESSED||request.transactions().at("executions").account_include_size()!=6||!request.slots().at("forks").interslot_updates())valid=false;if(n>1&&!request.has_from_slot())valid=false;
  for(int i=0;i<250;i++){if(i==200){for(int p=0;p<512;p++){geyser::SubscribeUpdate ping;ping.mutable_ping();stream->Write(ping);}}geyser::SubscribeUpdate u;auto*t=u.mutable_transaction();t->set_slot(1000+i);t->set_bank_id(100);auto*info=t->mutable_transaction();info->set_signature(std::string(64,char(i)));auto*m=info->mutable_meta();m->add_log_messages(std::string(600,'A'));if(i%3==0)m->mutable_err()->set_err("failed");std::string raw=u.SerializeAsString();raw.append("\xa0\x9c\x01\x01",4);u.ParseFromString(raw);{std::lock_guard lock(mutex);expected.push_back(u.SerializeAsString());}if(!stream->Write(u))return grpc::Status(grpc::StatusCode::CANCELLED,"client stopped");if(i%50==0){geyser::SubscribeUpdate ping;ping.mutable_ping();stream->Write(ping);}}
  return grpc::Status(n==1?grpc::StatusCode::UNAVAILABLE:grpc::StatusCode::RESOURCE_EXHAUSTED,"injected transport failure");
 }
};
pid_t launch(const fs::path&binary,const fs::path&root,const fs::path&config){std::vector<std::string>args={binary.string(),"--directory",root.string(),"--credentials",config.string(),"--reserve-bytes","0"};std::vector<char*>argv;for(auto&s:args)argv.push_back(s.data());argv.push_back(nullptr);pid_t pid;require(posix_spawn(&pid,binary.c_str(),nullptr,nullptr,argv.data(),environ)==0,"spawn client failed");return pid;}
void stop(pid_t pid){kill(pid,SIGTERM);int status=0;for(int i=0;i<70;i++){if(waitpid(pid,&status,WNOHANG)==pid){require(WIFEXITED(status)&&WEXITSTATUS(status)==0,"native graceful exit failed");return;}usleep(100000);}kill(pid,SIGKILL);waitpid(pid,&status,0);throw std::runtime_error("native shutdown timeout");}
int main(int argc,char**argv){try{require(argc==5,"binary output cert key arguments required");fs::path root=argv[2];require(!fs::exists(root),"new transport test directory required");fs::create_directories(root);auto cert=textfile(argv[3]);setenv("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",argv[3],1);Feed feed;grpc::SslServerCredentialsOptions ssl;ssl.pem_key_cert_pairs.push_back({textfile(argv[4]),cert});grpc::ServerBuilder builder;int port=0;builder.AddListeningPort("127.0.0.1:0",grpc::SslServerCredentials(ssl),&port);builder.RegisterService(&feed);auto server=builder.BuildAndStart();require(server&&port>0,"TLS fixture server startup");auto config=root/"private.json";atomic_json(config,{{"schema",1},{"endpoint","https://localhost:"+std::to_string(port)},{"credentials",Json::array({{{"id","fixture"},{"token","test-private-token"}}})}},true);
 auto pid=launch(argv[1],root/"archive",config);Json status;bool ready=false;for(int i=0;i<150;i++){usleep(100000);try{status=read_json(root/"archive/status.json");if(status.value("retained",0)==500&&status.value("sharedQuotaCooldownSeconds",0.0)>45){ready=true;break;}}catch(...){}}if(!ready){kill(pid,SIGKILL);waitpid(pid,nullptr,0);throw std::runtime_error("native transport/replay/cooldown proof timed out");}stop(pid);
 require(feed.calls==2&&feed.replays>=1&&feed.peak==1&&feed.valid,"request, TLS metadata or connection bound differs");require(status["failed"]==168&&status["successful"]==332,"failed/success input preservation");require(status["queue"]["pendingFrames"]==0,"undrained live input");require(status["replayOverlapObserved"]==true,"replay overlap not observed");if(std::getenv("TICK_TEST_PING_WRITE_FAILURE"))require(status.value("pingWriteHalfCloseObserved",false),"injected half-close regression case not exercised");size_t n=0;scan(root/"archive/originals/00000001/inputs.sli",[&](Frame&&f){std::lock_guard lock(feed.mutex);require(f.raw==feed.expected.at(n++),"transport wire differs from retained original");});require(n==500,"wire frame count");
 auto restart=launch(argv[1],root/"archive",config);usleep(2000000);stop(restart);require(feed.calls==2,"quota cooldown bypassed by process restart");server->Shutdown();fs::remove(config);Json result={{"passed",true},{"retainedWireFrames",n},{"failedFrames",168},{"maxProviderConnections",feed.peak.load()},{"reconnectReplay",true},{"halfClosedWritesDoNotDropUnreadResults",status.value("pingWriteHalfCloseObserved",false)},{"unknownWireFieldsPreserved",true},{"quotaCooldownSurvivesRestart",true},{"gracefulRestart",true},{"status",status},{"scope","local TLS controlled feed, not mainnet latency"}};atomic_json(root/"report.json",result,true);std::cout<<result.dump()<<'\n';return 0;
 }catch(const std::exception&e){std::cerr<<"transport checks failed: "<<e.what()<<'\n';return 1;}}
