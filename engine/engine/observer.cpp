#include "observer.hpp"
#include <algorithm>
#include <bit>
#include <charconv>
#include <cstring>
#include <cmath>
#include <map>
#include <openssl/evp.h>
#include <openssl/sha.h>
#if defined(__x86_64__)
#include <immintrin.h>
#endif
namespace slotline::observer {
uint16_t u16(const uint8_t*p){return uint16_t(p[0])|uint16_t(p[1])<<8;}
uint32_t u32(const uint8_t*p){return uint32_t(u16(p))|uint32_t(u16(p+2))<<16;}
uint64_t u64(const uint8_t*p){return uint64_t(u32(p))|uint64_t(u32(p+4))<<32;}
Hash hash(Bytes b){Hash h;SHA256(b.data(),b.size(),h.data());return h;}
static Hash digest(Bytes a,Bytes b,Bytes c={}){
 Hash h;SHA256_CTX ctx;SHA256_Init(&ctx);SHA256_Update(&ctx,a.data(),a.size());
 SHA256_Update(&ctx,b.data(),b.size());if(!c.empty())SHA256_Update(&ctx,c.data(),c.size());
 SHA256_Final(h.data(),&ctx);return h;
}
static constexpr uint8_t leaf_prefix[]={0,'S','O','L','A','N','A','_','M','E','R','K','L','E','_','S','H','R','E','D','S','_','L','E','A','F'};
static constexpr uint8_t node_prefix[]={1,'S','O','L','A','N','A','_','M','E','R','K','L','E','_','S','H','R','E','D','S','_','N','O','D','E'};
static Hash parent(Bytes a,Bytes b){return digest(node_prefix,a.first(20),b.first(20));}
struct Cursor {
 Bytes bytes; size_t pos{};
 Bytes take(size_t n){if(n>bytes.size()-pos)throw Error("truncated wire");auto b=bytes.subspan(pos,n);pos+=n;return b;}
 uint8_t byte(){return take(1)[0];}
 uint16_t compact(){uint32_t n=0;for(unsigned i=0;i<3;i++){uint8_t b=byte();
   if(i==2 && (b&0xfc))throw Error("shortvec overflow");n|=uint32_t(b&127)<<(7*i);
   if(!(b&128)){if(i&&!(b&127))throw Error("noncanonical shortvec");return uint16_t(n);}}
   throw Error("shortvec overflow");}
};
Wire parse_wire(Bytes raw,std::span<const Hash> watched){
 if(!raw.empty()&&raw[0]==129){
  Cursor c{raw};c.byte();Wire w;w.versioned=true;auto required=c.byte(),readonly_signed=c.byte(),readonly_unsigned=c.byte();auto mask_bytes=c.take(4);auto mask=u32(mask_bytes.data());
  if(mask&~31u||((mask&3u)!=0&&(mask&3u)!=3))throw Error("V1 configuration mask");c.take(32);auto instructions=c.byte(),keys=c.byte();
  if(!required||required>12||!keys||keys>64||required>keys||readonly_signed>=required||readonly_unsigned>keys-required||instructions>64)throw Error("V1 header bounds");
  auto accounts=c.take(size_t(keys)*32);for(unsigned i=0;i<keys;i++)for(unsigned j=0;j<i;j++)if(std::memcmp(accounts.data()+32*i,accounts.data()+32*j,32)==0)throw Error("V1 duplicate address");
  auto config=c.take(std::popcount(mask)*4);if(mask&16){auto heap=u32(config.data()+config.size()-4);if(heap<32768||heap>262144||heap%1024)throw Error("V1 heap bounds");}
  auto headers=c.take(instructions*4);for(unsigned i=0;i<instructions;i++){auto h=headers.data()+4*i;if(h[0]>=keys)throw Error("V1 program index");for(auto index:c.take(h[1]))if(index>=keys)throw Error("V1 account index");c.take(u16(h+2));}
  w.message_size=c.pos;auto signatures=c.take(required*64);std::copy_n(signatures.begin(),64,w.signature.begin());if(c.pos>4096)throw Error("V1 transaction bounds");
  w.selected=watched.empty();for(const auto&target:watched)for(unsigned i=0;i<keys;i++)if(std::memcmp(target.data(),accounts.data()+32*i,32)==0)w.selected=true;w.size=c.pos;return w;
 }
 Cursor c{raw};Wire w;auto signatures=c.compact();if(!signatures||signatures>12)throw Error("signature count");
 auto sig=c.take(size_t(signatures)*64);std::copy_n(sig.begin(),64,w.signature.begin());
 auto required=c.byte();if(required&128){if(required!=128)throw Error("unverified message version");w.versioned=true;required=c.byte();}
 auto readonly_signed=c.byte(),readonly_unsigned=c.byte();auto keys=c.compact();
 if(!keys||keys>256||required!=signatures||required>keys||readonly_signed>=required||readonly_unsigned>keys-required)throw Error("message header");
 auto accounts=c.take(size_t(keys)*32);c.take(32);auto instructions=c.compact();if(instructions>256)throw Error("instruction count");
 std::array<uint8_t,256> indexes{};size_t index_count=0;uint16_t maximum=0;
 for(unsigned i=0;i<instructions;i++){
   auto program=c.byte();maximum=std::max<uint16_t>(maximum,program);indexes[index_count++]=program;
   auto n=c.compact();for(auto a:c.take(n))maximum=std::max<uint16_t>(maximum,a);
   c.take(c.compact());
 }
 uint16_t loaded=0;
 if(w.versioned){auto lookups=c.compact();if(lookups>64)throw Error("lookup count");
   for(unsigned i=0;i<lookups;i++){c.take(32);auto a=c.compact();c.take(a);auto b=c.compact();c.take(b);
     loaded+=a+b;if(loaded>256)throw Error("lookup capacity");}
   w.has_lookups=loaded>0;
 }
 if(keys+loaded>256||(instructions&&maximum>=keys+loaded))throw Error("account index");
 if(c.pos>1232)throw Error("transaction MTU");
 // Static keys include CPI programs/pools. Lookup keys are unresolved until
 // executed metadata arrives, so they are retained as dependencies, not dropped.
 w.selected=watched.empty();
 for(const auto& target:watched)for(unsigned i=0;i<keys;i++)
   if(std::memcmp(target.data(),accounts.data()+32*i,32)==0)w.selected=true;
 w.size=c.pos;return w;
}
struct Field {uint32_t number{},wire{};uint64_t value{};Bytes bytes{};};
struct Proto {
 Cursor c;
 uint64_t varint(){uint64_t n=0;for(unsigned i=0;i<10;i++){auto b=c.byte();if(i==9&&b>1)throw Error("protobuf overflow");n|=uint64_t(b&127)<<(7*i);if(!(b&128))return n;}throw Error("protobuf varint");}
 bool next(Field& f){if(c.pos==c.bytes.size())return false;auto tag=varint();
   if(!tag||(tag>>3)>536870911)throw Error("protobuf tag");f={};f.number=tag>>3;f.wire=tag&7;
   if(f.wire==0)f.value=varint();else if(f.wire==2){auto n=varint();if(n>c.bytes.size()-c.pos)throw Error("protobuf length");f.bytes=c.take(n);}
   else if(f.wire==1)f.bytes=c.take(8);else if(f.wire==5)f.bytes=c.take(4);else throw Error("protobuf wire type");return true;}
};
static bool verify_message(Bytes signature,Bytes message,Bytes leader){
 if(signature.size()!=64||leader.size()!=32)throw Error("signature/key size");auto key=EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519,nullptr,leader.data(),32);if(!key)throw Error("crypto key");
 auto ctx=EVP_MD_CTX_new();if(!ctx){EVP_PKEY_free(key);throw Error("crypto context");}bool ok=EVP_DigestVerifyInit(ctx,nullptr,nullptr,nullptr,key)==1&&EVP_DigestVerify(ctx,signature.data(),64,message.data(),message.size())==1;EVP_MD_CTX_free(ctx);EVP_PKEY_free(key);return ok;
}
template<size_t N> struct FixedList {
 std::array<Bytes,N> items{};size_t n{};
 void add(Bytes b){if(n==N)throw Error("protobuf repeated bound");items[n++]=b;}
};
static void expect(const Field& f,uint32_t wire){if(f.wire!=wire)throw Error("protobuf known field type");}
static ProtoWire parse_result_uncached(Bytes raw,std::span<const Hash> watched){
 if(raw.size()>262144)throw Error("metadata bound");ProtoWire r;Bytes tx,meta,signature;bool have_tx=false,have_meta=false;
 Proto p{{raw}};Field f;while(p.next(f)){
  if(f.number==1){expect(f,2);if(!signature.empty())throw Error("duplicate signature");signature=f.bytes;}
  else if(f.number==3){expect(f,2);if(have_tx)throw Error("duplicate transaction");tx=f.bytes;have_tx=true;}
  else if(f.number==4){expect(f,2);if(have_meta)throw Error("duplicate metadata");meta=f.bytes;have_meta=true;}
  else if(f.number==5){expect(f,0);r.index=f.value;}
 }
 if(!have_tx||!have_meta||signature.size()!=64||r.index>UINT32_MAX)throw Error("missing executed result identity/index");
 // Execution failures are not chart candidates. In particular a failed
 // sanitation may have invalid account indices or malformed wire semantics;
 // it must be rejected without stopping unrelated successful transactions.
 Proto early{{meta}};Field error;while(early.next(error))if(error.number==1){expect(error,2);r.failed=true;}
 if(r.failed){std::copy_n(signature.begin(),64,r.signature.begin());return r;}
 FixedList<12> sigs;Bytes message;bool have_message=false;p={{tx}};
 while(p.next(f)){if(f.number==1){expect(f,2);sigs.add(f.bytes);}else if(f.number==2){expect(f,2);if(have_message)throw Error("duplicate message");message=f.bytes;have_message=true;}}
 if(!have_message||!sigs.n||sigs.items[0].size()!=64||std::memcmp(sigs.items[0].data(),signature.data(),64))throw Error("signature mismatch");
 FixedList<256> keys,instructions;FixedList<64> lookups;Bytes recent,header,config;bool versioned=false,have_header=false,have_config=false;
 p={{message}};while(p.next(f)){
  if(f.number==1){expect(f,2);if(have_header)throw Error("duplicate header");header=f.bytes;have_header=true;}
  else if(f.number==2){expect(f,2);keys.add(f.bytes);}
  else if(f.number==3){expect(f,2);recent=f.bytes;}
  else if(f.number==4){expect(f,2);instructions.add(f.bytes);}
  else if(f.number==5){expect(f,0);if(f.value>1)throw Error("version bool");versioned=f.value;}
  else if(f.number==6){expect(f,2);lookups.add(f.bytes);}
  else if(f.number==7){expect(f,2);if(have_config)throw Error("duplicate V1 config");config=f.bytes;have_config=true;}
 }
 if(!have_header||recent.size()!=32||(!versioned&&lookups.n))throw Error("message identity");
 std::array<uint8_t,3> h{};p={{header}};while(p.next(f))if(f.number>=1&&f.number<=3){expect(f,0);if(f.value>255)throw Error("header integer");h[f.number-1]=f.value;}
 auto add=[&](Bytes b){if(b.size()>r.bytes.size()-r.size)throw Error("wire bound");std::copy(b.begin(),b.end(),r.bytes.begin()+r.size);r.size+=b.size();};
 auto byte=[&](uint8_t b){add(Bytes(&b,1));};
 auto compact=[&](size_t n){if(n>65535)throw Error("shortvec bound");do{auto b=uint8_t(n&127);n>>=7;byte(b|(n?128:0));}while(n);};
 if(have_config){
  if(!versioned||lookups.n||keys.n>64||instructions.n>64||sigs.n!=h[0])throw Error("V1 metadata identity");uint32_t mask=0;std::array<uint64_t,5>values{};std::array<bool,5>present{};p={{config}};
  while(p.next(f)){if(f.number<1||f.number>5)throw Error("unknown V1 config field");expect(f,0);auto i=f.number-1;if(present[i]||(i&&f.value>UINT32_MAX))throw Error("V1 config overflow/duplicate");present[i]=true;values[i]=f.value;mask|=i?1u<<(i+1):3u;}
  auto le=[&](uint64_t value,unsigned n){for(unsigned i=0;i<n;i++)byte(value>>(8*i));};byte(129);add(h);le(mask,4);add(recent);byte(instructions.n);byte(keys.n);
  for(size_t i=0;i<keys.n;i++){if(keys.items[i].size()!=32)throw Error("V1 key size");add(keys.items[i]);}
  for(unsigned i=0;i<5;i++)if(present[i])le(values[i],i?4:8);
  struct Instruction {uint8_t program;Bytes accounts,data;};std::array<Instruction,64>decoded{};
  for(size_t i=0;i<instructions.n;i++){auto&d=decoded[i];uint64_t program=0;p={{instructions.items[i]}};
   while(p.next(f)){if(f.number==1){expect(f,0);program=f.value;}else if(f.number==2){expect(f,2);d.accounts=f.bytes;}else if(f.number==3){expect(f,2);d.data=f.bytes;}}
   if(program>255||d.accounts.size()>255||d.data.size()>65535)throw Error("V1 instruction bounds");d.program=program;byte(d.program);byte(d.accounts.size());le(d.data.size(),2);
  }for(size_t i=0;i<instructions.n;i++){add(decoded[i].accounts);add(decoded[i].data);}
  size_t message_size=r.size;for(size_t i=0;i<sigs.n;i++){if(sigs.items[i].size()!=64)throw Error("V1 signature size");add(sigs.items[i]);}
  auto wire=parse_wire(Bytes(r.bytes.data(),r.size),watched);if(wire.size!=r.size||wire.message_size!=message_size)throw Error("V1 wire suffix");
  // This verifies canonical reconstruction against the real sender's signature,
  // without executing or signing a transaction. A wrong encoding cannot pass.
  for(size_t i=0;i<sigs.n;i++)if(!verify_message(sigs.items[i],Bytes(r.bytes.data(),message_size),keys.items[i]))throw Error("V1 canonical signature mismatch");
  p={{meta}};while(p.next(f))if(f.number==12||f.number==13){expect(f,2);if(!f.bytes.empty())throw Error("V1 unexpected lookup addresses");}
  r.signature=wire.signature;r.selected=wire.selected;return r;
 }
 compact(sigs.n);for(size_t i=0;i<sigs.n;i++){if(sigs.items[i].size()!=64)throw Error("signature size");add(sigs.items[i]);}
 if(versioned)byte(128);add(h);compact(keys.n);
 r.selected=watched.empty();auto select=[&](Bytes b){if(b.size()!=32)throw Error("account key size");for(auto&t:watched)if(std::memcmp(t.data(),b.data(),32)==0)r.selected=true;};
 for(size_t i=0;i<keys.n;i++){select(keys.items[i]);add(keys.items[i]);}add(recent);compact(instructions.n);
 for(size_t i=0;i<instructions.n;i++){
  uint64_t program=0;Bytes accounts,data;p={{instructions.items[i]}};
  while(p.next(f)){if(f.number==1){expect(f,0);program=f.value;}else if(f.number==2){expect(f,2);accounts=f.bytes;}else if(f.number==3){expect(f,2);data=f.bytes;}}
  if(program>255)throw Error("program index");byte(program);compact(accounts.size());add(accounts);compact(data.size());add(data);
 }
 if(versioned){compact(lookups.n);for(size_t i=0;i<lookups.n;i++){
  Bytes account,writable,readonly;p={{lookups.items[i]}};while(p.next(f)){
   if(f.number==1){expect(f,2);account=f.bytes;}else if(f.number==2){expect(f,2);writable=f.bytes;}else if(f.number==3){expect(f,2);readonly=f.bytes;}}
  if(account.size()!=32)throw Error("lookup identity");add(account);compact(writable.size());add(writable);compact(readonly.size());add(readonly);
 }}
 p={{meta}};size_t loaded_count=0;while(p.next(f)){
  if(f.number==1){expect(f,2);r.failed=true;}
  else if(f.number==12||f.number==13){expect(f,2);select(f.bytes);if(++loaded_count>256)throw Error("loaded account bound");}
 }
 auto wire=parse_wire(Bytes(r.bytes.data(),r.size));if(wire.size!=r.size)throw Error("wire suffix");r.signature=wire.signature;return r;
}
ProtoWire parse_result(Bytes raw,std::span<const Hash>watched){
 if(!watched.empty())return parse_result_uncached(raw,watched);if(raw.size()>262144)throw Error("metadata bound");
 // Selection, matching and execution decode consume the same immutable result.
 // Cache its validated canonical wire so V1 sender signatures are checked once,
 // rather than once per downstream stage. Full-input SHA256 keys prevent stale
 // status/balance substitutions; this never caches an execution decision.
 struct Cache {bool used{};Hash fingerprint{};ProtoWire wire{};};
 thread_local std::array<Cache,8>cache{};thread_local size_t next{};auto fingerprint=hash(raw);
 for(auto&item:cache)if(item.used&&item.fingerprint==fingerprint)return item.wire;
 auto result=parse_result_uncached(raw,watched);auto&item=cache[next++%cache.size()];item.fingerprint=fingerprint;item.wire=result;item.used=true;return result;
}
static Hash base58(Bytes text){
 if(text.empty()||text.size()>44)throw Error("mint text bound");constexpr char alphabet[]="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";Hash result{};
 struct MintCache {std::array<uint8_t,44> text{};Hash value{};uint8_t size{};};
 thread_local std::array<MintCache,256> cache{};uint64_t fingerprint=1469598103934665603ull;
 for(auto c:text)fingerprint=(fingerprint^c)*1099511628211ull;
 auto&entry=cache[fingerprint&255];
 if(entry.size==text.size()&&std::memcmp(entry.text.data(),text.data(),text.size())==0)return entry.value;
 for(auto c:text){auto p=std::strchr(alphabet,c);if(!c||!p)throw Error("mint base58");unsigned carry=p-alphabet;
  for(int i=31;i>=0;i--){carry+=unsigned(result[i])*58;result[i]=carry&255;carry>>=8;}if(carry)throw Error("mint overflow");}
 size_t leading=0;while(leading<text.size()&&text[leading]=='1')leading++;size_t zeros=0;while(zeros<32&&!result[zeros])zeros++;
 if(leading!=zeros)throw Error("mint base58 length");std::copy(text.begin(),text.end(),entry.text.begin());entry.size=text.size();entry.value=result;return result;
}
// Match the retained Decimal(precision=28, HALF_EVEN) reference exactly.
// Binary extended precision can differ by one ULP and turn a harmless replay
// into a conflict. Integer long division has no balance cancellation or peg.
static double decimal_ratio(uint64_t quote,uint64_t base,int scale){
 auto whole=quote/base;uint64_t remainder=quote%base;
 std::string digits=whole?std::to_string(whole):"";int exponent=whole?int(digits.size())-1:-1;
 auto digit=[&](){auto value=__uint128_t(remainder)*10;remainder=uint64_t(value%base);return char('0'+value/base);};
 while(digits.empty()){char next=digit();if(next=='0')exponent--;else digits+=next;}
 while(digits.size()<28)digits+=digit();
 auto twice=__uint128_t(remainder)*2;
 if(twice>base||(twice==base&&(digits.back()-'0')%2)){
  int i=int(digits.size())-1;while(i>=0&&digits[i]=='9')digits[i--]='0';
  if(i>=0)digits[i]++;else digits.insert(digits.begin(),'1');
 }
 digits+='e';digits+=std::to_string(exponent-27+scale);double result{};
 auto parsed=std::from_chars(digits.data(),digits.data()+digits.size(),result);
 if(parsed.ec!=std::errc{}||parsed.ptr!=digits.data()+digits.size())throw Error("decimal ratio conversion");return result;
}
static double decimal_units(uint64_t units,unsigned decimals){
 auto text=std::to_string(units)+"e-"+std::to_string(decimals);double result{};
 auto parsed=std::from_chars(text.data(),text.data()+text.size(),result);
 if(parsed.ec!=std::errc{}||parsed.ptr!=text.data()+text.size())throw Error("decimal units conversion");return result;
}
struct ExecutionView {
 ProtoWire result;Bytes meta;std::array<Bytes,256> keys{};size_t key_count{};
 FixedList<256> top;FixedList<1024> inner;
};
static ExecutionView execution_view(Bytes raw){
 ExecutionView v;v.result=parse_result(raw);if(v.result.failed)return v;
 Proto p{{raw}};Field f;Bytes tx,message;while(p.next(f)){if(f.number==3)tx=f.bytes;else if(f.number==4)v.meta=f.bytes;}
 p={{tx}};while(p.next(f))if(f.number==2)message=f.bytes;
 auto key=[&](Bytes b){if(v.key_count==256||b.size()!=32)throw Error("executed key bound");v.keys[v.key_count++]=b;};
 p={{message}};while(p.next(f)){if(f.number==2)key(f.bytes);else if(f.number==4)v.top.add(f.bytes);}
 p={{v.meta}};while(p.next(f))if(f.number==12)key(f.bytes);
 p={{v.meta}};while(p.next(f)){if(f.number==13)key(f.bytes);else if(f.number==5){Proto block{{f.bytes}};Field q;while(block.next(q))if(q.number==2){expect(q,2);v.inner.add(q.bytes);}}}
 return v;
}
static int decode_amm_view(const ExecutionView&view,const AmmConfig&cfg,Fill&fill){
 static constexpr char raydium_text[]="675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8";
 static const Hash raydium=base58(Bytes((const uint8_t*)raydium_text,sizeof(raydium_text)-1));
 static constexpr char cpmm_text[]="CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C";
 static const Hash cpmm=base58(Bytes((const uint8_t*)cpmm_text,sizeof(cpmm_text)-1));
 static constexpr char clmm_text[]="CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK";
 static const Hash clmm=base58(Bytes((const uint8_t*)clmm_text,sizeof(clmm_text)-1));
 static constexpr char ps_text[]="pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",dl_text[]="LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo";
 static const Hash pumpswap=base58(Bytes((const uint8_t*)ps_text,sizeof(ps_text)-1));
 static const Hash dlmm=base58(Bytes((const uint8_t*)dl_text,sizeof(dl_text)-1));
 static constexpr char whirlpool_text[]="whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc";
 static const Hash whirlpool=base58(Bytes((const uint8_t*)whirlpool_text,sizeof(whirlpool_text)-1));
 static const auto input=hash(Bytes((const uint8_t*)"global:swap_base_input",22));
 static const auto output=hash(Bytes((const uint8_t*)"global:swap_base_output",23));
 static const auto swap=hash(Bytes((const uint8_t*)"global:swap",11));
 static const auto swap_v2=hash(Bytes((const uint8_t*)"global:swap_v2",14));
 if((cfg.program!=raydium&&cfg.program!=cpmm&&cfg.program!=clmm&&cfg.program!=pumpswap&&cfg.program!=dlmm&&cfg.program!=whirlpool)||cfg.base_decimals>18||cfg.quote_decimals>18||cfg.base_vault==cfg.quote_vault||cfg.base_mint==cfg.quote_mint)return -6;
 const auto&result=view.result;if(result.failed)return -5;
 const auto meta=view.meta;const auto&keys=view.keys;const auto key_count=view.key_count;const auto&top=view.top;const auto&inner=view.inner;Proto p{{meta}};Field f;
 auto equal=[&](size_t i,const Hash&key){return i<key_count&&std::memcmp(keys[i].data(),key.data(),32)==0;};
 int base_index=-1,quote_index=-1;for(size_t i=0;i<key_count;i++){if(equal(i,cfg.base_vault)){if(base_index!=-1)return -6;base_index=i;}if(equal(i,cfg.quote_vault)){if(quote_index!=-1)return -6;quote_index=i;}}
 if(base_index<0||quote_index<0)return 0;
 unsigned matches=0;bool valid_swap=false;
 auto instruction=[&](Bytes bytes){uint64_t program=0;Bytes accounts,data;Proto p{{bytes}};Field f;
  while(p.next(f)){if(f.number==1){expect(f,0);program=f.value;}else if(f.number==2){expect(f,2);accounts=f.bytes;}else if(f.number==3){expect(f,2);data=f.bytes;}}
  if(program>=key_count)throw Error("executed program index");bool pool=false;for(auto i:accounts){if(i>=key_count)throw Error("executed account index");pool|=equal(i,cfg.pool);}
  if(equal(program,cfg.program)&&pool){matches++;valid_swap=cfg.program==raydium?
   data.size()==17&&(data[0]==9||data[0]==11||data[0]==16||data[0]==17):
   cfg.program==cpmm?data.size()==24&&(std::memcmp(data.data(),input.data(),8)==0||std::memcmp(data.data(),output.data(),8)==0):
   cfg.program==clmm?data.size()==41&&data.back()<=1&&(std::memcmp(data.data(),swap.data(),8)==0||std::memcmp(data.data(),swap_v2.data(),8)==0):false;
   if(cfg.program==pumpswap||cfg.program==dlmm){
    // Hash once and use heterogeneous lookup: no per-invocation string/tree
    // construction, even for names longer than the small-string capacity.
    static const auto discriminators=[](){std::map<std::string,Hash,std::less<>> h;for(auto name:{"buy","sell","buy_v2","sell_v2","buy_exact_quote_in","buy_exact_quote_in_v2","swap","swap2","swap_exact_out","swap_exact_out2","swap_with_price_impact","swap_with_price_impact2"}){auto text=std::string("global:")+name;h.emplace(name,hash(Bytes((const uint8_t*)text.data(),text.size())));}return h;}();
    auto is=[&](const char*name){return data.size()>=8&&std::memcmp(data.data(),discriminators.find(name)->second.data(),8)==0;};
    unsigned ma,mb,va,vb;
    if(cfg.program==pumpswap){
     valid_swap=(is("sell")||is("buy_v2")||is("sell_v2")||is("buy_exact_quote_in_v2"))?data.size()==24:
      (is("buy")||is("buy_exact_quote_in"))&&(data.size()==24||(data.size()==25&&data[24]<=1));ma=3;mb=4;va=7;vb=8;
    }else{
     auto impact=is("swap_with_price_impact")||is("swap_with_price_impact2");size_t at=24;
     if(impact){valid_swap=data.size()>=17&&data[16]<=1;at=valid_swap?19+4*data[16]:SIZE_MAX;}else valid_swap=is("swap")||is("swap2")||is("swap_exact_out")||is("swap_exact_out2");
     if(valid_swap){bool extended=is("swap2")||is("swap_exact_out2")||is("swap_with_price_impact2");
      if(!extended)valid_swap=data.size()==at;
      else if(at>data.size()||data.size()-at<4)valid_swap=false;
      else{auto count=u32(data.data()+at);at+=4;valid_swap=count<=32;
       for(unsigned i=0;i<count&&valid_swap;i++){if(at>=data.size()||data[at]>4){valid_swap=false;break;}auto kind=data[at++];if(kind==3)at++;if(at>=data.size()||!data[at]){valid_swap=false;break;}at++;}valid_swap&=at==data.size();}
     }ma=6;mb=7;va=2;vb=3;
    }
    valid_swap&=accounts.size()>std::max({ma,mb,va,vb})&&equal(accounts[0],cfg.pool);
    if(valid_swap)valid_swap=(equal(accounts[ma],cfg.base_mint)&&equal(accounts[mb],cfg.quote_mint)&&equal(accounts[va],cfg.base_vault)&&equal(accounts[vb],cfg.quote_vault))||
     (equal(accounts[ma],cfg.quote_mint)&&equal(accounts[mb],cfg.base_mint)&&equal(accounts[va],cfg.quote_vault)&&equal(accounts[vb],cfg.base_vault));
   }
   if(cfg.program==whirlpool){
    valid_swap=data.size()>=42&&data[40]<=1&&data[41]<=1;
    const bool v1=valid_swap&&std::memcmp(data.data(),swap.data(),8)==0;
    if(v1){valid_swap=data.size()==42&&accounts.size()>=11&&equal(accounts[2],cfg.pool);
     if(valid_swap)valid_swap=(equal(accounts[4],cfg.base_vault)&&equal(accounts[6],cfg.quote_vault))||(equal(accounts[4],cfg.quote_vault)&&equal(accounts[6],cfg.base_vault));
    }else{
     valid_swap&=data.size()>=43&&std::memcmp(data.data(),swap_v2.data(),8)==0&&accounts.size()>=15;
     if(valid_swap)valid_swap=equal(accounts[4],cfg.pool)&&data[42]<=1;
     if(valid_swap&&data[42]==0)valid_swap=data.size()==43;
     else if(valid_swap){
      valid_swap=data.size()>=47;
      if(valid_swap){const auto n=u32(data.data()+43);valid_swap=n<=32&&data.size()==47+2*n;std::array<bool,7> seen{};unsigned remaining=0;
       for(unsigned i=0;i<n&&valid_swap;i++){auto kind=data[47+2*i],length=data[48+2*i];if((kind!=0&&kind!=1&&kind!=6)||(kind==6&&length>3)){valid_swap=false;break;}
        if(length){if(seen[kind]){valid_swap=false;break;}seen[kind]=true;remaining+=length;}}
       valid_swap&=remaining<=accounts.size()-15;
      }
     }
     if(valid_swap)valid_swap=(equal(accounts[5],cfg.base_mint)&&equal(accounts[6],cfg.quote_mint)&&equal(accounts[8],cfg.base_vault)&&equal(accounts[10],cfg.quote_vault))||
      (equal(accounts[5],cfg.quote_mint)&&equal(accounts[6],cfg.base_mint)&&equal(accounts[8],cfg.quote_vault)&&equal(accounts[10],cfg.base_vault));
    }
   }
  }
 };
 for(size_t i=0;i<top.n;i++)instruction(top.items[i]);for(size_t i=0;i<inner.n;i++)instruction(inner.items[i]);
 if(matches!=1||!valid_swap)return 0;
 if(cfg.program==pumpswap||cfg.program==dlmm||cfg.program==whirlpool){
  static constexpr char spl_text[]="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",t22_text[]="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
  static const Hash spl=base58(Bytes((const uint8_t*)spl_text,sizeof(spl_text)-1)),t22=base58(Bytes((const uint8_t*)t22_text,sizeof(t22_text)-1));
  std::array<Bytes,256> groups{};std::array<bool,256> have{};p={{meta}};
  while(p.next(f))if(f.number==5){uint64_t index=0;Proto g{{f.bytes}};Field q;while(g.next(q))if(q.number==1){expect(q,0);index=q.value;}
   if(index>=top.n||have[index])return 0;groups[index]=f.bytes;have[index]=true;}
  unsigned transfers=0;bool okay=true;
  for(size_t at=0;at<top.n&&okay;at++){
   std::array<bool,17> scope{};unsigned previous=0;
   auto visit=[&](Bytes raw,bool outer){uint64_t program=0,height=outer?1:0;Bytes accounts,data;Proto ix{{raw}};Field q;
    while(ix.next(q)){if(q.number==1){expect(q,0);program=q.value;}else if(q.number==2)accounts=q.bytes;else if(q.number==3)data=q.bytes;else if(q.number==4){expect(q,0);height=q.value;}}
    if(program>=key_count||height<1||height>16||(!outer&&height<2)||height>previous+1){okay=false;return;}
    for(auto i:accounts)if(i>=key_count){okay=false;return;}
    for(unsigned i=height;i<scope.size();i++)scope[i]=false;bool inherited=height>1&&scope[height-1],target=false,touch=false;
    for(auto i:accounts){target|=equal(i,cfg.pool);touch|=equal(i,cfg.base_vault)||equal(i,cfg.quote_vault);}target&=equal(program,cfg.program);
    if((equal(program,spl)||equal(program,t22))&&touch){size_t dest=0;bool valid=false;
     if(data.size()==9&&data[0]==3){dest=1;valid=accounts.size()>=3;}
     else if(data.size()==10&&data[0]==12){dest=2;valid=accounts.size()>=4;}
     else if(data.size()==19&&data[0]==26&&data[1]==1){dest=2;valid=accounts.size()>=4;}
     if(!valid||!inherited||!(equal(accounts[0],cfg.base_vault)||equal(accounts[0],cfg.quote_vault)||equal(accounts[dest],cfg.base_vault)||equal(accounts[dest],cfg.quote_vault))){okay=false;return;}transfers++;
    }scope[height]=inherited||target;previous=height;
   };
   visit(top.items[at],true);if(have[at]){Proto g{{groups[at]}};Field q;while(okay&&g.next(q))if(q.number==2)visit(q.bytes,false);}
  }
  if(!okay||transfers<2)return 0;
 }
 uint64_t amount[2][2]{};unsigned found[2][2]{};
 p={{meta}};while(p.next(f))if(f.number==7||f.number==8){auto side=f.number-7;uint64_t index=0;Bytes mint,ui;Proto b{{f.bytes}};Field q;
  while(b.next(q)){if(q.number==1){expect(q,0);index=q.value;}else if(q.number==2){expect(q,2);mint=q.bytes;}else if(q.number==3){expect(q,2);ui=q.bytes;}}
  int asset=index==uint64_t(base_index)?0:index==uint64_t(quote_index)?1:-1;if(asset<0)continue;
  if(base58(mint)!=(asset?cfg.quote_mint:cfg.base_mint))return -6;uint64_t decimals=0;Bytes text;b={{ui}};
  while(b.next(q)){if(q.number==2){expect(q,0);decimals=q.value;}else if(q.number==3){expect(q,2);text=q.bytes;}}
  if(decimals!=(asset?cfg.quote_decimals:cfg.base_decimals)||text.empty()||text.size()>20)return -6;
  uint64_t n=0;for(auto c:text){if(c<'0'||c>'9'||n>(UINT64_MAX-(c-'0'))/10)return -6;n=n*10+(c-'0');}
  amount[asset][side]=n;found[asset][side]++;
 }
 for(unsigned a=0;a<2;a++)for(unsigned b=0;b<2;b++)if(found[a][b]!=1)return -6;
 // Subtract integer units before conversion: avoid cancellation of huge balances.
 auto delta=[&](unsigned a){auto before=amount[a][0],after=amount[a][1];return after>=before?after-before:before-after;};auto base=delta(0),quote=delta(1);
 if(!base||!quote||(amount[0][1]<amount[0][0])==(amount[1][1]<amount[1][0]))return 0;
 fill={decimal_ratio(quote,base,int(cfg.base_decimals)-int(cfg.quote_decimals)),decimal_units(quote,cfg.quote_decimals),decimal_units(base,cfg.base_decimals),result.index};
 if(!std::isfinite(fill.price)||!std::isfinite(fill.volume)||!std::isfinite(fill.base)||fill.price<=0||fill.volume<=0||fill.base<=0)return -6;return 1;
}
int decode_amm(Bytes raw,const AmmConfig&cfg,Fill&fill){return decode_amm_view(execution_view(raw),cfg,fill);}
Shred parse_shred(Bytes raw,uint16_t version,uint32_t max_index){
 if(raw.size()<89||!version||max_index<32||max_index>32768)throw Error("shred bounds");Shred s;
 auto variant=raw[64];auto kind=variant>>4;if((variant&15)!=6||(kind!=6&&kind!=7&&kind!=9&&kind!=11))throw Error("unverified shred variant");
 s.data=kind==9||kind==11;s.resigned=kind==7||kind==11;s.size=s.data?1203:1228;
 if(raw.size()<s.size||raw.size()>1232)throw Error("shred length");
 s.slot=u64(raw.data()+65);s.index=u32(raw.data()+73);s.version=u16(raw.data()+77);s.fec=u32(raw.data()+79);
 if(s.version!=version||s.index>=max_index||s.fec%32||s.fec>max_index-32)throw Error("shred identity");
 s.proof_offset=s.size-120-(s.resigned?64:0);size_t chain_offset=s.proof_offset-32;
 s.erasure_size=chain_offset-(s.data?64:89);std::copy_n(raw.begin()+chain_offset,32,s.chain.begin());
 if(s.data){if(s.index<s.fec||s.index>=s.fec+32)throw Error("data FEC position");s.shard=s.index-s.fec;
  s.parent=u16(raw.data()+83);s.flags=raw[85];s.data_size=u16(raw.data()+86);
  if(s.data_size<88||s.data_size>chain_offset||s.parent>s.slot||(s.slot&&s.parent==0)||(s.flags&128&&!(s.flags&64)))throw Error("data header");
 }else{if(u16(raw.data()+83)!=32||u16(raw.data()+85)!=32||u16(raw.data()+87)>=32)throw Error("32:32 coding header");
  auto position=u16(raw.data()+87);if(s.index<position||s.index-position>max_index-32)throw Error("code index");s.shard=32+position;s.code_first=s.index-position;
 }
 s.root=digest(leaf_prefix,raw.subspan(64,s.proof_offset-64));unsigned index=s.shard;
 for(unsigned i=0;i<6;i++){auto other=raw.subspan(s.proof_offset+20*i,20);s.root=index&1?parent(other,s.root):parent(s.root,other);index>>=1;}
 return s;
}
bool verify_signature(const Signature& sig,const Hash& root,const Hash& leader){
 auto key=EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519,nullptr,leader.data(),32);if(!key)throw Error("crypto key");
 auto ctx=EVP_MD_CTX_new();if(!ctx){EVP_PKEY_free(key);throw Error("crypto context");}
 bool ok=EVP_DigestVerifyInit(ctx,nullptr,nullptr,nullptr,key)==1&&EVP_DigestVerify(ctx,sig.data(),64,root.data(),32)==1;
 EVP_MD_CTX_free(ctx);EVP_PKEY_free(key);return ok;
}
static uint8_t scalar_gf(uint8_t a,uint8_t b){unsigned x=a,y=b,z=0;while(y){if(y&1)z^=x;y>>=1;x<<=1;if(x&256)x^=0x11d;}return z;}
// 64KiB immutable table, initialized once. Byte loops reuse a single 256-byte
// coefficient row rather than branch through eight polynomial steps per byte.
static const std::array<std::array<uint8_t,256>,256> gf_table=[](){std::array<std::array<uint8_t,256>,256>table{};
 for(unsigned a=0;a<256;a++)for(unsigned b=0;b<256;b++)table[a][b]=scalar_gf(a,b);return table;}();
static uint8_t mul(uint8_t a,uint8_t b){return gf_table[a][b];}
static uint8_t power(uint8_t a,unsigned n){uint8_t v=1;while(n){if(n&1)v=mul(v,a);a=mul(a,a);n>>=1;}return v;}
using Matrix=std::array<std::array<uint8_t,32>,32>;
static Matrix invert(Matrix a){Matrix b{};for(unsigned i=0;i<32;i++)b[i][i]=1;
 for(unsigned i=0;i<32;i++){unsigned pivot=i;while(pivot<32&&!a[pivot][i])pivot++;if(pivot==32)throw Error("singular RS matrix");std::swap(a[i],a[pivot]);std::swap(b[i],b[pivot]);
  auto scale=power(a[i][i],254);for(unsigned k=0;k<32;k++){a[i][k]=mul(a[i][k],scale);b[i][k]=mul(b[i][k],scale);}
  for(unsigned j=0;j<32;j++)if(j!=i){auto f=a[j][i];for(unsigned k=0;k<32;k++){a[j][k]^=mul(f,a[i][k]);b[j][k]^=mul(f,b[i][k]);}}
 }return b;}
ReedSolomon::ReedSolomon(){Matrix top{};std::array<std::array<uint8_t,32>,64> v{};
 for(unsigned i=0;i<64;i++)for(unsigned j=0;j<32;j++)v[i][j]=power(i,j);
 for(unsigned i=0;i<32;i++)top[i]=v[i];auto inverse=invert(top);
 for(unsigned i=0;i<64;i++)for(unsigned j=0;j<32;j++)for(unsigned k=0;k<32;k++)rows_[i][j]^=mul(v[i][k],inverse[k][j]);
}
static void scalar_xor(uint8_t*dst,const uint8_t*src,size_t n,uint8_t f){const auto&row=gf_table[f];for(size_t i=0;i<n;i++)dst[i]^=row[src[i]];}
#if defined(__x86_64__)
__attribute__((target("avx2"))) static void avx_xor(uint8_t*dst,const uint8_t*src,size_t n,uint8_t f){
 alignas(32) uint8_t low[32],high[32];for(unsigned i=0;i<16;i++){low[i]=low[i+16]=mul(i,f);high[i]=high[i+16]=mul(i<<4,f);}
 auto l=_mm256_load_si256((__m256i*)low),h=_mm256_load_si256((__m256i*)high),mask=_mm256_set1_epi8(15);size_t i=0;
 for(;i+32<=n;i+=32){auto x=_mm256_loadu_si256((const __m256i*)(src+i));auto y=_mm256_xor_si256(_mm256_shuffle_epi8(l,_mm256_and_si256(x,mask)),_mm256_shuffle_epi8(h,_mm256_and_si256(_mm256_srli_epi16(x,4),mask)));
  _mm256_storeu_si256((__m256i*)(dst+i),_mm256_xor_si256(_mm256_loadu_si256((const __m256i*)(dst+i)),y));}scalar_xor(dst+i,src+i,n-i,f);
}
#endif
void ReedSolomon::restore(std::array<std::array<uint8_t,1232>,64>&shards,uint64_t present,size_t n,bool scalar)const{
 if(n>1232||std::popcount(present)<32)throw Error("insufficient RS shards");Matrix selected{};std::array<unsigned,32> ids{};unsigned k=0;
 uint64_t mask=0;for(unsigned i=0;i<64&&k<32;i++)if(present&(uint64_t(1)<<i)){selected[k]=rows_[i];ids[k++]=i;mask|=uint64_t(1)<<i;}
 const Matrix*inverse=nullptr;for(auto&plan:plans_)if(plan.mask==mask){inverse=&plan.inverse;break;}
 if(!inverse){auto&plan=plans_[next_plan_++%plans_.size()];plan.inverse=invert(selected);plan.mask=mask;inverse=&plan.inverse;}const auto&inv=*inverse;
 auto accumulate=[&](uint8_t*dst,const uint8_t*src,uint8_t f){if(!f)return;
#if defined(__x86_64__)
  if(!scalar&&__builtin_cpu_supports("avx2")){avx_xor(dst,src,n,f);return;}
#endif
  scalar_xor(dst,src,n,f);};
 // First rebuild absent data from immutable present shards, then parity.
 for(unsigned row=0;row<32;row++)if(!(present&(uint64_t(1)<<row))){shards[row].fill(0);for(unsigned j=0;j<32;j++)accumulate(shards[row].data(),shards[ids[j]].data(),inv[row][j]);}
 for(unsigned row=32;row<64;row++)if(!(present&(uint64_t(1)<<row))){shards[row].fill(0);for(unsigned j=0;j<32;j++)accumulate(shards[row].data(),shards[j].data(),rows_[row][j]);}
}
FecArena::FecArena(size_t capacity):capacity_(capacity),table_size_(std::bit_ceil(capacity*2)),free_count_(capacity){
 if(!capacity||capacity>4096)throw Error("FEC capacity");entries_=std::make_unique<Fec[]>(capacity);index_=std::make_unique<Index[]>(table_size_);free_=std::make_unique<uint32_t[]>(capacity);
 for(size_t i=0;i<capacity;i++)free_[i]=i;
}
const Fec* FecArena::add(Bytes raw,const Hash&leader,uint16_t version,uint32_t max_index,uint64_t stamp){
 received++;Shred s;try{s=parse_shred(raw,version,max_index);}catch(const Error&){rejected++;return nullptr;}
 if(s.slot<=retired_&&retired_){rejected++;return nullptr;}
 Signature sig;std::copy_n(raw.begin(),64,sig.begin());Fec* entry=nullptr;Index*empty=nullptr;
 // Metadata probes stay in packed cache lines; large packet arenas are touched
 // only after a hit. Occupancy <= 50%, with no packet-time heap allocation.
 auto start=(u64(s.root.data())^s.slot^s.fec)&(table_size_-1);for(size_t n=0;n<table_size_;n++){auto&item=index_[(start+n)&(table_size_-1)];
  if(item.state!=1){if(!empty)empty=&item;if(!item.state)break;continue;}
  if(item.slot==s.slot&&item.fec==s.fec&&item.root==s.root){entry=&entries_[item.entry];break;}}
 if(!entry){if(!verify_signature(sig,s.root,leader)){rejected++;return nullptr;}if(!free_count_||!empty)throw Error("FEC arena full; retained inputs require recovery barrier");
  auto id=free_[--free_count_];*empty={s.root,s.slot,s.fec,id,1};entry=&entries_[id];entry->used=true;entry->slot=s.slot;entry->index=s.fec;entry->root=s.root;entry->chain=s.chain;entry->leader=leader;entry->signature=sig;entry->version=s.version;entry->resigned=s.resigned;entry->max_index=max_index;
 }
 auto&e=*entry;if(e.leader!=leader||e.signature!=sig||e.chain!=s.chain||e.version!=s.version||e.resigned!=s.resigned||e.max_index!=max_index){rejected++;return nullptr;}
 if(!s.data){if(e.have_code&&e.code_first!=s.code_first){rejected++;return nullptr;}e.have_code=true;e.code_first=s.code_first;}
 auto bit=uint64_t(1)<<s.shard;
 if(e.present&bit){if(std::memcmp(e.packets[s.shard].data(),raw.data(),s.proof_offset))throw Error("conflicting signed shred");duplicate++;return nullptr;}
 if(e.complete){duplicate++;return nullptr;}std::copy_n(raw.begin(),s.size,e.packets[s.shard].begin());e.present|=bit;e.stamp=stamp;
 if(std::popcount(e.present)<32)return nullptr;
 // All-data sets can be emitted without parity; with missing data require a
 // verified coding header and reconstruct the entire signed Merkle tree.
 if(uint32_t(e.present)!=0xffffffffu&&!e.have_code)return nullptr;
 if(e.have_code){std::array<std::array<uint8_t,1232>,64> shards{};
  for(unsigned i=0;i<64;i++)if(e.present&(uint64_t(1)<<i))std::copy_n(e.packets[i].begin()+(i<32?64:89),s.erasure_size,shards[i].begin());
  rs_.restore(shards,e.present,s.erasure_size);
  auto missing=~e.present;for(unsigned i=0;i<64;i++)if(missing&(uint64_t(1)<<i)){
   auto&p=e.packets[i];p.fill(0);std::copy(e.signature.begin(),e.signature.end(),p.begin());
   if(i<32)std::copy_n(shards[i].begin(),s.erasure_size,p.begin()+64);
   else{p[64]=(e.resigned?0x76:0x66);auto put16=[&](size_t off,uint16_t v){p[off]=v;p[off+1]=v>>8;};auto put32=[&](size_t off,uint32_t v){put16(off,v);put16(off+2,v>>16);};
    put32(65,e.slot);put32(69,e.slot>>32);put32(73,e.code_first+i-32);put16(77,e.version);put32(79,e.index);put16(83,32);put16(85,32);put16(87,i-32);std::copy_n(shards[i].begin(),s.erasure_size,p.begin()+89);}
   auto size=i<32?1203:1228;auto proof=size-120-(e.resigned?64:0);std::copy(e.chain.begin(),e.chain.end(),p.begin()+proof-32);
  }
  std::array<Hash,127> tree{};for(unsigned i=0;i<64;i++){auto size=i<32?1203:1228;auto proof=size-120-(e.resigned?64:0);tree[63+i]=digest(leaf_prefix,Bytes(e.packets[i].data()+64,proof-64));}
  for(int i=62;i>=0;i--)tree[i]=parent(tree[2*i+1],tree[2*i+2]);if(tree[0]!=e.root)throw Error("RS reconstruction changed signed root");
  for(unsigned i=0;i<32;i++)if(missing&(uint64_t(1)<<i)){auto proof=1203-120-(e.resigned?64:0);unsigned node=63+i;
   for(unsigned level=0;level<6;level++){auto sibling=node&1?node+1:node-1;std::copy_n(tree[sibling].begin(),20,e.packets[i].begin()+proof+20*level);node=(node-1)/2;}}
 }
 uint16_t parent_offset=0;for(unsigned i=0;i<32;i++){
  auto d=parse_shred(Bytes(e.packets[i].data(),1203),version,max_index);
  if(d.root!=e.root||d.slot!=e.slot||d.fec!=e.index||d.shard!=i||d.chain!=e.chain||d.resigned!=e.resigned||!d.data||(i&&d.parent!=parent_offset))throw Error("invalid restored data identity");parent_offset=d.parent;
 }
 e.complete=true;completed++;return &e;
}
void FecArena::retire(uint64_t slot){if(slot<retired_)throw Error("FEC retirement regressed");for(size_t i=0;i<table_size_;i++){
 auto&item=index_[i];if(item.state==1&&item.slot<=slot){entries_[item.entry]=Fec{};free_[free_count_++]=item.entry;item.state=2;}}
 retired_=slot;
}
JoinCache::JoinCache(size_t capacity):capacity_(capacity),table_size_(std::bit_ceil(capacity*2)){
 if(!capacity||capacity>1048576)throw Error("join capacity");records_=std::make_unique<JoinRecord[]>(table_size_);
}
size_t JoinCache::bucket(uint64_t slot,const Signature&sig)const{return (u64(sig.data())^(slot*0x9e3779b97f4a7c15ULL))&(table_size_-1);}
int JoinCache::observe(uint64_t slot,Bytes raw,const Hash&branch){
 if(!slot||branch==Hash{})return -4;if(slot<=retired_)return -3;auto w=parse_wire(raw);if(w.size!=raw.size())return -4;auto digest=hash(raw);
 auto start=bucket(slot,w.signature);JoinRecord*empty=nullptr;
 for(size_t i=0;i<table_size_;i++){auto&r=records_[(start+i)&(table_size_-1)];if(r.state!=1){if(!empty)empty=&r;if(!r.state)break;continue;}
  if(r.slot==slot&&r.signature==w.signature&&r.branch==branch)return r.wire==digest?0:-1;
 }if(count_==capacity_||!empty)return -2;*empty={slot,w.signature,digest,branch,1};count_++;return 1;
}
int JoinCache::match(uint64_t slot,Bytes raw,const Hash&branch){
 return match_any(slot,raw,std::span<const Hash>(&branch,1));
}
int JoinCache::match_any(uint64_t slot,Bytes raw,std::span<const Hash>branches){
 if(!slot||branches.empty()||branches.size()>512)return -4;for(auto&branch:branches)if(branch==Hash{})return -4;
 if(slot<=retired_)return -3;auto result=parse_result(raw);if(result.failed)return -5;auto digest=hash(Bytes(result.bytes.data(),result.size));
 auto start=bucket(slot,result.signature);for(size_t i=0;i<table_size_;i++){auto&r=records_[(start+i)&(table_size_-1)];if(!r.state)return 0;
  if(r.state==1&&r.slot==slot&&r.signature==result.signature&&std::find(branches.begin(),branches.end(),r.branch)!=branches.end())return r.wire==digest?1:-1;}return 0;
}
void JoinCache::retire(uint64_t slot){if(slot<retired_)throw Error("retirement regressed");for(size_t i=0;i<table_size_;i++)if(records_[i].state==1&&records_[i].slot<=slot){records_[i].state=2;count_--;}retired_=slot;}
}
using namespace slotline::observer;
static thread_local std::array<char,256> last_error{};
namespace {
struct PoolAddressIndex {
 struct alignas(64) Row {Hash key{};uint32_t offset=0;bool used=false;};
 std::vector<Row> rows;size_t mask;uint32_t count=0,capacity;
 static uint64_t bucket(Bytes key){uint64_t h=1469598103934665603ULL;for(auto c:key){h^=c;h*=1099511628211ULL;}return h;}
 PoolAddressIndex(const uint8_t*keys,uint32_t n,uint32_t reserve):capacity(reserve){
  if(!keys||!n||n>reserve||reserve>262144)throw Error("cold pool index bound");
  size_t size=2;while(size<size_t(reserve)*2)size*=2;rows.resize(size);mask=size-1;append(keys,n);
 }
 void append(const uint8_t*keys,uint32_t n){
  if(!keys||!n||n>capacity-count)throw Error("cold pool append bound");
  std::map<Hash,bool> batch;
  // Preflight the whole batch so rejected appends never publish a prefix.
  for(uint32_t i=0;i<n;i++){Bytes key(keys+i*32,32);Hash h;std::copy_n(key.data(),32,h.begin());
   if(h==Hash{}||find(key)>=0||!batch.emplace(h,true).second)throw Error("duplicate or empty pool append");}
  for(uint32_t i=0;i<n;i++){
   Bytes key(keys+i*32,32);Hash h;std::copy_n(key.data(),32,h.begin());if(h==Hash{})throw Error("empty pool address");
   size_t p=bucket(key)&mask;while(rows[p].used){if(rows[p].key==h)throw Error("duplicate pool address");p=(p+1)&mask;}
   rows[p].key=h;rows[p].offset=count+i;rows[p].used=true;
  }
  count+=n;
 }
 int find(Bytes key)const{
  if(key.size()!=32)throw Error("candidate key size");size_t p=bucket(key)&mask;
  while(rows[p].used){if(std::memcmp(rows[p].key.data(),key.data(),32)==0)return rows[p].offset;p=(p+1)&mask;}return -1;
 }
};
}
extern "C" {
void*sl_pool_index_new(const uint8_t*keys,uint32_t n){try{return new PoolAddressIndex(keys,n,n);}catch(...){return nullptr;}}
void*sl_pool_index_reserved_new(const uint8_t*keys,uint32_t n,uint32_t reserve){try{return new PoolAddressIndex(keys,n,reserve);}catch(...){return nullptr;}}
int sl_pool_index_append(void*p,const uint8_t*keys,uint32_t n){try{if(!p)return -4;static_cast<PoolAddressIndex*>(p)->append(keys,n);return 1;}catch(...){return -4;}}
void sl_pool_index_delete(void*p){delete static_cast<PoolAddressIndex*>(p);}
int sl_pool_index_keys(void*p,const uint8_t*raw,uint32_t n,uint32_t*out,uint32_t*count){try{
 if(!p||!raw||!out||!count||*count<256||n%32||n>256*32)return -4;
 *count=0;std::array<uint32_t,256> found{};size_t used=0;
 auto select=[&](Bytes key){
  int offset=static_cast<PoolAddressIndex*>(p)->find(key);if(offset<0)return;
  for(size_t i=0;i<used;i++)if(found[i]==uint32_t(offset))return;
  found[used++]=offset;};
 for(uint32_t i=0;i<n;i+=32)select(Bytes(raw+i,32));
 // Preserve transaction account order, matching the reference implementation.
 std::copy_n(found.begin(),used,out);*count=used;return 1;
 }catch(...){return -4;}}
int sl_pool_index_batch(void*p,const uint8_t*raw,uint32_t bytes,const uint32_t*offsets,uint32_t n,uint32_t*out,uint32_t*counts){
 if(!p||!raw||!offsets||!out||!counts||!n||n>1024||bytes>1024*256*32||offsets[0]||offsets[n]!=bytes)return -4;
 for(uint32_t i=0;i<n;i++)if(offsets[i]>offsets[i+1]||(offsets[i+1]-offsets[i])%32||offsets[i+1]-offsets[i]>256*32)return -4;
 for(uint32_t i=0;i<n;i++){counts[i]=256;if(sl_pool_index_keys(p,raw+offsets[i],offsets[i+1]-offsets[i],out+256*i,counts+i)!=1)return -4;}
 return 1;
}
int sl_pool_index_candidates(void*p,const uint8_t*raw,uint32_t n,uint32_t*out,uint32_t*count){try{
 if(!p||!raw||!out||!count||*count<256)return -4;auto r=parse_result(Bytes(raw,n));*count=0;if(r.failed)return -5;
 std::array<uint32_t,256> found{};size_t used=0;
 auto select=[&](Bytes key){int offset=static_cast<PoolAddressIndex*>(p)->find(key);if(offset<0)return;
  for(size_t i=0;i<used;i++)if(found[i]==uint32_t(offset))return;
  if(used==found.size())throw Error("candidate output bound");found[used++]=offset;};
 Proto info{{Bytes(raw,n)}};Field f;Bytes transaction,meta,message;
 while(info.next(f)){if(f.number==3)transaction=f.bytes;else if(f.number==4)meta=f.bytes;}
 Proto tx{{transaction}};while(tx.next(f))if(f.number==2)message=f.bytes;
 std::array<Bytes,256> keys{};size_t key_count=0;FixedList<256> top;FixedList<1024> inner;
 auto account=[&](Bytes key){if(key.size()!=32||key_count==keys.size())throw Error("candidate account bound");keys[key_count++]=key;};
 Proto m{{message}};while(m.next(f)){if(f.number==2)account(f.bytes);else if(f.number==4)top.add(f.bytes);}
 m={{meta}};while(m.next(f))if(f.number==12)account(f.bytes);
 m={{meta}};while(m.next(f)){if(f.number==13)account(f.bytes);else if(f.number==5){Proto block{{f.bytes}};Field b;while(block.next(b))if(b.number==2)inner.add(b.bytes);}}
 static const std::array<Hash,5> programs=[](){std::array<Hash,5> p{};unsigned i=0;
  for(auto text:{"675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8","CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C","CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK","pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA","LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"})p[i++]=base58(Bytes((const uint8_t*)text,std::strlen(text)));return p;}();
 auto instruction=[&](Bytes raw){Proto p{{raw}};Field f;uint64_t program=0;Bytes accounts;
  while(p.next(f)){if(f.number==1){expect(f,0);program=f.value;}else if(f.number==2){expect(f,2);accounts=f.bytes;}}
  if(program>=key_count)throw Error("candidate instruction program index");bool supported=false;
  for(auto&expected:programs)supported|=std::memcmp(expected.data(),keys[program].data(),32)==0;
  for(auto a:accounts){if(a>=key_count)throw Error("candidate instruction account index");if(supported)select(keys[a]);}
 };
 for(size_t i=0;i<top.n;i++)instruction(top.items[i]);for(size_t i=0;i<inner.n;i++)instruction(inner.items[i]);
 std::sort(found.begin(),found.begin()+used);std::copy_n(found.begin(),used,out);*count=used;return 1;
 }catch(...){return -4;}}
void*sl_observer_new(uint32_t n){try{return new JoinCache(n);}catch(...){return nullptr;}}
void sl_observer_delete(void*p){delete static_cast<JoinCache*>(p);}
int sl_observer_observe(void*p,uint64_t slot,const uint8_t*raw,uint32_t n,const uint8_t*branch){try{if(!p||!raw||!branch)return -4;Hash b;std::copy_n(branch,32,b.begin());return static_cast<JoinCache*>(p)->observe(slot,Bytes(raw,n),b);}catch(...){return -4;}}
int sl_observer_match(void*p,uint64_t slot,const uint8_t*raw,uint32_t n,const uint8_t*branch){try{if(!p||!raw||!branch)return -4;Hash b;std::copy_n(branch,32,b.begin());return static_cast<JoinCache*>(p)->match(slot,Bytes(raw,n),b);}catch(...){return -4;}}
int sl_observer_match_any(void*p,uint64_t slot,const uint8_t*raw,uint32_t n,const uint8_t*branches,uint32_t count){try{if(!p||!raw||!branches||!count||count>512)return -4;std::array<Hash,512>b{};for(unsigned i=0;i<count;i++)std::copy_n(branches+i*32,32,b[i].begin());return static_cast<JoinCache*>(p)->match_any(slot,Bytes(raw,n),std::span<const Hash>(b.data(),count));}catch(...){return -4;}}
uint64_t sl_observer_count(void*p){return p?static_cast<JoinCache*>(p)->count():0;}
int sl_observer_retire(void*p,uint64_t slot){try{if(!p)return -4;static_cast<JoinCache*>(p)->retire(slot);return 1;}catch(...){return -4;}}
int sl_observer_result(const uint8_t*raw,uint32_t n,uint8_t*out,uint32_t*size,uint64_t*index){try{last_error[0]=0;if(!raw||!out||!size||!index)return -4;auto r=parse_result(Bytes(raw,n));if(*size<r.size)return -4;std::copy_n(r.bytes.begin(),r.size,out);*size=r.size;*index=r.index;return r.failed?-5:1;}catch(const std::exception&e){std::strncpy(last_error.data(),e.what(),last_error.size()-1);last_error.back()=0;return -4;}catch(...){return -4;}}
const char*sl_observer_last_error(){return last_error.data();}
int sl_observer_selected(const uint8_t*raw,uint32_t n,const uint8_t*keys,uint32_t count){try{if(!raw||!keys||!count||count>256)return -4;std::array<Hash,256>watched{};for(unsigned i=0;i<count;i++)std::copy_n(keys+i*32,32,watched[i].begin());auto r=parse_result(Bytes(raw,n),std::span<const Hash>(watched.data(),count));return r.failed?-5:r.selected?1:0;}catch(...){return -4;}}
int sl_observer_candidates(const uint8_t*raw,uint32_t n,const uint8_t*watched,uint32_t count,uint8_t*out){try{
 if(!raw||!watched||!out||!count||count>100)return -4;std::fill_n(out,count,0);auto r=parse_result(Bytes(raw,n));if(r.failed)return -5;
 auto select=[&](Bytes key){if(key.size()!=32)throw Error("candidate key size");for(unsigned i=0;i<count;i++)if(std::memcmp(watched+i*32,key.data(),32)==0)out[i]=1;};
 Cursor wire{Bytes(r.bytes.data(),r.size)};
 if(r.bytes[0]==129){wire.take(41);auto accounts=wire.byte();for(unsigned i=0;i<accounts;i++)select(wire.take(32));}
 else{auto sigs=wire.compact();wire.take(sigs*64);auto required=wire.byte();if(required&128)wire.byte();wire.take(2);auto accounts=wire.compact();for(unsigned i=0;i<accounts;i++)select(wire.take(32));}
 Proto info{{Bytes(raw,n)}};Field f;while(info.next(f))if(f.number==4){Proto meta{{f.bytes}};Field m;while(meta.next(m))if(m.number==12||m.number==13)select(m.bytes);}
 return 1;
 }catch(...){return -4;}}
int sl_observer_amm(const uint8_t*raw,uint32_t n,const AmmConfig*cfg,Fill*out){try{if(!raw||!cfg||!out)return -4;return decode_amm(Bytes(raw,n),*cfg,*out);}catch(...){return -4;}}
int sl_observer_amm_batch(const uint8_t*raw,uint32_t n,const AmmConfig*configs,uint32_t registry_size,const uint32_t*indices,uint32_t count,Fill*out,int32_t*status){try{
 if(!raw||!n||n>262144||!configs||!registry_size||registry_size>262144||!indices||!count||count>256||!out||!status)return -4;
 for(unsigned i=0;i<count;i++)if(indices[i]>=registry_size)return -4;
 // Publish the complete batch only after every candidate has been parsed.
 // Fixed scratch arrays prevent unbounded allocation and partial publication.
 std::array<Fill,256> fills{};std::array<int32_t,256> results{};
 const auto view=execution_view(Bytes(raw,n));
 for(unsigned i=0;i<count;i++)results[i]=decode_amm_view(view,configs[indices[i]],fills[i]);
 std::copy_n(fills.begin(),count,out);std::copy_n(results.begin(),count,status);return 1;
 }catch(...){return -4;}}
}
