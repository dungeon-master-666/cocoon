#pragma once

#include "WorkerProxyInfo.h"
#include "WorkerStats.h"
#include "WorkerProxyConnection.h"
#include "WorkerRunningRequest.hpp"

#include "auto/tl/cocoon_api.h"
#include "common/bitstring.h"
#include "runners/BaseRunner.hpp"

#include "td/actor/actor.h"
#include "td/utils/misc.h"
#include "td/utils/port/IPAddress.h"
#include "ton/http/http-client.h"
#include <memory>
#include <map>

namespace cocoon {

class WorkerRunner : public BaseRunner {
 public:
  WorkerRunner(std::string engine_config_filename, td::actor::Scheduler *scheduler)
      : BaseRunner(RunnerRole::Worker, std::move(engine_config_filename), scheduler) {
  }

  /* CONST PARAMS */
  static constexpr td::int64 min_worker_payout_sum() {
    return to_nano(10);
  }
  static constexpr td::int64 min_worker_payout_sum_on_close() {
    return to_nano(0.01);
  }
  static constexpr td::int64 min_worker_payout_sum_on_idle() {
    return to_nano(0.2);
  }
  static constexpr td::int32 min_proto_version() {
    return 3;
  }
  static constexpr td::int32 max_proto_version() {
    return 4;
  }

  /* SIMPLE GETTERS */
  const auto &owner_address() const {
    return owner_address_;
  }
  const auto &model_name() const {
    return model_name_;
  }
  const auto &model_base_name() const {
    return model_base_name_;
  }
  auto coefficient() const {
    return coefficient_;
  }
  auto http_is_ready() const {
    return http_is_ready_;
  }
  bool is_disabled() const {
    return is_force_disabled_ || !uplink_ok_;
  }
  auto max_active_requests() const {
    return max_active_requests_;
  }
  const auto &machine_description_json() const {
    return machine_description_json_;
  }

  /* SIMPLE SETTERS */
  void set_owner_address(block::StdAddress owner_address) {
    owner_address_ = owner_address;
  }
  void set_http_ready(bool value) {
    http_is_ready_ = value;
  }
  void set_model_name(std::string value) {
    model_name_ = std::move(value);
    auto p = model_name_.find('@');
    if (p == std::string::npos) {
      model_base_name_ = model_name_;
    } else {
      model_base_name_ = model_name_.substr(0, p);
    }
  }
  void set_max_active_requests(td::int32 value) {
    max_active_requests_ = value;
  }

  /* PAYOUT */
  void proxy_request_payout(WorkerProxyInfo &proxy);

  /* PROXY REQUEST */
  void receive_request(WorkerProxyInfo &proxy, TcpClient::ConnectionId connection_id,
                       cocoon_api::proxy_runQueryEx &req);
  void finish_request(TcpClient::ConnectionId connection_id, const td::Bits256 &proxy_request_id,
                      td::actor::ActorId<WorkerRunningRequest> request);
  void cancel_requests(TcpClient::ConnectionId connection_id);

  /* ALLOCATORS */
  std::unique_ptr<ProxyOutboundConnection> allocate_proxy_outbound_connection(TcpClient::ConnectionId connection_id,
                                                                              TcpClient::TargetId target_id,
                                                                              const RemoteAppType &remote_app_type,
                                                                              const td::Bits256 &remote_app_hash,
                                                                              const td::Bits256 &verified_by) override {
    return std::make_unique<WorkerProxyConnection>(this, remote_app_type, remote_app_hash, verified_by, connection_id,
                                                   target_id);
  }
  std::unique_ptr<ProxyTarget> allocate_proxy_target(TcpClient::TargetId target_id,
                                                     const td::IPAddress &addr) override {
    return std::make_unique<ProxyTarget>(this, addr, target_id);
  }

  /* INITIALIZATION */
  void load_config(td::Promise<td::Unit> promise) override;
  td::Status create_http_client(td::Slice forward_requests_to);
  void custom_initialize(td::Promise<td::Unit> promise) override;
  void wait_server_alive(td::Promise<td::Unit> promise);

  /* CRON */
  void alarm() override;

  /* INBOUND MESSAGE HANDLERS */
  void receive_message(TcpClient::ConnectionId connection_id, td::BufferSlice query) override;
  void receive_query(TcpClient::ConnectionId connection_id, td::BufferSlice query,
                     td::Promise<td::BufferSlice> promise) override;

  /* PROXY DB */
  std::shared_ptr<WorkerProxyInfo> get_proxy_info(const std::string &proxy_sc_address_str) const {
    auto it = proxies_.find(proxy_sc_address_str);
    if (it != proxies_.end()) {
      return it->second;
    } else {
      return nullptr;
    }
  }
  td::Result<std::shared_ptr<WorkerProxyInfo>> register_proxy(
      TcpClient::ConnectionId connection_id, td::Bits256 proxy_public_key, block::StdAddress proxy_owner_address,
      block::StdAddress proxy_sc_address, block::StdAddress worker_sc_address,
      ton::tl_object_ptr<cocoon_api::proxy_SignedPayment> payment);
  void update_proxy_payment_status(const std::string &proxy_sc_address_str, td::BufferSlice info);

  /* CONTROL */
  void set_force_disabled(bool value);
  void send_state_update_to_proxies();
  void set_coefficient(td::int32 value);
  td::Status change_model(std::string new_model);

  /* UPLINK */
  void set_uplink_is_ok(bool value);

  /* HTTP FORWARDING */
  void send_http_request(
      std::unique_ptr<ton::http::HttpRequest> request, std::shared_ptr<ton::http::HttpPayload> payload,
      td::Timestamp timeout,
      td::Promise<std::pair<std::unique_ptr<ton::http::HttpResponse>, std::shared_ptr<ton::http::HttpPayload>>>
          promise) {
    LOG(INFO) << "sending HTTP request to " << forward_requests_to_;
    td::actor::send_closure(http_client_, &ton::http::HttpClient::send_request, std::move(request), std::move(payload),
                            timeout, std::move(promise));
  }

  /* HTTP HANDLING */
  std::string http_generate_main();
  std::string http_generate_json_stats();
  std::string http_payout(std::string proxy_sc_address);
  std::string http_worker_set_force_disabled(bool value) {
    set_force_disabled(value);
    return wrap_short_answer_to_http("state updated");
  }
  std::string http_worker_change_coefficient();
  std::string http_worker_change_coefficient(td::CSlice str) {
    if (str == "") {
      return http_worker_change_coefficient();
    }
    char *pEnd = NULL;
    double d = strtod(str.c_str(), &pEnd);
    if (*pEnd) {
      return wrap_short_answer_to_http(PSTRING() << "failed to parse '" << str << "' as double");
    }

    auto v = static_cast<td::int32>(d * 1000);
    set_coefficient(v);
    return wrap_short_answer_to_http(PSTRING() << "coefficient set to " << (v * 0.001));
  }
  std::string wrap_short_answer_to_http(std::string text) {
    td::StringBuilder sb;
    sb << "<!DOCTYPE html>\n";
    sb << "<html><body>\n";
    sb << text << "<br/>\n";
    sb << "<a href=\"/stats\">return to stats</a>\n";
    sb << "</html></body>\n";
    return sb.as_cslice().str();
  };

 private:
  block::StdAddress owner_address_;
  std::string model_name_;
  std::string model_base_name_;

  td::int32 coefficient_;
  using RequestKey = std::pair<TcpClient::ConnectionId, td::Bits256>;
  std::map<RequestKey, td::actor::ActorOwn<WorkerRunningRequest>> active_requests_;
  td::int32 max_active_requests_{200};

  std::unique_ptr<td::Ed25519::PrivateKey> wallet_private_key_;
  td::Bits256 wallet_public_key_;

  std::map<std::string, std::shared_ptr<WorkerProxyInfo>> proxies_;
  td::Timestamp next_check_all_clients_at_;
  td::Timestamp next_payment_compare_at_;

  td::actor::ActorOwn<ton::http::HttpClient> http_client_;
  td::IPAddress forward_requests_to_;

  bool http_is_ready_{false};
  bool is_force_disabled_{false};
  bool uplink_ok_{false};
  td::uint32 params_version_{0};
  std::shared_ptr<WorkerStats> stats_ = std::make_shared<WorkerStats>();

  td::Bits256 local_image_hash_unverified_;

  std::string change_model_script_;
  std::string machine_description_json_;
};

}  // namespace cocoon
