#include "WorkerUplinkMonitor.h"
#include "WorkerRunner.h"
#include "boost-http/http.h"
#include "td/actor/ActorId.h"
#include "td/actor/actor.h"
#include "td/actor/common.h"
#include "td/utils/Time.h"
#include "runners/helpers/HttpSender.hpp"
#include "td/utils/buffer.h"
#include "boost-http/http-client.h"
#include <memory>

namespace cocoon {

void WorkerUplinkMonitor::send_request() {
  class Cb : public http::HttpRequestCallback {
   public:
    Cb(td::actor::ActorId<WorkerUplinkMonitor> self, td::actor::Scheduler *scheduler)
        : self_(self), scheduler_(scheduler) {
    }

    void receive_answer(td::int32 status_code, std::string content_type,
                        std::vector<std::pair<std::string, std::string>> headers, std::string body_part = "",
                        bool is_completed = false) override {
      status_code_ = status_code;
      if (is_completed)
        complete(status_code_);
    }
    void receive_payload_part(std::string body_part, bool is_completed) override {
      if (is_completed)
        complete(status_code_);
    }
    void receive_error(td::Status error) override {
      complete(502);
    }

   private:
    void complete(td::int32 code) {
      scheduler_->run_in_context(
          [&]() { td::actor::send_closure_later(self_, &WorkerUplinkMonitor::got_http_answer, code); });
    }
    td::int32 status_code_{502};
    td::actor::ActorId<WorkerUplinkMonitor> self_;
    td::actor::Scheduler *scheduler_;
  };

  request_ = http::run_http_request(addr_, http::HttpCallback::RequestType::Get, "/v1/models", {}, "", 30.0,
                                    std::make_unique<Cb>(actor_id(this), scheduler_));
}

void WorkerUplinkMonitor::got_http_answer(td::int32 status_code) {
  requests_completed(status_code == 200);
}

void WorkerUplinkMonitor::requests_completed(bool is_success) {
  if (is_success != cur_state_) {
    LOG(INFO) << "changing uplink state: ready=" << (is_success ? "YES" : "NO");
    cur_state_ = is_success;
    td::actor::send_closure(runner_, &WorkerRunner::set_uplink_is_ok, is_success);
  }
  next_check_at_ = td::Timestamp::in(td::Random::fast(1.0, 2.0));
  alarm_timestamp().relax(next_check_at_);
}

}  // namespace cocoon
