#pragma once
#include "pipeline/Profile.h"
#include "tee/cocoon/Tee.h"
#include <boost/asio/ssl.hpp>
#include <openssl/evp.h>

namespace cocoon::pipeline {
struct PeerIdentity {
  std::string public_key;
  std::string image_hash;
  bool operator==(const PeerIdentity &) const = default;
};

struct Identity {
  TeeCertAndKey certificate;
  PeerIdentity peer;
};

std::string random_id();
std::string dev_image_hash();
RATLSPolicyRef peer_policy(SecurityMode mode);
Identity make_identity(const std::string &certificate_base);
TeeCertAndKey dev_certificate(const std::string &variant = "valid");
PeerIdentity certificate_identity(const TeeCertAndKey &certificate);
void configure_tls(boost::asio::ssl::context &context, const Identity &identity);
std::function<bool(bool, boost::asio::ssl::verify_context &)> peer_verifier(
    SecurityMode mode, std::shared_ptr<std::optional<PeerIdentity>> result);

class NetworkKey {
 public:
  NetworkKey();
  const std::string &public_key() const {
    return public_key_;
  }

 private:
  std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)> key_{nullptr, EVP_PKEY_free};
  std::string public_key_;
};
}  // namespace cocoon::pipeline
