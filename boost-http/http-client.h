#pragma once

#include "http.h"
#include "td/utils/port/IPAddress.h"

namespace cocoon {

namespace http {

class HttpClientSession;

// Copyable, thread-safe cancellation handle. It does not keep the session alive;
// dropping a handle does not cancel (callers may intentionally ignore it).
// cancel() is asynchronous and idempotent, including before start/after finish.
class HttpRequestHandle {
 public:
  HttpRequestHandle() = default;
  explicit HttpRequestHandle(std::weak_ptr<HttpClientSession> session) : session_(std::move(session)) {
  }
  void cancel() const;
  bool expired() const {
    return session_.expired();
  }

 private:
  std::weak_ptr<HttpClientSession> session_;
};

// timeout is a total budget from this call, including scheduling and all I/O.
HttpRequestHandle run_http_request(const td::IPAddress& addr, HttpCallback::RequestType request_type, std::string url,
                                   std::vector<std::pair<std::string, std::string>> headers, std::string payload,
                                   double timeout, std::unique_ptr<HttpRequestCallback> callback);
}

}  // namespace cocoon
