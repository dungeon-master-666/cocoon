#include "pipeline/Channel.h"
#include "pipeline/Protocol.h"
#include <iostream>
#include <stdexcept>

using namespace cocoon::pipeline;
namespace {
void require(bool value, const char *message) {
  if (!value)
    throw std::runtime_error(message);
}
void rejected(const ProtocolReply &reply) {
  require(reply.frame.at("payload").contains("error") && reply.action == GroupAction::None && !reply.renew_lease,
          "invalid frame mutated state or renewed lease");
}

void protocol_tests() {
  auto head = validate_config(
      {{"profile", "simulator-dev-pp2-v1"}, {"rank", 0}, {"role", "head"}, {"group", {{"peer_port", 12310}}}},
      SecurityMode::Dev);
  auto member = validate_config(
      {{"profile", "simulator-dev-pp2-v1"}, {"rank", 1}, {"role", "member"}, {"group", {{"listen_port", 12310}}}},
      SecurityMode::Dev);
  PeerIdentity a{random_id(), dev_image_hash()}, b{random_id(), dev_image_hash()};
  NetworkKey ak, bk;
  auto ah = descriptor(head, a, random_id(), ak.public_key());
  auto bh = descriptor(member, b, random_id(), bk.public_key());
  MemberSession session(member, bh, a);
  auto epoch = random_id();
  auto hello = request_frame(head, epoch, 1, "Hello", {{"peer", ah}, {"challenge", random_id()}});
  for (const auto *field : {"identity", "image_hash", "role", "rank", "backend_kind", "profile_id", "network_key"}) {
    auto bad = hello;
    bad["payload"]["peer"][field] = "invalid";
    rejected(session.receive(bad, false));
    require(session.epoch().empty(), "invalid Hello selected epoch");
  }
  auto wrong_config = hello;
  wrong_config["config_digest"] = random_id();
  rejected(session.receive(wrong_config, false));
  for (const Json &bad_sequence : {Json(0), Json(-1), Json(true), Json(1.0), Json(1000000001)}) {
    auto bad = hello;
    bad["sequence"] = bad_sequence;
    rejected(session.receive(bad, false));
    require(session.epoch().empty(), "malformed sequence changed state");
  }
  auto hi = session.receive(hello, false);
  require(hi.renew_lease && !hi.frame.at("payload").contains("error"), "Hello failed");
  auto repeated_hello = session.receive(hello, false);
  require(repeated_hello.frame == hi.frame && !repeated_hello.renew_lease, "Hello replay renewed lease");
  auto early_commit = request_frame(head, epoch, 2, "Commit", {{"roster_digest", random_id()}});
  rejected(session.receive(early_commit, false));
  auto roster = Json::array({ah, bh});
  auto digest = config_digest(roster);
  auto prepare = request_frame(head, epoch, 2, "Prepare",
                               {{"roster", roster},
                                {"roster_digest", digest},
                                {"group_id", group_id(a.public_key, epoch, head.digest)},
                                {"echo", hi.frame.at("payload").at("challenge")},
                                {"lease_ms", head.profile.lease_ms}});
  for (const auto *field : {"rank", "identity", "boot_id", "network_key", "overlay_ip"}) {
    auto bad = prepare;
    bad["payload"]["roster"][1][field] = ah[field];
    bad["payload"]["roster_digest"] = config_digest(bad["payload"]["roster"]);
    rejected(session.receive(bad, false));
  }
  auto wrong_nonce = prepare;
  wrong_nonce["payload"]["echo"] = random_id();
  rejected(session.receive(wrong_nonce, false));
  auto prepared = session.receive(prepare, false);
  require(prepared.frame.at("payload").at("prepared") == true, "Prepare failed");
  require(session.receive(prepare, false).frame == prepared.frame, "Prepare is not idempotent");
  auto reused_id = prepare;
  reused_id["payload"]["lease_ms"] = 999;
  rejected(session.receive(reused_id, false));
  auto commit = request_frame(head, epoch, 3, "Commit", {{"roster_digest", digest}});
  require(session.receive(commit, false).action == GroupAction::Start, "Commit did not start backend");
  require(session.receive(commit, false).action == GroupAction::None, "Commit replay started backend twice");
  auto replayed_sequence = commit;
  replayed_sequence["request_id"] = random_id();
  rejected(session.receive(replayed_sequence, false));
  auto ready = request_frame(head, epoch, 4, "Ready", {{"roster_digest", digest}});
  rejected(session.receive(ready, false));
  require(session.receive(ready, true).frame.at("payload").at("ready") == true, "Ready failed");
  auto stop = request_frame(head, epoch, 5, "Stop", {{"roster_digest", digest}, {"restart", true}});
  auto old = stop;
  old["epoch"] = random_id();
  rejected(session.receive(old, true));
  require(session.ready(), "old epoch changed readiness");
  auto stopped = session.receive(stop, true);
  require(stopped.action == GroupAction::Stop && !session.ready(), "Stop failed");
  auto repeat_stop = session.receive(stop, false);
  require(repeat_stop.frame == stopped.frame && repeat_stop.action == GroupAction::None, "Stop is not idempotent");

  // Old messages must also be harmless inside a replacement session/epoch.
  MemberSession replacement(member, bh, a);
  auto next_hello = hello;
  next_hello["epoch"] = random_id();
  next_hello["request_id"] = random_id();
  require(replacement.receive(next_hello, false).renew_lease, "new epoch Hello failed");
  rejected(replacement.receive(stop, false));
  require(replacement.epoch() == next_hello["epoch"].get<std::string>() && !replacement.committed(),
          "old Stop changed new group");
}

bool tls_pair(const std::string &client_variant, const std::string &server_variant, SecurityMode server_policy) {
  namespace asio = boost::asio;
  asio::io_context io;
  auto make = [](const std::string &variant) {
    auto cert = dev_certificate(variant);
    return Identity{cert, certificate_identity(cert)};
  };
  auto client_context = std::make_shared<asio::ssl::context>(asio::ssl::context::tls);
  auto server_context = std::make_shared<asio::ssl::context>(asio::ssl::context::tls);
  auto ci = make(client_variant), si = make(server_variant);
  configure_tls(*client_context, ci);
  configure_tls(*server_context, si);
  auto client = std::make_shared<Channel>(io, client_context, SecurityMode::Dev);
  auto server = std::make_shared<Channel>(io, server_context, server_policy);
  asio::ip::tcp::acceptor listener(io, {asio::ip::make_address("127.0.0.1"), 0});
  int ready_count = 0;
  bool failed = false;
  auto close = [&] {
    client->close();
    server->close();
    listener.close();
  };
  client->on_ready = [&] {
    require(client->peer() == si.peer, "wrong verified server identity");
    if (++ready_count == 2)
      close();
  };
  server->on_ready = [&] {
    require(server->peer() == ci.peer, "wrong verified client identity");
    if (++ready_count == 2)
      close();
  };
  client->on_error = server->on_error = [&](const std::string &) {
    failed = true;
    close();
  };
  listener.async_accept(server->socket(), [&](boost::system::error_code ec) {
    if (!ec)
      server->handshake(true);
  });
  client->connect(listener.local_endpoint());
  io.run_for(std::chrono::seconds(3));
  close();
  io.restart();
  io.poll();
  return ready_count == 2 && !failed;
}

void network_protocol_tests() {
  auto config = [](int rank) {
    return validate_config(
        {{"profile", "simulator-dev-pp2-wg-v1"},
         {"rank", rank},
         {"role", rank ? "member" : "head"},
         {"group", {{rank ? "listen_port" : "peer_port", 12310}}},
         {"network",
          {{"underlay_ip", rank ? "198.18.0.2" : "198.18.0.1"}, {"peer_ip", rank ? "198.18.0.1" : "198.18.0.2"}}}},
        SecurityMode::Dev);
  };
  auto h = config(0), m = config(1);
  require(h.digest == m.digest, "placement changed network profile digest");
  PeerIdentity hi{random_id(), dev_image_hash()}, mi{random_id(), dev_image_hash()};
  NetworkKey hk, mk;
  auto hd = descriptor(h, hi, random_id(), hk.public_key()), md = descriptor(m, mi, random_id(), mk.public_key());
  auto epoch = random_id();
  MemberSession member(m, md, hi);
  auto hello = request_frame(h, epoch, 1, "Hello", {{"peer", hd}, {"challenge", random_id()}});
  auto bad = hello;
  bad["payload"]["peer"]["network_endpoint"]["ip"] = "198.18.0.99";
  rejected(member.receive(bad, false, false));
  auto reply = member.receive(hello, false, false);
  auto roster = Json::array({hd, md});
  auto digest = config_digest(roster);
  auto prepare = request_frame(h, epoch, 2, "Prepare",
                               {{"roster", roster},
                                {"roster_digest", digest},
                                {"group_id", group_id(hi.public_key, epoch, h.digest)},
                                {"echo", reply.frame["payload"]["challenge"]},
                                {"lease_ms", h.profile.lease_ms}});
  require(member.receive(prepare, false, false).renew_lease, "network Prepare failed");
  rejected(member.receive(request_frame(h, epoch, 3, "Commit", {{"roster_digest", digest}}), false, false));
  auto configure = request_frame(h, epoch, 3, "ConfigureNetwork", {{"roster_digest", digest}});
  require(member.receive(configure, false, false).action == GroupAction::ConfigureNetwork, "network setup not emitted");
  require(member.receive(configure, false, false).action == GroupAction::None, "network replay reconfigured keys");
  auto commit = request_frame(h, epoch, 4, "Commit", {{"roster_digest", digest}});
  rejected(member.receive(commit, false, false));
  auto heartbeat = request_frame(h, epoch, 4, "Heartbeat", {{"roster_digest", digest}});
  auto heartbeat_reply = member.receive(heartbeat, false, false);
  require(heartbeat_reply.renew_lease && heartbeat_reply.frame["payload"]["network_ready"] == false,
          "network formation heartbeat failed");
  commit["sequence"] = 5;
  require(member.receive(commit, false, true).action == GroupAction::Start, "healthy network Commit failed");
}
}  // namespace

int main() {
  try {
    protocol_tests();
    network_protocol_tests();
    require(tls_pair("valid", "valid", SecurityMode::Dev), "valid mutual TLS failed");
    for (const auto *bad : {"wrong-image", "wrong-key", "missing-evidence", "expired"}) {
      require(!tls_pair(bad, "valid", SecurityMode::Dev), "invalid client evidence accepted");
      require(!tls_pair("valid", bad, SecurityMode::Dev), "invalid server evidence accepted");
    }
    require(!tls_pair("valid", "valid", SecurityMode::Production), "production accepted dev TLS evidence");
    std::cout << "PASS: membership invariants, idempotency, stale epochs and real mutual TLS policies\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
