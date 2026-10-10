#pragma once
// Exact verified customer mints share the existing executed-result subscription.
// No RPC, connection, credential or raw-packet decoding is added here.
#include <nlohmann/json.hpp>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <algorithm>
#include <filesystem>
#include <map>
#include <string>
#include <vector>
#include <stdexcept>
namespace tick::managed {
using Json=nlohmann::json;
inline bool key32(const std::string&s){
 if(s.size()<32||s.size()>44)return false;
 const std::string alphabet="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
 unsigned char bytes[33]={};
 for(char ch:s){auto digit=alphabet.find(ch);if(digit==std::string::npos)return false;unsigned carry=digit;for(int i=32;i>=0;i--){carry+=unsigned(bytes[i])*58;bytes[i]=carry&255;carry>>=8;}if(carry)return false;}
 size_t leading=s.find_first_not_of('1');if(leading==std::string::npos)leading=s.size();size_t at=0;while(at<33&&bytes[at]==0)at++;
 return leading+33-at==32;
}
inline std::map<std::string,uint64_t> parse(const Json&j,uint64_t now){
 if(j.at("schema")!=1||!j.at("targets").is_array()||j.at("targets").size()>80)throw std::runtime_error("managed scope bound");
 std::map<std::string,uint64_t> result;
 for(const auto&r:j.at("targets")){auto mint=r.at("mint").get<std::string>();if(!key32(mint)||!r.at("expires").is_number_integer())throw std::runtime_error("managed scope identity");auto expiry=r.at("expires").get<int64_t>();if(expiry<0||uint64_t(expiry)>now+366ULL*86400||result.contains(mint))throw std::runtime_error("managed lease bound");result.emplace(mint,uint64_t(expiry));}
 return result;
}
inline std::map<std::string,uint64_t> load(const std::filesystem::path&p,uint64_t now){
 int fd=::open(p.c_str(),O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0){if(errno==ENOENT)return {};throw std::runtime_error("managed scope read");}
 struct stat s{};if(fstat(fd,&s)||!S_ISREG(s.st_mode)||s.st_size<0||s.st_size>32768||s.st_uid!=geteuid()||(s.st_mode&022)){close(fd);throw std::runtime_error("managed scope file bound");}
 std::string raw(s.st_size,'\0');size_t used=0;while(used<raw.size()){auto n=::read(fd,raw.data()+used,raw.size()-used);if(n<=0){close(fd);throw std::runtime_error("managed scope truncated");}used+=n;}close(fd);return parse(Json::parse(raw),now);
}
inline std::vector<std::string> active(const std::map<std::string,uint64_t>&leases,uint64_t now){std::vector<std::string> result;for(auto&[mint,expiry]:leases)if(expiry>now)result.push_back(mint);return result;}
}
