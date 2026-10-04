#include "pipeline/Network.h"
#include <cerrno>
#include <fcntl.h>
#include <unistd.h>
#include <openssl/crypto.h>
#include <stdexcept>

namespace cocoon::pipeline {
Network::Network(const Config &config, std::string run_dir)
    : config_(config), run_dir_(std::move(run_dir)), name_("cp-" + random_id().substr(0, 24)) {
}

Network::~Network() {
  if (owner_fd_ >= 0)
    ::close(owner_fd_);
  if (status_fd_ >= 0)
    ::close(status_fd_);
}

void Network::start(const Json &roster, const NetworkKey &key) {
#ifndef __linux__
  throw std::runtime_error("WireGuard namespaces require Linux");
#else
  if (started_ || geteuid() != 0)
    throw std::runtime_error("network guardian requires root and a fresh epoch");
  setup_ = {{"namespace", name_},
            {"rank", config_.rank},
            {"roster", roster},
            {"private_key", key.private_key_base64()},
            {"underlay_ip", config_.network->underlay_ip},
            {"peer_ip", config_.network->peer_ip},
            {"owner_token", random_id()}};
  spawn_guardian(false);
#endif
}

void Network::spawn_guardian(bool cleanup) {
#ifdef __linux__
  int owner[2], status[2];
  if (pipe2(owner, O_CLOEXEC) != 0)
    throw std::runtime_error("cannot create network owner pipe");
  if (pipe2(status, O_CLOEXEC) != 0) {
    close(owner[0]);
    close(owner[1]);
    throw std::runtime_error("cannot create network status pipe");
  }
  owner_fd_ = owner[1];
  status_fd_ = status[0];
  setup_["cleanup"] = cleanup;
  auto wire = setup_.dump() + "\n";
  if (setup_.contains("private_key")) {
    auto &secret = setup_["private_key"].get_ref<std::string &>();
    OPENSSL_cleanse(secret.data(), secret.size());
    setup_.erase("private_key");
  }
  if (wire.size() > 4096) {
    close(owner[0]);
    close(status[1]);
    OPENSSL_cleanse(wire.data(), wire.size());
    throw std::runtime_error("network setup exceeds pipe budget");
  }
  LaunchPlan plan{PIPELINE_PYTHON,
                  {PIPELINE_PYTHON, "-I", "-u", PIPELINE_NETWORK_HELPER},
                  {"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C", "LC_ALL=C"},
                  run_dir_ + "/network-" + name_ + (cleanup ? "-cleanup.log" : ".log"),
                  "",
                  "",
                  owner[0],
                  status[1]};
  try {
    (cleanup ? cleanup_process_ : process_).start(plan);
  } catch (...) {
    close(owner[0]);
    close(status[1]);
    OPENSSL_cleanse(wire.data(), wire.size());
    throw;
  }
  close(owner[0]);
  close(status[1]);
  started_ = true;
  auto written = write(owner_fd_, wire.data(), wire.size());
  OPENSSL_cleanse(wire.data(), wire.size());
  if (written != static_cast<ssize_t>(wire.size()) || fcntl(status_fd_, F_SETFL, O_NONBLOCK) != 0) {
    throw std::runtime_error("cannot send private setup to local guardian");
  }
  last_message_ = Clock::now();
#endif
}

void Network::tick() {
  if (!started_ || finished_)
    return;
  char bytes[4096];
  for (int count = 0; count < 4; ++count) {
    auto n = read(status_fd_, bytes, sizeof(bytes));
    if (n <= 0)
      break;
    input_.append(bytes, static_cast<size_t>(n));
    if (input_.size() > 16384) {
      failure_ = "network guardian status limit";
      break;
    }
    size_t end;
    while ((end = input_.find('\n')) != std::string::npos) {
      auto line = input_.substr(0, end);
      input_.erase(0, end + 1);
      try {
        auto event = parse_json(line);
        require_fields(event, {"state", "healthy", "clean", "failure"}, "network status");
        last_message_ = Clock::now();
        configured_ = event.at("state") == "configured";
        healthy_ = event.value("healthy", false);
        clean_ = event.at("state") == "stopped" && event.value("clean", false);
        if (event.contains("failure"))
          failure_ = event.at("failure").get<std::string>();
      } catch (...) {
        failure_ = "invalid network guardian status";
        clean_ = false;
      }
    }
  }
  if (fallback_) {
    cleanup_process_.inspect();
    if (!fallback_stopping_ && (cleanup_process_.exited() || Clock::now() >= cleanup_deadline_)) {
      fallback_stopping_ = true;
      cleanup_process_.stop(Clock::now(), 0, 2000);
    }
    if (fallback_stopping_ && cleanup_process_.tick_stop(Clock::now())) {
      finished_ = true;
      if (cleanup_process_.cleanup_failed())
        clean_ = false;
    }
    return;
  }
  process_.inspect();
  if (!stopping_) {
    if (process_.exited() && failure_.empty())
      failure_ = "network guardian exited";
    if (Clock::now() - last_message_ > std::chrono::seconds(4) && failure_.empty())
      failure_ = "network guardian watchdog expired";
  } else if (process_.tick_stop(Clock::now())) {
    if (clean_)
      finished_ = true;
    else {
      // The first guardian is reaped before recovery can touch its resources.
      // The root-only journal proves ownership; this process receives no keys.
      if (status_fd_ >= 0)
        close(status_fd_);
      status_fd_ = -1;
      input_.clear();
      fallback_ = true;
      cleanup_deadline_ = Clock::now() + std::chrono::seconds(6);
      try {
        spawn_guardian(true);
      } catch (...) {
        finished_ = true;
        clean_ = false;
        throw;
      }
      if (owner_fd_ >= 0) {
        close(owner_fd_);
        owner_fd_ = -1;
      }
    }
  }
}

void Network::stop() {
  if (stopping_)
    return;
  stopping_ = true;
  healthy_ = false;
  if (!started_) {
    finished_ = clean_ = true;
    return;
  }
  if (owner_fd_ >= 0) {
    close(owner_fd_);
    owner_fd_ = -1;
  }
  process_.stop(Clock::now(), 5000, 2000);
}

Json Network::status() const {
  return {{"namespace", name_},
          {"configured", configured()},
          {"healthy", healthy()},
          {"failure", failure_},
          {"cleanup_complete", finished_ && clean_ && !process_.cleanup_failed()},
          {"cleanup_failed", cleanup_failed()},
          {"cleanup_guardian", cleanup_process_.status()},
          {"guardian", process_.status()}};
}
}  // namespace cocoon::pipeline
