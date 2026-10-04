#include "pipeline/Process.h"

#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdexcept>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

namespace cocoon::pipeline {
namespace {
void check(int code, const char *operation) {
  if (code) {
    throw std::runtime_error(std::string(operation) + ": " + std::strerror(code));
  }
}
}  // namespace

void Process::start(const LaunchPlan &plan) {
  if (pid_ != -1 || plan.argv.empty() || plan.argv.front() != plan.executable) {
    throw std::runtime_error("invalid process launch plan or repeated start");
  }
  posix_spawn_file_actions_t actions;
  posix_spawnattr_t attr;
  check(posix_spawn_file_actions_init(&actions), "spawn actions");
  int code = posix_spawnattr_init(&attr);
  if (code) {
    posix_spawn_file_actions_destroy(&actions);
    check(code, "spawn attributes");
  }
  try {
    check(posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0), "spawn stdin");
    check(posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, plan.log_path.c_str(), O_WRONLY | O_CREAT | O_EXCL,
                                           0600),
          "spawn log");
    check(posix_spawn_file_actions_adddup2(&actions, STDOUT_FILENO, STDERR_FILENO), "spawn stderr");
    sigset_t empty, defaults;
    sigemptyset(&empty);
    sigemptyset(&defaults);
    for (int sig : {SIGTERM, SIGINT, SIGPIPE}) {
      sigaddset(&defaults, sig);
    }
    check(posix_spawnattr_setsigmask(&attr, &empty), "spawn signal mask");
    check(posix_spawnattr_setsigdefault(&attr, &defaults), "spawn signal defaults");
    check(posix_spawnattr_setpgroup(&attr, 0), "spawn process group");
    short flags = POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF;
#ifdef POSIX_SPAWN_CLOEXEC_DEFAULT
    flags |= POSIX_SPAWN_CLOEXEC_DEFAULT;
#endif
    check(posix_spawnattr_setflags(&attr, flags), "spawn flags");
    std::vector<char *> argv, env;
    for (const auto &arg : plan.argv) {
      argv.push_back(const_cast<char *>(arg.c_str()));
    }
    for (const auto &entry : plan.env) {
      env.push_back(const_cast<char *>(entry.c_str()));
    }
    argv.push_back(nullptr);
    env.push_back(nullptr);
    pid_t child;
    check(posix_spawn(&child, plan.executable.c_str(), &actions, &attr, argv.data(), env.data()), "spawn backend");
    pid_ = child;
  } catch (...) {
    posix_spawn_file_actions_destroy(&actions);
    posix_spawnattr_destroy(&attr);
    throw;
  }
  posix_spawn_file_actions_destroy(&actions);
  posix_spawnattr_destroy(&attr);
}

void Process::inspect() {
  if (pid_ < 0 || exited_) {
    return;
  }
  // Keep the leader waitable until the final group signal. Its PID then cannot
  // be reused for an unrelated process group between exit detection and stop.
  siginfo_t info{};
  auto result = waitid(P_PID, static_cast<id_t>(pid_), &info, WEXITED | WNOHANG | WNOWAIT);
  if (result == 0 && info.si_pid == pid_) {
    exited_ = true;
    exit_status_ = info.si_code == CLD_EXITED ? info.si_status : 128 + info.si_status;
  } else if (result < 0 && errno != EINTR) {
    // Without child ownership, signalling the old numeric PGID is unsafe.
    cleanup_failed_ = true;
    finished_ = true;
    throw std::runtime_error("lost ownership of backend process");
  }
}

bool Process::group_alive() const {
  if (pid_ < 0) {
    return false;
  }
  if (kill(-pid_, 0) == 0 || errno == EPERM) {
    return true;
  }
  if (errno != ESRCH) {
    throw std::runtime_error("cannot inspect backend process group");
  }
  return false;
}

void Process::signal_group(int sig) {
  if (pid_ > 0 && kill(-pid_, sig) < 0 && errno != ESRCH) {
    // macOS can return EPERM for a group whose only member is the waitable
    // zombie leader. Reap it after the final signal attempt, then still require
    // the entire group to disappear; EPERM alone never proves cleanup.
    if (errno == EPERM) {
      inspect();
      if (exited_) {
        return;
      }
    }
    throw std::runtime_error("cannot signal backend process group");
  }
}

void Process::stop(Time now, int grace_ms, int kill_ms) {
  if (stopping_ || finished_) {
    return;
  }
  stopping_ = true;
  kill_ms_ = kill_ms;
  term_deadline_ = now + std::chrono::milliseconds(grace_ms);
  kill_deadline_ = term_deadline_ + std::chrono::milliseconds(kill_ms);
  signal_group(SIGTERM);
}

bool Process::tick_stop(Time now) {
  if (finished_) {
    return true;
  }
  inspect();
  if (pid_ < 0) {
    finished_ = true;
  } else if (now >= term_deadline_ && !killed_) {
    try {
      signal_group(SIGKILL);
    } catch (...) {
      if (now >= kill_deadline_) {
        cleanup_failed_ = true;
        finished_ = true;
      }
      throw;
    }
    killed_ = true;
    kill_deadline_ = now + std::chrono::milliseconds(kill_ms_);
  }
  if (killed_ && !reaped_) {
    int status;
    auto result = waitpid(pid_, &status, WNOHANG);
    if (result == pid_) {
      reaped_ = true;
      exited_ = true;
      exit_status_ = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
    } else if (result < 0 && errno != EINTR) {
      cleanup_failed_ = true;
      finished_ = true;
      throw std::runtime_error("cannot reap backend process");
    }
  }
  if (reaped_ && !group_alive()) {
    finished_ = true;
  } else if (killed_ && now >= kill_deadline_) {
    cleanup_failed_ = true;
    finished_ = true;
  }
  return finished_;
}

Json Process::status() const {
  Json out = {{"pid", pid_},
              {"pgid", pid_},
              {"exited", exited_},
              {"reaped", reaped_},
              {"kill_sent", killed_},
              {"cleanup_complete", finished_ && !cleanup_failed_},
              {"cleanup_failed", cleanup_failed_}};
  if (exited_) {
    out["exit_code"] = exit_status_;
  }
  return out;
}

Process::~Process() {
  // Last resort for exceptions, also bounded. The normal actor path is asynchronous.
  if (pid_ > 0 && !finished_) {
    try {
      stop(Clock::now(), 0, 2000);
      if (!killed_) {
        signal_group(SIGKILL);
        killed_ = true;
        kill_deadline_ = Clock::now() + std::chrono::seconds(2);
      }
      while (!tick_stop(Clock::now())) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
      }
    } catch (...) {
      // Destructors cannot propagate; the caller reports the original failure.
    }
  }
}
}  // namespace cocoon::pipeline
