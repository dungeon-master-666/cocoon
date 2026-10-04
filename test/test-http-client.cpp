#include "boost-http/http-client.h"
#include <boost/asio.hpp>
#include <boost/beast.hpp>
#include <iostream>
#include <thread>

namespace cocoon::http {
boost::asio::io_context &io_context();
}
namespace asio = boost::asio;
using Tcp = asio::ip::tcp;

struct Result {
  int headers = 0, completed = 0, errors = 0;
  std::string body;
  bool late = false;
};

class Callback : public cocoon::http::HttpRequestCallback {
 public:
  explicit Callback(Result &result) : result_(result) {
  }
  void receive_answer(td::int32, std::string, std::vector<std::pair<std::string, std::string>>, std::string part,
                      bool completed) override {
    result_.headers++;
    receive_payload_part(std::move(part), completed);
  }
  void receive_payload_part(std::string part, bool completed) override {
    if (result_.completed || result_.errors)
      result_.late = true;
    result_.body += part;
    result_.completed += completed;
  }
  void receive_error(td::Status) override {
    if (result_.completed || result_.errors)
      result_.late = true;
    result_.errors++;
  }

 private:
  Result &result_;
};

void test(const std::string &wire, bool success, int headers, const std::string &body, bool refuse = false) {
  asio::io_context server_io;
  Tcp::acceptor acceptor(server_io, Tcp::endpoint(asio::ip::make_address("127.0.0.1"), 0));
  auto port = acceptor.local_endpoint().port();
  std::thread server;
  if (refuse)
    acceptor.close();
  else
    server = std::thread([&] {
      Tcp::socket socket(server_io);
      acceptor.accept(socket);
      boost::beast::flat_buffer buffer;
      boost::beast::http::request<boost::beast::http::string_body> request;
      boost::beast::error_code error;
      boost::beast::http::read(socket, buffer, request, error);
      asio::write(socket, asio::buffer(wire), error);
      socket.shutdown(Tcp::socket::shutdown_both, error);
    });
  td::IPAddress addr;
  addr.init_host_port("127.0.0.1", port).ensure();
  Result result;
  auto &io = cocoon::http::io_context();
  io.restart();
  cocoon::http::run_http_request(addr, cocoon::http::HttpCallback::RequestType::Get, "/", {}, "", 1,
                                 std::make_unique<Callback>(result));
  io.run_for(std::chrono::seconds(3));
  if (server.joinable())
    server.join();
  if (result.late || result.completed != (success ? 1 : 0) || result.errors != (success ? 0 : 1) ||
      result.headers != headers || result.body != body) {
    throw std::runtime_error("HTTP framing/terminal callback invariant failed");
  }
}

int main() {
  try {
    test("HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc", true, 1, "abc");
    test("HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nabc", true, 1, "abc");
    test("HTTP/1.1 204 No Content\r\n\r\n", true, 1, "");
    test("HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nabc", false, 1, "abc");
    test("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n", true, 1, "abc");
    test("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n", false, 1, "abc");
    test("HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n", false, 1, "");
    test("HTTP/1.1 200 OK\r\nContent-Len", false, 0, "");
    test("", false, 0, "");
    test("", false, 0, "", true);
    std::cout << "PASS: 10 HTTP framing/transport cases, exactly one terminal callback\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
