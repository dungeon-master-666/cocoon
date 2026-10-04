#include "pipeline/Backend.h"
#include <algorithm>
#include <iostream>
#include <stdexcept>
using namespace cocoon::pipeline;
namespace {
void check(bool condition, const char *message) {
  if (!condition) throw std::runtime_error(message);
}
template <class F> void rejects(F action) {
  bool rejected = false;
  try { action(); } catch (const std::exception &) { rejected = true; }
  check(rejected, "invalid profile accepted");
}
}
int main() {
  try {
    Json emitted;
    for (const auto *profile : {"vllm-qwen3-0.6b-dev-pp2-wg-v1", "vllm-qwen3-14b-dev-pp2-wg-v1"}) {
      Json runtime = {{"profile", profile}, {"rank", 0}, {"role", "head"}, {"group", {{"peer_port", 12310}}},
                      {"network", {{"underlay_ip", "198.18.0.1"}, {"peer_ip", "198.18.0.2"}}},
                      {"gate", {{"listen_port", 18080}}}};
      auto head = validate_config(runtime, SecurityMode::Dev);
      auto adapter = make_adapter(head);
      auto plan = adapter->build_launch_plan(head, "/tmp/vllm-contract");
      const auto helper = Json::parse(plan.argv.back());
      check(head.profile.wireguard && head.profile.backend == "vllm", "wrong backend/network");
      check(head.effective.at("backend_oci_digest").get<std::string>().find("@sha256:") != std::string::npos,
            "image not pinned");
      check(head.effective.at("model_manifest").at("files").contains("tokenizer.json"), "model not pinned");
      auto args = helper.at("argv").get<std::vector<std::string>>();
      auto value = [&](const char *key) { auto p = std::find(args.begin(), args.end(), key);
        check(p != args.end() && p + 1 != args.end(), "missing backend option"); return *(p + 1); };
      check(value("--pipeline-parallel-size") == "2" && value("--tensor-parallel-size") == "1" && value("--node-rank") == "0" &&
            value("--nnodes") == "2" && value("--host") == "127.0.0.1" &&
            value("--master-addr") == "10.231.0.1" && value("--master-port") == "29501" &&
            value("--max-num-seqs") == "1" && value("--cpu-offload-gb") == "0", "unsafe launch topology/options");
      check(std::find(plan.env.begin(), plan.env.end(), "NCCL_SOCKET_IFNAME==wg0") != plan.env.end(), "NCCL route not pinned");
      check(std::find(plan.env.begin(), plan.env.end(), "HF_HUB_OFFLINE=1") != plan.env.end(), "online model fetch allowed");
      check(adapter->warmup(head).method == "POST", "head does not exercise model");
      Json health = {{"status", "ok"}, {"backend_alive", true}, {"rank", 0},
                     {"config_digest", head.digest}, {"backend_version", "0.29.0+cu129"}};
      check(adapter->valid_health(head, health), "healthy local backend rejected");
      rejects([&] { check(adapter->valid_warmup(head, health), "health is not generation"); });
      Json warmup = {{"model", head.effective.at("api_model")},
                     {"choices", {{{"message", {{"content", "Hello"}}}, {"finish_reason", "length"}}}},
                     {"usage", {{"prompt_tokens", 8}, {"completion_tokens", 4}}}};
      check(adapter->valid_warmup(head, warmup), "full model warmup rejected");
      warmup["choices"][0]["message"]["content"] = "";
      check(!adapter->valid_warmup(head, warmup), "empty warmup accepted");
      auto member_runtime = runtime;
      member_runtime.erase("gate"); member_runtime["rank"] = 1; member_runtime["role"] = "member";
      member_runtime["group"] = {{"listen_port", 12310}};
      member_runtime["network"] = {{"underlay_ip", "198.18.0.2"}, {"peer_ip", "198.18.0.1"}};
      auto member = validate_config(member_runtime, SecurityMode::Dev);
      check(head.digest == member.digest, "placement changed group digest");
      auto member_adapter = make_adapter(member);
      auto mp = member_adapter->build_launch_plan(member, "/tmp/vllm-contract");
      auto member_args = Json::parse(mp.argv.back()).at("argv").get<std::vector<std::string>>();
      check(std::find(member_args.begin(), member_args.end(), "--headless") != member_args.end(), "member is not headless");
      auto other = runtime; other["profile"] = std::string(profile).replace(0, 4, "sglang");
      check(validate_config(other, SecurityMode::Dev).digest != head.digest, "mixed backends have same digest");
      check(mp.api_socket == mp.health_socket && member_adapter->warmup(member).method == "GET",
            "member wrongly requires a public generation API");
      check(!member_adapter->valid_health(member, health), "head health accepted at member");
      rejects([&] { validate_config(runtime, SecurityMode::Production); });
      for (auto key : {"group", "network"}) {
        auto invalid = runtime; invalid.erase(key); rejects([&] { validate_config(invalid, SecurityMode::Dev); });
      }
      for (auto key : {"simulator", "command", "env", "model_path", "backend_oci_digest"}) {
        auto invalid = runtime; invalid[key] = Json::object(); rejects([&] { validate_config(invalid, SecurityMode::Dev); });
      }
      auto invalid = runtime; invalid["limits"] = {{"max_num_seqs", 2}};
      rejects([&] { validate_config(invalid, SecurityMode::Dev); });
      emitted.push_back({{"effective", head.effective}, {"helper", helper}});
    }
    std::cout << emitted.dump() << '\n';
    return 0;
  } catch (const std::exception &error) { std::cerr << error.what() << '\n'; return 1; }
}
