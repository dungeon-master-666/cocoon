#include "WorkerRunningRequest.hpp"
#include "WorkerRunner.h"
#include "auto/tl/cocoon_api.h"
#include "boost-http/http.h"
#include "common/bitstring.h"
#include "errorcode.h"
#include "nlohmann/json_fwd.hpp"
#include "runners/helpers/HttpSender.hpp"
#include "runners/helpers/ValidateRequest.h"

#include "cocoon-tl-utils/cocoon-tl-utils.hpp"
#include "td/actor/actor.h"
#include "td/utils/Random.h"
#include "td/utils/JsonBuilder.h"
#include "td/utils/Status.h"
#include "td/utils/Time.h"
#include "td/utils/buffer.h"
#include "td/utils/port/Clocks.h"
#include "tl-utils/common-utils.hpp"
#include "tl/TlObject.h"
#include "boost-http/http-client.h"
#include "boost-http/pipeline-metadata.h"
#include <nlohmann/json.hpp>
#include <memory>
#include <vector>

namespace cocoon {

WorkerRunningRequest::WorkerRunningRequest(td::Bits256 proxy_request_id, TcpClient::ConnectionId proxy_connection_id,
                                           td::BufferSlice data, td::Bits256 worker_private_key, double timeout,
                                           td::IPAddress http_server_address, std::string model_base_name,
                                           td::int32 coefficient, td::int32 proto_version, bool enable_debug,
                                           std::shared_ptr<RunnerConfig> runner_config,
                                           td::actor::ActorId<WorkerRunner> runner, td::actor::Scheduler *scheduler,
                                           std::shared_ptr<WorkerStats> stats)
    : proxy_request_id_(proxy_request_id)
    , proxy_connection_id_(proxy_connection_id)
    , data_(std::move(data))
    , timeout_(timeout)
    , http_server_address_(http_server_address)
    , model_base_name_(std::move(model_base_name))
    , coefficient_(coefficient)
    , proto_version_(proto_version)
    , enable_debug_(enable_debug)
    , runner_(runner)
    , scheduler_(scheduler)
    , stats_(std::move(stats)) {
  worker_private_key_ = worker_private_key;
  runner_config_ = runner_config;
}

/*
 *
 * 1. unpack request
 * 2. forward http request 
 * 3. receive http answer
 * 4. start downloading http answer payload
 *
 */
void WorkerRunningRequest::start_request() {
  LOG(INFO) << "worker request " << proxy_request_id_.to_hex() << ": received";
  stats()->requests_received++;
  if (!check_deadline())
    return;

  auto R = cocoon::fetch_tl_object<cocoon_api::http_request>(std::move(data_), true);
  if (R.is_error()) {
    send_error(R.move_as_error_prefix("worker: invalid http request: "));
    return;
  }

  auto req = R.move_as_ok();

  // we count only bytes in payload
  stats()->request_bytes_received += (double)req->payload_.size();

  static const std::string v_stream = "stream";
  static const std::string v_stream_options = "stream_options";
  static const std::string v_include_usage = "include_usage";

  td::BufferSlice new_payload;

  std::unique_ptr<ton::http::HttpRequest> request;
  http::HttpCallback::RequestType request_type;
  std::string url;
  std::vector<std::pair<std::string, std::string>> headers;
  auto S = [&]() {
    if (req->method_ == "POST" || req->method_ == "post" || req->payload_.size() > 0) {
      request_type = http::HttpCallback::RequestType::Post;
    } else {
      request_type = http::HttpCallback::RequestType::Get;
    }

    url = req->url_;

    std::string content_type;
    for (auto &h : req->headers_) {
      auto name_copy = h->name_;
      std::transform(name_copy.begin(), name_copy.end(), name_copy.begin(),
                     [](unsigned char c) { return std::tolower(c); });
      if (name_copy == "content-type") {
        content_type = h->value_;
      }
      if (name_copy == "content-length" || name_copy == "transfer-encoding" || name_copy == "connection" ||
          name_copy.rfind(http::pipeline_header_prefix, 0) == 0) {
        continue;
      }
      headers.emplace_back(h->name_, h->value_);
    }
    std::string model;
    TRY_RESULT_ASSIGN(new_payload, validate_decrypt_request(req->url_, content_type, std::move(req->payload_), &model,
                                                            nullptr, worker_private_key_, &client_public_key_));
    if (model != model_base_name_) {
      return td::Status::Error(ton::ErrorCode::protoviolation, "model name mismatch");
    }
    postprocessor_ = std::make_unique<AnswerPostprocessor>(
        coefficient_, runner_config_->root_contract_config->prompt_tokens_price_multiplier(),
        runner_config_->root_contract_config->cached_tokens_price_multiplier(),
        runner_config_->root_contract_config->completion_tokens_price_multiplier(),
        runner_config_->root_contract_config->reasoning_tokens_price_multiplier(),
        runner_config_->root_contract_config->price_per_token(), worker_private_key_, client_public_key_);
    postprocessor_->add_prompt(req->payload_.as_slice());
    postprocessor_->set_transcription(url == "/v1/audio/transcriptions");

    TRY_RESULT_ASSIGN(request, ton::http::HttpRequest::create(req->method_, req->url_, req->http_version_));
    for (auto &x : req->headers_) {
      auto name = x->name_;
      std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });
      if (name == "content-length" || x->name_ == "transfer-encoding" || x->name_ == "connection") {
        continue;
      }
      ton::http::HttpHeader h{x->name_, x->value_};
      TRY_STATUS(h.basic_check());
      request->add_header(std::move(h));
    }
    TRY_STATUS(request->complete_parse_header());
    return td::Status::OK();
  }();
  if (S.is_error()) {
    send_error(S.move_as_error_prefix("worker: invalid http request: "));
    return;
  }

  LOG(INFO) << "working request " << proxy_request_id_.to_hex() << ": sending request to url " << req->url_
            << " method=" << req->method_;

  class Cb : public http::HttpRequestCallback {
   public:
    Cb(td::actor::ActorId<WorkerRunningRequest> self, td::actor::Scheduler *scheduler)
        : self_(self), scheduler_(scheduler) {
    }
    void receive_answer(td::int32 status_code, std::string content_type,
                        std::vector<std::pair<std::string, std::string>> headers, std::string body_part = "",
                        bool is_completed = false) override {
      scheduler_->run_in_context([&]() {
        td::actor::send_closure_later(self_, &WorkerRunningRequest::process_request_response, status_code,
                                      std::move(headers), body_part, is_completed);
      });
    }
    void receive_payload_part(std::string body_part, bool is_completed) override {
      scheduler_->run_in_context([&]() {
        td::actor::send_closure_later(self_, &WorkerRunningRequest::send_payload_part, body_part, is_completed);
      });
    }
    void receive_error(td::Status error) override {
      scheduler_->run_in_context(
          [&]() { td::actor::send_closure_later(self_, &WorkerRunningRequest::send_error, std::move(error)); });
    }

   private:
    td::actor::ActorId<WorkerRunningRequest> self_;
    td::actor::Scheduler *scheduler_;
  };
  if (!check_deadline())
    return;
  headers.emplace_back(http::pipeline_request_id_header,
                       PSTRING() << proxy_connection_id_ << ":" << proxy_request_id_.to_hex());
  headers.emplace_back(http::pipeline_timeout_header, std::to_string(timeout_ - run_time()));
  http_request_ = http::run_http_request(http_server_address_, request_type, std::move(url), std::move(headers),
                                         new_payload.as_slice().str(), timeout_ - run_time(),
                                         std::make_unique<Cb>(actor_id(this), scheduler_));
}

bool WorkerRunningRequest::check_deadline() {
  if (completed_)
    return false;
  if (run_time() >= timeout_) {
    send_error(td::Status::Error(ton::ErrorCode::timeout, "worker: timeout"));
    return false;
  }
  return true;
}

void WorkerRunningRequest::process_request_response(td::int32 status_code,
                                                    std::vector<std::pair<std::string, std::string>> headers,
                                                    std::string payload_part, bool payload_is_completed) {
  if (!check_deadline())
    return;
  backend_status_ = status_code;
  if (status_code < 200 || status_code >= 300) {
    backend_headers_ = std::move(headers);
    receive_http_error_payload(std::move(payload_part), payload_is_completed);
    return;
  }
  for (const auto &header : headers) {
    auto name = header.first;
    std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });
    if (name == "content-type") {
      postprocessor_->set_sse(header.second.find("text/event-stream") == 0);
    }
  }
  send_answer(status_code, std::move(headers), std::move(payload_part), payload_is_completed);
}

void WorkerRunningRequest::send_error(td::Status error) {
  if (completed_) {
    return;
  }
  LOG(WARNING) << "worker request " << proxy_request_id_.to_hex() << " failed: " << error;

  if (sent_answer_ && postprocessor_ && !postprocessor_->error_payload().empty()) {
    send_processed_payload(postprocessor_->error_payload(), false);
  }

  auto final_info = create_final_info(false);
  auto ans = cocoon::create_serialize_tl_object<cocoon_api::proxy_queryAnswerErrorEx>(
      proxy_request_id_, error.code(), error.message().str(), 1, std::move(final_info));
  td::actor::send_closure(runner_, &WorkerRunner::send_message_to_connection, proxy_connection_id_, std::move(ans));
  sent_answer_ = true;

  finish_request(false);
}

td::Result<std::string> WorkerRunningRequest::process_payload(td::Slice payload, bool completed) {
  if (backend_status_ < 200 || backend_status_ >= 300)
    return payload.str();
  TRY_RESULT(output, postprocessor_->add_next_answer_slice(payload));
  if (completed) {
    TRY_RESULT(tail, postprocessor_->finalize());
    output += tail;
  }
  return output;
}

void WorkerRunningRequest::receive_http_error_payload(std::string payload, bool completed) {
  // Diagnostics are bounded independently of the future streaming/backpressure
  // work. A malformed/oversized error response is itself an upstream error.
  if (error_body_.size() + payload.size() > (1 << 20)) {
    send_error(td::Status::Error(ton::ErrorCode::notready, "backend HTTP error body exceeds 1 MiB"));
    return;
  }
  error_body_ += payload;
  if (!completed)
    return;
  if (!worker_private_key_.is_zero()) {
    auto value = nlohmann::json::parse(error_body_, nullptr, false);
    if (!value.is_object())
      value = {{"error", {{"message", error_body_}}}};
    encrypt_json(value, worker_private_key_, client_public_key_, false);
    error_body_ = value.dump();
    for (auto &header : backend_headers_) {
      auto name = header.first;
      std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });
      if (name == "content-type")
        header.second = "application/json";
    }
  }
  // Preserve the upstream HTTP status/body, then send the existing TL error
  // terminal. A completed HTTP error body is not a successful inference.
  send_answer(backend_status_, std::move(backend_headers_), std::move(error_body_), false);
  send_error(td::Status::Error(ton::ErrorCode::notready, PSTRING() << "backend HTTP status " << backend_status_));
}

void WorkerRunningRequest::send_answer(td::int32 status_code, std::vector<std::pair<std::string, std::string>> headers,
                                       std::string orig_payload, bool payload_is_completed) {
  if (!check_deadline())
    return;
  LOG(DEBUG) << "worker request " << proxy_request_id_.to_hex() << ": starting sending answer";

  auto result = process_payload(orig_payload, payload_is_completed);
  if (result.is_error()) {
    send_error(result.move_as_error());
    return;
  }
  auto payload_to_send = result.move_as_ok();
  if (!check_deadline())
    return;

  stats()->answer_bytes_sent += (double)payload_to_send.size();
  if (payload_to_send.size() > 0) {
    payload_parts_++;
    payload_bytes_ += payload_to_send.size();
  }

  //http.response http_version:string status_code:int reason:string headers:(vector http.header) payload:bytes = http.Response;
  auto res = cocoon::cocoon_api::make_object<cocoon_api::http_response>(
      "HTTP/1.1", status_code, "", std::vector<ton::tl_object_ptr<cocoon_api::http_header>>{},
      td::BufferSlice(payload_to_send));

  for (auto &h : headers) {
    auto name = h.first;
    std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });
    if (name == "content-length" || name == "transfer-encoding" || name == "connection") {
      continue;
    }
    res->headers_.push_back(cocoon::cocoon_api::make_object<cocoon_api::http_header>(h.first, h.second));
  }

  // Add debug timing headers using Unix timestamps
  res->headers_.push_back(cocoon::cocoon_api::make_object<cocoon_api::http_header>(
      "X-Cocoon-Worker-Start", PSTRING() << td::StringBuilder::FixedDouble(started_at_unix_, 6)));
  res->headers_.push_back(cocoon::cocoon_api::make_object<cocoon_api::http_header>(
      "X-Cocoon-Worker-End", PSTRING() << td::StringBuilder::FixedDouble(td::Clocks::system(), 6)));

  if (payload_is_completed) {
    res->headers_.push_back(
        cocoon::cocoon_api::make_object<cocoon_api::http_header>("Content-Length", PSTRING() << res->payload_.size()));
  } else {
    res->headers_.push_back(cocoon::cocoon_api::make_object<cocoon_api::http_header>("Transfer-Encoding", "chunked"));
  }

  auto serialized_res = cocoon::serialize_tl_object(res, true);
  td::BufferSlice ans;
  auto final_info = payload_is_completed ? create_final_info() : nullptr;
  ans = cocoon::create_serialize_tl_object<cocoon_api::proxy_queryAnswerEx>(
      proxy_request_id_, std::move(serialized_res), payload_is_completed ? 1 : 0, std::move(final_info));
  td::actor::send_closure(runner_, &WorkerRunner::send_message_to_connection, proxy_connection_id_, std::move(ans));
  sent_answer_ = true;

  if (payload_is_completed) {
    finish_request(true);
  }
}

void WorkerRunningRequest::send_payload_part(std::string orig_payload_part, bool payload_is_completed) {
  if (!check_deadline())
    return;
  if (backend_status_ < 200 || backend_status_ >= 300) {
    receive_http_error_payload(std::move(orig_payload_part), payload_is_completed);
    return;
  }
  LOG(DEBUG) << "worker request " << proxy_request_id_.to_hex() << ": sending next payload part";

  CHECK(sent_answer_);
  CHECK(!completed_);

  auto result = process_payload(orig_payload_part, payload_is_completed);
  if (result.is_error()) {
    send_error(result.move_as_error());
    return;
  }
  auto payload_to_send = result.move_as_ok();

  if (!check_deadline())
    return;
  send_processed_payload(std::move(payload_to_send), payload_is_completed);
}

void WorkerRunningRequest::send_processed_payload(std::string payload_to_send, bool payload_is_completed) {
  if (!payload_to_send.size() && !payload_is_completed) {
    return;
  }

  payload_parts_++;
  payload_bytes_ += payload_to_send.size();
  stats()->answer_bytes_sent += (double)payload_to_send.size();

  td::BufferSlice ans;
  auto final_info = payload_is_completed ? create_final_info() : nullptr;
  ans = cocoon::create_serialize_tl_object<cocoon_api::proxy_queryAnswerPartEx>(
      proxy_request_id_, td::BufferSlice(payload_to_send), payload_is_completed ? 1 : 0, std::move(final_info));
  td::actor::send_closure(runner_, &WorkerRunner::send_message_to_connection, proxy_connection_id_, std::move(ans));

  if (payload_is_completed) {
    finish_request(true);
  }
}

void WorkerRunningRequest::finish_request(bool is_success) {
  if (completed_) {
    return;
  }
  if (postprocessor_) {
    auto tokens_used =
        is_success ? postprocessor_->usage() : ton::create_tl_object<cocoon_api::tokensUsed>(0, 0, 0, 0, 0);
    LOG(INFO) << "worker request " << proxy_request_id_.to_hex()
              << ": completed: success=" << (is_success ? "YES" : "NO") << " time=" << run_time()
              << " payload_parts=" << payload_parts_ << " payload_bytes=" << payload_bytes_
              << " tokens_used=" << tokens_used->prompt_tokens_used_ << "+" << tokens_used->cached_tokens_used_ << "+"
              << tokens_used->completion_tokens_used_ << "+" << tokens_used->reasoning_tokens_used_ << "="
              << tokens_used->total_tokens_used_;
    stats_->total_adjusted_tokens_used += (double)tokens_used->total_tokens_used_;
    stats_->prompt_adjusted_tokens_used += (double)tokens_used->prompt_tokens_used_;
    stats_->cached_adjusted_tokens_used += (double)tokens_used->cached_tokens_used_;
    stats_->completion_adjusted_tokens_used += (double)tokens_used->completion_tokens_used_;
    stats_->reasoning_adjusted_tokens_used += (double)tokens_used->reasoning_tokens_used_;
  }
  completed_ = true;
  http_request_.cancel();
  http_request_ = {};
  if (is_success) {
    stats()->requests_success++;
  } else {
    stats()->requests_failed++;
  }

  stats()->total_requests_time += run_time();

  td::actor::send_closure(runner_, &WorkerRunner::finish_request, proxy_connection_id_, proxy_request_id_,
                          actor_id(this));

  stop();
}

std::string WorkerRunningRequest::generate_worker_debug_inner() {
  nlohmann::json v;
  v["type"] = "worker_stats";
  v["start_time"] = started_at_unix_;
  v["answer_receive_start_at"] = received_answer_at_unix_;
  v["answer_receive_end_at"] = td::Clocks::system();
  return v.dump();
}

ton::tl_object_ptr<cocoon_api::proxy_queryFinalInfo> WorkerRunningRequest::create_final_info(bool success) {
  ton::tl_object_ptr<cocoon_api::tokensUsed> tokens_used =
      success && postprocessor_ ? postprocessor_->usage()
                                : ton::create_tl_object<cocoon_api::tokensUsed>(0, 0, 0, 0, 0);
  return ton::create_tl_object<cocoon_api::proxy_queryFinalInfo>(
      (enable_debug_ ? 1 : 0) | (proto_version_ >= 2 ? 2 : 0), std::move(tokens_used), generate_worker_debug(),
      started_at_unix_, td::Clocks::system());
}

}  // namespace cocoon
