#include "core.hpp"
#include <cassert>
#include <iostream>
#include <random>

using namespace slotline;
Event fill(uint64_t id,uint64_t slot,uint32_t market,uint8_t fork){
 Event e{};e.slot=slot;e.time=61;e.market=market;e.price=1+double(id%1000)/10;
 e.volume=6;e.base=3;e.fork[0]=fork;e.transaction=uint32_t(id%100);
 for(size_t n=0;n<8;++n)e.id[n]=uint8_t(id>>(n*8));
 return e;
}
int main(){
 Core core(32768,100,2400);
 for(uint64_t n=1;n<=20000;++n){auto e=fill(n,10+uint64_t(n%2),1,uint8_t(n%2));assert(core.apply(e)==1);}
 Candle before{};assert(core.candle(1,60,before));assert(before.count==20000);
 for(unsigned n=0;n<1000;++n){Candle c{};assert(core.candle(1,60,c));assert(c.count==before.count&&c.volume==before.volume&&c.open==before.open&&c.close==before.close);}
 Event root{};root.action=2;root.slot=10;assert(core.apply(root)==1);
 Candle rooted{};assert(core.candle(1,60,rooted));assert(rooted.count==20000&&rooted.volume==before.volume);
 Event dead{};dead.action=1;dead.fork[0]=1;assert(core.apply(dead)==1);
 Candle repaired{};assert(core.candle(1,60,repaired));assert(repaired.count==10000&&repaired.volume==60000);
 assert(core.apply(fill(20001,12,1,2))==1);assert(core.candle(1,60,repaired));assert(repaired.count==10001);
 dead.fork[0]=0;assert(core.apply(dead)==0);assert(core.active()==1);
 root.slot=12;assert(core.apply(root)==1);assert(core.active()==0);
 // Fresh slots reuse reclaimed capacity without reusing unrooted identities.
 std::mt19937_64 random(1729);
 for(uint64_t n=20002;n<80002;++n){auto e=fill(n,20+n/1000,uint32_t(1+random()%100),uint8_t(random()%8));assert(core.apply(e)==1);
  if(n%1000==0){root.slot=e.slot-1;assert(core.apply(root)>=0);}
  if(n%701==0){dead.fork[0]=uint8_t(random()%8);assert(core.apply(dead)>=0);}
 }
 assert(core.active()<2001);
 std::cout<<"native core cached reads, bulk rooting/retraction and 80000 retained/reclaimed fixture checks passed\n";
}
