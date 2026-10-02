#include "ClientRunningRequest.h"
#include "ClientRunner.hpp"
#include "auto/tl/cocoon_api.h"
#include "checksum.h"
#include "common/bitstring.h"
#include "nlohmann/detail/conversions/from_json.hpp"
#include "nlohmann/detail/conversions/to_json.hpp"
#include "td/actor/actor.h"
#include "td/utils/StringBuilder.h"
#include "td/utils/Time.h"
#include "td/utils/buffer.h"
#include "runners/helpers/HttpSender.hpp"
#include "runners/helpers/ValidateRequest.h"

#include "auto/tl/cocoon_api_json.h"
#include "cocoon-tl-utils/cocoon-tl-utils.hpp"
#include "td/utils/misc.h"
#include "td/utils/port/Clocks.h"
#include "tl/TlObject.h"
#include <utility>

#include <nlohmann/json.hpp>
#include <nlohmann/json_fwd.hpp>

namespace cocoon {

void ClientRunningRequest::start_up() {
  LOG(INFO) << "client request " << request_id_.to_hex() << ": starting";

  std::string content_type;
  for (auto &h : in_request_headers_) {
    auto name_copy = h.first;
    std::transform(name_copy.begin(), name_copy.end(), name_copy.begin(),
                   [](unsigned char c) { return std::tolower(c); });
    if (name_copy == "content-type") {
      content_type = h.second;
    }
  }

  stats()->requests_received++;
  stats()->request_bytes_received += (double)in_payload_.size();
  std::string model = "test1";
  td::int32 max_coefficient = 4000;
  td::int64 max_tokens = 10000;
  //max_tokens = 0;
  //max_coefficient = 0;
  double timeout = 120.0;

  td::Bits256 encrypted_with = td::Bits256::zero();

  auto S = [&]() -> td::Status {
    TRY_RESULT(new_payload,
               validate_client_request(in_request_path_, content_type, in_payload_, &model, &max_tokens,
                                       &max_coefficient, &timeout, &enable_debug_, &ext_request_id_, &encrypted_with));
    in_payload_ = std::move(new_payload);
    return td::Status::OK();
  }();

  if (S.is_error()) {
    return return_error(S.move_as_error_prefix("failed to parse request: "), nullptr);
  }

  encrypted_with_ = encrypted_with;

  auto req = cocoon::cocoon_api::make_object<cocoon_api::http_request>(
      "POST", in_request_path_, "HTTP/1.0", std::vector<ton::tl_object_ptr<cocoon_api::http_header>>(),
      td::BufferSlice(in_payload_));
  for (auto &h : in_request_headers_) {
    req->headers_.push_back(cocoon::cocoon_api::make_object<cocoon_api::http_header>(h.first, h.second));
  }
  if (req->payload_.size()) {
    req->headers_.push_back(
        cocoon::cocoon_api::make_object<cocoon_api::http_header>("Content-Length", PSTRING() << req->payload_.size()));
  }

  auto request_data = cocoon::serialize_tl_object(req, true);

  td::BufferSlice request_data_wrapped;
  if (proto_version_ == 0) {
    request_data_wrapped = cocoon::create_serialize_tl_object<cocoon_api::client_runQuery>(
        model, std::move(request_data), max_coefficient, (int)max_tokens, timeout * 0.95, request_id_,
        min_config_version_);
  } else {
    request_data_wrapped = cocoon::create_serialize_tl_object<cocoon_api::client_runQueryEx>(
        model, std::move(request_data), max_coefficient, (int)max_tokens, timeout * 0.95, request_id_,
        min_config_version_, 1 | (proto_version_ >= 3 ? 2 : 0), enable_debug_, encrypted_with_);
  }

  td::actor::send_closure(client_runner_, &ClientRunner::send_message_to_connection, proxy_connection_id_,
                          std::move(request_data_wrapped));

  in_request_headers_.clear();
  in_payload_.clear();
}

void ClientRunningRequest::process_answer_ex_impl(cocoon_api::client_queryAnswerEx &ans) {
  if (answer_sent_) {
    LOG(ERROR) << "client request " << request_id_.to_hex() << ": received duplicate answer";
    return;
  }

  LOG(DEBUG) << "client request " << request_id_.to_hex() << ": received answer";

  auto R = fetch_tl_object<cocoon_api::http_response>(std::move(ans.answer_), true);
  if (R.is_error()) {
    LOG(ERROR) << "client request " << request_id_.to_hex() << ": received malformed answer: " << R.move_as_error();
    return;
  }
  auto response = R.move_as_ok();

  received_answer_at_unix_ = td::Clocks::system();

  stats()->answer_bytes_sent += (double)response->payload_.size();

  std::vector<std::pair<std::string, std::string>> headers;
  for (auto &x : response->headers_) {
    headers.emplace_back(x->name_, x->value_);
    auto name = x->name_;
    std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });
    if (name == "content-type") {
      sse_response_ = x->value_.find("text/event-stream") == 0;
    }
  }
  headers.emplace_back("X-Cocoon-Client-Start", PSTRING() << td::StringBuilder::FixedDouble(started_at_unix_, 6));
  headers.emplace_back("X-Cocoon-Client-End", PSTRING() << td::StringBuilder::FixedDouble(td::Clocks::system(), 6));

  callback_->receive_answer(response->status_code_, "application/json", std::move(headers));
  answer_sent_ = true;

  bool is_completed = ans.flags_ & 1;

  if (response->payload_.size() > 0) {
    payload_parts_++;
    payload_bytes_ += response->payload_.size();

    add_payload_part(std::move(response->payload_), is_completed, ans.final_info_);
  }

  if (is_completed) {
    finish_request(true, std::move(ans.final_info_));
  }
}

void ClientRunningRequest::process_answer_ex_impl(cocoon_api::client_queryAnswerErrorEx &ans) {
  LOG(DEBUG) << "client request " << request_id_.to_hex() << ": received error";

  if (!answer_sent_) {
    return_error(td::Status::Error(ans.error_code_, ans.error_), std::move(ans.final_info_));
  } else {
    finish_request(false, std::move(ans.final_info_));
  }
}

void ClientRunningRequest::process_answer_ex_impl(cocoon_api::client_queryAnswerPartEx &ans) {
  if (!answer_sent_) {
    LOG(ERROR) << "client request " << request_id_.to_hex() << ": received payload part before answer";
    return;
  }

  LOG(DEBUG) << "client request " << request_id_.to_hex() << ": received payload part";
  payload_parts_++;
  payload_bytes_ += ans.answer_.size();
  stats()->answer_bytes_sent += (double)ans.answer_.size();

  bool is_completed = ans.flags_ & 1;
  add_payload_part(std::move(ans.answer_), is_completed, ans.final_info_);

  if (is_completed) {
    finish_request(true, std::move(ans.final_info_));
  }
}

void ClientRunningRequest::return_error_str(td::int32 ton_error_code, std::string error,
                                            ton::tl_object_ptr<cocoon_api::client_queryFinalInfo> final_info) {
  LOG(WARNING) << "client request " << request_id_.to_hex() << ": sending error: " << error;
  CHECK(!answer_sent_);

  td::int32 error_code;
  std::string error_string;
  switch (ton_error_code) {
    case ton::ErrorCode::timeout:
      error_code = ton::http::HttpStatusCode::status_gateway_timeout;
      error_string = "Gateway Timeout";
      break;
    case ton::ErrorCode::notready:
      error_code = ton::http::HttpStatusCode::status_bad_gateway;
      error_string = "Bad Gateway";
      break;
    case ton::ErrorCode::protoviolation:
      error_code = ton::http::HttpStatusCode::status_bad_request;
      error_string = "Bad Request";
      break;
    default:
      error_code = ton::http::HttpStatusCode::status_internal_server_error;
      error_string = "Internal Server Error";
      break;
  }

  td::StringBuilder sb;
  nlohmann::json v = nlohmann::json::object();
  v["error_code"] = ton_error_code;
  v["error"] = error;
  if (final_info && final_info->proxy_debug_.size() > 0) {
    v["proxy"] = nlohmann::json::parse(final_info->proxy_debug_);
  }
  if (final_info && final_info->worker_debug_.size() > 0) {
    v["worker"] = nlohmann::json::parse(final_info->worker_debug_);
  }
  if (enable_debug_) {
    v["client"] = generate_client_debug_inner();
  }
  sb << v.dump();

  auto data = sb.as_cslice();

  callback_->receive_answer(error_code, "application/json", {}, data.str(), false);
  answer_sent_ = true;

  finish_request(false, std::move(final_info));
}

void ClientRunningRequest::finish_request(bool is_success,
                                          ton::tl_object_ptr<cocoon_api::client_queryFinalInfo> final_info) {
  if (payload_completed_) {
    LOG(ERROR) << "client request " << request_id_.to_hex() << ": duplicate last payload part";
    return;
  }
  CHECK(answer_sent_);
  LOG(INFO) << "client request " << request_id_.to_hex() << ": completed: success=" << (is_success ? "YES" : "NO")
            << " time=" << run_time() << " payload_parts=" << payload_parts_ << " payload_bytes=" << payload_bytes_;
  if (is_success) {
    stats()->requests_success++;
  } else {
    stats()->requests_failed++;
  }
  stats()->total_requests_time += run_time();
  if (final_info) {
    stats()->total_worker_requests_time += (final_info->worker_end_time_ - final_info->worker_start_time_);
    stats()->total_proxy_requests_time += (final_info->proxy_end_time_ - final_info->proxy_start_time_);
  } else {
    stats()->total_worker_requests_time += run_time();
    stats()->total_proxy_requests_time += run_time();
  }

  callback_->receive_payload_part("", true);
  payload_completed_ = true;

  td::actor::send_closure(client_runner_, &ClientRunner::finish_request, request_id_, proxy_);

  stop();
}

const std::shared_ptr<ClientStats> ClientRunningRequest::stats() const {
  return client_runner_.get_actor_unsafe().stats();
}

nlohmann::json ClientRunningRequest::generate_client_debug_inner() {
  nlohmann::json v;
  v["type"] = "client_stats";
  v["start_time"] = started_at_unix_;
  v["answer_receive_start_at"] = received_answer_at_unix_;
  v["answer_receive_end_at"] = td::Clocks::system();
  return v;
}

void ClientRunningRequest::add_last_payload_part_with_debug(
    td::BufferSlice part, const ton::tl_object_ptr<cocoon_api::client_queryFinalInfo> &info) {
  nlohmann::json v = nlohmann::json::object();
  if (info && info->proxy_debug_.size() > 0) {
    v["proxy"] = nlohmann::json::parse(info->proxy_debug_);
  }
  if (info && info->worker_debug_.size() > 0) {
    v["worker"] = nlohmann::json::parse(info->worker_debug_);
  }
  v["client"] = generate_client_debug_inner();

  if (enable_debug_ && !ext_request_id_.is_zero()) {
    td::actor::send_closure(client_runner_, &ClientRunner::add_request_debug_info, ext_request_id_, v.dump());
  }

  if (sse_response_) {
    callback_->receive_payload_part(part.as_slice().str(), false);
    // SSE comments preserve stream framing, including after the [DONE] event.
    callback_->receive_payload_part(": cocoon-debug " + v.dump() + "\n\n", false);
    return;
  }

  if (part.size() == 0) {
    nlohmann::json w;
    w["debug"] = v;
    callback_->receive_payload_part(w.dump() + "\n", false);
    return;
  }

  auto S = part.as_slice();

  static const auto is_ws = [&](char c) { return c == ' ' || c == '\n' || c == '\r' || c == '\t'; };

  auto SS = S.copy();
  while (SS.size() > 0 && is_ws(SS.back())) {
    SS.remove_suffix(1);
  }

  td::StringBuilder sb;
  std::stringstream ss(S.str());
  bool written = false;
  while (true) {
    try {
      size_t old_pos = ss.tellg();
      nlohmann::json w;
      ss >> w;
      size_t pos = ss.tellg();
      if (pos == old_pos) {
        break;
      }
      if (pos != SS.size()) {
        continue;
      }

      w["debug"] = v;
      callback_->receive_payload_part(S.truncate(old_pos).str() + "\n", false);
      callback_->receive_payload_part(w.dump() + "\n", false);
      written = true;
      break;
    } catch (...) {
      break;
    }
  }
  if (!written) {
    callback_->receive_payload_part(S.str(), false);
    nlohmann::json w;
    w["debug"] = v;
    callback_->receive_payload_part(w.dump() + "\n", false);
    return;
  }
}

}  // namespace cocoon
