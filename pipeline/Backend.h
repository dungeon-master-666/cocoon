#pragma once

#include "pipeline/Process.h"
#include <memory>

namespace cocoon::pipeline {
struct ProbeRequest {
  std::string method;
  std::string target;
  std::string body;
};

// Process ownership stays in the supervisor; the adapter describes launches and
// backend-specific health/warmup semantics. It never declares group readiness.
class BackendAdapter {
 public:
  virtual ~BackendAdapter() = default;
  virtual LaunchPlan build_launch_plan(const Config &config, const std::string &run_dir) const = 0;
  virtual ProbeRequest health() const = 0;
  virtual ProbeRequest warmup(const Config &config) const = 0;
  virtual bool valid_health(const Config &config, const Json &body) const = 0;
  virtual bool valid_warmup(const Config &config, const Json &body) const = 0;
};
std::unique_ptr<BackendAdapter> make_adapter(const Config &config);
}  // namespace cocoon::pipeline
