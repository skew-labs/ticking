// Local framed pipe worker; no listener, credentials or public control port.
#include "http_receiver.cpp"
#include <nlohmann/json.hpp>
#include <iostream>
using J=nlohmann::json;
int main(){
 std::ios::sync_with_stdio(false);std::cin.tie(nullptr);
 for(;;){
  std::string line;char ch;
  while(std::cin.get(ch)&&ch!='\n'){
   if(line.size()>=8u*1024*1024)return 2;
   line.push_back(ch);
  }
  if(line.empty()&&std::cin.eof())return 0;
  try{
   auto q=J::parse(line);auto url=q.at("url").get<std::string>();
   std::string payload=q.value("body",std::string{});TickHttpResult r{};
   tick_http_fetch(url.c_str(),q.contains("body")?payload.c_str():nullptr,payload.size(),q.at("limit"),q.at("timeout_ms"),&r);
   std::cout<<J{{"code",r.code},{"status",r.status},{"size",r.size},{"retry_after",r.retry_after},{"total_us",r.total_us},{"connect_us",r.connect_us},{"new_connections",r.new_connections}}.dump()<<'\n';
   if(r.size)std::cout.write(r.data,r.size);
   std::cout.flush();if(!std::cout)return 3;
  }catch(...){std::cout<<"{\"code\":-4,\"size\":0}\n"<<std::flush;}
 }
}
