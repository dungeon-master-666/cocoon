#pragma once
#include "pipeline/Backend.h"

namespace cocoon::pipeline {
bool is_sglang_profile(const std::string &id);
void configure_sglang_profile(Profile &profile);
void configure_sglang_effective(Json &effective);
std::unique_ptr<BackendAdapter> make_sglang_adapter();
}  // namespace cocoon::pipeline
