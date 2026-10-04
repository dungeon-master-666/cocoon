#include "runners/helpers/ValidateRequest.h"

#include "Ed25519.h"
#include <iostream>
#include <stdexcept>
#include <string>

namespace {
void require(bool condition, const char *message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

td::Bits256 key(char value) {
  td::Bits256 result;
  std::fill(result.as_slice().begin(), result.as_slice().end(), value);
  return result;
}
td::Bits256 public_key(td::Bits256 secret) {
  td::Ed25519::PrivateKey private_key(td::SecureString(secret.as_slice()));
  td::Bits256 result;
  result.as_slice().copy_from(private_key.get_public_key().move_as_ok().as_octet_string());
  return result;
}

void test_answer(bool sse, size_t fragment_size, bool encrypted, const std::string &newline) {
  cocoon::AnswerPostprocessor processor(1000, 10000, 10000, 10000, 10000, 2, encrypted ? key(1) : td::Bits256::zero(),
                                        encrypted ? public_key(key(2)) : td::Bits256::zero());
  processor.set_sse(sse);
  const std::string body =
      R"({"choices":[{"delta":{"content":"hello"}}],"usage":{"prompt_tokens":34,"completion_tokens":100,"total_tokens":134,"prompt_tokens_details":{"cached_tokens":11},"completion_tokens_details":{"reasoning_tokens":10}}})";
  const std::string input =
      sse ? "data: " + body + newline + newline + "data: [DONE]" + newline + newline : body + "\n";
  std::string output;
  for (size_t pos = 0; pos < input.size(); pos += fragment_size) {
    output += processor.add_next_answer_slice(td::Slice(input).substr(pos, fragment_size)).move_as_ok();
  }
  require(output.find("[DONE]") == std::string::npos, "terminal emitted before HTTP completion");
  if (!sse)
    require(output.empty(), "JSON emitted before HTTP completion");
  output += processor.finalize().move_as_ok();
  require(processor.finalize().is_error(), "duplicate finalize accepted");
  if (sse) {
    require(output.find("data: ") == 0, "missing SSE data");
    auto end = output.find("\n\n");
    require(end != std::string::npos, "missing SSE event boundary");
    require(output.substr(end + 2) == "data: [DONE]\n\n", "missing or duplicated terminal event");
    output = output.substr(6, end - 6);
  }
  auto answer = nlohmann::json::parse(output);
  if (encrypted) {
    require(output.find("hello") == std::string::npos, "plaintext leaked");
    auto sender = public_key(key(1));
    cocoon::decrypt_json(answer, key(2), sender, true, false).ensure();
  }
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
void test_invalid(bool sse, const std::string &input, size_t fragment_size) {
  cocoon::AnswerPostprocessor p(1000, 10000, 10000, 10000, 10000, 2, td::Bits256::zero(), td::Bits256::zero());
  p.set_sse(sse);
  std::string emitted;
  for (size_t pos = 0; pos < input.size(); pos += fragment_size) {
    auto result = p.add_next_answer_slice(td::Slice(input).substr(pos, fragment_size));
    if (result.is_error())
      break;
    emitted += result.move_as_ok();
  }
  auto result = p.finalize();
  require(result.is_error(), "invalid response finalized successfully");
  require(p.finalize().is_error(), "failure was not sticky");
  require(emitted.find("[DONE]") == std::string::npos, "failed answer emitted terminal");
}

void test_error_encryption() {
  cocoon::AnswerPostprocessor p(1000, 10000, 10000, 10000, 10000, 2, key(1), public_key(key(2)));
  p.set_sse(true);
  auto r = p.add_next_answer_slice("event: error\ndata: {\"error\":{\"message\":\"private error detail\"}}\n\n");
  require(r.is_error(), "backend error accepted");
  require(r.error().message().str().find("private error detail") == std::string::npos,
          "error detail leaked to control");
  auto payload = p.error_payload();
  require(payload.find("private error detail") == std::string::npos, "error detail leaked to payload");
  auto value = nlohmann::json::parse(payload.substr(payload.find("data: ") + 6));
  auto sender = public_key(key(1));
  cocoon::decrypt_json(value, key(2), sender, true, false).ensure();
  require(value["error"]["message"] == "private error detail", "encrypted error detail lost");
}

void test_sse_fields(size_t fragment) {
  cocoon::AnswerPostprocessor p(1000, 10000, 10000, 10000, 10000, 2, td::Bits256::zero(), td::Bits256::zero());
  p.set_sse(true);
  const std::string input =
      "\xEF\xBB\xBF: keepalive\r\n\r\nevent: message\nid: 7\ndata: {\"choices\":[],\ndata: \"usage\":null}\n\ndata: "
      "[DONE]\n\n";
  std::string output;
  for (size_t pos = 0; pos < input.size(); pos += fragment) {
    output += p.add_next_answer_slice(td::Slice(input).substr(pos, fragment)).move_as_ok();
  }
  output += p.finalize().move_as_ok();
  require(output == ": keepalive\n\nevent: message\nid: 7\ndata: {\"choices\":[],\"usage\":null}\n\ndata: [DONE]\n\n",
          "SSE fields/multiline data changed");
}

void test_transcription(size_t fragment, bool encrypted) {
  cocoon::AnswerPostprocessor p(1000, 10000, 10000, 10000, 10000, 2, encrypted ? key(1) : td::Bits256::zero(),
                                encrypted ? public_key(key(2)) : td::Bits256::zero());
  p.set_sse(true);
  p.set_transcription(true);
  const std::string input =
      "event: transcript.text.done\ndata: {\"type\":\"transcript.text.done\",\"text\":\"hello\"}\n\n";
  for (size_t pos = 0; pos < input.size(); pos += fragment) {
    require(p.add_next_answer_slice(td::Slice(input).substr(pos, fragment)).move_as_ok().empty(),
            "transcription terminal emitted before HTTP completion");
  }
  auto output = p.finalize().move_as_ok();
  auto value = nlohmann::json::parse(output.substr(output.find("data: ") + 6));
  if (encrypted) {
    require(output.find("hello") == std::string::npos, "transcription plaintext leaked");
    auto sender = public_key(key(1));
    cocoon::decrypt_json(value, key(2), sender, true, false).ensure();
  }
  require(value["text"] == "hello" && value["type"] == "transcript.text.done", "transcription changed");
  require(p.finalize().is_error(), "transcription completed twice");

  cocoon::AnswerPostprocessor bad(1000, 10000, 10000, 10000, 10000, 2, td::Bits256::zero(), td::Bits256::zero());
  bad.set_sse(true);
  bad.set_transcription(true);
  bad.add_next_answer_slice("data: {\"type\":\"transcript.text.delta\",\"delta\":\"hello\"}\n\n").ensure();
  require(bad.finalize().is_error(), "incomplete transcription accepted");
}

}  // namespace

int main() {
  try {
    for (bool sse : {false, true}) {
      for (size_t fragment : {size_t(1), size_t(7), size_t(4096)}) {
        for (bool encrypted : {false, true})
          for (const std::string &newline : {"\n", "\r\n", "\r"})
            test_answer(sse, fragment, encrypted, newline);
      }
    }
    for (size_t fragment : {size_t(1), size_t(7), size_t(4096)}) {
      test_sse_fields(fragment);
      test_transcription(fragment, false);
      test_transcription(fragment, true);
      test_invalid(true, "event: error\ndata: \xFF\n\n", fragment);
      for (const std::string &body : {"", " ", "{", "{\"x\":", "{}garbage", "{}{}", "[]", "null",
                                      "{\"error\":{\"message\":\"bad\"}}", "{\"type\":\"error\",\"message\":\"bad\"}"})
        test_invalid(false, body, fragment);
      for (const std::string &body :
           {"", "data: {}\n\n", "data: [DONE]\n", "data: {bad}\n\ndata: [DONE]\n\n",
            "data: {}\n\ndata: [DONE]\n\ndata: [DONE]\n\n", "data: [DONE]\n\ndata: {}\n\n",
            "data: {\"error\":{\"message\":\"bad\"}}\n\ndata: [DONE]\n\n", "event: error\ndata: failure\n\n",
            "data: {\"type\":\"error\",\"message\":\"bad\"}\n\n",
            "data: {\"type\":\"transcript.text.done\",\"text\":\"hello\"}\n\n"})
        test_invalid(true, body, fragment);
    }
    test_error_encryption();
    std::cout << "PASS: JSON/SSE content and usage across fragmented HTTP reads\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
