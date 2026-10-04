#pragma once

#include <nlohmann/json.hpp>
#include <string>

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
};

void validate_profile(const Profile &profile, SecurityMode build_policy);
Config validate_config(const Json &runtime, SecurityMode build_policy);
Json read_config(const std::string &path);
std::string config_digest(const Json &effective);
}  // namespace cocoon::pipeline
