#pragma once
#include "pipeline/Backend.h"

namespace cocoon::pipeline {
bool is_vllm_profile(const std::string &id);
void configure_vllm_profile(Profile &profile);
void configure_vllm_effective(Json &effective);
std::unique_ptr<BackendAdapter> make_vllm_adapter();
}  // namespace cocoon::pipeline
