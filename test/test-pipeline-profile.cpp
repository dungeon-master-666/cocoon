#include "pipeline/Backend.h"

#include <iostream>
#include <stdexcept>

using namespace cocoon::pipeline;
namespace {
void require(bool condition, const char *message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}
template <class F>
void rejects(F action) {
  bool rejected = false;
  try {
    action();
  } catch (const std::exception &) {
    rejected = true;
  }
  require(rejected, "invalid configuration accepted");
}
}  // namespace

int main() {
  try {
    Json head = {{"profile", "simulator-dev-pp2-v1"}, {"rank", 0}, {"role", "head"}};
    auto a = validate_config(head, SecurityMode::Dev);
    Json member = {{"role", "member"}, {"rank", 1}, {"profile", "simulator-dev-pp2-v1"}};
    auto b = validate_config(member, SecurityMode::Dev);
    require(a.digest == b.digest && a.digest.size() == 64, "rank-specific fields changed config digest");
    auto explicit_defaults = head;
    explicit_defaults["limits"] = {{"max_model_len", 512}, {"max_num_seqs", 2}, {"max_num_batched_tokens", 512}};
    require(validate_config(explicit_defaults, SecurityMode::Dev).digest == a.digest, "defaults not normalized");
    explicit_defaults["limits"]["max_num_seqs"] = 1;
    require(validate_config(explicit_defaults, SecurityMode::Dev).digest != a.digest, "limits absent from digest");
    rejects([&] { validate_config(head, SecurityMode::Production); });
    auto prod_sim = a.profile;
    prod_sim.security_mode = SecurityMode::Production;
    rejects([&] { validate_profile(prod_sim, SecurityMode::Production); });
    for (const auto &backend : {"sglang", "vllm", "arbitrary"}) {
      auto unknown = a.profile;
      unknown.backend = backend;
      rejects([&] { validate_profile(unknown, SecurityMode::Dev); });
    }
    for (const auto &key : {"security_mode", "command", "env", "backend_oci_digest", "trust_remote_code", "tp_size"}) {
      auto invalid = head;
      invalid[key] = "injected";
      rejects([&] { validate_config(invalid, SecurityMode::Dev); });
    }
    for (const Json &rank : {Json(-1), Json(2), Json(true), Json(0.0), Json("0"), Json(18446744073709551615ULL)}) {
      auto invalid = head;
      invalid["rank"] = rank;
      rejects([&] { validate_config(invalid, SecurityMode::Dev); });
    }
    for (const auto &limit : {"max_model_len", "max_num_seqs", "max_num_batched_tokens"}) {
      auto invalid = head;
      invalid["limits"] = {{limit, 100000}};
      rejects([&] { validate_config(invalid, SecurityMode::Dev); });
    }
    auto plan = make_adapter(a)->build_launch_plan(a, "/tmp/pipeline-test");
    require(plan.argv.front() == plan.executable && plan.api_socket == "/tmp/pipeline-test/backend.sock" &&
                plan.health_socket == "/tmp/pipeline-test/health.sock",
            "invalid launch plan");
    require(plan.env.size() == 3, "unexpected inherited environment");
    Json network = {{"profile", "simulator-dev-pp2-wg-v1"},
                    {"rank", 0},
                    {"role", "head"},
                    {"group", {{"peer_port", 12310}}},
                    {"network", {{"underlay_ip", "198.18.0.1"}, {"peer_ip", "198.18.0.2"}}}};
    auto wired = validate_config(network, SecurityMode::Dev);
    require(wired.profile.wireguard && wired.digest != a.digest, "network policy absent from digest");
    rejects([&] { validate_config(network, SecurityMode::Production); });
    for (const auto &ip :
         {"127.0.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255", "10.231.0.2", "::1", "198.18.0.1", "198.018.0.2"}) {
      auto invalid = network;
      invalid["network"]["peer_ip"] = ip;
      rejects([&] { validate_config(invalid, SecurityMode::Dev); });
    }
    for (const auto &field : {"private_key", "allowed_ips", "namespace", "mtu"}) {
      auto invalid = network;
      invalid["network"][field] = "injected";
      rejects([&] { validate_config(invalid, SecurityMode::Dev); });
    }
    for (const auto &field : {"group", "network"}) {
      auto invalid = network;
      invalid.erase(field);
      rejects([&] { validate_config(invalid, SecurityMode::Dev); });
    }
    auto invalid = network;
    invalid["group"]["peer_port"] = 12311;
    rejects([&] { validate_config(invalid, SecurityMode::Dev); });
    invalid = network;
    invalid["profile"] = "simulator-dev-pp2-v1";
    rejects([&] { validate_config(invalid, SecurityMode::Dev); });
    std::cout << "PASS: profile policy, schema, limits, canonical digest and launch plan\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
