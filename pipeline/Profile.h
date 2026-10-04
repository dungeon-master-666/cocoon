#pragma once

#include <nlohmann/json.hpp>
#include <string>
#include <optional>

namespace cocoon::pipeline {
using Json = nlohmann::json;
enum class SecurityMode { Production, Dev };

struct Profile {
  std::string id;
  SecurityMode security_mode;
  std::string backend;
  int pp_size;
  int startup_ms = 3000;
  int warmup_ms = 2000;
  int probe_ms = 500;
  int probe_interval_ms = 200;
  int watchdog_ms = 1500;
  int stop_ms = 500;
  int kill_ms = 2000;
  int heartbeat_ms = 200;
  int lease_ms = 1500;
  int formation_ms = 4000;
  int restart_ms = 500;
  int max_restarts = 2;
  bool wireguard = false;
};

struct GroupConfig {
  std::string listen_host;
  int listen_port = 0;
  std::string peer_host;
  int peer_port = 0;
  std::string certificate_base;
};

struct NetworkConfig {
  std::string underlay_ip;
  std::string peer_ip;
};

struct Config {
  Profile profile;
  Json effective;
  std::string digest;
  int rank;
  std::string role;
  std::string scenario;
  int startup_delay_ms;
  int warmup_delay_ms;
  std::optional<GroupConfig> group;
  std::optional<NetworkConfig> network;
};

void validate_profile(const Profile &profile, SecurityMode build_policy);
Config validate_config(const Json &runtime, SecurityMode build_policy);
Json read_config(const std::string &path);
std::string config_digest(const Json &effective);
void require_fields(const Json &value, std::initializer_list<const char *> allowed, const char *where);
Json parse_json(const std::string &text);
}  // namespace cocoon::pipeline
