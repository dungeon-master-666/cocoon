#include "pipeline/Backend.h"

#include <stdexcept>

namespace cocoon::pipeline {
namespace {
class SimulatorAdapter final : public BackendAdapter {
 public:
  LaunchPlan build_launch_plan(const Config &config, const std::string &run_dir) const override {
    auto socket = run_dir + "/backend.sock";
    auto health_socket = run_dir + "/health.sock";
    return {PIPELINE_PYTHON,
            {PIPELINE_PYTHON,
             "-I",
             "-u",
             PIPELINE_SIMULATOR,
             "--socket",
             socket,
             "--health-socket",
             health_socket,
             "--rank",
             std::to_string(config.rank),
             "--config-digest",
             config.digest,
             "--model",
             config.effective.at("model_identifier").get<std::string>(),
             "--max-model-len",
             std::to_string(config.effective.at("max_model_len").get<int>()),
             "--max-num-seqs",
             std::to_string(config.effective.at("max_num_seqs").get<int>()),
             "--max-num-batched-tokens",
             std::to_string(config.effective.at("max_num_batched_tokens").get<int>()),
             "--scenario",
             config.scenario,
             "--startup-delay-ms",
             std::to_string(config.startup_delay_ms),
             "--warmup-delay-ms",
             std::to_string(config.warmup_delay_ms)},
            {"PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C"},
            run_dir + "/backend.log",
            socket,
            health_socket};
  }
  ProbeRequest health() const override {
    return {"GET", "/health", ""};
  }
  ProbeRequest warmup(const Config &config) const override {
    return {"POST", "/v1/chat/completions",
            Json({{"model", config.effective.at("model_identifier")},
                  {"messages", {{{"role", "user"}, {"content", "pipeline warmup"}}}},
                  {"max_tokens", 2},
                  {"stream", false}})
                .dump()};
  }
  bool valid_health(const Config &config, const Json &body) const override {
    return body.at("status") == "ok" && body.at("rank") == config.rank && body.at("config_digest") == config.digest &&
           body.at("security_mode") == "dev";
  }
  bool valid_warmup(const Config &config, const Json &body) const override {
    return body.at("model") == config.effective.at("model_identifier") && body.at("choices").size() == 1 &&
           body.at("choices").at(0).at("message").at("content") == "simulated reply" &&
           body.at("choices").at(0).at("finish_reason") == "stop" && body.at("usage").at("completion_tokens") == 2;
  }
};
}  // namespace

std::unique_ptr<BackendAdapter> make_adapter(const Config &config) {
  validate_profile(config.profile, config.profile.security_mode);
  if (config.profile.backend == "simulator") {
    return std::make_unique<SimulatorAdapter>();
  }
  throw std::runtime_error("unsupported backend adapter");
}
}  // namespace cocoon::pipeline
