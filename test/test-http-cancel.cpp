#include "boost-http/http-client.h"
#include "errorcode.h"
#include <boost/asio.hpp>
#include <atomic>
#include <chrono>
#include <cmath>
#include <functional>
#include <iostream>
#include <limits>
#include <thread>
#include <vector>

namespace cocoon::http {
boost::asio::io_context &io_context();
}
namespace asio = boost::asio;
using Tcp = asio::ip::tcp;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

void require(bool ok, const std::string &message) {
  if (!ok)
    throw std::runtime_error(message);
}

// An independent, bounded loopback peer. It observes EOF/reset from the client;
// stopping the fixture is not counted as proof that cancellation closed a socket.
class Server {
 public:
  explicit Server(std::string mode)
      : acceptor_(io_, Tcp::endpoint(asio::ip::make_address("127.0.0.1"), 0)), mode_(std::move(mode)) {
    port = acceptor_.local_endpoint().port();
    acceptor_.non_blocking(true);
    listener_ = std::thread([this] {
      while (!stopped_) {
        Tcp::socket socket(io_);
        boost::system::error_code error;
        acceptor_.accept(socket, error);
        if (!error) {
          accepted++;
          peers_.emplace_back([this, socket = std::move(socket)]() mutable { serve(socket); });
        } else if (error != asio::error::would_block && error != asio::error::try_again) {
          failed = true;
          break;
        }
        std::this_thread::sleep_for(1ms);
      }
    });
  }
  ~Server() {
    stop();
  }
  void stop() {
    stopped_ = true;
    if (listener_.joinable())
      listener_.join();
    for (auto &peer : peers_)
      if (peer.joinable())
        peer.join();
  }
  unsigned short port;
  std::atomic<int> accepted{0}, ready{0}, closed{0};
  std::atomic<bool> failed{false};

 private:
  bool write(Tcp::socket &socket, const std::string &data, Clock::time_point end) {
    size_t offset = 0;
    while (offset < data.size() && !stopped_ && Clock::now() < end) {
      boost::system::error_code error;
      offset += socket.write_some(asio::buffer(data.data() + offset, data.size() - offset), error);
      if (error && error != asio::error::would_block && error != asio::error::try_again)
        return false;
      if (error)
        std::this_thread::sleep_for(1ms);
    }
    return offset == data.size();
  }
  void serve(Tcp::socket &socket) {
    socket.non_blocking(true);
    auto end = Clock::now() + 3s;
    char data[16384];
    std::string headers;
    if (mode_ == "write-stall") {
      ready++;
      // Exceed the sender's socket buffers without reading its large request.
      std::this_thread::sleep_for(250ms);
    } else {
      while (!stopped_ && Clock::now() < end && headers.find("\r\n\r\n") == std::string::npos) {
        boost::system::error_code error;
        auto n = socket.read_some(asio::buffer(data), error);
        if (!error)
          headers.append(data, n);
        else if (error != asio::error::would_block && error != asio::error::try_again) {
          closed++;
          return;
        }
        std::this_thread::sleep_for(1ms);
      }
      ready++;
      if (mode_ == "success" || mode_ == "race") {
        if (mode_ == "race")
          std::this_thread::sleep_for(2ms);
        write(socket, "HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc", end);
      } else if (mode_ != "headers-stall") {
        write(socket, "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", end);
        if (mode_ == "body-stall" || mode_ == "trickle")
          write(socket, "1\r\nx\r\n", end);
      }
    }
    auto next = Clock::now();
    while (!stopped_ && Clock::now() < end) {
      boost::system::error_code error;
      socket.read_some(asio::buffer(data), error);
      if (error && error != asio::error::would_block && error != asio::error::try_again) {
        closed++;
        return;
      }
      if (mode_ == "trickle" && Clock::now() >= next) {
        write(socket, "1\r\nx\r\n", end);
        next = Clock::now() + 10ms;
      }
      std::this_thread::sleep_for(1ms);
    }
    if (!stopped_)
      failed = true;
  }
  asio::io_context io_;
  Tcp::acceptor acceptor_;
  std::string mode_;
  std::atomic<bool> stopped_{false};
  std::thread listener_;
  std::vector<std::thread> peers_;
};

struct Result {
  std::atomic<int> headers{0}, chunks{0}, success{0}, errors{0}, code{0};
  std::atomic<bool> late{false}, destroyed{false};
  std::atomic<double> finished_after{0};
};
class Callback : public cocoon::http::HttpRequestCallback {
 public:
  Callback(Result &result, Clock::time_point start, std::function<void(std::string)> notify)
      : r_(result), start_(start), notify_(std::move(notify)) {
  }
  ~Callback() override {
    r_.destroyed = true;
  }
  void receive_answer(td::int32, std::string, std::vector<std::pair<std::string, std::string>>, std::string part,
                      bool done) override {
    r_.headers++;
    notify_("headers");
    receive_payload_part(std::move(part), done);
  }
  void receive_payload_part(std::string part, bool done) override {
    if (r_.success || r_.errors)
      r_.late = true;
    if (!part.empty()) {
      r_.chunks++;
      notify_("body");
    }
    if (done) {
      r_.success++;
      r_.finished_after = std::chrono::duration<double>(Clock::now() - start_).count();
      notify_("late");
    }
  }
  void receive_error(td::Status error) override {
    if (r_.success || r_.errors)
      r_.late = true;
    r_.code = error.code();
    r_.errors++;
    r_.finished_after = std::chrono::duration<double>(Clock::now() - start_).count();
    notify_("late");
  }

 private:
  Result &r_;
  Clock::time_point start_;
  std::function<void(std::string)> notify_;
};

void test(const std::string &mode, const std::string &cancel_at, double timeout = 60, int count = 1) {
  Server server(mode);
  auto &io = cocoon::http::io_context();
  io.restart();
  auto work = asio::make_work_guard(io);
  td::IPAddress addr;
  addr.init_host_port("127.0.0.1", server.port).ensure();
  std::vector<Result> results(count);
  std::vector<cocoon::http::HttpRequestHandle> handles(count);
  auto start = Clock::now();
  for (int i = 0; i < count; ++i) {
    auto cb = std::make_unique<Callback>(results[i], start, [&, i](const std::string &phase) {
      if (phase == cancel_at) {
        handles[i].cancel();
        handles[i].cancel();
      }
    });
    handles[i] = cocoon::http::run_http_request(addr,
                                                mode == "write-stall" ? cocoon::http::HttpCallback::RequestType::Post
                                                                      : cocoon::http::HttpCallback::RequestType::Get,
                                                "/", {}, mode == "write-stall" ? std::string(8 << 20, 'x') : "",
                                                timeout, std::move(cb));
    if (cancel_at == "before") {
      handles[i].cancel();
      handles[i].cancel();
    }
  }
  if (cancel_at == "queued")
    std::this_thread::sleep_for(100ms);
  std::vector<std::thread> workers;
  for (int i = 0; i < 4; ++i)
    workers.emplace_back([&] { io.run_for(3s); });
  auto limit = Clock::now() + 2s;
  if (cancel_at == "external" || cancel_at == "race") {
    while (server.ready < count && Clock::now() < limit)
      std::this_thread::sleep_for(1ms);
    std::thread canceller([&] {
      for (const auto &h : handles) {
        h.cancel();
        h.cancel();
      }
    });
    for (const auto &h : handles)
      h.cancel();
    canceller.join();
  }
  bool no_connect = cancel_at == "before" || cancel_at == "queued" || !std::isfinite(timeout) || timeout <= 0;
  auto completed = [&] {
    for (const auto &r : results)
      if (r.success + r.errors != 1 || !r.destroyed)
        return false;
    return server.closed == (no_connect ? 0 : count);
  };
  while (!completed() && Clock::now() < limit)
    std::this_thread::sleep_for(1ms);
  bool clean_before_fixture_stop = completed() && !server.failed;
  for (const auto &h : handles) {
    h.cancel();
    h.cancel();
  }
  server.stop();
  work.reset();
  for (auto &worker : workers)
    worker.join();
  require(clean_before_fixture_stop, mode + ": terminal/callback/socket not released within 2s");
  for (int i = 0; i < count; ++i) {
    auto &r = results[i];
    require(!r.late && r.success + r.errors == 1 && handles[i].expired(),
            mode + ": duplicate terminal or session retained");
    if (mode == "success")
      require(r.success == 1, "late cancellation changed success");
    else if (cancel_at != "race")
      require(r.errors == 1, "cancel/deadline succeeded");
    if (cancel_at == "deadline" || cancel_at == "queued") {
      require(r.code == ton::ErrorCode::timeout, "wrong timeout error");
      if (timeout > 0 && std::isfinite(timeout))
        require(r.finished_after >= timeout * 0.8 && r.finished_after < 1, "fractional/absolute deadline changed");
    }
  }
  require(!no_connect || server.accepted == 0, "cancelled/expired request opened a socket");
  std::cout << "PASS " << mode << " / " << cancel_at << " x" << count << '\n';
}

int main() {
  try {
    test("headers-stall", "before");
    test("headers-stall", "queued", 0.05);
    for (double timeout :
         {0.0, -1.0, std::numeric_limits<double>::infinity(), std::numeric_limits<double>::quiet_NaN()})
      test("headers-stall", "deadline", timeout);
    test("headers-stall", "external");
    test("body-stall", "headers");
    test("body-stall", "body");
    for (const std::string &mode : {"headers-stall", "body-stall", "trickle", "write-stall"})
      test(mode, "deadline", 0.15);
    test("write-stall", "external");
    test("success", "late");
    test("race", "race", 60, 64);
    test("headers-stall", "external", 60, 128);
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
