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
  if (gate_)
    gate_->close();
  if (group_)
    group_->close();
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
  auto group_status = group_ ? group_->status() : Json(nullptr);
  const bool group_state = group_ && !stopping_ && !finished_ && state_ != "BACKOFF";
  return {{"state", group_state ? group_status.at("state") : Json(state_)},
          {"local_state", state_},
          {"security_mode", config_.profile.security_mode == SecurityMode::Dev ? "dev" : "production"},
          {"hardware_attested", false},
          {"local_ready", state_ == "LOCAL_READY"},
          {"group_ready", group_ && group_->ready() && !stopping_ && !finished_},
          {"epoch", group_ ? group_status.at("epoch") : Json(nullptr)},
          {"group", group_status},
          {"gate", gate_ ? gate_->status() : Json(nullptr)},
          {"boot_id", boot_id_},
          {"attempt", attempt_},
          {"last_failure", last_failure_},
          {"rank", config_.rank},
          {"role", config_.role},
          {"profile", config_.profile.id},
          {"config_digest", config_.digest},
          {"failure", failure_.empty() ? Json(nullptr) : Json(failure_)},
          {"process", process_->status()},
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
    if (config_.gate_port) {
      gate_ = std::make_shared<Gate>(io_, config_, [this] { return gate_state(); });
      gate_->start();
    }
    if (config_.group) {
      boot_id_ = random_id();
      identity_ = make_identity(config_.group->certificate_base);
      form_group();
    } else
      start_backend();
  } catch (const std::exception &error) {
    begin_stop(error.what());
  }
  alarm_timestamp() = td::Timestamp::in(0.02);
}

void Agent::form_group() {
  group_ = std::make_shared<Group>(config_, io_, *identity_, boot_id_, run_dir_);
  group_->start();
  transition("FORMING");
}

void Agent::start_backend() {
  auto directory = run_dir_;
  if (config_.group) {
    directory += "/e" + std::to_string(attempt_);
    if (mkdir(directory.c_str(), 0700) < 0)
      throw std::runtime_error("cannot create epoch runtime directory");
  }
  plan_ = adapter_->build_launch_plan(config_, directory);
  if (config_.profile.wireguard) {
#ifdef __linux__
    // Traversable parent, root-owned control/status. Only the epoch directory
    // belongs to the unprivileged backend; it cannot replace agent files.
    if (chmod(run_dir_.c_str(), 0711) < 0 || chown(directory.c_str(), 65534, 65534) < 0)
      throw std::runtime_error("cannot prepare unprivileged backend directory");
    auto args = std::vector<std::string>{PIPELINE_SANDBOX, group_->network_namespace(), std::to_string(getpid())};
    args.insert(args.end(), plan_.argv.begin(), plan_.argv.end());
    if (config_.profile.backend == "simulator")
      args.insert(args.end(), {"--overlay-ip", config_.rank == 0 ? "10.231.0.1" : "10.231.0.2"});
    plan_.executable = PIPELINE_SANDBOX;
    plan_.argv = std::move(args);
#else
    throw std::runtime_error("WireGuard backend requires Linux");
#endif
  }
  deadline_ = Clock::now() + std::chrono::milliseconds(config_.profile.startup_ms);
  next_probe_ = Clock::now();
  transition("STARTING");
  process_->start(plan_);
  backend_started_ = true;
  publish();
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
  if (gate_)
    gate_->invalidate();
  if (group_)
    group_->stop(reason);
  if (probe_) {
    probe_->cancel();
    probe_.reset();
  }
  state_ = "STOPPING";
  try {
    process_->stop(Clock::now(), config_.profile.stop_ms, config_.profile.kill_ms);
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
  if (group_)
    group_->close();
  if (group_ && !group_->cleanup_done())
    return;
  if (group_ && group_->cleanup_failed()) {
    failure_ = "network cleanup not confirmed; refusing restart";
    *exit_code_ = 1;
  }
  if (process_->cleanup_failed()) {
    failure_ += (failure_.empty() ? "" : "; ") + std::string("backend cleanup deadline exceeded");
    *exit_code_ = 1;
  } else {
    for (const auto &path : {plan_.api_socket, plan_.health_socket}) {
      if (!path.empty()) {
        unlink(path.c_str());
      }
    }
  }
  if (config_.group && !shutdown_ && !failure_.empty() && !process_->cleanup_failed() &&
      attempt_ < config_.profile.max_restarts && identity_ && (!group_ || !group_->cleanup_failed())) {
    last_failure_ = failure_;
    restart_at_ = Clock::now() + std::chrono::milliseconds(config_.profile.restart_ms);
    transition("BACKOFF");
    stopping_ = false;
    return;
  }
  finished_ = true;
  if (gate_)
    gate_->close();
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

GateState Agent::gate_state() const {
  if (!group_ || !group_->ready() || stopping_ || finished_ || state_ != "LOCAL_READY" ||
      Clock::now() - last_health_ >= std::chrono::milliseconds(config_.profile.watchdog_ms))
    return {};
  return {true, group_->epoch(), plan_.api_socket};
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
              shutdown_ = true;
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
  io_.restart();
  // Bound network work so a busy authenticated peer cannot starve watchdogs.
  for (int work = 0; work < 64 && io_.poll_one() != 0; ++work) {
  }
  const auto now = Clock::now();
  if (stop_requested.load(std::memory_order_relaxed)) {
    shutdown_ = true;
    begin_stop("");
  }
  if (group_ && !stopping_ && state_ != "BACKOFF") {
    group_->tick(state_ == "LOCAL_READY");
    if (group_->stopped()) {
      if (group_->failure().empty())
        shutdown_ = true;
      begin_stop(group_->failure());
    } else if (group_->take_start())
      start_backend();
    auto snapshot = group_->status().dump();
    if (snapshot != last_group_status_) {
      last_group_status_ = snapshot;
      publish();
    }
  }
  if (gate_)
    gate_->tick();
  if (listener_ >= 0) {
    poll_control(now);
  }
  if (stopping_) {
    if (process_->tick_stop(now)) {
      finish_stop();
    }
    return;
  }
  if (state_ == "BACKOFF") {
    if (now >= restart_at_) {
      ++attempt_;
      group_.reset();
      process_ = std::make_unique<Process>();
      plan_ = {};
      backend_started_ = false;
      failure_.clear();
      *exit_code_ = 0;
      // Certificates may change only between epochs, after confirmed cleanup.
      // Dev defaults issue a fresh identity; explicit fixtures are reloaded.
      identity_ = make_identity(config_.group->certificate_base);
      form_group();
    }
    return;
  }
  if (!backend_started_)
    return;
  process_->inspect();
  if (process_->exited()) {
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
