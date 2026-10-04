#pragma once

#include "pipeline/Profile.h"
#include <boost/asio.hpp>
#include <functional>
#include <map>
#include <memory>

namespace cocoon::pipeline {
struct GateState {
  bool ready = false;
  std::string epoch;
  std::string socket;
};

// Runs exclusively on Agent's io_context/actor. No separate polling service can
// retain readiness after the owner closes the epoch.
class Gate : public std::enable_shared_from_this<Gate> {
 public:
  Gate(boost::asio::io_context &io, const Config &config, std::function<GateState()> state);
  void start();
  void invalidate();
  void tick();
  void close();
  Json status() const;

 private:
  class Session;
  friend class Session;
  void accept();
  bool admit(const std::string &request_id) const;
  boost::asio::io_context &io_;
  Config config_;
  std::function<GateState()> state_;
  boost::asio::ip::tcp::acceptor acceptor_;
  std::map<uint64_t, std::shared_ptr<Session>> sessions_;
  uint64_t next_id_ = 0, accepted_ = 0, completed_ = 0, failed_ = 0;
  bool closed_ = false;
};
}  // namespace cocoon::pipeline
