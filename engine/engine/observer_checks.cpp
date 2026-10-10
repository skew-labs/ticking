#include "observer.hpp"
#include <iostream>
#include <thread>
#include <cassert>
using namespace slotline::observer;
static uint8_t gf(uint8_t a,uint8_t b){unsigned x=a,y=b,v=0;while(y){if(y&1)v^=x;y>>=1;x<<=1;if(x&256)x^=0x11d;}return v;}
int main(){
 ReedSolomon rs;for(unsigned i=0;i<32;i++)for(unsigned j=0;j<32;j++)assert(rs.rows()[i][j]==(i==j));
 std::array<std::array<uint8_t,1232>,64> original{};constexpr size_t bytes=987;
 for(unsigned i=0;i<32;i++)for(size_t j=0;j<bytes;j++)original[i][j]=(i*17+j*31+(j>>3))&255;
 for(unsigned i=32;i<64;i++)for(size_t n=0;n<bytes;n++)for(unsigned j=0;j<32;j++)original[i][n]^=gf(original[j][n],rs.rows()[i][j]);
 for(auto mask:{0xffffffff00000000ULL,0xaaaaaaaaaaaaaaaaULL,0x5555555555555555ULL,0xffff0000ffffULL}){
  auto native=original,scalar=original;for(unsigned i=0;i<64;i++)if(!(mask&(1ULL<<i))){native[i].fill(0);scalar[i].fill(0);}
  rs.restore(native,mask,bytes);rs.restore(scalar,mask,bytes,true);assert(native==original);assert(scalar==original);
 }
 auto ring=std::make_unique<Spsc<uint64_t,1024>>();std::thread producer([&]{for(uint64_t i=0;i<100000;i++){uint64_t*p;while(!(p=ring->producer()))std::this_thread::yield();*p=i;ring->publish();}});
 for(uint64_t i=0;i<100000;i++){uint64_t*p;while(!(p=ring->consumer()))std::this_thread::yield();assert(*p==i);ring->release();}producer.join();assert(!ring->consumer());
 JoinCache cache(1);cache.retire(100);bool regressed=false;try{cache.retire(99);}catch(const Error&){regressed=true;}assert(regressed);
 std::array<uint8_t,64> pool_keys{};pool_keys[0]=1;pool_keys[32]=2;
 assert(!sl_pool_index_new(pool_keys.data(),0));auto address_index=sl_pool_index_new(pool_keys.data(),2);assert(address_index);
 pool_keys[32]=1;assert(!sl_pool_index_new(pool_keys.data(),2));
 std::array<uint8_t,8192>malformed{};uint64_t seed=0x51a710;
 for(unsigned round=0;round<10000;round++){for(auto&byte:malformed){seed^=seed<<13;seed^=seed>>7;seed^=seed<<17;byte=seed;}
  auto n=seed%malformed.size();try{parse_wire(Bytes(malformed.data(),n));}catch(const Error&){}
  try{parse_result(Bytes(malformed.data(),n));}catch(const Error&){}
  try{parse_shred(Bytes(malformed.data(),n),27350,32768);}catch(const Error&){}
  std::array<uint32_t,256> offsets{};uint32_t capacity=256;assert(sl_pool_index_candidates(address_index,malformed.data(),n,offsets.data(),&capacity)==-4||capacity<=256);
 }
 sl_pool_index_delete(address_index);
 std::cout<<"{\"checks\":[\"systematic_matrix\",\"four_erasure_patterns_scalar_and_avx2\",\"100000_spsc_ordered_handoffs\",\"retirement_regression\",\"cold_pool_index_bounds_and_duplicates\",\"10000_bounded_malformed_parser_inputs_and_index_outputs\"],\"passed\":true}\n";
}
