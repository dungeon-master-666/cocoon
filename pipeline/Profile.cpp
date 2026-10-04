#include "pipeline/Profile.h"

#include "td/utils/crypto.h"
#include <fstream>
#include <set>
#include <stdexcept>

namespace cocoon::pipeline {
namespace {
void fields(const Json &value, std::initializer_list<const char *> allowed, const char *where) {
  if (!value.is_object()) {
    throw std::runtime_error(std::string(where) + " must be an object");
  }
  std::set<std::string> names(allowed.begin(), allowed.end());
  for (auto it = value.begin(); it != value.end(); ++it) {
    if (!names.count(it.key())) {
      throw std::runtime_error(std::string(where) + ": unsupported field " + it.key());
    }
  }
}

int integer(const Json &obj, const char *key, int fallback, int low, int high) {
  if (!obj.contains(key)) {
    return fallback;
  }
  const auto &v = obj.at(key);
  if (!v.is_number_integer() || v < low || v > high) {
    throw std::runtime_error(std::string(key) + ": integer outside supported range");
  }
  return v.get<int>();
}
}  // namespace

void validate_profile(const Profile &profile, SecurityMode build_policy) {
  if (profile.backend == "simulator" && profile.security_mode != SecurityMode::Dev) {
    throw std::runtime_error("simulator is forbidden in a production profile");
  }
  if (profile.security_mode != build_policy) {
    throw std::runtime_error("profile security policy does not match this executable");
  }
  if (profile.backend != "simulator") {
    throw std::runtime_error("backend adapter is not implemented; no fallback is allowed");
  }
  if (profile.pp_size != 2) {
    throw std::runtime_error("unsupported pipeline size");
  }
}

std::string config_digest(const Json &effective) {
  // nlohmann::json uses sorted object keys; validation normalizes all types.
  auto bytes = td::sha256(effective.dump());
  constexpr char hex[] = "0123456789abcdef";
  std::string result;
  for (unsigned char b : bytes) {
    result += hex[b >> 4];
    result += hex[b & 15];
  }
  return result;
}

Config validate_config(const Json &runtime, SecurityMode build_policy) {
  fields(runtime, {"profile", "rank", "role", "limits", "simulator"}, "runtime");
  const auto id = runtime.at("profile").get<std::string>();
  // Trusted catalogue, compiled into the measured executable. Runtime selects
  // an entry; it cannot supply commands, images, policy or environment variables.
  if (id != "simulator-dev-pp2-v1") {
    throw std::runtime_error("unknown or unsupported profile: " + id);
  }
  Profile profile{id, SecurityMode::Dev, "simulator", 2};
  validate_profile(profile, build_policy);
  if (!runtime.contains("rank")) {
    throw std::runtime_error("rank is required");
  }
  int rank = integer(runtime, "rank", 0, 0, profile.pp_size - 1);
  auto role = runtime.at("role").get<std::string>();
  if (role != (rank == 0 ? "head" : "member")) {
    throw std::runtime_error("role does not match rank");
  }
  const auto limits = runtime.value("limits", Json::object());
  fields(limits, {"max_model_len", "max_num_seqs", "max_num_batched_tokens"}, "limits");
  int context = integer(limits, "max_model_len", 512, 16, 512);
  int seqs = integer(limits, "max_num_seqs", 2, 1, 2);
  int batch = integer(limits, "max_num_batched_tokens", 512, context, 512);
  const auto sim = runtime.value("simulator", Json::object());
  fields(sim, {"scenario", "startup_delay_ms", "warmup_delay_ms"}, "simulator");
  const auto scenario = sim.value("scenario", std::string("normal"));
  const std::set<std::string> scenarios{"normal",      "startup-exit", "startup-hang",      "warmup-error",
                                        "warmup-hang", "health-hang",  "crash-after-ready", "stubborn-child"};
  if (!scenarios.count(scenario)) {
    throw std::runtime_error("unsupported simulator scenario");
  }
  Json effective = {{"protocol_version", 1},
                    {"profile_id", id},
                    {"security_mode", "dev"},
                    {"security_policy_version", "dev-local-v1"},
                    {"model_identifier", "cocoon-simulator@v1:dev-fixture"},
                    {"model_verity_root", nullptr},
                    {"model_verification", "compiled-dev-fixture"},
                    {"model_architecture", "simulator"},
                    {"backend_kind", "simulator"},
                    {"backend_oci_digest", nullptr},
                    {"backend_artifact", "bundled-simulator-v1"},
                    {"adapter_version", 1},
                    {"runtime_compatibility_id", "simulator-http-uds-v1"},
                    {"pp_size", 2},
                    {"tp_size", 1},
                    {"dp_size", 1},
                    {"layer_partition", {{0, 1}, {1, 2}}},
                    {"dtype", "synthetic"},
                    {"quantization", "none"},
                    {"tokenizer_identity", "simulator-whitespace-v1"},
                    {"chat_template_identity", "simulator-v1"},
                    {"max_model_len", context},
                    {"max_num_seqs", seqs},
                    {"max_num_batched_tokens", batch},
                    {"gpu_memory_policy", "no-gpu-dev-only"},
                    {"prefix_cache_policy", "disabled"},
                    {"capabilities", {"text-json", "text-sse", "usage", "cancel-by-disconnect"}},
                    {"validated_backend_options", Json::object()},
                    {"lifecycle",
                     {{"startup_ms", profile.startup_ms},
                      {"warmup_ms", profile.warmup_ms},
                      {"probe_ms", profile.probe_ms},
                      {"probe_interval_ms", profile.probe_interval_ms},
                      {"watchdog_ms", profile.watchdog_ms},
                      {"stop_ms", profile.stop_ms},
                      {"kill_ms", profile.kill_ms}}}};
  return {profile,
          effective,
          config_digest(effective),
          rank,
          role,
          scenario,
          integer(sim, "startup_delay_ms", 0, 0, 30000),
          integer(sim, "warmup_delay_ms", 0, 0, 30000)};
}

Json read_config(const std::string &path) {
  std::ifstream file(path, std::ios::binary);
  if (!file) {
    throw std::runtime_error("cannot open config: " + path);
  }
  std::string text(65537, '\0');
  file.read(text.data(), static_cast<std::streamsize>(text.size()));
  text.resize(static_cast<size_t>(file.gcount()));
  if (text.size() > 65536) {
    throw std::runtime_error("config exceeds 64 KiB");
  }
  // Duplicate keys are rejected rather than silently choosing the last value.
  std::vector<std::set<std::string>> keys;
  return Json::parse(text, [&](int depth, Json::parse_event_t event, Json &value) {
    if (depth > 16) {
      throw std::runtime_error("config nesting exceeds 16 levels");
    }
    if (event == Json::parse_event_t::object_start) {
      keys.emplace_back();
    } else if (event == Json::parse_event_t::key) {
      if (!keys.back().insert(value.get<std::string>()).second) {
        throw std::runtime_error("duplicate config key");
      }
    } else if (event == Json::parse_event_t::object_end) {
      keys.pop_back();
    }
    return true;
  });
}
}  // namespace cocoon::pipeline
