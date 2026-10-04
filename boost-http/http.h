#pragma once

#include "td/actor/PromiseFuture.h"
#include <memory>
#include <vector>
#include <string>

namespace cocoon {

namespace http {

class HttpRequestCallback {
 public:
  virtual ~HttpRequestCallback() = default;
  virtual void receive_answer(td::int32 status_code, std::string content_type,
                              std::vector<std::pair<std::string, std::string>> headers, std::string body_part = "",
                              bool is_completed = false) = 0;
  virtual void receive_payload_part(std::string body_part, bool is_completed) = 0;
  // Exactly one terminal callback: completed=true OR receive_error. An error
  // after headers must never be translated into a successful end of body.
  virtual void receive_error(td::Status error) = 0;
};

class HttpCallback {
 public:
  enum class RequestType { Get, Post };
  virtual ~HttpCallback() = default;
  virtual void receive_request(RequestType request_type, std::vector<std::pair<std::string, std::string>> headers,
                               std::string path, std::vector<std::pair<std::string, std::string>> args,
                               std::string body, std::unique_ptr<HttpRequestCallback> answer_callback) = 0;
};

void init_http_server(td::uint16 port, std::shared_ptr<HttpCallback> callback);

}  // namespace http

}  // namespace cocoon
