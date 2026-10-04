#pragma once

#include "runners/BaseRunner.hpp"

namespace cocoon {

class WorkerRunner;

class WorkerProxyConnection : public ProxyOutboundConnection {
 public:
  WorkerProxyConnection(BaseRunner *runner, const RemoteAppType &remote_app_type, const td::Bits256 &remote_app_hash,
                        const td::Bits256 &verified_by, TcpClient::ConnectionId connection_id,
                        TcpClient::TargetId target_id)
      : ProxyOutboundConnection(runner, remote_app_type, remote_app_hash, verified_by, connection_id, target_id) {
  }
  void send_handshake() override;
  void pre_close() override;
  void received_handshake_answer(td::BufferSlice answer);
  void received_compare_answer(td::BufferSlice answer);
  void received_extended_compare_answer(td::BufferSlice answer);
  void send_handshake_complete();
  void received_handshake_complete_answer(td::BufferSlice answer);

  WorkerRunner *runner();

  const auto &proxy_sc_address_str() const {
    return proxy_sc_address_str_;
  }

  auto proto_version() const {
    return proto_version_;
  }

 private:
  std::string proxy_sc_address_str_;
  td::int32 proto_version_;
};

}  // namespace cocoon
