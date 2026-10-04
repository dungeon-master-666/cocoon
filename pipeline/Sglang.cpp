#include "pipeline/Sglang.h"
#include "sglang-models.h"

namespace cocoon::pipeline {
namespace {
constexpr auto image = "lmsysorg/sglang:v0.5.10.post1-cu130-runtime@sha256:"
                       "715c461258624eae38124dcb1e1f620cbe307594fd3403ab92caf1b7017afd0f";
class SglangAdapter final : public BackendAdapter {
 public:
  LaunchPlan build_launch_plan(const Config &c, const std::string &dir) const override {
    const auto &e = c.effective;
    const auto model = e.at("model_manifest").at("model");
    const auto name = model.at("id").get<std::string>();
    const auto path = "/models/" + name.substr(name.find('/') + 1) + "/" + model.at("revision").get<std::string>();
    const auto socket = dir + "/backend.sock", health_socket = dir + "/health.sock";
    std::vector<std::string> argv = {
        "/usr/bin/python3", "-m", "sglang.launch_server", "--model-path", path, "--served-model-name", name,
        "--dtype", "bfloat16", "--tp-size", "1", "--pp-size", "2", "--nnodes", "2", "--node-rank",
        std::to_string(c.rank), "--dist-init-addr", "10.231.0.1:29500", "--host", "127.0.0.1", "--port", "30000",
        "--disable-radix-cache", "--enable-cache-report", "--disable-cuda-graph", "--disable-piecewise-cuda-graph",
        "--disable-overlap-schedule", "--chunked-prefill-size", "-1", "--attention-backend", "triton",
        "--sampling-backend", "pytorch", "--context-length", std::to_string(e.at("max_model_len").get<int>()),
        "--max-running-requests", "1", "--max-total-tokens", std::to_string(e.at("max_num_batched_tokens").get<int>()),
        "--mem-fraction-static", "0.90", "--enable-metrics", "--enable-metrics-for-all-schedulers",
        "--decode-log-interval", "1", "--watchdog-timeout", "30", "--log-level", "warning"};
    Json helper = {{"rank", c.rank}, {"config_digest", c.digest}, {"api_socket", socket},
                   {"health_socket", health_socket}, {"model_path", path}, {"model_manifest", e.at("model_manifest")},
                   {"backend_version", "0.5.10.post1"}, {"argv", argv}};
    return {PIPELINE_PYTHON,
            {PIPELINE_PYTHON, "-I", "-u", PIPELINE_SGLANG_HELPER, helper.dump()},
            {"PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "HOME=" + dir,
             "TMPDIR=" + dir, "CUDA_VISIBLE_DEVICES=0", "HF_HUB_OFFLINE=1", "HF_DATASETS_OFFLINE=1",
             "TOKENIZERS_PARALLELISM=false", "OMP_NUM_THREADS=4", "PYTHONUNBUFFERED=1", "NCCL_NET=Socket",
             "NCCL_SOCKET_IFNAME==wg0", "GLOO_SOCKET_IFNAME=wg0", "NCCL_SOCKET_FAMILY=AF_INET",
             "NCCL_IB_DISABLE=1", "NCCL_P2P_DISABLE=1", "NCCL_SHM_DISABLE=1", "NCCL_DEBUG=INFO",
             "NCCL_DEBUG_SUBSYS=INIT,NET", "TORCH_NCCL_ASYNC_ERROR_HANDLING=1",
             "TORCH_NCCL_WAIT_TIMEOUT_DUMP_MILSEC=1000",
             "LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64",
             "SGLANG_HOST_IP=" + std::string(c.rank == 0 ? "10.231.0.1" : "10.231.0.2")},
            dir + "/backend.log", c.rank == 0 ? socket : health_socket, health_socket};
  }
  ProbeRequest health() const override {
    return {"GET", "/health", ""};
  }
  ProbeRequest warmup(const Config &c) const override {
    // Non-head SGLang has only a dummy health server. Only the head can
    // exercise the complete model; group readiness requires BOTH local checks.
    if (c.rank != 0)
      return health();
    return {"POST", "/v1/chat/completions",
            Json({{"model", c.effective.at("api_model")}, {"messages", {{{"role", "user"}, {"content", "Say hello."}}}},
                  {"max_tokens", 4}, {"temperature", 0}, {"stream", false},
                  {"chat_template_kwargs", {{"enable_thinking", false}}}}).dump()};
  }
  bool valid_health(const Config &c, const Json &body) const override {
    return body.at("status") == "ok" && body.at("rank") == c.rank && body.at("config_digest") == c.digest &&
           body.at("backend_version") == "0.5.10.post1" && body.at("backend_alive") == true;
  }
  bool valid_warmup(const Config &c, const Json &body) const override {
    if (c.rank != 0)
      return valid_health(c, body);
    const auto &choices = body.at("choices");
    const auto &usage = body.at("usage");
    return body.at("model") == c.effective.at("api_model") && choices.is_array() && choices.size() == 1 &&
           choices.at(0).at("message").at("content").is_string() &&
           !choices.at(0).at("message").at("content").get<std::string>().empty() &&
           (choices.at(0).at("finish_reason") == "stop" || choices.at(0).at("finish_reason") == "length") &&
           usage.at("prompt_tokens").is_number_integer() && usage.at("prompt_tokens") > 0 &&
           usage.at("completion_tokens").is_number_integer() && usage.at("completion_tokens") > 0 &&
           usage.at("completion_tokens") <= 4;
  }
};
}  // namespace

bool is_sglang_profile(const std::string &id) {
  return id == "sglang-qwen3-0.6b-dev-pp2-wg-v1" || id == "sglang-qwen3-14b-dev-pp2-wg-v1";
}

void configure_sglang_profile(Profile &p) {
  p.backend = "sglang";
  p.wireguard = true;
  p.startup_ms = 900000;
  p.warmup_ms = 90000;
  p.formation_ms = 1200000;
  p.probe_ms = 2000;
  p.probe_interval_ms = 500;
  p.watchdog_ms = 8000;
  p.heartbeat_ms = 500;
  p.lease_ms = 5000;
  p.stop_ms = 10000;
  p.kill_ms = 5000;
  p.restart_ms = 1000;
}

void configure_sglang_effective(Json &e) {
  auto manifest = Json::parse(sglang_models_json).at(e.at("profile_id") == "sglang-qwen3-14b-dev-pp2-wg-v1" ? "large" : "small");
  const auto &m = manifest.at("model");
  e["model_manifest"] = manifest;
  e["model_identifier"] = m.at("id").get<std::string>() + "@" + m.at("revision").get<std::string>();
  e["api_model"] = m.at("id");
  e["model_verification"] = "compiled-sha256-manifest-readonly-dev";
  e["model_architecture"] = "Qwen3ForCausalLM";
  e["backend_kind"] = "sglang";
  e["backend_oci_digest"] = image;
  e["backend_artifact"] = "sglang-0.5.10.post1-cu130";
  e["runtime_compatibility_id"] = "sglang-uds-helper-v1";
  const int layers = m.at("layers").get<int>();
  e["layer_partition"] = {{0, layers / 2}, {layers / 2, layers}};
  e["dtype"] = "bfloat16";
  e["tokenizer_identity"] = manifest.at("files").at("tokenizer.json").at("sha256");
  e["chat_template_identity"] = manifest.at("files").at("tokenizer_config.json").at("sha256");
  e["gpu_memory_policy"] = "fraction-0.90-no-cpu-offload";
  e["validated_backend_options"] = {{"chunked_prefill_size", -1}, {"cuda_graph", false},
                                    {"overlap_schedule", false}, {"attention_backend", "triton"},
                                    {"sampling_backend", "pytorch"}, {"metrics_all_schedulers", true},
                                    {"decode_log_interval", 1}, {"watchdog_seconds", 30}};
  e["capabilities"] = {"text-json", "text-sse", "usage", "cancel-by-disconnect-and-rid-abort"};
}

std::unique_ptr<BackendAdapter> make_sglang_adapter() {
  return std::make_unique<SglangAdapter>();
}
}  // namespace cocoon::pipeline
