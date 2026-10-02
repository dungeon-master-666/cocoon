#include "common/bitstring.h"
#include "td/utils/filesystem.h"
#include "td/utils/misc.h"
#include "td/utils/port/signals.h"
#include "td/utils/OptionParser.h"
#include "helpers/ValidateRequest.h"
#include <nlohmann/json.hpp>
#include <iostream>
#include <ostream>
#include <sstream>

int main(int argc, char **argv) {
  SET_VERBOSITY_LEVEL(verbosity_INFO);

  bool decrypt_mode_ = false;
  std::string private_key_file_;
  td::Bits256 public_key = td::Bits256::zero();

  td::set_default_failure_signal_handler().ensure();

  td::OptionParser option_parser;
  option_parser.add_option('d', "decrypt", "decrypt mode", [&]() { decrypt_mode_ = true; });
  option_parser.add_option('k', "private-key", "private key file", [&](td::Slice v) { private_key_file_ = v.str(); });
  option_parser.add_checked_option('p', "public-key", "public key", [&](td::Slice v) {
    TRY_RESULT_ASSIGN(public_key, cocoon::parse_bits256_from_json(v));
    return td::Status::OK();
  });
  option_parser.run(argc, argv, 0).ensure();

  td::Bits256 private_key;
  {
    auto R = td::read_file_str(private_key_file_);
    if (R.is_error()) {
      LOG(FATAL) << "cannot read file '" << private_key_file_ << "'";
    }
    auto res = R.move_as_ok();
    if (res.size() != 32) {
      LOG(FATAL) << "private key in '" << private_key_file_ << "' must be 32 bytes long";
    }
    private_key.as_slice().copy_from(res);
  }

  std::string input{std::istreambuf_iterator<char>(std::cin), std::istreambuf_iterator<char>()};
  std::stringstream ss(input);
  while (true) {
    try {
      nlohmann::json v;
      ss >> v;
      if (decrypt_mode_) {
        auto S = cocoon::decrypt_json(v, private_key, public_key, true, false);
        if (S.is_error()) {
          LOG(ERROR) << "failed to decrypt: " << S;
        }
      } else {
        cocoon::encrypt_json(v, private_key, public_key, true);
      }
      std::cout << v.dump() << "\n" << std::flush;
    } catch (...) {
      break;
    }
  }
  return 0;
}
