#include "pipeline/Channel.h"
#include <algorithm>

namespace cocoon::pipeline {
using Error = boost::system::error_code;
namespace asio = boost::asio;

Channel::Channel(asio::io_context &io, std::shared_ptr<asio::ssl::context> context, SecurityMode policy)
    : context_(std::move(context))
    , stream_(io, *context_)
    , timer_(io)
    , expiry_timer_(io)
    , peer_(std::make_shared<std::optional<PeerIdentity>>()) {
  stream_.set_verify_callback(peer_verifier(policy, peer_));
}

void Channel::deadline() {
  const auto generation = ++deadline_generation_;
  timer_.expires_after(std::chrono::seconds(2));
  timer_.async_wait([self = shared_from_this(), generation](Error ec) {
    if (!ec && generation == self->deadline_generation_)
      self->fail("TLS/frame deadline exceeded");
  });
}

void Channel::connect(const Tcp::endpoint &endpoint) {
  deadline();
  socket().async_connect(endpoint, [self = shared_from_this()](Error ec) {
    if (self->closed_)
      return;
    if (ec)
      self->fail("connect: " + ec.message());
    else
      self->handshake(false);
  });
}

void Channel::handshake(bool server) {
  deadline();
  stream_.async_handshake(server ? asio::ssl::stream_base::server : asio::ssl::stream_base::client,
                          [self = shared_from_this()](Error ec) {
                            if (self->closed_)
                              return;
                            if (ec || !self->peer_->has_value()) {
                              self->fail("mutual TLS verification failed: " + ec.message());
                              return;
                            }
                            if (!self->arm_expiry())
                              return;
                            if (self->on_ready)
                              self->on_ready();
                            if (!self->closed_)
                              self->read();
                          });
}

bool Channel::arm_expiry() {
  // TLS only checks validity at the handshake. Retire the group before either
  // pinned certificate expires, including on a long-lived active connection.
  auto lifetime = [](const X509 *cert) -> int64_t {
    int days = 0, seconds = 0;
    if (!cert || ASN1_TIME_diff(&days, &seconds, nullptr, X509_get0_notAfter(cert)) != 1)
      return 0;
    return static_cast<int64_t>(days) * 86400 + seconds;
  };
  std::unique_ptr<X509, decltype(&X509_free)> peer(SSL_get1_peer_certificate(stream_.native_handle()), X509_free);
  auto seconds = std::min(lifetime(peer.get()), lifetime(SSL_get_certificate(stream_.native_handle()))) - 5;
  if (seconds <= 0) {
    fail("TLS certificate renewal required");
    return false;
  }
  expiry_timer_.expires_after(std::chrono::seconds(seconds));
  expiry_timer_.async_wait([self = shared_from_this()](Error ec) {
    if (!ec)
      self->fail("TLS certificate renewal required");
  });
  return true;
}

void Channel::read() {
  deadline();
  asio::async_read(stream_, asio::buffer(header_), [self = shared_from_this()](Error ec, size_t) {
    if (self->closed_)
      return;
    if (ec) {
      self->fail("control read: " + ec.message());
      return;
    }
    uint32_t size = 0;
    for (auto b : self->header_)
      size = (size << 8) | b;
    if (size == 0 || size > 65536) {
      self->fail("control frame exceeds 64 KiB");
      return;
    }
    self->body_.resize(size);
    asio::async_read(self->stream_, asio::buffer(self->body_), [self](Error ec, size_t) {
      if (self->closed_)
        return;
      if (ec) {
        self->fail("control body: " + ec.message());
        return;
      }
      try {
        auto frame = parse_json(self->body_);
        if (self->on_frame)
          self->on_frame(frame);
      } catch (const std::exception &error) {
        self->fail(error.what());
        return;
      }
      if (!self->closed_)
        self->read();
    });
  });
}

void Channel::send(const Json &frame) {
  if (closed_)
    return;
  auto body = frame.dump();
  if (body.size() > 65536 || writes_.size() >= 8 || queued_bytes_ + body.size() + 4 > 262144) {
    fail("control write queue limit exceeded");
    return;
  }
  uint32_t size = static_cast<uint32_t>(body.size());
  std::string wire(4, '\0');
  for (int i = 0; i < 4; ++i)
    wire[static_cast<size_t>(i)] = static_cast<char>(size >> (24 - 8 * i));
  wire += body;
  queued_bytes_ += wire.size();
  writes_.push_back(std::move(wire));
  if (writes_.size() == 1)
    write();
}

void Channel::write() {
  asio::async_write(stream_, asio::buffer(writes_.front()), [self = shared_from_this()](Error ec, size_t) {
    if (self->closed_)
      return;
    if (ec) {
      self->fail("control write: " + ec.message());
      return;
    }
    self->queued_bytes_ -= self->writes_.front().size();
    self->writes_.pop_front();
    if (!self->writes_.empty())
      self->write();
  });
}

void Channel::close() {
  if (closed_)
    return;
  closed_ = true;
  timer_.cancel();
  expiry_timer_.cancel();
  Error ignored;
  socket().close(ignored);
  // Pending async_write owns buffers through shared_from_this until completion.
}

void Channel::fail(const std::string &reason) {
  if (closed_)
    return;
  close();
  if (on_error)
    on_error(reason);
}
}  // namespace cocoon::pipeline
