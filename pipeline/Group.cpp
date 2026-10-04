#include "pipeline/Group.h"
#include <algorithm>

namespace cocoon::pipeline {
namespace asio = boost::asio;
using Tcp = asio::ip::tcp;
using Error = boost::system::error_code;

Group::Group(Config config, asio::io_context &io, const Identity &identity, const std::string &boot,
             std::string run_dir)
    : config_(std::move(config))
    , io_(io)
    , tls_(std::make_shared<asio::ssl::context>(asio::ssl::context::tls))
    , acceptor_(io)
    , identity_(identity.peer) {
  configure_tls(*tls_, identity);
  if (config_.profile.wireguard)
    network_ = std::make_unique<Network>(config_, std::move(run_dir));
  self_ = descriptor(config_, identity_, boot, network_key_.public_key());
  if (config_.rank == 0)
    epoch_ = random_id();
  challenge_ = random_id();
  formation_deadline_ = Clock::now() + std::chrono::milliseconds(config_.profile.formation_ms);
  retry_at_ = Clock::now();
}

void Group::start() {
  if (config_.rank == 1) {
    Tcp::endpoint endpoint(asio::ip::make_address(config_.group->listen_host),
                           static_cast<unsigned short>(config_.group->listen_port));
    acceptor_.open(endpoint.protocol());
    acceptor_.set_option(Tcp::acceptor::reuse_address(true));
    acceptor_.bind(endpoint);
    acceptor_.listen(8);
    accept();
  } else
    dial();
}

void Group::bind_channel(const std::shared_ptr<Channel> &channel) {
  auto weak = weak_from_this();
  std::weak_ptr<Channel> peer = channel;
  channel->on_ready = [weak, peer] {
    if (auto self = weak.lock())
      if (auto c = peer.lock())
        self->authenticated(c);
  };
  channel->on_frame = [weak, peer](const Json &frame) {
    if (auto self = weak.lock()) {
      if (!self->closed_ && self->channel_ == peer.lock())
        self->receive(frame);
    }
  };
  channel->on_error = [weak, peer](const std::string &reason) {
    if (auto self = weak.lock()) {
      if (self->closed_ || self->stopping_)
        return;
      if (self->channel_ == peer.lock()) {
        if (self->config_.rank == 0 && self->state_ == "FORMING" && !self->leased_ && reason.starts_with("connect:")) {
          self->channel_.reset();
          self->retry_at_ = Clock::now() + std::chrono::milliseconds(200);
        } else
          self->fail(reason);
      } else
        self->rejection_ = reason;
    }
  };
}

void Group::accept() {
  if (closed_)
    return;
  auto candidate = std::make_shared<Channel>(io_, tls_, config_.profile.security_mode);
  bind_channel(candidate);
  acceptor_.async_accept(candidate->socket(), [weak = weak_from_this(), candidate](Error ec) {
    if (auto self = weak.lock()) {
      if (self->closed_) {
        candidate->close();
        return;
      }
      self->candidates_.erase(
          std::remove_if(self->candidates_.begin(), self->candidates_.end(), [](const auto &c) { return c->closed(); }),
          self->candidates_.end());
      if (!ec && !self->stopping_ && !self->channel_ && self->candidates_.size() < 4) {
        self->candidates_.push_back(candidate);
        candidate->handshake(true);
      } else
        candidate->close();
      self->accept();
    } else
      candidate->close();
  });
}

void Group::dial() {
  channel_ = std::make_shared<Channel>(io_, tls_, config_.profile.security_mode);
  bind_channel(channel_);
  channel_->connect(Tcp::endpoint(asio::ip::make_address(config_.group->peer_host),
                                  static_cast<unsigned short>(config_.group->peer_port)));
}

void Group::authenticated(const std::shared_ptr<Channel> &channel) {
  if (closed_ || stopping_ || (channel_ && channel_ != channel)) {
    channel->close();
    return;
  }
  channel_ = channel;
  if (channel->peer().public_key == identity_.public_key) {
    fail("duplicate TLS participant identity");
    return;
  }
  if (config_.rank == 0)
    send("Hello", {{"peer", self_}, {"challenge", challenge_}});
  else
    member_ = std::make_unique<MemberSession>(config_, self_, channel->peer());
}

bool Group::lease_valid() {
  if (leased_ && Clock::now() >= lease_deadline_) {
    fail("control lease expired");
    return false;
  }
  return true;
}

void Group::receive(const Json &frame) {
  if (closed_ || (!stopping_ && !lease_valid()))
    return;
  if (config_.rank == 1) {
    if (!member_) {
      fail("message before authenticated Hello");
      return;
    }
    auto reply = member_->receive(frame, local_ready_, !network_ || network_->healthy());
    channel_->send(reply.frame);
    if (reply.frame.at("payload").contains("error")) {
      rejection_ = reply.frame.at("payload").at("error").get<std::string>();
      return;
    }
    epoch_ = member_->epoch();
    roster_ = member_->roster();
    roster_digest_ = member_->roster_digest();
    group_id_ = member_->id();
    if (reply.action == GroupAction::ConfigureNetwork) {
      configuring_ = true;
      network_->start(roster_, network_key_);
    }
    if (reply.renew_lease && !stopping_) {
      leased_ = true;
      lease_deadline_ = Clock::now() + std::chrono::milliseconds(config_.profile.lease_ms);
    }
    if (reply.action == GroupAction::Start) {
      committed_ = true;
      start_pending_ = true;
      state_ = "STARTING";
    }
    if (reply.action == GroupAction::Stop) {
      stopping_ = true;
      ready_ = false;
      state_ = "STOPPING";
      failure_ = reply.frame.at("payload").at("restart").get<bool>() ? "head stopped failed group" : "";
    }
    if (member_->ready() && !stopping_) {
      ready_ = true;
      state_ = "READY";
    } else if (!stopping_ && !committed_ && !roster_.is_null())
      state_ = "PREPARED";
    return;
  }
  try {
    validate_frame(frame, "response");
    if (pending_.is_null() || frame.at("epoch") != epoch_ || frame.at("request_id") != pending_.at("request_id")) {
      rejection_ = "stale or unsolicited response";
      return;
    }
    if (frame.at("config_digest") != config_.digest || frame.at("sequence") != pending_.at("sequence") ||
        frame.at("op") != pending_.at("op"))
      throw std::runtime_error("response binding mismatch");
    const auto &p = frame.at("payload");
    if (p.contains("error"))
      throw std::runtime_error(p.at("error").get<std::string>());
    auto op = pending_.at("op").get<std::string>();
    pending_ = nullptr;
    leased_ = true;
    lease_deadline_ = Clock::now() + std::chrono::milliseconds(config_.profile.lease_ms);
    heartbeat_at_ = Clock::now() + std::chrono::milliseconds(config_.profile.heartbeat_ms);
    if (op == "Hello") {
      require_fields(p, {"peer", "echo", "challenge"}, "Hello response");
      validate_descriptor(p.at("peer"), config_, 1, channel_->peer());
      if (p.at("echo") != challenge_)
        throw std::runtime_error("head challenge mismatch");
      roster_ = Json::array({self_, p.at("peer")});
      validate_roster(roster_, config_, self_, p.at("peer"));
      roster_digest_ = config_digest(roster_);
      group_id_ = group_id(identity_.public_key, epoch_, config_.digest);
      send("Prepare", {{"roster", roster_},
                       {"roster_digest", roster_digest_},
                       {"group_id", group_id_},
                       {"echo", p.at("challenge")},
                       {"lease_ms", config_.profile.lease_ms}});
    } else if (op == "Prepare") {
      if (p != Json({{"prepared", true}, {"roster_digest", roster_digest_}}))
        throw std::runtime_error("invalid Prepare acknowledgement");
      state_ = "PREPARED";
      if (network_) {
        configuring_ = true;
        send("ConfigureNetwork", {{"roster_digest", roster_digest_}});
        network_->start(roster_, network_key_);
      } else
        send("Commit", {{"roster_digest", roster_digest_}});
    } else if (op == "ConfigureNetwork") {
      if (p != Json({{"configuring", true}}))
        throw std::runtime_error("invalid ConfigureNetwork acknowledgement");
    } else if (op == "Commit") {
      if (p != Json({{"committed", true}}))
        throw std::runtime_error("invalid Commit acknowledgement");
      committed_ = true;
      start_pending_ = true;
      state_ = "STARTING";
    } else if (op == "Heartbeat") {
      require_fields(p, {"local_ready", "ready", "network_ready"}, "Heartbeat response");
      if (p.size() != (network_ ? 3 : 2) || !p.at("ready").is_boolean())
        throw std::runtime_error("invalid Heartbeat acknowledgement");
      peer_ready_ = p.at("local_ready").get<bool>();
      if (network_)
        peer_network_ready_ = p.at("network_ready").get<bool>();
    } else if (op == "Ready") {
      if (p != Json({{"ready", true}}))
        throw std::runtime_error("invalid Ready acknowledgement");
      ready_ = true;
      state_ = "READY";
    }
  } catch (const std::exception &error) {
    fail(error.what());
  }
}

void Group::send(const std::string &op, Json payload) {
  pending_ = request_frame(config_, epoch_, ++sequence_, op, payload);
  channel_->send(pending_);
}

void Group::tick(bool local_ready) {
  local_ready_ = local_ready;
  if (network_) {
    network_->tick();
    if (!stopping_ && !network_->failure().empty())
      fail(network_->failure());
  }
  if (closed_ || stopping_ || !lease_valid())
    return;
  const auto now = Clock::now();
  if (!ready_ && now >= formation_deadline_) {
    fail("group formation/warmup deadline exceeded");
    return;
  }
  if (config_.rank == 0) {
    if (!channel_ && now >= retry_at_)
      dial();
    if ((committed_ || configuring_) && pending_.is_null()) {
      if (!committed_ && network_->healthy() && peer_network_ready_)
        send("Commit", {{"roster_digest", roster_digest_}});
      else if (committed_ && local_ready_ && peer_ready_ &&
               (!network_ || (network_->healthy() && peer_network_ready_)) && !ready_)
        send("Ready", {{"roster_digest", roster_digest_}});
      else if (now >= heartbeat_at_)
        send("Heartbeat", {{"roster_digest", roster_digest_}});
    }
  }
}

bool Group::take_start() {
  bool start = start_pending_;
  start_pending_ = false;
  return start && !stopping_;
}

void Group::stop(const std::string &reason) {
  if (stopping_)
    return;
  stopping_ = true;
  ready_ = false;
  state_ = "STOPPING";
  failure_ = reason;
  if (config_.rank == 0 && channel_ && !channel_->closed() && !roster_digest_.empty()) {
    send("Stop", {{"roster_digest", roster_digest_}, {"restart", !reason.empty()}});
  } else if (channel_)
    channel_->close();
}

void Group::fail(const std::string &reason) {
  stop(reason);
}

void Group::close() {
  if (closed_)
    return;
  closed_ = true;
  Error ignored;
  acceptor_.close(ignored);
  if (channel_)
    channel_->close();
  for (auto &c : candidates_)
    c->close();
  if (network_)
    network_->stop();
}

bool Group::cleanup_done() {
  if (!network_)
    return true;
  network_->tick();
  return network_->cleanup_done();
}

Json Group::status() const {
  return {{"state", state_},
          {"ready", ready()},
          {"epoch", epoch_.empty() ? Json(nullptr) : Json(epoch_)},
          {"group_id", group_id_.empty() ? Json(nullptr) : Json(group_id_)},
          {"roster", roster_},
          {"roster_digest", roster_digest_},
          {"identity", identity_.public_key},
          {"network_key", network_key_.public_key()},
          {"failure", failure_},
          {"last_rejection", rejection_},
          {"transport", "mutual-tls-1.3"},
          {"attestation", "dev-fixture"},
          {"network_setup", network_ ? "wireguard-netns-v1" : "simulated"},
          {"network", network_ ? network_->status() : Json(nullptr)}};
}
}  // namespace cocoon::pipeline
