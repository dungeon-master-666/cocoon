#pragma once
#include "pipeline/Channel.h"
#include "pipeline/Process.h"
#include "pipeline/Protocol.h"

namespace cocoon::pipeline {
class Group : public std::enable_shared_from_this<Group> {
 public:
  Group(Config config, boost::asio::io_context &io, const Identity &identity, const std::string &boot);
  void start();
  void tick(bool local_ready);
  void stop(const std::string &reason);
  void close();
  bool take_start();
  bool stopped() const {
    return stopping_;
  }
  const std::string &failure() const {
    return failure_;
  }
  bool ready() const {
    return ready_ && !stopping_;
  }
  Json status() const;

 private:
  void accept();
  void dial();
  void bind_channel(const std::shared_ptr<Channel> &channel);
  void authenticated(const std::shared_ptr<Channel> &channel);
  void receive(const Json &frame);
  void send(const std::string &op, Json payload);
  void fail(const std::string &reason);
  bool lease_valid();

  Config config_;
  boost::asio::io_context &io_;
  std::shared_ptr<boost::asio::ssl::context> tls_;
  boost::asio::ip::tcp::acceptor acceptor_;
  PeerIdentity identity_;
  NetworkKey network_key_;
  Json self_, roster_, pending_;
  std::string epoch_, group_id_, roster_digest_, challenge_, state_ = "FORMING", failure_, rejection_;
  std::unique_ptr<MemberSession> member_;
  std::shared_ptr<Channel> channel_;
  std::vector<std::shared_ptr<Channel>> candidates_;
  Time formation_deadline_, lease_deadline_, heartbeat_at_, retry_at_;
  uint64_t sequence_ = 0;
  bool leased_ = false, local_ready_ = false, peer_ready_ = false, start_pending_ = false;
  bool committed_ = false, ready_ = false, stopping_ = false, closed_ = false;
};
}  // namespace cocoon::pipeline
