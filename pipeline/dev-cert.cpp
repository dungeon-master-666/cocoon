#include "pipeline/Security.h"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <set>
#include <sys/stat.h>

using namespace cocoon::pipeline;
int main(int argc, char **argv) {
  try {
    if (argc < 2 || argc > 3)
      throw std::runtime_error(
          "usage: pipeline-dev-cert NEW_BASE [valid|wrong-image|wrong-key|missing-evidence|expired|short-lived]");
    std::string base = argv[1], variant = argc == 3 ? argv[2] : "valid";
    if (!std::set<std::string>{"valid", "wrong-image", "wrong-key", "missing-evidence", "expired", "short-lived"}.count(
            variant)) {
      throw std::runtime_error("unknown dev fixture");
    }
    for (const auto *suffix : {"_cert.pem", "_key.pem"}) {
      if (std::filesystem::exists(base + suffix))
        throw std::runtime_error("refusing to overwrite certificate files");
    }
    umask(0077);
    auto cert = dev_certificate(variant);
    std::ofstream certificate(base + "_cert.pem"), key(base + "_key.pem");
    certificate << cert.cert_pem();
    key << cert.key_pem();
    certificate.close();
    key.close();
    if (!certificate || !key)
      throw std::runtime_error("cannot write dev certificate");
    auto peer = certificate_identity(cert);
    std::cout
        << Json({{"identity", peer.public_key}, {"expected_dev_image", dev_image_hash()}, {"variant", variant}}).dump()
        << '\n';
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
