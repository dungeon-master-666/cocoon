#include <boost/asio.hpp>
#include <boost/asio/io_context.hpp>
#include <boost/beast.hpp>
#include <boost/beast/core/error.hpp>
#include <boost/beast/http/status.hpp>
#include <deque>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <vector>
#include "http.h"
#include "www-form-urlencoded.h"

namespace cocoon {

namespace http {

namespace asio = boost::asio;
namespace beast = boost::beast;
namespace http = beast::http;
using tcp = asio::ip::tcp;

class HttpSession : public std::enable_shared_from_this<HttpSession> {
 public:
  explicit HttpSession(tcp::socket &&socket, std::shared_ptr<HttpCallback> callback)
      : stream_(std::move(socket)), strand_(stream_.get_executor()), callback_(std::move(callback)) {
  }

  void run() {
    self_ = shared_from_this();
    asio::dispatch(strand_, [self = shared_from_this()] { self->read_next_request(); });
  }
  ~HttpSession() {
  }

 private:
  enum class WriteType { Headers, Chunk, Final };
  static bool allow_header(std::string name) {
    std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });

    return !(name == "host" || name == "connection" || name == "transfer-encoding" || name == "content-length");
  }

  void read_next_request() {
    req_ = {};
    buffer_.consume(buffer_.size());

    q_.clear();
    writing_ = false;
    finished_ = false;
    aborted_ = false;
    header_sr_.reset();

    http::async_read(stream_, buffer_, req_,
                     asio::bind_executor(strand_, [self = shared_from_this()](beast::error_code ec, std::size_t) {
                       if (ec == http::error::end_of_stream) {
                         self->close();
                         return;
                       }
                       if (ec) {
                         LOG(ERROR) << "failed to read from inbound HTTP connection: " << ec.message();
                         self->close();
                         return;
                       }
                       self->start_next_request();
                     }));
  }

  void start_next_request() {
    switch (req_.method()) {
      case http::verb::get:
      case http::verb::post:
        handle_request();
        break;
      case http::verb::options:
        send_headers(http::status::no_content, "", {});
        finish();
        break;
      default:
        send_error(http::status::method_not_allowed, "Method not allowed\n");
        break;
    }
  }

  void handle_request() {
    HttpCallback::RequestType request_type = HttpCallback::RequestType::Get;
    switch (req_.method()) {
      case http::verb::get:
        request_type = HttpCallback::RequestType::Get;
        break;
      case http::verb::post:
        request_type = HttpCallback::RequestType::Post;
        break;
      default:
        UNREACHABLE();
    }

    std::vector<std::pair<std::string, std::string>> headers;
    for (auto &h : req_) {
      auto name = h.name_string();
      if (allow_header(name)) {
        auto value = h.value();
        headers.emplace_back(name, value);
      }
    }

    auto url = req_.target();
    for (const auto &s : {"http://", "https://"}) {
      if (url.starts_with(s)) {
        url.remove_prefix(strlen(s));
        auto p = url.find('/');
        if (p != url.npos) {
          url.remove_prefix(p);
        } else {
          url = "/";
        }
      }
    }

    std::string_view args;
    auto p = url.find('?');
    if (p != url.npos) {
      args = url.substr(p + 1);
      url = url.substr(0, p);
    }

    std::vector<std::pair<std::string, std::string>> args_vec;
    if (args.size() > 0) {
      args_vec = parse_x_www_form_urlencoded(td::Slice(args.begin(), args.end()));
    }

    class Cb : public HttpRequestCallback {
     public:
      Cb(std::weak_ptr<HttpSession> self, asio::strand<asio::any_io_executor> strand)
          : self_(std::move(self)), strand_(std::move(strand)) {
      }

      void receive_answer(td::int32 status_code, std::string content_type,
                          std::vector<std::pair<std::string, std::string>> headers, std::string body_part = "",
                          bool is_completed = false) override {
        CHECK(!already_started_);
        CHECK(!already_completed_);
        already_started_ = true;
        asio::post(strand_, [weak = self_, status_code = (http::status)status_code,
                             content_type = std::move(content_type), headers = std::move(headers)]() mutable {
          if (auto self = weak.lock()) {
            self->send_headers(status_code, content_type, std::move(headers));
          }
        });
        receive_payload_part(std::move(body_part), is_completed);
      }
      void receive_payload_part(std::string body_part, bool is_completed) override {
        CHECK(already_started_);
        CHECK(!already_completed_);
        if (body_part.size() > 0) {
          asio::post(strand_, [weak = self_, body_part = std::move(body_part)]() mutable {
            if (auto self = weak.lock()) {
              self->enqueue_part(std::move(body_part));
            }
          });
        }
        if (is_completed) {
          already_completed_ = true;
          asio::post(strand_, [weak = self_, body_part = std::move(body_part)]() mutable {
            if (auto self = weak.lock()) {
              self->finish();
            }
          });
        }
      }

      void receive_error(td::Status error) override {
        if (already_completed_)
          return;
        already_completed_ = true;
        already_started_ = true;
        asio::post(strand_, [weak = self_]() {
          if (auto self = weak.lock()) {
            self->abort();
          }
        });
      }

      ~Cb() {
        if (!already_started_) {
          already_started_ = true;
          already_completed_ = true;
          asio::post(strand_, [weak = self_]() mutable {
            if (auto self = weak.lock()) {
              self->send_headers(http::status::internal_server_error, "text/plain", {});
              self->enqueue_part(PSTRING() << "lost http callback");
              self->finish();
            }
          });
        }
        if (!already_completed_) {
          already_completed_ = true;
          asio::post(strand_, [weak = self_]() mutable {
            if (auto self = weak.lock()) {
              self->abort();
            }
          });
        }
      }

     private:
      std::weak_ptr<HttpSession> self_;
      asio::strand<asio::any_io_executor> strand_;

      bool already_started_{false};
      bool already_completed_{false};
    };

    callback_->receive_request(request_type, std::move(headers), url, std::move(args_vec), req_.body(),
                               std::make_unique<Cb>(shared_from_this(), strand_));
  }

  void send_headers(http::status status, std::string content_type,
                    std::vector<std::pair<std::string, std::string>> headers) {
    res_.version(req_.version());
    res_.result((http::status)status);
    res_.set(http::field::server, "cocoon-client");
    if (content_type.size() > 0) {
      res_.set(http::field::content_type, content_type);
    }
    res_.keep_alive(req_.keep_alive());
    for (auto &h : headers) {
      if (allow_header(h.first)) {
        res_.set(h.first, h.second);
      }
    }
    res_.chunked(true);

    header_sr_.emplace(res_);

    writing_ = true;
    http::async_write_header(
        stream_, *header_sr_,
        asio::bind_executor(strand_, [self = shared_from_this()](beast::error_code error, std::size_t) {
          if (error) {
            LOG(ERROR) << "http: failed to write answer header: " << error.message();
            self->close();
            return;
          }
          self->write_completed(std::move(error), WriteType::Headers);
        }));
  }

  void send_error(http::status status, std::string body) {
    send_headers(status, "text/plain", {});
    enqueue_part(body);
    finish();
  }

  void enqueue_part(std::string part) {
    if (finished_) {
      return;
    }
    q_.push_back(std::make_shared<std::string>(std::move(part)));
    send_next_part();
  }

  void finish() {
    if (finished_) {
      return;
    }
    finished_ = true;
    send_next_part();
  }

  void abort() {
    // Flush already queued body chunks, then close WITHOUT the final HTTP
    // chunk. Consumers must observe a truncated response, not successful EOF.
    aborted_ = true;
    finished_ = true;
    send_next_part();
  }

  void write_completed(beast::error_code error, WriteType write_type) {
    writing_ = false;

    if (error) {
      LOG(ERROR) << "http write answer failed: " << error.message();
      close();
      return;
    }

    switch (write_type) {
      case WriteType::Headers:
        send_next_part();
        return;
      case WriteType::Chunk:
        q_.pop_front();
        send_next_part();
        return;
      case WriteType::Final:
        if (req_.keep_alive()) {
          read_next_request();
        } else {
          close();
        }
        return;
    }
  }

  void send_next_part() {
    if (writing_) {
      return;
    }

    if (!q_.empty()) {
      writing_ = true;
      auto buf = q_.front();

      asio::async_write(
          stream_, http::make_chunk(asio::buffer(*buf)),
          asio::bind_executor(strand_, [self = shared_from_this(), buf](beast::error_code error, std::size_t) {
            self->write_completed(std::move(error), WriteType::Chunk);
          }));

      return;
    }

    if (finished_) {
      if (aborted_) {
        close();
        return;
      }
      writing_ = true;

      asio::async_write(stream_, http::make_chunk_last(),
                        asio::bind_executor(strand_, [self = shared_from_this()](beast::error_code error, std::size_t) {
                          self->write_completed(std::move(error), WriteType::Final);
                        }));
    }
  }

  void close() {
    beast::error_code error;
    if (stream_.socket().shutdown(tcp::socket::shutdown_send, error).failed()) {
      LOG(ERROR) << "failed to close http socket: " << error.message();
    }
    stream_.socket().close(error);
    self_ = nullptr;
  }

 private:
  beast::tcp_stream stream_;
  asio::strand<asio::any_io_executor> strand_;
  std::shared_ptr<HttpCallback> callback_;

  beast::flat_buffer buffer_;

  http::request<http::string_body> req_;

  http::response<http::empty_body> res_;
  std::optional<http::response_serializer<http::empty_body>> header_sr_;

  std::deque<std::shared_ptr<std::string>> q_;
  bool writing_{false};
  bool finished_{false};
  bool aborted_{false};
  std::shared_ptr<HttpSession> self_;
};

class HttpListener : public std::enable_shared_from_this<HttpListener> {
 public:
  HttpListener(asio::io_context &io, tcp::endpoint ep, std::shared_ptr<HttpCallback> callback)
      : acceptor_(io), callback_(std::move(callback)) {
    acceptor_.open(ep.protocol());
    acceptor_.set_option(asio::socket_base::reuse_address(true));
    acceptor_.bind(ep);
    acceptor_.listen(asio::socket_base::max_listen_connections);
  }

  void run() {
    acceptor_.async_accept([self = shared_from_this()](beast::error_code ec, tcp::socket socket) {
      if (!ec) {
        std::make_shared<HttpSession>(std::move(socket), self->callback_)->run();
      }
      self->run();
    });
  }

 private:
  tcp::acceptor acceptor_;
  std::shared_ptr<HttpCallback> callback_;
};

boost::asio::io_context io;
static std::vector<std::thread> threads;

void init_http_server(td::uint16 port, std::shared_ptr<HttpCallback> callback) {
  std::make_shared<cocoon::http::HttpListener>(io, boost::asio::ip::tcp::endpoint(boost::asio::ip::tcp::v4(), port),
                                               std::move(callback))
      ->run();

  for (int i = 0; i < 10; ++i) {
    threads.emplace_back([&] { io.run(); });
  }
}

boost::asio::io_context &io_context() {
  return io;
}

}  // namespace http

}  // namespace cocoon
