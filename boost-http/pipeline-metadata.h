#pragma once

namespace cocoon::http {
// Reserved local worker -> pipeline gate metadata. Never trust client values.
inline constexpr char pipeline_header_prefix[] = "x-cocoon-pipeline-";
inline constexpr char pipeline_request_id_header[] = "x-cocoon-pipeline-request-id";
inline constexpr char pipeline_timeout_header[] = "x-cocoon-pipeline-timeout-seconds";
}  // namespace cocoon::http
