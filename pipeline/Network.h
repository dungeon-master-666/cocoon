#pragma once
#include "pipeline/Process.h"
#include "pipeline/Security.h"

namespace cocoon::pipeline {
// Owns a local privileged guardian. Only the initial anonymous pipe carries
// private key material; stdout/status/logs contain public state exclusively.
class Network {
 public:
  Network(const Config &config, std::string run_dir);
  ~Network();
  void start(const Json &roster, const NetworkKey &key);
  void tick();
  void stop();
  bool configured() const {
    return configured_ && failure_.empty();
  }
  bool healthy() const {
    return configured() && healthy_;
  }
  bool cleanup_done() const {
    return finished_;
  }
  bool cleanup_failed() const {
    return finished_ && (!clean_ || process_.cleanup_failed());
  }
  const std::string &failure() const {
    return failure_;
  }
  const std::string &name() const {
    return name_;
  }
  Json status() const;

 private:
  void spawn_guardian(bool cleanup);
  Config config_;
  Json setup_;
  std::string run_dir_, name_, input_, failure_;
  Process process_;
  Process cleanup_process_;
  int owner_fd_ = -1, status_fd_ = -1;
  bool started_ = false, configured_ = false, healthy_ = false, stopping_ = false, finished_ = false, clean_ = false;
  Time last_message_;
  Time cleanup_deadline_;
  bool fallback_ = false, fallback_stopping_ = false;
};
}  // namespace cocoon::pipeline
