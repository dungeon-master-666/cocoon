#include "pipeline/Protocol.h"
#include <algorithm>
#include <set>
#include <stdexcept>

namespace cocoon::pipeline {
namespace {
void require(bool condition, const char *message) {
  if (!condition)
    throw std::runtime_error(message);
}
void hex_id(const Json &value) {
  require(value.is_string(), "identity must be a string");
  auto text = value.get<std::string>();
  require(text.size() == 64 &&
              std::all_of(text.begin(), text.end(),
                          [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); }) &&
              text != std::string(64, '0'),
          "invalid 256-bit identity");
}
Json response(const Json &request, const Json &payload) {
  auto result = request;
  result["kind"] = "response";
  result["payload"] = payload;
  return result;
}
}  // namespace

Json descriptor(const Config &config, const PeerIdentity &identity, const std::string &boot,
                const std::string &network_key) {
  return {{"rank", config.rank},
          {"role", config.role},
          {"identity", identity.public_key},
          {"image_hash", identity.image_hash},
          {"boot_id", boot},
          {"network_key", network_key},
          {"overlay_ip", config.rank == 0 ? "10.231.0.1" : "10.231.0.2"},
          {"profile_id", config.profile.id},
          {"backend_kind", config.profile.backend},
          {"local_checks", "dev-model-fixture-no-gpu"}};
}

void validate_descriptor(const Json &v, const Config &config, int rank, const PeerIdentity &identity) {
  require_fields(v,
                 {"rank", "role", "identity", "image_hash", "boot_id", "network_key", "overlay_ip", "profile_id",
                  "backend_kind", "local_checks"},
                 "peer descriptor");
  require(v.size() == 10, "incomplete peer descriptor");
  require(v.at("rank").is_number_integer() && v.at("rank") == rank, "unexpected rank");
  require(v.at("role") == (rank == 0 ? "head" : "member"), "unexpected role");
  require(v.at("identity") == identity.public_key && v.at("image_hash") == identity.image_hash,
          "descriptor does not match verified TLS identity/image");
  require(v.at("profile_id") == config.profile.id && v.at("backend_kind") == config.profile.backend,
          "backend/profile mismatch");
  require(v.at("local_checks") == "dev-model-fixture-no-gpu", "local checks are not ready");
  require(v.at("overlay_ip") == (rank == 0 ? "10.231.0.1" : "10.231.0.2"), "invalid overlay address");
  for (const auto *key : {"identity", "image_hash", "boot_id", "network_key"})
    hex_id(v.at(key));
}

void validate_roster(const Json &roster, const Config &config, const Json &head, const Json &member) {
  require(roster.is_array() && roster.size() == static_cast<size_t>(config.profile.pp_size), "wrong roster size");
  require(roster.at(0) == head && roster.at(1) == member, "roster differs from authenticated Hello identities");
  for (const auto *field : {"rank", "identity", "boot_id", "network_key", "overlay_ip"}) {
    std::set<std::string> seen;
    for (const auto &entry : roster) {
      require(seen.insert(entry.at(field).dump()).second, "duplicate roster rank, identity, boot or network key");
    }
  }
}

std::string group_id(const std::string &head_key, const std::string &epoch, const std::string &digest) {
  return config_digest({{"head_key", head_key}, {"epoch", epoch}, {"config_digest", digest}});
}

Json request_frame(const Config &config, const std::string &epoch, uint64_t sequence, const std::string &op,
                   const Json &payload) {
  return {{"version", 1},
          {"kind", "request"},
          {"op", op},
          {"epoch", epoch},
          {"config_digest", config.digest},
          {"sequence", sequence},
          {"request_id", random_id()},
          {"payload", payload}};
}

void validate_frame(const Json &f, const char *kind) {
  require_fields(f, {"version", "kind", "op", "epoch", "config_digest", "sequence", "request_id", "payload"}, "frame");
  require(f.size() == 8 && f.at("version").is_number_integer() && f.at("version") == 1 && f.at("kind") == kind,
          "unsupported frame version or kind");
  require(f.at("op").is_string() && f.at("op").get<std::string>().size() <= 32 && f.at("payload").is_object(),
          "invalid operation or payload");
  require(f.at("sequence").is_number_integer() && f.at("sequence") > 0 && f.at("sequence") <= 1000000000,
          "invalid sequence");
  hex_id(f.at("epoch"));
  hex_id(f.at("config_digest"));
  hex_id(f.at("request_id"));
}

MemberSession::MemberSession(Config config, Json self, PeerIdentity head_identity)
    : config_(std::move(config))
    , self_(std::move(self))
    , head_identity_(std::move(head_identity))
    , challenge_(random_id()) {
}

ProtocolReply MemberSession::receive(const Json &frame, bool local_ready) {
  try {
    validate_frame(frame, "request");
    require(frame.at("config_digest") == config_.digest, "config digest mismatch");
    require(epoch_.empty() || frame.at("epoch") == epoch_, "stale or foreign epoch");
    auto id = frame.at("request_id").get<std::string>();
    if (auto found = cache_.find(id); found != cache_.end()) {
      require(found->second.first == frame, "request ID reused with a different payload");
      return {found->second.second, GroupAction::None, false};
    }
    require(frame.at("sequence").get<uint64_t>() > sequence_, "replayed sequence");
    if (frame.at("op") != "Heartbeat") {
      require(cache_.size() < 8, "too many lifecycle requests");
    }
    auto result = apply(frame, local_ready);
    sequence_ = frame.at("sequence").get<uint64_t>();
    if (frame.at("op") != "Heartbeat") {
      cache_.emplace(id, std::make_pair(frame, result.frame));
    }
    return result;
  } catch (const std::exception &error) {
    // Invalid/stale frames cannot mutate state, spawn processes or renew leases.
    return {response(frame, {{"error", error.what()}}), GroupAction::None, false};
  }
}

ProtocolReply MemberSession::apply(const Json &frame, bool local_ready) {
  const auto op = frame.at("op").get<std::string>();
  const auto &p = frame.at("payload");
  require(!stopped_, "group already stopped");
  if (op == "Hello") {
    require_fields(p, {"peer", "challenge"}, "Hello");
    require(p.size() == 2 && epoch_.empty(), "Hello already completed");
    validate_descriptor(p.at("peer"), config_, 0, head_identity_);
    hex_id(p.at("challenge"));
    require(p.at("peer").at("identity") != self_.at("identity") && p.at("peer").at("boot_id") != self_.at("boot_id") &&
                p.at("peer").at("network_key") != self_.at("network_key"),
            "duplicate participant");
    head_ = p.at("peer");
    epoch_ = frame.at("epoch").get<std::string>();
    return {response(frame, {{"peer", self_}, {"echo", p.at("challenge")}, {"challenge", challenge_}}),
            GroupAction::None, true};
  }
  require(!epoch_.empty(), "Hello required first");
  if (op == "Prepare") {
    require_fields(p, {"roster", "roster_digest", "group_id", "echo", "lease_ms"}, "Prepare");
    require(p.size() == 5 && !prepared_, "group already prepared");
    require(p.at("echo") == challenge_, "member challenge mismatch");
    require(p.at("lease_ms").is_number_integer() && p.at("lease_ms") == config_.profile.lease_ms, "invalid lease");
    validate_roster(p.at("roster"), config_, head_, self_);
    auto digest = config_digest(p.at("roster"));
    auto gid = group_id(head_identity_.public_key, epoch_, config_.digest);
    require(p.at("roster_digest") == digest && p.at("group_id") == gid, "roster/group digest mismatch");
    roster_ = p.at("roster");
    roster_digest_ = digest;
    group_id_ = gid;
    prepared_ = true;
    return {response(frame, {{"prepared", true}, {"roster_digest", digest}}), GroupAction::None, true};
  }
  require(prepared_, "Prepare required first");
  if (op == "Stop") {
    require_fields(p, {"roster_digest", "restart"}, "Stop");
    require(p.size() == 2 && p.at("roster_digest") == roster_digest_ && p.at("restart").is_boolean(), "invalid Stop");
    stopped_ = true;
    ready_ = false;
    return {response(frame, {{"stopped", true}, {"restart", p.at("restart")}}), GroupAction::Stop, false};
  }
  require_fields(p, {"roster_digest"}, "lifecycle payload");
  require(p.size() == 1 && p.at("roster_digest") == roster_digest_, "roster digest mismatch");
  if (op == "Commit") {
    require(!committed_, "group already committed");
    committed_ = true;
    return {response(frame, {{"committed", true}}), GroupAction::Start, true};
  }
  require(committed_, "Commit required first");
  if (op == "Heartbeat") {
    return {response(frame, {{"local_ready", local_ready}, {"ready", ready_}}), GroupAction::None, true};
  }
  if (op == "Ready") {
    require(local_ready && !ready_, "local warmup not complete or group already ready");
    ready_ = true;
    return {response(frame, {{"ready", true}}), GroupAction::None, true};
  }
  throw std::runtime_error("unknown membership operation");
}
}  // namespace cocoon::pipeline
