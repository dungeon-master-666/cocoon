#pragma once

#include "pipeline/HttpProbe.h"
#include "pipeline/Group.h"
#include "td/actor/actor.h"
#include <atomic>
#include <csignal>
#include <vector>

namespace cocoon::pipeline {
extern std::atomic<bool> stop_requested;

class Agent final : public td::actor::Actor {
 public:
  Agent(Config config, std::string run_dir, int *exit_code);
  ~Agent() override;

 private:
  struct Client {
    int fd;
    Time deadline;
    std::string input;
    std::string output;
    size_t sent = 0;
  };
  void start_up() override;
  void start_backend();
  void form_group();
  void alarm() override;
  void tick();
  void begin_stop(const std::string &reason);
  void finish_stop();
  void transition(std::string state);
  Json status() const;
  void publish();
  void open_control();
  void poll_control(Time now);
  void close_control();
  bool probe_valid(bool warmup) const;

  Config config_;
  std::string run_dir_;
  int *exit_code_;
  std::unique_ptr<BackendAdapter> adapter_;
  LaunchPlan plan_;
  std::unique_ptr<Process> process_ = std::make_unique<Process>();
  boost::asio::io_context io_;
  std::shared_ptr<HttpProbe> probe_;
  std::string state_ = "BOOTING";
  std::string failure_;
  Time deadline_;
  Time next_probe_;
  Time last_health_;
  int listener_ = -1;
  std::vector<Client> clients_;
  bool stopping_ = false;
  bool finished_ = false;
  bool shutdown_ = false;
  bool backend_started_ = false;
  int attempt_ = 0;
  Time restart_at_;
  std::string boot_id_;
  std::string last_failure_;
  std::string last_group_status_;
  std::optional<Identity> identity_;
  std::shared_ptr<Group> group_;
};
}  // namespace cocoon::pipeline
