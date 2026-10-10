// Bounded HTTPS ingress. One reusable curl connection cache per owner thread.
// Callers own scheduling, source identity, durable receipts and normalization.
#include <curl/curl.h>
#include <algorithm>
#include <charconv>
#include <cctype>
#include <cstring>
#include <mutex>
#include <string>
#include <string_view>
#include <cstdint>

namespace {
constexpr size_t MAX_FRAME=32u*1024*1024;
std::once_flag init_flag;
struct Receiver { CURL* curl=nullptr; std::string bytes; long retry=0; size_t limit=0;
 Receiver(){std::call_once(init_flag,[]{curl_global_init(CURL_GLOBAL_DEFAULT);});curl=curl_easy_init();}
 ~Receiver(){if(curl)curl_easy_cleanup(curl);}
};
thread_local Receiver receiver;
size_t body(char*data,size_t size,size_t count,void*ctx){
 auto&r=*static_cast<Receiver*>(ctx);
 if(size && count>SIZE_MAX/size)return 0;
 size_t n=size*count;if(n>r.limit-r.bytes.size())return 0;
 try{r.bytes.append(data,n);return n;}catch(...){return 0;}
}
size_t header(char*data,size_t size,size_t count,void*ctx){
 if(size && count>SIZE_MAX/size)return 0;
 size_t n=size*count;std::string_view s(data,n);constexpr std::string_view prefix="retry-after:";
 if(s.size()>=prefix.size()&&std::equal(prefix.begin(),prefix.end(),s.begin(),[](char a,char b){return a==std::tolower(static_cast<unsigned char>(b));})){
  s.remove_prefix(prefix.size());while(!s.empty()&&s.front()==' ')s.remove_prefix(1);
  long value=0;auto [end,ec]=std::from_chars(s.data(),s.data()+s.size(),value);
  if(ec==std::errc()&&end>s.data()&&value>=0)static_cast<Receiver*>(ctx)->retry=std::min(value,86400L);
 }
 return n;
}
}
extern "C" {
struct TickHttpResult { const char* data; uint64_t size; int64_t status,retry_after,total_us,connect_us,new_connections; int code; };
int tick_http_fetch(const char*url,const char*payload,uint64_t payload_size,uint64_t limit,int timeout_ms,TickHttpResult*out) noexcept {
 if(!out)return -1;
 *out={};
 try {
  if(!url||std::strncmp(url,"https://",8)||!limit||limit>MAX_FRAME||payload_size>MAX_FRAME||(!payload&&payload_size)||timeout_ms<100||timeout_ms>30000)return out->code=-1;
  auto&r=receiver;if(!r.curl)return out->code=-2;
  r.bytes.clear();r.limit=limit;r.retry=0;r.bytes.reserve(std::min<uint64_t>(limit,65536));
  CURL*c=r.curl;curl_easy_reset(c); // Keeps DNS/TLS sessions and live connections.
  curl_easy_setopt(c,CURLOPT_URL,url);curl_easy_setopt(c,CURLOPT_PROTOCOLS_STR,"https");
  curl_easy_setopt(c,CURLOPT_FOLLOWLOCATION,0L);curl_easy_setopt(c,CURLOPT_SSL_VERIFYPEER,1L);curl_easy_setopt(c,CURLOPT_SSL_VERIFYHOST,2L);
  curl_easy_setopt(c,CURLOPT_NOSIGNAL,1L);curl_easy_setopt(c,CURLOPT_TCP_NODELAY,1L);curl_easy_setopt(c,CURLOPT_TCP_KEEPALIVE,1L);
  curl_easy_setopt(c,CURLOPT_MAXCONNECTS,8L);curl_easy_setopt(c,CURLOPT_CONNECTTIMEOUT_MS,std::min(static_cast<long>(timeout_ms),5000L));
  curl_easy_setopt(c,CURLOPT_TIMEOUT_MS,static_cast<long>(timeout_ms));curl_easy_setopt(c,CURLOPT_ACCEPT_ENCODING,"");
  curl_easy_setopt(c,CURLOPT_WRITEFUNCTION,body);curl_easy_setopt(c,CURLOPT_WRITEDATA,&r);
  curl_easy_setopt(c,CURLOPT_HEADERFUNCTION,header);curl_easy_setopt(c,CURLOPT_HEADERDATA,&r);
  curl_easy_setopt(c,CURLOPT_USERAGENT,"Ticking-Native-Ingress/1");
  curl_slist*headers=nullptr;
  if(payload){headers=curl_slist_append(headers,"Content-Type: application/json");curl_easy_setopt(c,CURLOPT_HTTPHEADER,headers);curl_easy_setopt(c,CURLOPT_POSTFIELDS,payload);curl_easy_setopt(c,CURLOPT_POSTFIELDSIZE_LARGE,static_cast<curl_off_t>(payload_size));}
  auto code=curl_easy_perform(c);curl_slist_free_all(headers);
  long status=0,connections=0;curl_off_t total=0,connect=0;
  curl_easy_getinfo(c,CURLINFO_RESPONSE_CODE,&status);curl_easy_getinfo(c,CURLINFO_NUM_CONNECTS,&connections);
  curl_easy_getinfo(c,CURLINFO_TOTAL_TIME_T,&total);curl_easy_getinfo(c,CURLINFO_CONNECT_TIME_T,&connect);
  *out={r.bytes.data(),r.bytes.size(),status,r.retry,total,connect,connections,static_cast<int>(code)};
  return out->code;
 }catch(...){return out->code=-3;}
}
}
