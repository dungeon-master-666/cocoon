#include "pipeline/Vllm.h"
#include "sglang-models.h"

namespace cocoon::pipeline {
namespace {
constexpr auto image = "vllm/vllm-openai:v0.29.0-cu129@sha256:"
                       "7ef5a35d1ef8ce2cf9d671dd91eec6e367c5849262e0362b4d3d4a26be0d87d2";
class VllmAdapter final : public BackendAdapter {
 public:
  LaunchPlan build_launch_plan(const Config &c, const std::string &dir) const override {
    const auto &e = c.effective;
    const auto model = e.at("model_manifest").at("model");
    const auto name = model.at("id").get<std::string>();
    const auto path = "/models/" + name.substr(name.find('/') + 1) + "/" + model.at("revision").get<std::string>();
    const auto socket = dir + "/backend.sock", health_socket = dir + "/health.sock";
    std::vector<std::string> argv = {
        "/usr/local/bin/vllm", "serve", path, "--served-model-name", name, "--dtype", "bfloat16",
        "--distributed-executor-backend", "mp", "--tensor-parallel-size", "1", "--pipeline-parallel-size", "2",
        "--nnodes", "2", "--node-rank", std::to_string(c.rank), "--master-addr", "10.231.0.1",
        "--master-port", "29501", "--max-model-len", std::to_string(e.at("max_model_len").get<int>()),
        "--max-num-seqs", "1", "--max-num-batched-tokens", std::to_string(e.at("max_num_batched_tokens").get<int>()),
        "--gpu-memory-utilization", "0.90", "--cpu-offload-gb", "0", "--enforce-eager",
        "--no-enable-prefix-caching", "--enable-prompt-tokens-details", "--no-enable-log-requests"};
    if (c.rank == 0)
      argv.insert(argv.end(), {"--host", "127.0.0.1", "--port", "30000", "--middleware", "vllm_control.CancellationMiddleware"});
    else
      argv.push_back("--headless");
    Json helper = {{"backend", "vllm"}, {"rank", c.rank}, {"config_digest", c.digest}, {"api_socket", socket},
                   {"health_socket", health_socket}, {"model_path", path}, {"model_manifest", e.at("model_manifest")},
                   {"backend_version", "0.29.0+cu129"}, {"argv", argv}};
    return {PIPELINE_PYTHON,
            {PIPELINE_PYTHON, "-I", "-u", PIPELINE_VLLM_HELPER, helper.dump()},
            {"PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "HOME=" + dir,
             "TMPDIR=" + dir, "CUDA_VISIBLE_DEVICES=0", "HF_HUB_OFFLINE=1", "HF_DATASETS_OFFLINE=1",
             "TOKENIZERS_PARALLELISM=false", "OMP_NUM_THREADS=4", "PYTHONUNBUFFERED=1", "NCCL_NET=Socket",
             "NCCL_SOCKET_IFNAME==wg0", "GLOO_SOCKET_IFNAME=wg0", "NCCL_SOCKET_FAMILY=AF_INET",
             "NCCL_IB_DISABLE=1", "NCCL_P2P_DISABLE=1", "NCCL_SHM_DISABLE=1", "NCCL_DEBUG=INFO",
             "NCCL_DEBUG_SUBSYS=INIT,NET", "TORCH_NCCL_ASYNC_ERROR_HANDLING=1",
             "TORCH_NCCL_WAIT_TIMEOUT_DUMP_MILSEC=1000",
             "LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64",
             "PYTHONPATH=" PIPELINE_HELPER_DIR, "VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30",
             "VLLM_NO_USAGE_STATS=1", "DO_NOT_TRACK=1", "VLLM_LOG_STATS_INTERVAL=1",
             "VLLM_HOST_IP=" + std::string(c.rank == 0 ? "10.231.0.1" : "10.231.0.2")},
            dir + "/backend.log", c.rank == 0 ? socket : health_socket, health_socket};
  }
  ProbeRequest health() const override {
    return {"GET", "/health", ""};
  }
  ProbeRequest warmup(const Config &c) const override {
    // The headless member has no HTTP API. Only the head can exercise the
    // complete model; group readiness requires BOTH local checks.
    if (c.rank != 0)
      return health();
    return {"POST", "/v1/chat/completions",
            Json({{"model", c.effective.at("api_model")}, {"messages", {{{"role", "user"}, {"content", "Say hello."}}}},
                  {"max_tokens", 4}, {"temperature", 0}, {"stream", false},
                  {"chat_template_kwargs", {{"enable_thinking", false}}}}).dump()};
  }
  bool valid_health(const Config &c, const Json &body) const override {
    return body.at("status") == "ok" && body.at("rank") == c.rank && body.at("config_digest") == c.digest &&
           body.at("backend_version") == "0.29.0+cu129" && body.at("backend_alive") == true;
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

bool is_vllm_profile(const std::string &id) {
  return id == "vllm-qwen3-0.6b-dev-pp2-wg-v1" || id == "vllm-qwen3-14b-dev-pp2-wg-v1";
}

void configure_vllm_profile(Profile &p) {
  p.backend = "vllm";
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

void configure_vllm_effective(Json &e) {
  auto manifest = Json::parse(sglang_models_json).at(e.at("profile_id") == "vllm-qwen3-14b-dev-pp2-wg-v1" ? "large" : "small");
  const auto &m = manifest.at("model");
  e["model_manifest"] = manifest;
  e["model_identifier"] = m.at("id").get<std::string>() + "@" + m.at("revision").get<std::string>();
  e["api_model"] = m.at("id");
  e["model_verification"] = "compiled-sha256-manifest-readonly-dev";
  e["model_architecture"] = "Qwen3ForCausalLM";
  e["backend_kind"] = "vllm";
  e["backend_oci_digest"] = image;
  e["backend_artifact"] = "vllm-0.29.0+cu129";
  e["runtime_compatibility_id"] = "vllm-uds-helper-v1";
  const int layers = m.at("layers").get<int>();
  e["layer_partition"] = {{0, layers / 2}, {layers / 2, layers}};
  e["dtype"] = "bfloat16";
  e["tokenizer_identity"] = manifest.at("files").at("tokenizer.json").at("sha256");
  e["chat_template_identity"] = manifest.at("files").at("tokenizer_config.json").at("sha256");
  e["gpu_memory_policy"] = "fraction-0.90-no-cpu-offload";
  e["validated_backend_options"] = {{"executor", "mp"}, {"enforce_eager", true},
                                    {"cpu_offload_gb", 0}, {"rpc_timeout_seconds", 30},
                                    {"member_headless", true}, {"prompt_tokens_details", true}};
  e["capabilities"] = {"text-json", "text-sse", "usage", "cancel-by-disconnect-and-task-abort"};
}

std::unique_ptr<BackendAdapter> make_vllm_adapter() {
  return std::make_unique<VllmAdapter>();
}
}  // namespace cocoon::pipeline
