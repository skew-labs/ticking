#pragma once
// Minimal HTTP/1.1 server for the local tool API: N epoll workers (one SO_REUSEPORT TCP listener
// each, optional shared Unix socket with EPOLLEXCLUSIVE), keep-alive, in-order replies.  A handler
// may answer later from any thread (Reply is copyable); the reply is posted to the owning worker's
// mailbox and the worker is woken through an eventfd.  Bounded: 64 KiB headers, 16 MiB bodies.
#include "util.hpp"
#include <arpa/inet.h>
#include <fcntl.h>
#include <functional>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <unordered_map>

namespace machine::graphloop {

struct HttpRequest { std::string method, path, query, body; bool keep_alive = true; };
struct HttpResponse { int status = 200; std::string body; std::string ctype = "application/json"; };

inline std::string url_decode(std::string_view s) {
    std::string o; o.reserve(s.size());
    for (size_t i = 0; i < s.size(); i++) {
        if (s[i] == '%' && i + 2 < s.size() && hexval(s[i + 1]) >= 0 && hexval(s[i + 2]) >= 0) { o += char(hexval(s[i + 1]) * 16 + hexval(s[i + 2])); i += 2; }
        else if (s[i] == '+') o += ' '; else o += s[i];
    }
    return o;
}
inline std::string qparam(const std::string& q, std::string_view key, std::string def = {}) {
    size_t i = 0;
    while (i <= q.size()) {
        size_t z = q.find('&', i); if (z == std::string::npos) z = q.size();
        auto kv = std::string_view(q).substr(i, z - i); auto eq = kv.find('=');
        if (kv.substr(0, eq) == key) return eq == std::string_view::npos ? std::string() : url_decode(kv.substr(eq + 1));
        i = z + 1;
    }
    return def;
}
inline const char* status_text(int s) {
    switch (s) { case 200: return "OK"; case 400: return "Bad Request"; case 404: return "Not Found"; case 405: return "Method Not Allowed"; case 409: return "Conflict";
                 case 413: return "Payload Too Large"; case 431: return "Request Header Fields Too Large"; case 500: return "Internal Server Error"; case 503: return "Service Unavailable"; default: return "OK"; }
}

class HttpServer {
public:
    struct Reply { HttpServer* s = nullptr; int worker = -1; int fd = -1; uint64_t gen = 0; void operator()(HttpResponse r) const { s->post(worker, fd, gen, std::move(r)); } };
    using Handler = std::function<void(HttpRequest&, Reply)>;
private:
    struct Conn { int fd = -1; uint64_t gen = 0; std::string in, out; size_t out_off = 0; bool busy = false, close_after = false; };
    struct Worker {
        int ep = -1, efd = -1, lfd = -1; std::unordered_map<int, Conn> conns;
        std::mutex mu; std::vector<std::tuple<int, uint64_t, HttpResponse>> mail; std::thread th;
    };
    std::string host_, unix_path_; int port_; int ufd_ = -1; Handler h_; std::vector<int> cpus_;
    std::vector<std::unique_ptr<Worker>> w_; std::atomic<bool> stop_{false}; std::atomic<uint64_t> gen_{1};
    std::atomic<uint64_t> requests_{0};
    static thread_local int current_worker_;

    static void nonblock(int fd) { int f = ::fcntl(fd, F_GETFL); ::fcntl(fd, F_SETFL, f | O_NONBLOCK); }
    int tcp_listener() {
        int fd = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0); need(fd >= 0, "socket");
        int one = 1; ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one); ::setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof one);
        sockaddr_in a{}; a.sin_family = AF_INET; a.sin_port = htons(uint16_t(port_)); need(::inet_pton(AF_INET, host_.c_str(), &a.sin_addr) == 1, "listen host");
        need(::bind(fd, reinterpret_cast<sockaddr*>(&a), sizeof a) == 0, "bind " + host_ + ":" + std::to_string(port_));
        need(::listen(fd, 4096) == 0, "listen");
        return fd;
    }
    void close_conn(Worker& w, int fd) { ::epoll_ctl(w.ep, EPOLL_CTL_DEL, fd, nullptr); ::close(fd); w.conns.erase(fd); }
    void flush(Worker& w, Conn& c) {
        while (c.out_off < c.out.size()) {
            auto r = ::send(c.fd, c.out.data() + c.out_off, c.out.size() - c.out_off, MSG_NOSIGNAL);
            if (r > 0) { c.out_off += size_t(r); continue; }
            if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) { epoll_event e{}; e.events = EPOLLIN | EPOLLOUT | EPOLLRDHUP; e.data.fd = c.fd; ::epoll_ctl(w.ep, EPOLL_CTL_MOD, c.fd, &e); return; }
            if (r < 0 && errno == EINTR) continue;
            close_conn(w, c.fd); return;
        }
        c.out.clear(); c.out_off = 0;
        epoll_event e{}; e.events = EPOLLIN | EPOLLRDHUP; e.data.fd = c.fd; ::epoll_ctl(w.ep, EPOLL_CTL_MOD, c.fd, &e);
        if (c.close_after && !c.busy) close_conn(w, c.fd);
    }
    void respond(Worker& w, Conn& c, const HttpResponse& r) {
        std::string head = "HTTP/1.1 " + std::to_string(r.status) + " " + status_text(r.status) + "\r\nContent-Type: " + r.ctype + "\r\nContent-Length: " + std::to_string(r.body.size()) +
                           (c.close_after ? "\r\nConnection: close\r\n\r\n" : "\r\nConnection: keep-alive\r\n\r\n");
        c.out += head; c.out += r.body; c.busy = false;
        int fd = c.fd; flush(w, c);
        auto it = w.conns.find(fd);
        if (it != w.conns.end() && !it->second.busy && !it->second.in.empty()) process(w, it->second, index_of(w));
    }
    int index_of(Worker& w) { for (size_t i = 0; i < w_.size(); i++) if (w_[i].get() == &w) return int(i); return 0; }
    // Parse and dispatch at most one request (replies are strictly in order per connection).
    void process(Worker& w, Conn& c, int wi) {
        if (c.busy) return;
        auto he = c.in.find("\r\n\r\n");
        if (he == std::string::npos) { if (c.in.size() > 65536) { c.close_after = true; respond(w, c, {431, "{\"error\":\"HEADER_TOO_LARGE\"}"}); } return; }
        HttpRequest rq; size_t le = c.in.find("\r\n");
        std::string_view line(c.in.data(), le);
        auto s1 = line.find(' '), s2 = line.find(' ', s1 + 1);
        if (s1 == line.npos || s2 == line.npos) { c.close_after = true; respond(w, c, {400, "{\"error\":\"BAD_REQUEST_LINE\"}"}); return; }
        rq.method = std::string(line.substr(0, s1)); std::string target(line.substr(s1 + 1, s2 - s1 - 1)); auto ver = line.substr(s2 + 1);
        auto qp = target.find('?'); rq.path = target.substr(0, qp); if (qp != std::string::npos) rq.query = target.substr(qp + 1);
        rq.keep_alive = ver == "HTTP/1.1";
        size_t clen = 0;
        for (size_t p = le + 2; p < he;) {
            size_t e = c.in.find("\r\n", p); std::string_view h(c.in.data() + p, e - p); p = e + 2;
            auto colon = h.find(':'); if (colon == h.npos) continue;
            std::string k(h.substr(0, colon)); for (auto& ch : k) ch = char(std::tolower(uint8_t(ch)));
            auto v = h.substr(colon + 1); while (!v.empty() && v.front() == ' ') v.remove_prefix(1);
            if (k == "content-length") clen = size_t(std::strtoull(std::string(v).c_str(), nullptr, 10));
            else if (k == "connection") { std::string vv(v); for (auto& ch : vv) ch = char(std::tolower(uint8_t(ch))); if (vv == "close") rq.keep_alive = false; else if (vv == "keep-alive") rq.keep_alive = true; }
        }
        if (clen > (size_t(16) << 20)) { c.close_after = true; respond(w, c, {413, "{\"error\":\"BODY_TOO_LARGE\"}"}); return; }
        if (c.in.size() < he + 4 + clen) return;
        rq.body = c.in.substr(he + 4, clen);
        c.in.erase(0, he + 4 + clen);
        c.close_after = !rq.keep_alive; c.busy = true; requests_++;
        Reply rep{this, wi, c.fd, c.gen};
        try { h_(rq, rep); } catch (const std::exception& e) { auto it = w.conns.find(rep.fd); if (it != w.conns.end() && it->second.gen == rep.gen && it->second.busy) respond(w, it->second, {500, "{\"error\":" + jstr(e.what()) + "}"}); }
    }
    void post(int wi, int fd, uint64_t gen, HttpResponse r) {
        Worker& w = *w_[size_t(wi)];
        if (current_worker_ == wi) { auto it = w.conns.find(fd); if (it != w.conns.end() && it->second.gen == gen && it->second.busy) respond(w, it->second, r); return; }
        { std::lock_guard<std::mutex> l(w.mu); w.mail.emplace_back(fd, gen, std::move(r)); }
        uint64_t one = 1; [[maybe_unused]] auto x = ::write(w.efd, &one, 8);
    }
    void loop(int wi) {
        pin_thread(cpus_, size_t(wi)); current_worker_ = wi;
        Worker& w = *w_[size_t(wi)];
        std::vector<epoll_event> ev(256);
        while (!stop_.load()) {
            int k = ::epoll_wait(w.ep, ev.data(), int(ev.size()), 200);
            for (int i = 0; i < k; i++) {
                int fd = ev[size_t(i)].data.fd; uint32_t e = ev[size_t(i)].events;
                if (fd == w.efd) {
                    uint64_t x; [[maybe_unused]] auto r = ::read(w.efd, &x, 8);
                    std::vector<std::tuple<int, uint64_t, HttpResponse>> m; { std::lock_guard<std::mutex> l(w.mu); m.swap(w.mail); }
                    for (auto& [cfd, gen, resp] : m) { auto it = w.conns.find(cfd); if (it != w.conns.end() && it->second.gen == gen && it->second.busy) respond(w, it->second, resp); }
                    continue;
                }
                if (fd == w.lfd || fd == ufd_) {
                    for (;;) {
                        int c = ::accept4(fd, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
                        if (c < 0) break;
                        if (fd == w.lfd) { int one = 1; ::setsockopt(c, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one); }
                        Conn cn; cn.fd = c; cn.gen = gen_.fetch_add(1); w.conns[c] = std::move(cn);
                        epoll_event ce{}; ce.events = EPOLLIN | EPOLLRDHUP; ce.data.fd = c; ::epoll_ctl(w.ep, EPOLL_CTL_ADD, c, &ce);
                    }
                    continue;
                }
                auto it = w.conns.find(fd); if (it == w.conns.end()) continue;
                Conn& c = it->second;
                if (e & EPOLLOUT) { flush(w, c); if (!w.conns.count(fd)) continue; }
                if (e & (EPOLLIN | EPOLLRDHUP | EPOLLHUP | EPOLLERR)) {
                    char buf[65536]; bool closed = false;
                    for (;;) {
                        auto r = ::recv(fd, buf, sizeof buf, 0);
                        if (r > 0) { c.in.append(buf, size_t(r)); if (c.in.size() > (size_t(17) << 20)) { closed = true; break; } continue; }
                        if (r == 0) { closed = true; break; }
                        if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                        if (errno == EINTR) continue;
                        closed = true; break;
                    }
                    if (closed && !c.busy) { close_conn(w, fd); continue; }
                    if (closed) c.close_after = true;
                    process(w, c, wi);
                }
            }
        }
    }
public:
    HttpServer(std::string host, int port, std::string unix_path, int workers, std::vector<int> cpus, Handler h)
        : host_(std::move(host)), unix_path_(std::move(unix_path)), port_(port), h_(std::move(h)), cpus_(std::move(cpus)) {
        if (!unix_path_.empty()) {
            ::unlink(unix_path_.c_str());
            ufd_ = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0); need(ufd_ >= 0, "unix socket");
            sockaddr_un a{}; a.sun_family = AF_UNIX; need(unix_path_.size() < sizeof a.sun_path, "unix path length");
            std::strcpy(a.sun_path, unix_path_.c_str());
            need(::bind(ufd_, reinterpret_cast<sockaddr*>(&a), sizeof a) == 0 && ::listen(ufd_, 4096) == 0, "unix bind");
            ::chmod(unix_path_.c_str(), 0660);
        }
        for (int i = 0; i < workers; i++) {
            auto w = std::make_unique<Worker>();
            w->ep = ::epoll_create1(EPOLL_CLOEXEC); w->efd = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
            need(w->ep >= 0 && w->efd >= 0, "epoll/eventfd");
            epoll_event e{}; e.events = EPOLLIN; e.data.fd = w->efd; ::epoll_ctl(w->ep, EPOLL_CTL_ADD, w->efd, &e);
            if (port_ > 0) { w->lfd = tcp_listener(); epoll_event l{}; l.events = EPOLLIN; l.data.fd = w->lfd; ::epoll_ctl(w->ep, EPOLL_CTL_ADD, w->lfd, &l); }
            if (ufd_ >= 0) { epoll_event u{}; u.events = EPOLLIN | EPOLLEXCLUSIVE; u.data.fd = ufd_; ::epoll_ctl(w->ep, EPOLL_CTL_ADD, ufd_, &u); }
            w_.push_back(std::move(w));
        }
    }
    void start() { for (size_t i = 0; i < w_.size(); i++) w_[i]->th = std::thread([this, i] { loop(int(i)); }); }
    void stop() {
        stop_ = true;
        for (auto& w : w_) { uint64_t one = 1; [[maybe_unused]] auto r = ::write(w->efd, &one, 8); }
        for (auto& w : w_) if (w->th.joinable()) w->th.join();
        for (auto& w : w_) { for (auto& [fd, c] : w->conns) ::close(fd); if (w->lfd >= 0) ::close(w->lfd); ::close(w->efd); ::close(w->ep); }
        if (ufd_ >= 0) { ::close(ufd_); ::unlink(unix_path_.c_str()); }
    }
    uint64_t requests() const { return requests_.load(); }
};
inline thread_local int HttpServer::current_worker_ = -1;
} // namespace machine::graphloop
