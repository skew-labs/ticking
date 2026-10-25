// serve_sandbox: after landlock_read_only(root) + seccomp_serve(), a process can read files under root and nothing else, cannot
// open anything for writing, and cannot create sockets, connect or exec. Runs the checks in a forked child.
#include <machine/engine/serve_sandbox.hpp>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

using namespace machine::engine::serve;

int main() {
    namespace fs = std::filesystem;
    fs::path root = fs::temp_directory_path() / ("emv2-sandbox-test-" + std::to_string(::getpid()));
    fs::create_directories(root / "chain");
    { std::ofstream(root / "chain" / "manifest.json") << "{}"; }
    pid_t pid = ::fork();
    if (pid == 0) {
        int abi = landlock_read_only(root.string());
        if (!seccomp_serve()) _exit(10);
        int bad = 0;
        int fd = ::open((root / "chain" / "manifest.json").c_str(), O_RDONLY | O_CLOEXEC);
        if (fd < 0) bad |= 1; else ::close(fd);                                                        // read under root: allowed
        if (::open((root / "chain" / "new").c_str(), O_WRONLY | O_CREAT | O_CLOEXEC, 0600) >= 0 || errno != EPERM) bad |= 2;   // write-open: seccomp
        if (abi > 0 && ::open("/etc/hostname", O_RDONLY | O_CLOEXEC) >= 0) bad |= 4;                  // outside root: Landlock
        if (::socket(AF_INET, SOCK_STREAM, 0) >= 0 || errno != EPERM) bad |= 8;                        // no new sockets
        char* argv[] = {const_cast<char*>("/bin/true"), nullptr};
        ::execv("/bin/true", argv);
        if (errno != EPERM) bad |= 16;                                                                 // no exec
        _exit(bad);
    }
    int st = 0;
    ::waitpid(pid, &st, 0);
    fs::remove_all(root);
    if (!WIFEXITED(st) || WEXITSTATUS(st) != 0) { std::fprintf(stderr, "sandbox checks failed: status %d\n", WIFEXITED(st) ? WEXITSTATUS(st) : -1); return 1; }
    std::puts("serve_sandbox ok");
    return 0;
}
