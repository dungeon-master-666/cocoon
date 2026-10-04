#pragma once
#include "pipeline/Security.h"
#include <boost/asio.hpp>
#include <deque>

namespace cocoon::pipeline {
class Channel : public std::enable_shared_from_this<Channel> {
 public:
  using Tcp = boost::asio::ip::tcp;
  Channel(boost::asio::io_context &io, std::shared_ptr<boost::asio::ssl::context> context, SecurityMode policy);
  Tcp::socket &socket() {
    return stream_.next_layer();
  }
  void connect(const Tcp::endpoint &endpoint);
  void handshake(bool server);
  void send(const Json &frame);
  void close();
  const PeerIdentity &peer() const {
    return peer_->value();
  }
  bool closed() const {
    return closed_;
  }
  std::function<void()> on_ready;
  std::function<void(const Json &)> on_frame;
  std::function<void(const std::string &)> on_error;

 private:
  void fail(const std::string &reason);
  void read();
  void write();
  void deadline();
  bool arm_expiry();
  std::shared_ptr<boost::asio::ssl::context> context_;
  boost::asio::ssl::stream<Tcp::socket> stream_;
  boost::asio::steady_timer timer_;
  boost::asio::steady_timer expiry_timer_;
  std::shared_ptr<std::optional<PeerIdentity>> peer_;
  std::array<unsigned char, 4> header_{};
  std::string body_;
  std::deque<std::string> writes_;
  size_t queued_bytes_ = 0;
  uint64_t deadline_generation_ = 0;
  bool closed_ = false;
};
}  // namespace cocoon::pipeline
