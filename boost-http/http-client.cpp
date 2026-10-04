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
#include <atomic>
#include <cmath>

#include "http-client.h"
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
      : strand_(asio::make_strand(io))
      , stream_(strand_)
      , timer_(strand_)
      , addr_(addr)
      , request_type_(request_type)
      , url_(std::move(url))
      , headers_(std::move(headers))
      , payload_(std::move(payload))
      , timeout_(timeout)
      , callback_(std::move(callback)) {
  }

  void run() {
    asio::post(strand_, [self = shared_from_this()] { self->start(); });
  }

  void cancel() {
    if (cancel_requested_.exchange(true))
      return;
    asio::post(strand_, [self = shared_from_this()] {
      self->fail(td::Status::Error(ton::ErrorCode::cancelled, "backend HTTP request cancelled"));
    });
  }

 private:
  using Clock = std::chrono::steady_clock;

  void start() {
    if (!std::isfinite(timeout_) || timeout_ <= 0 ||
        timeout_ >= std::chrono::duration<double>(Clock::time_point::max() - created_at_).count()) {
      return fail(td::Status::Error(ton::ErrorCode::timeout, "invalid backend HTTP timeout"));
    }
    deadline_ = created_at_ + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(timeout_));
    if (stop_requested())
      return;
    timer_.expires_at(deadline_);
    timer_.async_wait([self = shared_from_this()](beast::error_code error) {
      if (!error)
        self->fail(td::Status::Error(ton::ErrorCode::timeout, "backend HTTP deadline exceeded"));
    });

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

    // The API already takes a resolved numeric IPAddress. Avoid introducing an
    // uncancellable resolver job for a numeric address.
    beast::error_code error;
    auto address = asio::ip::make_address(addr_.get_ip_str().str(), error);
    if (error)
      return fail("address", error);
    stream_.async_connect(tcp::endpoint(address, addr_.get_port()),
                          beast::bind_front_handler(&HttpClientSession::on_connect, shared_from_this()));
  }

 private:
  static bool allow_header(std::string name) {
    std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });

    return !(name == "host" || name == "connection" || name == "transfer-encoding" || name == "content-length");
  }

  void on_connect(beast::error_code error) {
    if (stop_requested())
      return;
    if (error) {
      return fail("connect", std::move(error));
    }

    http::async_write(stream_, req_, beast::bind_front_handler(&HttpClientSession::on_write, shared_from_this()));
  }

  void on_write(beast::error_code error, std::size_t) {
    if (stop_requested())
      return;
    if (error) {
      return fail("write", std::move(error));
    }

    http::async_read_header(stream_, buffer_, parser_,
                            beast::bind_front_handler(&HttpClientSession::on_read_header, shared_from_this()));
  }

  void on_read_header(beast::error_code error, std::size_t) {
    if (stop_requested())
      return;
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

    if (parser_.is_done()) {
      do_close();
      auto callback = std::move(callback_);
      callback->receive_answer(status_code, std::move(content_type), std::move(headers), "", true);
    } else {
      callback_->receive_answer(status_code, std::move(content_type), std::move(headers), "", false);
      read_payload();
    }
  }

  void read_payload() {
    if (stop_requested())
      return;
    auto &b = parser_.get().body();
    b.data = body_buf_;
    b.size = sizeof(body_buf_);

    http::async_read_some(stream_, buffer_, parser_,
                          beast::bind_front_handler(&HttpClientSession::on_read_payload, shared_from_this()));
  }

  void on_read_payload(beast::error_code error, std::size_t) {
    if (stop_requested())
      return;
    auto &b = parser_.get().body();

    std::size_t bytes = sizeof(body_buf_) - b.size;

    if (bytes > 0) {
      callback_->receive_payload_part(std::string(body_buf_, bytes), false);
    }
    if (stop_requested())
      return;

    if (error == http::error::need_buffer)
      error = {};

    if (error) {
      return fail("read_payload", error);
    }

    if (parser_.is_done()) {
      do_close();
      auto callback = std::move(callback_);
      callback->receive_payload_part("", true);
      return;
    }

    read_payload();
  }

  void do_close() {
    completed_ = true;
    timer_.cancel();
    beast::error_code error;
    stream_.socket().cancel(error);
    stream_.socket().shutdown(tcp::socket::shutdown_both, error);
    stream_.socket().close(error);
  }

  void fail(const char *what, beast::error_code ec) {
    if (completed_)
      return;
    LOG(ERROR) << "failed http client: " << what << ": " << ec.message();
    fail(td::Status::Error(ec == beast::error::timeout || ec == asio::error::timed_out ? ton::ErrorCode::timeout
                                                                                       : ton::ErrorCode::notready,
                           PSTRING() << "backend HTTP " << what << ": " << ec.message()));
  }

  void fail(td::Status error) {
    if (completed_)
      return;
    do_close();
    auto callback = std::move(callback_);
    callback->receive_error(std::move(error));
  }

  bool stop_requested() {
    if (completed_)
      return true;
    if (cancel_requested_.load()) {
      fail(td::Status::Error(ton::ErrorCode::cancelled, "backend HTTP request cancelled"));
      return true;
    }
    if (Clock::now() >= deadline_) {
      fail(td::Status::Error(ton::ErrorCode::timeout, "backend HTTP deadline exceeded"));
      return true;
    }
    return false;
  }

 private:
  asio::strand<asio::io_context::executor_type> strand_;
  beast::tcp_stream stream_;
  asio::steady_timer timer_;
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
  Clock::time_point created_at_ = Clock::now();
  Clock::time_point deadline_;
  std::atomic<bool> cancel_requested_{false};
  bool completed_{false};
};

void HttpRequestHandle::cancel() const {
  if (auto session = session_.lock())
    session->cancel();
}

HttpRequestHandle run_http_request(const td::IPAddress &addr, HttpCallback::RequestType request_type, std::string url,
                                   std::vector<std::pair<std::string, std::string>> headers, std::string payload,
                                   double timeout, std::unique_ptr<HttpRequestCallback> callback) {
  auto req = std::make_shared<HttpClientSession>(io_context(), addr, request_type, std::move(url), std::move(headers),
                                                 std::move(payload), timeout, std::move(callback));
  req->run();
  return HttpRequestHandle(req);
}

}  // namespace http

}  // namespace cocoon
