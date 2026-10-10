#include "core.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
namespace slotline {
size_t IdHash::operator()(const std::array<uint8_t,32>&v)const noexcept {size_t h=1469598103934665603ULL;for(auto b:v)h=(h^b)*1099511628211ULL;return h;}
void Aggregate::add(const Event&e){Order order{e.slot,e.transaction,e.event,e.id};if(!value.count){value={e.time/60*60,e.price,e.price,e.price,e.price,e.volume,e.base,1,0};first=last=order;return;}if(order<first){first=order;value.open=e.price;}if(order>last){last=order;value.close=e.price;}value.high=std::max(value.high,e.price);value.low=std::min(value.low,e.price);value.volume+=e.volume;value.base+=e.base;++value.count;}
Core::Core(size_t capacity,size_t markets,size_t window):pool_(capacity),markets_(markets),window_(window),changes_(65536){free_.reserve(capacity);for(size_t i=capacity;i>0;--i)free_.push_back(i-1);identities_.reserve(capacity);latest_.reserve(markets);}
void Core::changed(uint32_t market,uint64_t time){changes_[head_]={revision_,time,market,0};head_=(head_+1)%changes_.size();change_count_=std::min(change_count_+1,changes_.size());}
void Core::prune(uint32_t market,uint64_t latest){uint64_t cutoff=latest>window_*60?latest-window_*60:0;auto it=buckets_.lower_bound({market,0});while(it!=buckets_.end()&&it->first.first==market&&it->first.second<cutoff){if(!it->second.pending.empty()){++it;continue;}it=buckets_.erase(it);}}
int Core::apply(const Event&e){
 if(e.action==2){ // A rooted slot is irreversible. Finalize and reclaim event slots.
  if(e.slot<root_)return -1;
  if(e.slot==root_)return 0;
  for(auto &pair:buckets_){auto &b=pair.second;
   // Stable one-pass compaction avoids a vector tail move for every rooted
   // execution. Cached prices stay identical: rooting does not remove a fill.
   std::erase_if(b.pending,[&](uint32_t index){const auto &trade=pool_[index];if(trade.slot>e.slot)return false;b.finalized.add(trade);identities_.erase(trade.id);free_.push_back(index);return true;});
  }
  root_=e.slot;++revision_;for(const auto &v:latest_)prune(v.first,v.second);return 1;
 }
 if(e.action==1){ // Retract the exact fork/block identity, across its markets.
  bool any=false;for(auto &pair:buckets_){auto &b=pair.second;bool changed=false;
   std::erase_if(b.pending,[&](uint32_t index){if(pool_[index].fork!=e.fork)return false;identities_.erase(pool_[index].id);free_.push_back(index);changed=any=true;return true;});
   if(changed){b.cache_valid=false;b.revision=++revision_;this->changed(pair.first.first,pair.first.second);}
  }return any?1:0;
 }
 if(e.action!=0||!e.market||!e.slot||!e.time||!std::isfinite(e.price)||!std::isfinite(e.volume)||!std::isfinite(e.base)||e.price<=0||e.volume<=0||e.base<=0)return -1;
 if(e.slot<=root_)return -3;
 auto found=identities_.find(e.id);if(found!=identities_.end()){const auto &old=pool_[found->second];if(old.market!=e.market||old.fork!=e.fork||old.slot!=e.slot||old.time!=e.time||old.transaction!=e.transaction||old.event!=e.event||old.price!=e.price||old.volume!=e.volume||old.base!=e.base)return -1;return 0;}
 uint64_t bucket=e.time/60*60;auto lm=latest_.find(e.market);
 if(lm==latest_.end()&&latest_.size()>=markets_)return -2;
 if(lm!=latest_.end()&&bucket+window_*60<lm->second)return -3;
 if(free_.empty())return -2;
 uint32_t index=free_.back();free_.pop_back();pool_[index]=e;identities_.emplace(e.id,index);
 auto &b=buckets_[{e.market,bucket}];b.pending.push_back(index);if(b.cache_valid)b.cached.add(e);b.revision=++revision_;changed(e.market,bucket);latest_[e.market]=std::max(latest_[e.market],bucket);prune(e.market,latest_[e.market]);return 1;
}
int Core::changes(uint32_t market,uint64_t after,Candle*out,size_t limit)const {if(!change_count_)return 0;size_t first=(head_+changes_.size()-change_count_)%changes_.size();if(after+1<changes_[first].revision&&change_count_==changes_.size())return -1;std::map<uint64_t,Candle> result;for(size_t i=0;i<change_count_;++i){const auto &v=changes_[(first+i)%changes_.size()];if(v.market==market&&v.revision>after){Candle c{};if(candle(market,v.time,c))result[v.time]=c;}}if(result.size()>limit)return -1;size_t n=0;for(const auto &v:result)out[n++]=v.second;return n;}
bool Core::candle(uint32_t market,uint64_t time,Candle&out)const {auto it=buckets_.find({market,time/60*60});if(it==buckets_.end())return false;auto &b=it->second;if(!b.cache_valid){b.cached=b.finalized;for(auto index:b.pending)b.cached.add(pool_[index]);b.cache_valid=true;}out=b.cached.value;out.time=time/60*60;out.revision=b.revision;return true;}
size_t Core::history(uint32_t market,uint64_t before,Candle*out,size_t limit)const {auto it=buckets_.lower_bound({market,before?before:std::numeric_limits<uint64_t>::max()});size_t n=0;while(it!=buckets_.begin()&&n<limit){--it;if(it->first.first!=market)break;Candle c{};if(candle(market,it->first.second,c)&&c.count)out[n++]=c;}std::reverse(out,out+n);return n;}
}
extern "C" {
void *sl_create(uint32_t capacity,uint32_t markets,uint32_t window){try{return new slotline::Core(capacity,markets,window);}catch(...){return nullptr;}}
void sl_destroy(void *p){delete static_cast<slotline::Core*>(p);}
int sl_apply(void*p,const slotline::Event*e){try{return static_cast<slotline::Core*>(p)->apply(*e);}catch(...){return -2;}}
int sl_candle(void*p,uint32_t market,uint64_t time,slotline::Candle*out){return static_cast<slotline::Core*>(p)->candle(market,time,*out);}
uint32_t sl_history(void*p,uint32_t market,uint64_t before,slotline::Candle*out,uint32_t limit){return static_cast<slotline::Core*>(p)->history(market,before,out,limit);}
int sl_changes(void*p,uint32_t market,uint64_t after,slotline::Candle*out,uint32_t limit){return static_cast<slotline::Core*>(p)->changes(market,after,out,limit);}
uint64_t sl_revision(void*p){return static_cast<slotline::Core*>(p)->revision();}
uint64_t sl_active(void*p){return static_cast<slotline::Core*>(p)->active();}
}
