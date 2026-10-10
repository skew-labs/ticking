#include "native_archive.hpp"
#include "managed_scope.hpp"
#include "geyser.grpc.pb.h"
#include <grpcpp/grpcpp.h>
#include <grpcpp/impl/rpc_method.h>
#include <grpcpp/support/sync_stream.h>
#include <csignal>
#include <fcntl.h>
#include <iostream>
#include <regex>
#include <set>
#include <sys/stat.h>
#include <unistd.h>

// Preserve the original wire including unknown fields and their exact order.
// Generated SubscribeUpdate serialization is intentionally not the journal input.
struct WireUpdate {std::string raw;uint64_t stamp=0;};
namespace grpc {
template<>class SerializationTraits<WireUpdate> {
public:
 static Status Deserialize(ByteBuffer*buffer,WireUpdate*out){
  if(!buffer||!buffer->Length()||buffer->Length()>tick::archive::max_frame)return Status(StatusCode::RESOURCE_EXHAUSTED,"frame bound");
  std::vector<Slice>slices;auto status=buffer->Dump(&slices);if(!status.ok())return status;
  out->stamp=tick::archive::wall_ns();out->raw.clear();out->raw.reserve(buffer->Length());for(auto&s:slices)out->raw.append(reinterpret_cast<const char*>(s.begin()),s.size());buffer->Clear();return Status::OK;
 }
 static Status Serialize(const WireUpdate&,ByteBuffer*,bool*){return Status(StatusCode::UNIMPLEMENTED,"receive only");}
};
}
namespace {
using namespace tick::archive;
volatile sig_atomic_t stopped=0;
void signal_stop(int){stopped=1;}
struct Credential {std::string id,token;uint64_t until=0;unsigned failures=0;};
struct Credentials {
 std::string target;std::vector<Credential>items;size_t index=0;uint64_t global_until=0,rotations=0;
 explicit Credentials(const fs::path&p){int fd=::open(p.c_str(),O_RDONLY|O_CLOEXEC|O_NOFOLLOW);require(fd>=0,"private credential file unavailable");struct stat s{};if(fstat(fd,&s)||!S_ISREG(s.st_mode)||(s.st_mode&077)||s.st_uid!=geteuid()||s.st_size>32768){close(fd);throw std::runtime_error("private credential file permissions rejected");}std::string raw(s.st_size,'\0');size_t used=0;while(used<raw.size()){auto n=read(fd,raw.data()+used,raw.size()-used);if(n<=0){close(fd);throw std::runtime_error("credential read failed");}used+=n;}close(fd);auto j=Json::parse(raw);require(j.at("schema")==1,"credential schema rejected");auto endpoint=j.at("endpoint").get<std::string>();require(std::regex_match(endpoint,std::regex("https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?")),"TLS endpoint rejected");target=endpoint.substr(8);if(target.back()=='/')target.pop_back();std::set<std::string>ids,tokens;for(auto&v:j.at("credentials")){Credential c{v.at("id"),v.at("token")};require(std::regex_match(c.id,std::regex("[A-Za-z0-9-]{1,32}"))&&!c.token.empty()&&c.token.size()<=4096&&c.token.find_first_of(" \t\n\r\v\f")==std::string::npos,"credential entry rejected");require(ids.insert(c.id).second&&tokens.insert(c.token).second,"duplicate private credential");items.push_back(std::move(c));}require(!items.empty()&&items.size()<=5,"credential count bound");}
 bool available(){auto now=mono_ns();if(now<global_until)return false;for(size_t i=0;i<items.size();i++){auto n=(index+i)%items.size();if(items[n].until<=now){if(n!=index)rotations++;index=n;return true;}}return false;}
 void failure(grpc::StatusCode code){auto now=mono_ns();auto&c=items[index];c.failures++;if(code==grpc::StatusCode::RESOURCE_EXHAUSTED){global_until=now+60000000000ULL;return;}uint64_t delay=(code==grpc::StatusCode::UNAUTHENTICATED||code==grpc::StatusCode::PERMISSION_DENIED)?120:std::min(30u,1u<<std::min(c.failures,5u));c.until=now+delay*1000000000;if(items.size()>1){index=(index+1)%items.size();rotations++;}}
 Json status()const{auto now=mono_ns();auto remaining=global_until>now?global_until-now:0;return {{"credentials",items.size()},{"activeCredential",items[index].id},{"connectionsPerPool",1},{"rotations",rotations},{"sharedQuotaCooldownSeconds",remaining/1e9},{"sharedQuotaCooldownUntilUnixNs",remaining?wall_ns()+remaining:0}};}
};
const std::vector<std::string> programs={
 "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8","CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK","CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C","LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo","pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA","whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"};
bool ping_write(grpc::ClientReaderWriter<geyser::SubscribeRequest,WireUpdate>&stream,const geyser::SubscribeRequest&ping){
 auto result=stream.Write(ping);
#ifdef TICK_RECORDER_TEST_FAULTS
 // Compiled only into the loopback-only sanitizer/fault binary, never release.
 if(std::getenv("TICK_TEST_PING_WRITE_FAILURE"))return false;
#endif
 return result;
}
int run(Store&store,Credentials&pool,const fs::path&scope_path){
#ifdef TICK_RECORDER_TEST_FAULTS
 require(pool.target.rfind("localhost:",0)==0,"fault binary accepts loopback fixtures only");
#endif
 FrameQueue queue;std::atomic<bool>writer_failed{false},watch_done{false};std::atomic<uint64_t>last_message{mono_ns()};std::mutex call_mutex;grpc::ClientContext*active_context=nullptr;
 std::thread writer([&]{try{std::vector<Frame>batch;batch.reserve(128);while(queue.take(batch)){if(!batch.empty()){store.append(batch);queue.commit(batch.size());batch.clear();}store.report(queue.status());}store.finish();store.report(queue.status(),true);}catch(const std::exception&e){writer_failed=true;store.set({{"failure","original_writer_failure"},{"failureReason",e.what()},{"state","paused_reconciliation_required"}});queue.close();}});
 std::thread watchdog([&]{while(!watch_done){if(stopped||writer_failed||mono_ns()-last_message>45000000000ULL){std::lock_guard lock(call_mutex);if(active_context)active_context->TryCancel();}std::this_thread::sleep_for(std::chrono::milliseconds(100));}});
 std::map<std::string,uint64_t>leases;uint64_t next_scope=0;std::vector<std::string>watch_mints;
 auto scope=[&]{auto now=wall_ns()/1000000000ULL;try{leases=tick::managed::load(scope_path,now);}catch(...){store.set({{"managedScopeReadError",true}});}return tick::managed::active(leases,now);};
 int result=0;try{
 auto prior=store.snapshot();std::string previous=prior.value("activeCredential",std::string{});for(size_t i=0;i<pool.items.size();i++)if(pool.items[i].id==previous)pool.index=(i+1)%pool.items.size();uint64_t reconnects=prior.value("reconnects",uint64_t(0));
 auto cooldown=prior.value("sharedQuotaCooldownUntilUnixNs",uint64_t(0));auto now=wall_ns();if(cooldown>now)pool.global_until=mono_ns()+std::min(cooldown-now,uint64_t(60000000000ULL));
 store.set({{"programs",programs},{"publicationGate","existing processed-metadata decoder, fork and reference-price policies"},{"originalEncoding","SLI1 or lossless SLZ1; exact received protobuf bytes and CRC"}});
 while(!stopped&&!writer_failed){if(!pool.available()){store.set(pool.status());std::this_thread::sleep_for(std::chrono::milliseconds(100));continue;}queue.drain();auto&credential=pool.items[pool.index];store.set(pool.status());auto cursor=store.cursor();auto high=store.snapshot().at("lastSlot").get<uint64_t>();
 grpc::ChannelArguments args;args.SetMaxReceiveMessageSize(max_frame);args.SetInt(GRPC_ARG_KEEPALIVE_TIME_MS,20000);args.SetInt(GRPC_ARG_KEEPALIVE_TIMEOUT_MS,10000);auto channel=grpc::CreateCustomChannel(pool.target,grpc::SslCredentials(grpc::SslCredentialsOptions{}),args);auto stub=geyser::Geyser::NewStub(channel);
 store.set({{"connected",false},{"state","connecting"},{"resumeFromSlot",cursor},{"replayOverlapObserved",false}});
 if(cursor){grpc::ClientContext info;info.AddMetadata("x-token",credential.token);info.set_deadline(std::chrono::system_clock::now()+std::chrono::seconds(8));geyser::SubscribeReplayInfoRequest request;geyser::SubscribeReplayInfoResponse response;auto rc=stub->SubscribeReplayInfo(&info,request,&response);if(!rc.ok()){pool.failure(rc.error_code());store.set({{"lastTransportError",int(rc.error_code())}});continue;}require(response.has_first_available(),"provider replay range unavailable");store.set({{"firstAvailableReplaySlot",response.first_available()}});if(cursor<response.first_available()){store.gap(cursor,response.first_available(),response.SerializeAsString());cursor=response.first_available();store.set({{"resumeFromSlot",cursor}});}}
 grpc::ClientContext context;context.AddMetadata("x-token",credential.token);
 struct CallGuard {std::mutex&m;grpc::ClientContext*&p;grpc::ClientContext&c;~CallGuard(){std::lock_guard lock(m);c.TryCancel();p=nullptr;}} guard{call_mutex,active_context,context};
 {std::lock_guard lock(call_mutex);active_context=&context;last_message=mono_ns();}
 using Reader=grpc::ClientReaderWriter<geyser::SubscribeRequest,WireUpdate>;std::unique_ptr<Reader>stream(grpc::internal::ClientReaderWriterFactory<geyser::SubscribeRequest,WireUpdate>::Create(channel.get(),grpc::internal::RpcMethod("/geyser.Geyser/Subscribe",grpc::internal::RpcMethod::BIDI_STREAMING,channel),&context));
 geyser::SubscribeRequest request;request.set_commitment(geyser::PROCESSED);auto&executions=(*request.mutable_transactions())["executions"];executions.set_vote(false);for(auto&p:programs)executions.add_account_include(p);(*request.mutable_blocks_meta())["identity"];auto&forks=(*request.mutable_slots())["forks"];forks.set_filter_by_commitment(false);forks.set_interslot_updates(true);if(cursor)request.set_from_slot(cursor);
 watch_mints=scope();for(auto&mint:watch_mints)executions.add_account_include(mint);store.set({{"managedMints",watch_mints},{"managedScopePolicy","verified_exact_mint_on_shared_cpp_result_stream"},{"managedScopeStartSlot",high}});next_scope=mono_ns()+1000000000ULL;
 bool subscribed=stream->Write(request),writes_open=subscribed;WireUpdate wire;geyser::SubscribeUpdate parsed;bool connected=false,overlap=false;
 // A failed ping write can coincide with a server half-close. Continue reading
 // its already-sent results until EOF; write failure is not a read boundary.
 try{while(subscribed&&!stopped&&!writer_failed&&stream->Read(&wire)){last_message=mono_ns();
 if(writes_open&&mono_ns()>=next_scope){next_scope=mono_ns()+1000000000ULL;auto desired=scope();if(desired!=watch_mints){auto update=request;update.clear_from_slot();auto&filter=(*update.mutable_transactions())["executions"];filter.clear_account_include();for(auto&p:programs)filter.add_account_include(p);for(auto&m:desired)filter.add_account_include(m);writes_open=stream->Write(update);if(writes_open){watch_mints=std::move(desired);store.set({{"managedMints",watch_mints},{"managedScopeStartSlot",store.snapshot().value("lastSlot",uint64_t(0))},{"managedScopeReadError",false}});}else store.set({{"managedScopeWriteHalfCloseObserved",true}});}}
 auto counts=inspect(parsed,wire.raw);if(parsed.has_ping()){if(writes_open){geyser::SubscribeRequest ping;ping.mutable_ping()->set_id(1);writes_open=ping_write(*stream,ping);if(!writes_open)store.set({{"pingWriteHalfCloseObserved",true}});}continue;}if(!counts.retained)continue;if(!connected){store.set({{"connected",true},{"state","recording_executed_results"},{"activeCredential",credential.id}});connected=true;}credential.failures=0;if(cursor&&!overlap&&counts.last<=high){store.set({{"replayOverlapObserved",true}});overlap=true;}Frame frame{std::move(wire.raw),wire.stamp,counts};if(!queue.push(std::move(frame)))break;}}
 catch(...){context.TryCancel();{std::lock_guard lock(call_mutex);active_context=nullptr;}stream->Finish();throw;}
 if(stopped||writer_failed||!subscribed){context.TryCancel();}
 auto rc=stream->Finish();{std::lock_guard lock(call_mutex);active_context=nullptr;}queue.drain();store.set({{"connected",false},{"gapRecoveryRequired",true},{"reconnects",++reconnects},{"lastTransportError",int(rc.error_code())}});if(!stopped&&!writer_failed)pool.failure(rc.ok()?grpc::StatusCode::UNAVAILABLE:rc.error_code());
 }
 }catch(const std::exception&e){result=78;store.set({{"failure","native_recorder_failure"},{"failureReason",e.what()},{"state","paused_reconciliation_required"}});}
 watch_done=true;watchdog.join();queue.close();writer.join();if(writer_failed)result=78;store.set({{"connected",false},{"state",result?"paused_reconciliation_required":"stopped_cleanly"}});try{store.report(queue.status(),true);}catch(...){result=78;}return result;
}
}
int main(int argc,char**argv){using namespace tick::archive;std::signal(SIGTERM,signal_stop);std::signal(SIGINT,signal_stop);std::signal(SIGPIPE,SIG_IGN);
 try{fs::path root,credentials,replay;uint64_t reserve=32ULL*1024*1024*1024;bool inspect_only=false,cache_audit=false;for(int i=1;i<argc;i++){std::string a=argv[i];if(a=="--directory"&&i+1<argc)root=argv[++i];else if(a=="--credentials"&&i+1<argc)credentials=argv[++i];else if(a=="--replay-input"&&i+1<argc)replay=argv[++i];else if(a=="--reserve-bytes"&&i+1<argc)reserve=std::stoull(argv[++i]);else if(a=="--inspect")inspect_only=true;else if(a=="--cache-audit")cache_audit=true;else throw std::runtime_error("invalid native recorder arguments");}require(!root.empty(),"archive directory required");
 if(cache_audit){std::vector<fs::path>paths;for(auto&e:fs::directory_iterator(root/"originals"))if(e.is_directory())paths.push_back(e.path());std::sort(paths.begin(),paths.end());Json missing=Json::array();uint64_t count=0,total=0;for(size_t i=0;i+1<paths.size();i++){Counts c;uint64_t bytes=0;if(Checkpoints::load(paths[i],c,bytes)){count++;total+=bytes;}else missing.push_back(paths[i].filename().string());}std::cout<<Json{{"validClosed",count},{"validBytes",total},{"missing",missing}}.dump()<<'\n';return 0;}
 Store store(root,reserve);
 if(inspect_only){std::cout<<store.snapshot().dump()<<'\n';return 0;}
 if(!replay.empty()){auto start=mono_ns();std::vector<Frame>batch;batch.reserve(128);auto input=scan(replay,[&](Frame&&f){batch.push_back(std::move(f));if(batch.size()==128){store.append(batch);batch.clear();}});store.append(batch);store.finish();store.report(Json::object(),true);auto result=store.snapshot();result["inputSHA256"]=input.digest;result["inputFrames"]=input.frames;result["elapsedMs"]=(mono_ns()-start)/1e6;std::cout<<result.dump()<<'\n';return 0;}
 require(!credentials.empty(),"private credentials path required");Credentials pool(credentials);return run(store,pool,root.parent_path()/"managed-tokens/stream-targets.json");
 }catch(const std::exception&){std::cerr<<"Native recorder startup or integrity failure; retained originals unchanged, inspect private evidence.\n";return 78;}
}
