#include "http_receiver.cpp"
#include <cassert>
#include <iostream>
int main(){
 Receiver r;r.limit=4;
 assert(body(const_cast<char*>("abc"),1,3,&r)==3&&r.bytes=="abc");
 assert(body(const_cast<char*>("de"),1,2,&r)==0&&r.bytes=="abc");
 assert(body(nullptr,SIZE_MAX,2,&r)==0);
 char retry[]="Retry-After: 601\r\n";header(retry,1,sizeof(retry)-1,&r);assert(r.retry==601);
 char bad[]="retry-after: -1\r\n";header(bad,1,sizeof(bad)-1,&r);assert(r.retry==601);
 TickHttpResult out{};
 assert(tick_http_fetch("http://127.0.0.1",nullptr,0,100,1000,&out)==-1);
 assert(tick_http_fetch("https://example.com",nullptr,1,100,1000,&out)==-1);
 assert(tick_http_fetch("https://example.com",nullptr,0,MAX_FRAME+1,1000,&out)==-1);
 assert(tick_http_fetch("https://example.com",nullptr,0,100,0,&out)==-1);
 std::cout<<"8 native ingress boundary checks passed\n";
}
