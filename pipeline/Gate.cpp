#include "pipeline/Gate.h"
#include "boost-http/pipeline-metadata.h"
#include <boost/beast.hpp>
#include <algorithm>
#include <cmath>
#include <cstdio>

namespace cocoon::pipeline {
namespace asio = boost::asio;
namespace beast = boost::beast;
namespace http = beast::http;
using Error = boost::system::error_code;
using Clock = std::chrono::steady_clock;

namespace {
bool hop_header(beast::string_view name, const http::fields &fields) {
  for (const auto *blocked : {"host", "content-length", "transfer-encoding", "connection", "keep-alive",
                              "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade"})
    if (beast::iequals(name, blocked))
      return true;
  for (auto token : http::token_list(fields[http::field::connection]))
    if (beast::iequals(name, token))
      return true;
  return false;
}
bool reserved(beast::string_view name) {
  const beast::string_view prefix(cocoon::http::pipeline_header_prefix);
  return name.size() >= prefix.size() && beast::iequals(name.substr(0, prefix.size()), prefix);
}
}  // namespace

class Gate::Session : public std::enable_shared_from_this<Session> {
 public:
  Session(std::shared_ptr<Gate> gate, uint64_t id, asio::ip::tcp::socket socket)
      : gate_(gate)
      , id_(id)
      , client_(std::move(socket))
      , backend_(gate->io_)
      , timer_(gate->io_)
      , input_(16384)
      , response_buffer_(32768) {
    request_.header_limit(8192);
    request_.body_limit(gate->config_.profile.gate_body_bytes);
    response_.header_limit(8192);
    response_.body_limit(gate->config_.profile.gate_output_bytes);
  }
  void start() {
    deadline(Clock::now() + std::chrono::seconds(2));
    http::async_read(client_, input_, request_, [self = shared_from_this()](Error error, size_t) {
      if (self->done_)
        return;
      if (error) {
        self->finish(false);
        return;
      }
      self->received();
    });
  }
  bool valid() {
    if (done_)
      return false;
    auto gate = gate_.lock();
    auto state = gate ? gate->state_() : GateState{};
    if (!gate || gate->closed_ || (!epoch_.empty() && (!state.ready || state.epoch != epoch_)) ||
        Clock::now() >= deadline_) {
      finish(false);
      return false;
    }
    return true;
  }
  void finish(bool success) {
    if (done_)
      return;
    done_ = true;
    Error ignored;
    timer_.cancel();
    client_.shutdown(asio::ip::tcp::socket::shutdown_both, ignored);
    client_.close(ignored);
    backend_.shutdown(asio::local::stream_protocol::socket::shutdown_both, ignored);
    backend_.close(ignored);
    if (auto gate = gate_.lock()) {
      if (!epoch_.empty())
        (success ? gate->completed_ : gate->failed_)++;
      gate->sessions_.erase(id_);
    }
  }
  const std::string &request_id() const {
    return request_id_;
  }
  bool active() const {
    return !epoch_.empty() && !done_;
  }

 private:
  void deadline(Clock::time_point at) {
    deadline_ = at;
    timer_.expires_at(at);
    timer_.async_wait([self = shared_from_this()](Error error) {
      if (!error)
        self->finish(false);
    });
  }
  void reply(unsigned status, const Json &body) {
    local_reply_.emplace(static_cast<http::status>(status), 11);
    local_reply_->set(http::field::content_type, "application/json");
    local_reply_->keep_alive(false);
    local_reply_->body() = body.dump();
    local_reply_->prepare_payload();
    http::async_write(client_, *local_reply_, [self = shared_from_this()](Error, size_t) { self->finish(false); });
  }
  void received() {
    if (!valid())
      return;
    auto gate = gate_.lock();
    const auto &request = request_.get();
    const auto state = gate->state_();
    if (request.method() == http::verb::get && request.target() == "/v1/models") {
      if (!state.ready)
        return reply(503, {{"error", "pipeline group is not ready"}});
      return reply(200, {{"object", "list"},
                         {"data", {{{"id", gate->config_.effective.at("api_model")}, {"object", "model"}}}}});
    }
    if (request.method() != http::verb::post ||
        (request.target() != "/v1/chat/completions" && request.target() != "/v1/completions"))
      return reply(404, {{"error", "unsupported pipeline endpoint"}});
    if (!state.ready)
      return reply(503, {{"error", "pipeline group is not ready"}});
    const auto id = request[cocoon::http::pipeline_request_id_header];
    const auto budget = request[cocoon::http::pipeline_timeout_header];
    double seconds = 0;
    try {
      size_t used = 0;
      seconds = std::stod(std::string(budget), &used);
      if (used != budget.size() || !std::isfinite(seconds) || seconds <= 0 ||
          seconds * 1000 > gate->config_.profile.gate_timeout_ms)
        throw std::runtime_error("invalid timeout");
    } catch (...) {
      return reply(400, {{"error", "missing or invalid local timeout"}});
    }
    if (request.count(cocoon::http::pipeline_request_id_header) != 1 ||
        request.count(cocoon::http::pipeline_timeout_header) != 1 || id.empty() || id.size() > 96 ||
        !std::all_of(id.begin(), id.end(), [](unsigned char c) { return std::isalnum(c) || c == ':' || c == '-'; }))
      return reply(400, {{"error", "missing or invalid local request identity"}});
    if (!gate->admit(std::string(id)))
      return reply(429, {{"error", "pipeline capacity or request identity conflict"}});
    request_id_ = std::string(id);
    epoch_ = state.epoch;
    gate->accepted_++;
    deadline(Clock::now() + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(seconds)));

    // One request per connection. Observe disconnect even while the backend is
    // silent; waiting for a subsequent write would retain a hung generation.
    client_.async_read_some(asio::buffer(disconnect_byte_),
                            [self = shared_from_this()](Error, size_t) { self->finish(false); });
    upstream_.version(11);
    upstream_.method(request.method());
    upstream_.target(request.target());
    for (const auto &field : request)
      if (!hop_header(field.name_string(), request) && !reserved(field.name_string()))
        upstream_.insert(field.name_string(), field.value());
    upstream_.set(http::field::host, "localhost");
    upstream_.keep_alive(false);
    upstream_.body() = request.body();
    upstream_.prepare_payload();
    backend_.async_connect(asio::local::stream_protocol::endpoint(state.socket),
                           [self = shared_from_this()](Error error) {
                             if (!self->valid())
                               return;
                             if (error) {
                               self->finish(false);
                               return;
                             }
                             http::async_write(self->backend_, self->upstream_, [self](Error error, size_t) {
                               if (!self->valid())
                                 return;
                               if (error) {
                                 self->finish(false);
                                 return;
                               }
                               self->read_headers();
                             });
                           });
  }
  void read_headers() {
    http::async_read_header(backend_, response_buffer_, response_, [self = shared_from_this()](Error error, size_t) {
      if (!self->valid())
        return;
      if (error) {
        self->finish(false);
        return;
      }
      const auto &reply = self->response_.get();
      // Inference has a body; do not normalize 1xx/204/304 into success.
      if (reply.result_int() < 200 || reply.result_int() == 204 || reply.result_int() == 304) {
        self->finish(false);
        return;
      }
      self->headers_.version(11);
      self->headers_.result(reply.result_int());
      for (const auto &field : reply)
        if (!hop_header(field.name_string(), reply))
          self->headers_.insert(field.name_string(), field.value());
      self->headers_.keep_alive(false);
      self->headers_.chunked(true);
      self->serializer_.emplace(self->headers_);
      http::async_write_header(self->client_, *self->serializer_, [self](Error error, size_t) {
        if (!self->valid())
          return;
        if (error) {
          self->finish(false);
          return;
        }
        self->read_body();
      });
    });
  }
  void read_body() {
    if (!valid())
      return;
    if (response_.is_done()) {
      wire_ = "0\r\n\r\n";
      asio::async_write(client_, asio::buffer(wire_), [self = shared_from_this()](Error error, size_t) {
        if (self->valid())
          self->finish(!error);
      });
      return;
    }
    auto &body = response_.get().body();
    body.data = body_;
    body.size = sizeof(body_);
    http::async_read_some(backend_, response_buffer_, response_, [self = shared_from_this()](Error error, size_t) {
      if (!self->valid())
        return;
      if (error == http::error::need_buffer)
        error = {};
      const auto bytes = sizeof(self->body_) - self->response_.get().body().size;
      if (error) {
        self->finish(false);
        return;
      }
      if (!bytes) {
        self->read_body();
        return;
      }
      char prefix[32];
      std::snprintf(prefix, sizeof(prefix), "%zx\r\n", bytes);
      self->wire_ = std::string(prefix) + std::string(self->body_, bytes) + "\r\n";
      // Exactly one bounded chunk is in flight; read upstream only after the
      // downstream write completes. No unbounded queue ahead of the socket.
      asio::async_write(self->client_, asio::buffer(self->wire_), [self](Error error, size_t) {
        if (!self->valid())
          return;
        if (error) {
          self->finish(false);
          return;
        }
        self->read_body();
      });
    });
  }

  std::weak_ptr<Gate> gate_;
  uint64_t id_;
  asio::ip::tcp::socket client_;
  asio::local::stream_protocol::socket backend_;
  asio::steady_timer timer_;
  Clock::time_point deadline_;
  beast::flat_buffer input_, response_buffer_;
  http::request_parser<http::string_body> request_;
  http::request<http::string_body> upstream_;
  http::response_parser<http::buffer_body> response_;
  http::response<http::empty_body> headers_;
  std::optional<http::response_serializer<http::empty_body>> serializer_;
  std::optional<http::response<http::string_body>> local_reply_;
  char body_[16384], disconnect_byte_[1];
  std::string wire_, request_id_, epoch_;
  bool done_ = false;
};

Gate::Gate(asio::io_context &io, const Config &config, std::function<GateState()> state)
    : io_(io), config_(config), state_(std::move(state)), acceptor_(io) {
}

void Gate::start() {
  asio::ip::tcp::endpoint endpoint(asio::ip::make_address("127.0.0.1"), static_cast<unsigned short>(config_.gate_port));
  acceptor_.open(endpoint.protocol());
  acceptor_.set_option(asio::ip::tcp::acceptor::reuse_address(true));
  acceptor_.bind(endpoint);
  acceptor_.listen(16);
  accept();
}
void Gate::accept() {
  acceptor_.async_accept([self = shared_from_this()](Error error, asio::ip::tcp::socket socket) {
    if (self->closed_)
      return;
    if (!error && self->sessions_.size() < 16) {
      auto id = ++self->next_id_;
      auto session = std::make_shared<Session>(self, id, std::move(socket));
      self->sessions_.emplace(id, session);
      session->start();
    }
    self->accept();
  });
}
bool Gate::admit(const std::string &request_id) const {
  int active = 0;
  for (const auto &entry : sessions_) {
    if (entry.second->active()) {
      active++;
      if (entry.second->request_id() == request_id)
        return false;
    }
  }
  return active < config_.effective.at("max_num_seqs").get<int>();
}
void Gate::tick() {
  auto sessions = sessions_;
  for (const auto &entry : sessions)
    entry.second->valid();
}
void Gate::invalidate() {
  auto sessions = sessions_;
  for (const auto &entry : sessions)
    if (entry.second->active())
      entry.second->finish(false);
}
void Gate::close() {
  closed_ = true;
  Error ignored;
  acceptor_.close(ignored);
  auto sessions = sessions_;
  for (const auto &entry : sessions)
    entry.second->finish(false);
}
Json Gate::status() const {
  int active = 0;
  for (const auto &entry : sessions_)
    active += entry.second->active();
  return {{"listen", "127.0.0.1:" + std::to_string(config_.gate_port)},
          {"active_requests", active},
          {"connections", sessions_.size()},
          {"accepted", accepted_},
          {"completed", completed_},
          {"failed", failed_}};
}
}  // namespace cocoon::pipeline
