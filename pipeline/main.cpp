#include "pipeline/Agent.h"

#include <filesystem>
#include <iostream>
#include <sys/stat.h>

using namespace cocoon::pipeline;

int main(int argc, char **argv) {
  std::string config_path, run_dir;
  bool check_only = false;
#ifdef COCOON_PIPELINE_DEV
  constexpr auto policy = SecurityMode::Dev;
#else
  constexpr auto policy = SecurityMode::Production;
#endif
  try {
    for (int i = 1; i < argc; ++i) {
      const std::string arg = argv[i];
      if (arg == "--help") {
        std::cout << "pipeline-agent --config FILE [--check-config | --run-dir NEW_DIRECTORY]\n"
                     "Local supervisor only; group readiness requires membership and networking.\n"
                  << "Build policy: " << (policy == SecurityMode::Dev ? "dev" : "production") << '\n';
        return 0;
      } else if (arg == "--check-config") {
        check_only = true;
      } else if ((arg == "--config" || arg == "--run-dir") && i + 1 < argc) {
        auto &value = arg == "--config" ? config_path : run_dir;
        if (!value.empty()) {
          throw std::runtime_error("duplicate option: " + arg);
        }
        value = argv[++i];
      } else {
        throw std::runtime_error("unsupported option: " + arg);
      }
    }
    if (config_path.empty() || (check_only && !run_dir.empty()) || (!check_only && run_dir.empty())) {
      throw std::runtime_error("use --config FILE with either --check-config or --run-dir NEW_DIRECTORY");
    }
    auto config = validate_config(read_config(config_path), policy);
    if (check_only) {
      std::cout << Json({{"effective_config", config.effective},
                         {"config_digest", config.digest},
                         {"rank", config.rank},
                         {"role", config.role}})
                       .dump(2)
                << '\n';
      return 0;
    }
    // No existing runtime directory is reused or removed, including after a crash.
    // Validate before creating files or launching any backend.
    run_dir = std::filesystem::absolute(run_dir).lexically_normal().string();
    if (run_dir.size() + std::string("/control.sock").size() >= 104) {
      throw std::runtime_error("run directory path is too long for portable Unix sockets");
    }
    umask(0077);
    if (mkdir(run_dir.c_str(), 0700) < 0) {
      throw std::runtime_error("run directory must be new and its parent must exist");
    }
    std::signal(SIGPIPE, SIG_IGN);
    std::signal(SIGTERM, [](int) { stop_requested.store(true, std::memory_order_relaxed); });
    std::signal(SIGINT, [](int) { stop_requested.store(true, std::memory_order_relaxed); });
    int exit_code = 0;
    td::actor::Scheduler scheduler({1});
    scheduler.run_in_context(
        [&] { td::actor::create_actor<Agent>("pipeline-agent", std::move(config), run_dir, &exit_code).release(); });
    scheduler.run();
    return exit_code;
  } catch (const std::exception &error) {
    std::cerr << "pipeline-agent: " << error.what() << '\n';
    return 1;
  }
}
