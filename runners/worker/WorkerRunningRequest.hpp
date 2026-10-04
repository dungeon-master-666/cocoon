#pragma once

#include "auto/tl/cocoon_api.h"
#include "common/bitstring.h"
#include "errorcode.h"
#include "runners/BaseRunner.hpp"
#include "td/actor/ActorId.h"
#include "td/actor/common.h"
#include "td/utils/Time.h"
#include "td/utils/buffer.h"
#include "WorkerStats.h"
#include "td/utils/port/Clocks.h"
#include "runners/helpers/ValidateRequest.h"
#include "td/utils/port/IPAddress.h"
#include "tl/TlObject.h"
#include <memory>

namespace cocoon {

class WorkerRunner;

class WorkerRunningRequest : public td::actor::Actor {
 public:
  WorkerRunningRequest(td::Bits256 proxy_request_id, TcpClient::ConnectionId proxy_connection_id, td::BufferSlice data,
                       td::Bits256 encrypted_with, double timeout, td::IPAddress http_server_address,
                       std::string model_base_name, td::int32 coefficient, td::int32 proto_version, bool enable_debug,
                       std::shared_ptr<RunnerConfig> runner_config, td::actor::ActorId<WorkerRunner> runner,
                       td::actor::Scheduler *scheduler, std::shared_ptr<WorkerStats> stats);

  void start_up() override {
    alarm_timestamp() = td::Timestamp::in(timeout_);
    start_request();
  }

  void alarm() override {
    send_error(td::Status::Error(ton::ErrorCode::timeout, "worker: timeout"));
  }

  auto run_time() const {
    return td::Clocks::monotonic() - started_at_;
  }

  void start_request();
  void process_request_response(td::int32 status_code, std::vector<std::pair<std::string, std::string>> headers,
                                std::string payload_part, bool payload_is_completed);
  void send_error(td::Status error);
  void send_answer(td::int32 status_code, std::vector<std::pair<std::string, std::string>> headers,
                   std::string payload_part, bool payload_is_completed);
  void send_payload_part(std::string payload_part, bool payload_is_completed);
  void finish_request(bool is_success);

  WorkerStats *stats() {
    return stats_.get();
  }

  std::string generate_worker_debug() {
    if (!enable_debug_) {
      return "";
    } else {
      return generate_worker_debug_inner();
    }
  }

  ton::tl_object_ptr<cocoon_api::proxy_queryFinalInfo> create_final_info(bool success = true);

 private:
  std::string generate_worker_debug_inner();
  td::Result<std::string> process_payload(td::Slice payload, bool completed);
  void receive_http_error_payload(std::string payload, bool completed);
  void send_processed_payload(std::string payload, bool completed);
  td::int32 backend_status_{0};
  std::vector<std::pair<std::string, std::string>> backend_headers_;
  std::string error_body_;
  td::Bits256 proxy_request_id_;
  TcpClient::ConnectionId proxy_connection_id_;
  td::BufferSlice data_;
  double timeout_;
  td::IPAddress http_server_address_;
  std::string model_base_name_;
  td::int32 coefficient_;
  td::int32 proto_version_;
  bool enable_debug_;
  td::actor::ActorId<WorkerRunner> runner_;
  td::actor::Scheduler *scheduler_;
  std::shared_ptr<WorkerStats> stats_;

  td::int32 payload_parts_{0};
  td::int64 payload_bytes_{0};
  double started_at_ = td::Clocks::monotonic();
  double started_at_unix_ = td::Clocks::system();
  double received_answer_at_unix_ = td::Clocks::system();
  bool completed_{false};
  bool sent_answer_{false};

  td::Bits256 worker_private_key_ = td::Bits256::zero();
  td::Bits256 client_public_key_ = td::Bits256::zero();

  std::shared_ptr<RunnerConfig> runner_config_;
  std::unique_ptr<AnswerPostprocessor> postprocessor_;
};

}  // namespace cocoon
