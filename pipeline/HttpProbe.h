#pragma once

#include "pipeline/Backend.h"
#include <boost/asio.hpp>
#include <boost/asio/local/stream_protocol.hpp>
#include <boost/beast.hpp>
#include <memory>

namespace cocoon::pipeline {
class HttpProbe : public std::enable_shared_from_this<HttpProbe> {
 public:
  static std::shared_ptr<HttpProbe> start(boost::asio::io_context &io, const std::string &socket,
                                          const ProbeRequest &request, int timeout_ms);
  bool done() const {
    return done_;
  }
  bool ok() const {
    return error_.empty() && status_ == 200;
  }
  const std::string &body() const {
    return body_;
  }
  const std::string &error() const {
    return error_;
  }
  void cancel();

 private:
  explicit HttpProbe(boost::asio::io_context &io);
  void finish(const std::string &error);
  boost::asio::local::stream_protocol::socket socket_;
  boost::asio::steady_timer timer_;
  boost::beast::flat_buffer buffer_;
  boost::beast::http::request<boost::beast::http::string_body> request_;
  boost::beast::http::response_parser<boost::beast::http::string_body> parser_;
  bool done_ = false;
  unsigned status_ = 0;
  std::string body_;
  std::string error_;
};
}  // namespace cocoon::pipeline
