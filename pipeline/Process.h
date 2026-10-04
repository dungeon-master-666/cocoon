#pragma once

#include "pipeline/Profile.h"
#include <chrono>
#include <string>
#include <sys/types.h>
#include <vector>

namespace cocoon::pipeline {
using Clock = std::chrono::steady_clock;
using Time = Clock::time_point;

struct LaunchPlan {
  std::string executable;
  std::vector<std::string> argv;
  std::vector<std::string> env;
  std::string log_path;
  std::string api_socket;
  std::string health_socket;
  int input_fd = -1;
  int output_fd = -1;
};

// Owns only the process group created by posix_spawn. Backends must not daemonize
// or escape it. Linux deployment will additionally contain ranks in a cgroup.
class Process {
 public:
  Process() = default;
  Process(const Process &) = delete;
  Process &operator=(const Process &) = delete;
  ~Process();
  void start(const LaunchPlan &plan);
  void inspect();
  void stop(Time now, int grace_ms, int kill_ms);
  bool tick_stop(Time now);
  bool exited() const {
    return exited_;
  }
  bool cleanup_failed() const {
    return cleanup_failed_;
  }
  Json status() const;

 private:
  bool group_alive() const;
  void signal_group(int signal);
  pid_t pid_ = -1;
  bool exited_ = false;
  bool reaped_ = false;
  bool stopping_ = false;
  bool killed_ = false;
  bool finished_ = false;
  bool cleanup_failed_ = false;
  int exit_status_ = 0;
  int kill_ms_ = 0;
  Time term_deadline_;
  Time kill_deadline_;
};
}  // namespace cocoon::pipeline
