#include "runners/helpers/ValidateRequest.h"

#include <iostream>
#include <stdexcept>
#include <string>

namespace {
void require(bool condition, const char *message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

void test_answer(bool sse, size_t fragment_size) {
  cocoon::AnswerPostprocessor processor(1000, 10000, 10000, 10000, 10000, 2, td::Bits256::zero(), td::Bits256::zero());
  processor.set_sse(sse);
  const std::string body = R"({"choices":[{"delta":{"content":"hello"}}],"usage":{"prompt_tokens":34,"completion_tokens":100,"total_tokens":134,"prompt_tokens_details":{"cached_tokens":11},"completion_tokens_details":{"reasoning_tokens":10}}})";
  const std::string input = sse ? "data: " + body + "\r\n\r\ndata: [DONE]\r\n\r\n" : body + "\n";
  std::string output;
  for (size_t pos = 0; pos < input.size(); pos += fragment_size) {
    output += processor.add_next_answer_slice(td::Slice(input).substr(pos, fragment_size));
  }
  output += processor.finalize();
  if (sse) {
    require(output.find("data: ") == 0, "missing SSE data");
    auto end = output.find("\n\n");
    require(end != std::string::npos, "missing SSE event boundary");
    require(output.substr(end + 2) == "data: [DONE]\n\n", "missing or duplicated terminal event");
    output = output.substr(6, end - 6);
  }
  auto answer = nlohmann::json::parse(output);
  require(answer["choices"][0]["delta"]["content"] == "hello", "content changed");
  require(answer["usage"]["prompt_tokens"] == 34, "prompt usage changed");
  require(answer["usage"]["completion_tokens"] == 100, "completion usage changed");
  require(answer["usage"]["total_cost"] == 268, "incorrect cost");
  auto usage = processor.usage();
  require(usage->prompt_tokens_used_ == 23 && usage->cached_tokens_used_ == 11 &&
              usage->completion_tokens_used_ == 90 && usage->reasoning_tokens_used_ == 10 &&
              usage->total_tokens_used_ == 134,
          "incorrect accounting");
}
}  // namespace

int main() {
  try {
    for (bool sse : {false, true}) {
      for (size_t fragment : {size_t(1), size_t(7), size_t(4096)}) {
        test_answer(sse, fragment);
      }
    }
    std::cout << "PASS: JSON/SSE content and usage across fragmented HTTP reads\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
