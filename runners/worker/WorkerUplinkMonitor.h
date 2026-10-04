#pragma once
#include "boost-http/http-client.h"

#include "td/actor/ActorId.h"
#include "td/actor/common.h"
#include "td/utils/Random.h"
#include "td/utils/Time.h"
#include "td/utils/port/IPAddress.h"

namespace cocoon {

class WorkerRunner;

class WorkerUplinkMonitor : public td::actor::Actor {
 public:
  WorkerUplinkMonitor(td::IPAddress addr, td::actor::ActorId<WorkerRunner> runner, td::actor::Scheduler *scheduler)
      : addr_(addr), runner_(runner), scheduler_(scheduler) {
  }
  void alarm() override {
    td::actor::Actor::alarm();
    if (next_check_at_.is_in_past()) {
      next_check_at_ = td::Timestamp::never();
      send_request();
    }
    alarm_timestamp().relax(next_check_at_);
  }
  void start_up() override {
    send_request();
  }
  void tear_down() override {
    request_.cancel();
  }

  void send_request();
  void got_http_answer(td::int32 status_code);
  void requests_completed(bool is_success);

 private:
  http::HttpRequestHandle request_;
  td::IPAddress addr_;
  td::actor::ActorId<WorkerRunner> runner_;
  td::actor::Scheduler *scheduler_;
  td::Timestamp next_check_at_;
  bool cur_state_{false};
};

}  // namespace cocoon
