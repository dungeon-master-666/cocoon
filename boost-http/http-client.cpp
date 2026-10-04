#include "td/utils/SharedSlice.h"
#include "td/utils/buffer.h"
#include "td/utils/port/IPAddress.h"
#include <boost/asio.hpp>
#include <boost/beast.hpp>
#include <boost/beast/http.hpp>
#include <boost/beast/http/string_body.hpp>
#include <iostream>
#include <memory>
#include <string>

#include "http.h"
#include "errorcode.h"

namespace cocoon {

namespace http {

namespace asio = boost::asio;
namespace beast = boost::beast;
namespace http = beast::http;
using tcp = asio::ip::tcp;

extern boost::asio::io_context &io_context();

class HttpClientSession : public std::enable_shared_from_this<HttpClientSession> {
 public:
  HttpClientSession(asio::io_context &io, const td::IPAddress &addr, HttpCallback::RequestType request_type,
                    std::string url, std::vector<std::pair<std::string, std::string>> headers, std::string payload,
                    double timeout, std::unique_ptr<HttpRequestCallback> callback)
      : resolver_(io)
      , stream_(io)
      , addr_(addr)
      , request_type_(request_type)
      , url_(std::move(url))
      , headers_(std::move(headers))
      , payload_(std::move(payload))
      , timeout_(timeout)
      , callback_(std::move(callback)) {
  }

  void run() {
    self_ = shared_from_this();
    req_.version(11);  // http 1.1
    switch (request_type_) {
      case HttpCallback::RequestType::Get:
        req_.method(http::verb::get);
        req_.body().clear();
        payload_.clear();
        break;
      case HttpCallback::RequestType::Post:
        req_.method(http::verb::post);
        req_.body() = std::move(payload_);
        req_.prepare_payload();
        break;
    }
    req_.target(url_);
    req_.set(http::field::host, PSTRING() << addr_.get_ip_str());
    req_.set(http::field::user_agent, "cocoon-worker");
    for (auto &h : headers_) {
      req_.set(h.first, h.second);
    }

    parser_.body_limit((std::numeric_limits<std::uint64_t>::max)());
    parser_.eager(true);

    resolver_.async_resolve(addr_.get_ip_str().str(), PSTRING() << addr_.get_port(),
                            beast::bind_front_handler(&HttpClientSession::on_resolve, shared_from_this()));
  }

 private:
  static bool allow_header(std::string name) {
    std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });

    return !(name == "host" || name == "connection" || name == "transfer-encoding" || name == "content-length");
  }

  void on_resolve(beast::error_code error, tcp::resolver::results_type results) {
    if (error) {
      return fail("resolve", std::move(error));
    }

    stream_.expires_after(std::chrono::seconds((int)timeout_));
    stream_.async_connect(results, beast::bind_front_handler(&HttpClientSession::on_connect, shared_from_this()));
  }

  void on_connect(beast::error_code error, tcp::resolver::results_type::endpoint_type) {
    if (error) {
      return fail("connect", std::move(error));
    }

    http::async_write(stream_, req_, beast::bind_front_handler(&HttpClientSession::on_write, shared_from_this()));
  }

  void on_write(beast::error_code error, std::size_t) {
    if (error) {
      return fail("write", std::move(error));
    }

    http::async_read_header(stream_, buffer_, parser_,
                            beast::bind_front_handler(&HttpClientSession::on_read_header, shared_from_this()));
  }

  void on_read_header(beast::error_code error, std::size_t) {
    if (error) {
      return fail("read_headers", std::move(error));
    }

    const auto &res = parser_.get();

    auto status_code = res.result_int();
    std::string content_type;

    auto it = res.find(http::field::content_type);
    if (it != res.end()) {
      content_type = it->value();
    }

    std::vector<std::pair<std::string, std::string>> headers;
    for (auto &h : res) {
      if (!allow_header(h.name_string())) {
        continue;
      }
      headers.emplace_back(h.name_string(), h.value());
    }

    payload_completed_ = parser_.is_done();
    callback_->receive_answer(status_code, std::move(content_type), std::move(headers), "", payload_completed_);
    if (payload_completed_) {
      do_close();
    } else {
      read_payload();
    }
  }

  void read_payload() {
    auto &b = parser_.get().body();
    b.data = body_buf_;
    b.size = sizeof(body_buf_);

    http::async_read_some(stream_, buffer_, parser_,
                          beast::bind_front_handler(&HttpClientSession::on_read_payload, shared_from_this()));
  }

  void on_read_payload(beast::error_code error, std::size_t) {
    auto &b = parser_.get().body();

    std::size_t bytes = sizeof(body_buf_) - b.size;

    if (bytes > 0) {
      callback_->receive_payload_part(std::string(body_buf_, bytes), false);
    }

    if (error == http::error::need_buffer)
      error = {};

    if (error) {
      return fail("read_payload", error);
    }

    if (parser_.is_done()) {
      payload_completed_ = true;
      callback_->receive_payload_part("", true);
      return do_close();
    }

    read_payload();
  }

  void do_close() {
    beast::error_code error;
    stream_.socket().shutdown(tcp::socket::shutdown_both, error);
    stream_.socket().close(error);
    self_ = nullptr;
  }

  void fail(const char *what, beast::error_code ec) {
    LOG(ERROR) << "failed http client: " << what << ": " << ec.message();
    if (!payload_completed_) {
      payload_completed_ = true;
      callback_->receive_error(td::Status::Error(ec == beast::error::timeout || ec == asio::error::timed_out
                                                     ? ton::ErrorCode::timeout
                                                     : ton::ErrorCode::notready,
                                                 PSTRING() << "backend HTTP " << what << ": " << ec.message()));
    }
    do_close();
  }

 private:
  tcp::resolver resolver_;
  beast::tcp_stream stream_;
  beast::flat_buffer buffer_;

  http::request<http::string_body> req_;
  http::response_parser<http::buffer_body> parser_;

  td::IPAddress addr_;
  HttpCallback::RequestType request_type_;
  std::string url_;
  std::vector<std::pair<std::string, std::string>> headers_;
  std::string payload_;
  double timeout_;
  std::unique_ptr<HttpRequestCallback> callback_;

  char body_buf_[16 << 10];
  std::shared_ptr<HttpClientSession> self_;

  bool payload_completed_{false};
};

void run_http_request(const td::IPAddress &addr, HttpCallback::RequestType request_type, std::string url,
                      std::vector<std::pair<std::string, std::string>> headers, std::string payload, double timeout,
                      std::unique_ptr<HttpRequestCallback> callback) {
  auto req = std::make_shared<HttpClientSession>(io_context(), addr, request_type, std::move(url), std::move(headers),
                                                 std::move(payload), timeout, std::move(callback));
  req->run();
}

}  // namespace http

}  // namespace cocoon
