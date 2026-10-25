#pragma once
// emv2-serve sandbox, adapted from economic-machine's solution_pipeline sandbox (native/src/solution_pipeline.cpp) for a server:
// installed once, after the listeners are bound and the first builds are mapped, before the worker threads start (threads
// inherit it). seccomp keeps what serving needs: accept/epoll/recv/send on the bound sockets, mmap and pread of segments, and
// openat only with read-only flags (SIGHUP re-reads current/manifest.json and maps new segments). socket, connect, bind, listen,
// exec*, ptrace, write-opens and everything else answer EPERM. Landlock limits file reads to the data root.
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/landlock.h>
#include <linux/seccomp.h>
#include <linux/userfaultfd.h>
#include <string>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

namespace machine::engine::serve {

// Landlock: read files and directories under `root` only. Returns the ABI used (0 = Landlock unavailable, nothing applied).
inline int landlock_read_only(const std::string& root) {
    int abi = int(::syscall(SYS_landlock_create_ruleset, nullptr, 0, LANDLOCK_CREATE_RULESET_VERSION));
    if (abi < 1) return 0;
    landlock_ruleset_attr ra{};
    ra.handled_access_fs = LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR |
                           LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE | LANDLOCK_ACCESS_FS_MAKE_CHAR | LANDLOCK_ACCESS_FS_MAKE_DIR |
                           LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_SOCK | LANDLOCK_ACCESS_FS_MAKE_FIFO | LANDLOCK_ACCESS_FS_MAKE_BLOCK |
                           LANDLOCK_ACCESS_FS_MAKE_SYM;
#ifdef LANDLOCK_ACCESS_FS_REFER
    if (abi >= 2) ra.handled_access_fs |= LANDLOCK_ACCESS_FS_REFER;
#endif
#ifdef LANDLOCK_ACCESS_FS_TRUNCATE
    if (abi >= 3) ra.handled_access_fs |= LANDLOCK_ACCESS_FS_TRUNCATE;
#endif
    size_t ra_size = abi >= 4 ? sizeof ra : offsetof(landlock_ruleset_attr, handled_access_fs) + sizeof ra.handled_access_fs;
    int rs = int(::syscall(SYS_landlock_create_ruleset, &ra, ra_size, 0));
    if (rs < 0) return 0;
    int dir = ::open(root.c_str(), O_PATH | O_CLOEXEC | O_DIRECTORY);
    if (dir < 0) { ::close(rs); return 0; }
    landlock_path_beneath_attr pb{};
    pb.allowed_access = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR;
    pb.parent_fd = dir;
    bool ok = ::syscall(SYS_landlock_add_rule, rs, LANDLOCK_RULE_PATH_BENEATH, &pb, 0) == 0;
    ::close(dir);
    if (ok) ok = ::prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0 && ::syscall(SYS_landlock_restrict_self, rs, 0) == 0;
    ::close(rs);
    return ok ? abi : -1;
}

inline bool seccomp_serve() noexcept {
#define ALLOW(nr) BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, nr, 0, 1), BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)
    constexpr uint32_t write_flags = O_WRONLY | O_RDWR | O_CREAT | O_TRUNC | O_APPEND | (O_TMPFILE & ~O_DIRECTORY);   // O_DIRECTORY stays allowed (opendir)
    sock_filter rules[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)),
        // openat(dirfd, path, flags, mode): allowed only when flags carry no write/create bit
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_openat, 0, 4),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args) + 2 * sizeof(uint64_t)),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, write_flags, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        // ioctl: only the userfaultfd requests of the cold tier (coldmap.hpp)
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_ioctl, 0, 7),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args) + 1 * sizeof(uint64_t)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, uint32_t(UFFDIO_COPY), 4, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, uint32_t(UFFDIO_WAKE), 3, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, uint32_t(UFFDIO_REGISTER), 2, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, uint32_t(UFFDIO_UNREGISTER), 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)),
        ALLOW(SYS_read), ALLOW(SYS_write), ALLOW(SYS_close), ALLOW(SYS_pread64), ALLOW(SYS_preadv), ALLOW(SYS_lseek),
        ALLOW(SYS_fstat), ALLOW(SYS_newfstatat), ALLOW(SYS_statx), ALLOW(SYS_getdents64), ALLOW(SYS_readlinkat), ALLOW(SYS_fcntl),
        ALLOW(SYS_mmap), ALLOW(SYS_mprotect), ALLOW(SYS_munmap), ALLOW(SYS_mremap), ALLOW(SYS_brk), ALLOW(SYS_madvise),
        ALLOW(SYS_clone), ALLOW(SYS_futex), ALLOW(SYS_set_robust_list), ALLOW(SYS_get_robust_list),
        ALLOW(SYS_rt_sigaction), ALLOW(SYS_rt_sigprocmask), ALLOW(SYS_rt_sigreturn), ALLOW(SYS_rt_sigtimedwait), ALLOW(SYS_sigaltstack),
        ALLOW(SYS_getpid), ALLOW(SYS_gettid), ALLOW(SYS_tgkill), ALLOW(SYS_getrandom), ALLOW(SYS_prlimit64),
        ALLOW(SYS_sched_yield), ALLOW(SYS_sched_setaffinity), ALLOW(SYS_sched_getaffinity),
        ALLOW(SYS_clock_gettime), ALLOW(SYS_clock_nanosleep), ALLOW(SYS_nanosleep), ALLOW(SYS_exit), ALLOW(SYS_exit_group),
        ALLOW(SYS_epoll_wait), ALLOW(SYS_epoll_pwait), ALLOW(SYS_epoll_ctl), ALLOW(SYS_accept4), ALLOW(SYS_accept),
        ALLOW(SYS_recvfrom), ALLOW(SYS_sendto), ALLOW(SYS_sendmsg), ALLOW(SYS_recvmsg), ALLOW(SYS_setsockopt), ALLOW(SYS_getsockopt),
        ALLOW(SYS_shutdown), ALLOW(SYS_writev),
#ifdef SYS_rseq
        ALLOW(SYS_rseq),
#endif
#ifdef SYS_epoll_pwait2
        ALLOW(SYS_epoll_pwait2),
#endif
#ifdef SYS_clone3
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone3, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS),   // glibc falls back to clone
#endif
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
    };
#undef ALLOW
    sock_fprog prog{static_cast<unsigned short>(sizeof(rules) / sizeof(rules[0])), rules};
    return ::prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0 && ::syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC, &prog) == 0;
}
} // namespace machine::engine::serve
