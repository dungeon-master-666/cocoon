#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <grp.h>
#include <linux/capability.h>
#include <sched.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <string>

// Fixed local launch wrapper, not setuid. The trusted agent builds its argv.
int main(int argc, char **argv) {
  auto fail = [](const char *what) {
    std::fprintf(stderr, "backend sandbox: %s\n", what);
    return 1;
  };
  if (argc < 4 || geteuid() != 0)
    return fail("root launch required");
  std::string name = argv[1];
  if (name.size() != 27 || !name.starts_with("cp-") ||
      name.find_first_not_of("0123456789abcdef", 3) != std::string::npos)
    return fail("invalid namespace");
  const auto parent = static_cast<pid_t>(std::strtol(argv[2], nullptr, 10));
  if (parent <= 1 || getppid() != parent)
    return fail("agent disappeared");
  int fd = open(("/run/netns/" + name).c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0 || setns(fd, CLONE_NEWNET) != 0)
    return fail("cannot enter engine namespace");
  close(fd);
  rlimit core{0, 0};
  if (setrlimit(RLIMIT_CORE, &core) != 0 || prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0)
    return fail("cannot restrict process");
  for (int cap = 0; cap <= CAP_LAST_CAP; ++cap) {
    if (prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0)
      return fail("cannot drop capability bounding set");
  }
  if (setgroups(0, nullptr) != 0 || setresgid(65534, 65534, 65534) != 0 || setresuid(65534, 65534, 65534) != 0)
    return fail("cannot drop uid/gid");
  __user_cap_header_struct header{_LINUX_CAPABILITY_VERSION_3, 0};
  __user_cap_data_struct caps[2]{};
  if (syscall(SYS_capset, &header, caps) != 0 || prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0 ||
      prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) != 0 || getppid() != parent)
    return fail("cannot bind backend lifetime to agent");
  execv(argv[3], &argv[3]);
  return fail("exec failed");
}
