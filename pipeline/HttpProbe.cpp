#include "pipeline/HttpProbe.h"

namespace cocoon::pipeline {
namespace http = boost::beast::http;
using Error = boost::system::error_code;

HttpProbe::HttpProbe(boost::asio::io_context &io) : socket_(io), timer_(io), buffer_(73728) {
  parser_.body_limit(65536);
  parser_.header_limit(8192);
}

std::shared_ptr<HttpProbe> HttpProbe::start(boost::asio::io_context &io, const std::string &path,
                                            const ProbeRequest &request, int timeout_ms) {
  auto self = std::shared_ptr<HttpProbe>(new HttpProbe(io));
  self->request_.version(11);
  self->request_.method_string(request.method);
  self->request_.target(request.target);
  self->request_.set(http::field::host, "localhost");
  self->request_.set(http::field::content_type, "application/json");
  self->request_.set(http::field::connection, "close");
  self->request_.body() = request.body;
  self->request_.prepare_payload();
  self->timer_.expires_after(std::chrono::milliseconds(timeout_ms));
  self->timer_.async_wait([self](Error ec) {
    if (!ec) {
      self->finish("probe deadline exceeded");
    }
  });
  self->socket_.async_connect(boost::asio::local::stream_protocol::endpoint(path), [self](Error ec) {
    if (ec) {
      self->finish(ec.message());
      return;
    }
    http::async_write(self->socket_, self->request_, [self](Error ec, size_t) {
      if (ec) {
        self->finish(ec.message());
        return;
      }
      http::async_read(self->socket_, self->buffer_, self->parser_, [self](Error ec, size_t) {
        if (ec) {
          self->finish(ec.message());
          return;
        }
        if (!self->done_) {
          self->status_ = self->parser_.get().result_int();
          self->body_ = self->parser_.get().body();
          self->finish(self->status_ == 200 ? "" : "backend HTTP " + std::to_string(self->status_));
        }
      });
    });
  });
  return self;
}

void HttpProbe::finish(const std::string &error) {
  if (done_) {
    return;
  }
  done_ = true;
  error_ = error;
  Error ignored;
  timer_.cancel();
  socket_.close(ignored);
}
void HttpProbe::cancel() {
  finish("probe cancelled");
}
}  // namespace cocoon::pipeline
