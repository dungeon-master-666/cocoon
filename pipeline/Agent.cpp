#include "pipeline/Agent.h"

#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace cocoon::pipeline {
static_assert(std::atomic<bool>::is_always_lock_free);
std::atomic<bool> stop_requested{false};
namespace {
void nonblocking(int fd) {
  if (fcntl(fd, F_SETFD, FD_CLOEXEC) < 0 || fcntl(fd, F_SETFL, O_NONBLOCK) < 0) {
    throw std::runtime_error("cannot configure local control socket");
  }
}
}  // namespace

Agent::Agent(Config config, std::string run_dir, int *exit_code)
    : config_(std::move(config)), run_dir_(std::move(run_dir)), exit_code_(exit_code) {
}

Agent::~Agent() {
  close_control();
}

void Agent::open_control() {
  auto path = run_dir_ + "/control.sock";
  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  if (path.size() >= sizeof(address.sun_path)) {
    throw std::runtime_error("run directory path is too long for Unix sockets");
  }
  std::memcpy(address.sun_path, path.c_str(), path.size() + 1);
  listener_ = socket(AF_UNIX, SOCK_STREAM, 0);
  if (listener_ < 0) {
    throw std::runtime_error("cannot create local control socket");
  }
  nonblocking(listener_);
  if (bind(listener_, reinterpret_cast<sockaddr *>(&address), sizeof(address)) < 0 || chmod(path.c_str(), 0600) < 0 ||
      listen(listener_, 16) < 0) {
    throw std::runtime_error(std::string("cannot bind local control socket: ") + std::strerror(errno));
  }
}

void Agent::close_control() {
  for (auto &client : clients_) {
    close(client.fd);
  }
  clients_.clear();
  if (listener_ >= 0) {
    close(listener_);
    listener_ = -1;
    unlink((run_dir_ + "/control.sock").c_str());
  }
}

Json Agent::status() const {
  return {{"state", state_},
          {"security_mode", config_.profile.security_mode == SecurityMode::Dev ? "dev" : "production"},
          {"hardware_attested", false},
          {"local_ready", state_ == "LOCAL_READY"},
          {"group_ready", false},
          {"epoch", nullptr},
          {"rank", config_.rank},
          {"role", config_.role},
          {"profile", config_.profile.id},
          {"config_digest", config_.digest},
          {"failure", failure_.empty() ? Json(nullptr) : Json(failure_)},
          {"process", process_.status()},
          {"backend_socket", plan_.api_socket},
          {"health_socket", plan_.health_socket}};
}

void Agent::publish() {
  const auto data = status().dump();
  const auto tmp = run_dir_ + "/status.tmp";
  std::ofstream out(tmp, std::ios::trunc);
  out << data << '\n';
  out.close();
  if (!out || rename(tmp.c_str(), (run_dir_ + "/status.json").c_str()) < 0) {
    throw std::runtime_error("cannot write agent status");
  }
  std::cout << data << std::endl;
}

void Agent::transition(std::string state) {
  state_ = std::move(state);
  publish();
}

void Agent::start_up() {
  try {
    open_control();
    adapter_ = make_adapter(config_);
    plan_ = adapter_->build_launch_plan(config_, run_dir_);
    deadline_ = Clock::now() + std::chrono::milliseconds(config_.profile.startup_ms);
    next_probe_ = Clock::now();
    transition("STARTING");
    process_.start(plan_);
    publish();
  } catch (const std::exception &error) {
    begin_stop(error.what());
  }
  alarm_timestamp() = td::Timestamp::in(0.02);
}

void Agent::begin_stop(const std::string &reason) {
  if (!reason.empty() && failure_.empty()) {
    failure_ = reason;
    *exit_code_ = 1;
  }
  if (stopping_) {
    return;
  }
  stopping_ = true;
  if (probe_) {
    probe_->cancel();
    probe_.reset();
  }
  state_ = "STOPPING";
  try {
    process_.stop(Clock::now(), config_.profile.stop_ms, config_.profile.kill_ms);
    publish();
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    if (failure_.empty()) {
      failure_ = error.what();
    }
    *exit_code_ = 1;
  }
}

void Agent::finish_stop() {
  if (process_.cleanup_failed()) {
    failure_ += (failure_.empty() ? "" : "; ") + std::string("backend cleanup deadline exceeded");
    *exit_code_ = 1;
  } else {
    for (const auto &path : {plan_.api_socket, plan_.health_socket}) {
      if (!path.empty()) {
        unlink(path.c_str());
      }
    }
  }
  finished_ = true;
  state_ = failure_.empty() ? "STOPPED" : "FAILED";
  try {
    publish();
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    *exit_code_ = 1;
  }
  close_control();
  td::actor::SchedulerContext::get().stop();
  stop();
}

bool Agent::probe_valid(bool warmup) const {
  if (!probe_->ok()) {
    return false;
  }
  try {
    auto body = Json::parse(probe_->body());
    return warmup ? adapter_->valid_warmup(config_, body) : adapter_->valid_health(config_, body);
  } catch (const std::exception &) {
    return false;
  }
}

void Agent::poll_control(Time now) {
  // Bound both pending clients and work per actor tick. A stalled client cannot
  // block backend watchdogs, stop signals or other status clients.
  for (int count = 0; count < 8; ++count) {
    int fd = accept(listener_, nullptr, nullptr);
    if (fd < 0) {
      break;
    }
    if (clients_.size() == 16) {
      close(fd);
      continue;
    }
    try {
      nonblocking(fd);
    } catch (...) {
      close(fd);
      throw;
    }
    clients_.push_back({fd, now + std::chrono::seconds(1), "", "", 0});
  }
  for (auto it = clients_.begin(); it != clients_.end();) {
    bool closed = now >= it->deadline;
    if (!closed && it->output.empty()) {
      char data[4096];
      auto n = recv(it->fd, data, sizeof(data), 0);
      if (n > 0) {
        it->input.append(data, static_cast<size_t>(n));
        if (it->input.size() > 4096) {
          closed = true;
        } else if (it->input.find('\n') != std::string::npos) {
          Json reply;
          try {
            auto cmd = Json::parse(it->input);
            if (!cmd.is_object() || cmd.size() != 1 || !cmd.contains("op") ||
                (cmd["op"] != "status" && cmd["op"] != "stop")) {
              throw std::runtime_error("expected status or stop operation");
            }
            if (cmd["op"] == "stop") {
              begin_stop("");
            }
            reply = status();
          } catch (const std::exception &error) {
            reply = {{"error", error.what()}};
          }
          it->output = reply.dump() + "\n";
        }
      } else if (n == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
        closed = true;
      }
    }
    if (!closed && !it->output.empty()) {
      auto n = send(it->fd, it->output.data() + it->sent, it->output.size() - it->sent, 0);
      if (n > 0) {
        it->sent += static_cast<size_t>(n);
        closed = it->sent == it->output.size();
      } else if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
        closed = true;
      }
    }
    if (closed) {
      close(it->fd);
      it = clients_.erase(it);
    } else {
      ++it;
    }
  }
}

void Agent::tick() {
  const auto now = Clock::now();
  io_.restart();
  io_.poll();
  if (stop_requested.load(std::memory_order_relaxed)) {
    begin_stop("");
  }
  if (listener_ >= 0) {
    poll_control(now);
  }
  if (stopping_) {
    if (process_.tick_stop(now)) {
      finish_stop();
    }
    return;
  }
  process_.inspect();
  if (process_.exited()) {
    begin_stop("backend exited unexpectedly");
    return;
  }
  if ((state_ == "STARTING" || state_ == "WARMING") && now >= deadline_) {
    begin_stop(state_ == "STARTING" ? "backend startup deadline exceeded" : "backend warmup deadline exceeded");
    return;
  }
  if (probe_ && probe_->done()) {
    const bool warmup = state_ == "WARMING";
    const bool valid = probe_valid(warmup);
    const auto detail = probe_->error();
    probe_.reset();
    next_probe_ = now + std::chrono::milliseconds(config_.profile.probe_interval_ms);
    if (valid) {
      last_health_ = now;
      if (state_ == "STARTING") {
        deadline_ = now + std::chrono::milliseconds(config_.profile.warmup_ms);
        transition("WARMING");
        probe_ = HttpProbe::start(io_, plan_.api_socket, adapter_->warmup(config_), config_.profile.warmup_ms);
      } else if (warmup) {
        transition("LOCAL_READY");
      }
    } else if (warmup) {
      begin_stop("backend warmup failed" + (detail.empty() ? " (invalid response)" : ": " + detail));
    }
  }
  if (state_ == "LOCAL_READY" && now - last_health_ >= std::chrono::milliseconds(config_.profile.watchdog_ms)) {
    begin_stop("backend health watchdog expired");
  }
  if (!stopping_ && !probe_ && now >= next_probe_) {
    probe_ = HttpProbe::start(io_, plan_.health_socket, adapter_->health(), config_.profile.probe_ms);
  }
}

void Agent::alarm() {
  try {
    tick();
  } catch (const std::exception &error) {
    begin_stop(error.what());
  }
  if (!finished_) {
    alarm_timestamp() = td::Timestamp::in(0.02);
  }
}
}  // namespace cocoon::pipeline
