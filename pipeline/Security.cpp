#include "pipeline/Security.h"
#include "td/utils/misc.h"
#include "td/utils/crypto.h"
#include "td/utils/Random.h"
#include <openssl/pem.h>
#include <openssl/rand.h>
#include <stdexcept>

namespace cocoon::pipeline {
namespace {
tdx::RATLSAttestationReport fixture() {
  tdx::RATLSAttestationReport report{};
  auto marker = td::sha512("cocoon-pipeline-dev-evidence-v1:NOT-HARDWARE-ATTESTATION");
  report.mr_owner.as_mutable_slice().copy_from(td::Slice(marker).substr(0, 48));
  return report;
}

class DevTee final : public TeeInterface {
 public:
  explicit DevTee(std::string variant) : variant_(std::move(variant)) {
  }
  td::Result<RATLSAttestationReport> make_report(const td::UInt512 &claims) const override {
    auto report = fixture();
    report.reportdata = claims;
    if (variant_ == "wrong-image") {
      report.mr_td.as_mutable_slice()[0] = 1;
    }
    if (variant_ == "wrong-key") {
      report.reportdata = td::UInt512::zero();
    }
    return report;
  }
  td::Status prepare_cert_config(TeeCertConfig &config, const tde2e_core::PublicKey &key) const override {
    TRY_RESULT(report, make_report(hash_public_key(key)));
    config.extra_extensions.emplace_back(tdx::OID::TDX_QUOTE.c_str(), td::serialize(report.as_tdx()));
    config.extra_extensions.emplace_back(tdx::OID::TDX_USER_CLAIMS.c_str(), key.to_secure_string().as_slice().str());
    return td::Status::OK();
  }

 private:
  std::string variant_;
};

// Unlike the generic fake verifier, this dev verifier checks the actual fixture
// bytes and key binding. It still makes no claim about hardware measurements.
class DevEvidence final : public RATLSInterface {
 public:
  td::Result<tdx::RATLSAttestationReport> attest(const td::UInt512 &claims,
                                                 const tdx::RATLSExtensions &ext) const override {
    if (!ext.quote || !ext.user_claims || ext.quote->size() > 4096 || ext.user_claims->size() != 32) {
      return td::Status::Error("missing or invalid dev evidence");
    }
    tdx::RATLSAttestationReport report;
    TRY_STATUS(td::unserialize(report, *ext.quote));
    td::UInt512 supplied;
    td::sha512(*ext.user_claims, supplied.as_mutable_slice());
    if (report.mr_owner != fixture().mr_owner || supplied != claims || report.reportdata != claims) {
      return td::Status::Error("dev evidence is not bound to the TLS public key");
    }
    return report;
  }
  td::Result<sev::RATLSAttestationReport> attest(const td::UInt512 &, const sev::RATLSExtensions &) const override {
    return td::Status::Error("SEV evidence is not supported by the dev pipeline policy");
  }
};

class RejectProduction final : public RATLSPolicy {
 public:
  td::Result<RATLSAttestationReport> validate(const tde2e_core::PublicKey &) const override {
    return td::Status::Error("no approved production pipeline attestation profile; dev evidence forbidden");
  }
  td::Result<RATLSAttestationReport> validate(const tde2e_core::PublicKey &key,
                                              const tdx::RATLSExtensions &) const override {
    return validate(key);
  }
  td::Result<RATLSAttestationReport> validate(const tde2e_core::PublicKey &key,
                                              const sev::RATLSExtensions &) const override {
    return validate(key);
  }
};

class RecordingPolicy final : public RATLSPolicy {
 public:
  RecordingPolicy(RATLSPolicyRef policy, std::shared_ptr<std::optional<PeerIdentity>> result)
      : policy_(std::move(policy)), result_(std::move(result)) {
  }
  td::Result<RATLSAttestationReport> validate(const tde2e_core::PublicKey &key) const override {
    return record(key, policy_->validate(key));
  }
  td::Result<RATLSAttestationReport> validate(const tde2e_core::PublicKey &key,
                                              const tdx::RATLSExtensions &ext) const override {
    return record(key, policy_->validate(key, ext));
  }
  td::Result<RATLSAttestationReport> validate(const tde2e_core::PublicKey &key,
                                              const sev::RATLSExtensions &ext) const override {
    return record(key, policy_->validate(key, ext));
  }

 private:
  td::Result<RATLSAttestationReport> record(const tde2e_core::PublicKey &key,
                                            td::Result<RATLSAttestationReport> result) const {
    if (result.is_ok()) {
      *result_ =
          PeerIdentity{td::hex_encode(key.to_secure_string()), td::hex_encode(result.ok().image_hash().as_slice())};
    }
    return result;
  }
  RATLSPolicyRef policy_;
  std::shared_ptr<std::optional<PeerIdentity>> result_;
};
}  // namespace

std::string random_id() {
  unsigned char bytes[32];
  if (RAND_bytes(bytes, sizeof(bytes)) != 1) {
    throw std::runtime_error("secure random generation failed");
  }
  return td::hex_encode(td::Slice(bytes, sizeof(bytes)));
}

std::string dev_image_hash() {
  return td::hex_encode(tdx::image_hash(fixture()).as_slice());
}

RATLSPolicyRef peer_policy(SecurityMode mode) {
  if (mode == SecurityMode::Production) {
    // Fail closed until a measured production profile supplies real verifier,
    // nonempty image allowlist and approved collateral roots (hardware Gate E).
    return std::make_shared<RejectProduction>();
  }
  RATLSPolicyConfig config;
  config.tdx_config.allowed_image_hashes = {tdx::image_hash(fixture())};
  return RATLSPolicy::make(std::make_shared<DevEvidence>(), std::move(config));
}

TeeCertAndKey dev_certificate(const std::string &variant) {
  DevTee tee(variant);
  TeeCertConfig config;
  config.organization = "Cocoon pipeline DEV ONLY";
  config.validity_seconds = 3600;
  if (variant == "short-lived")
    config.validity_seconds = 8;
  if (variant == "expired") {
    config.current_time = static_cast<td::uint32>(std::time(nullptr) - 7200);
  }
  auto result = generate_cert_and_key(variant == "missing-evidence" ? nullptr : &tee, config);
  if (result.is_error()) {
    throw std::runtime_error(result.error().message().str());
  }
  return result.move_as_ok();
}

PeerIdentity certificate_identity(const TeeCertAndKey &certificate) {
  std::unique_ptr<BIO, decltype(&BIO_free)> bio(
      BIO_new_mem_buf(certificate.cert_pem().data(), static_cast<int>(certificate.cert_pem().size())), BIO_free);
  std::unique_ptr<X509, decltype(&X509_free)> cert(PEM_read_bio_X509(bio.get(), nullptr, nullptr, nullptr), X509_free);
  if (!cert)
    throw std::runtime_error("invalid local certificate");
  std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)> key(X509_get_pubkey(cert.get()), EVP_PKEY_free);
  unsigned char raw[32];
  size_t size = sizeof(raw);
  if (!key || EVP_PKEY_get_base_id(key.get()) != EVP_PKEY_ED25519 ||
      EVP_PKEY_get_raw_public_key(key.get(), raw, &size) != 1 || size != sizeof(raw)) {
    throw std::runtime_error("local certificate must use Ed25519");
  }
  return {td::hex_encode(td::Slice(raw, size)), dev_image_hash()};
}

Identity make_identity(const std::string &base) {
  auto cert = base.empty() ? td::Result<TeeCertAndKey>(dev_certificate()) : load_cert_and_key(base);
  if (cert.is_error())
    throw std::runtime_error(cert.error().message().str());
  auto identity = certificate_identity(cert.ok());
  return {cert.move_as_ok(), std::move(identity)};
}

void configure_tls(boost::asio::ssl::context &context, const Identity &identity) {
  namespace ssl = boost::asio::ssl;
  context.use_certificate_chain(boost::asio::buffer(identity.certificate.cert_pem()));
  context.use_private_key(boost::asio::buffer(identity.certificate.key_pem()), ssl::context::pem);
  auto native = context.native_handle();
  if (SSL_CTX_check_private_key(native) != 1 || SSL_CTX_set_min_proto_version(native, TLS1_3_VERSION) != 1) {
    throw std::runtime_error("invalid TLS configuration");
  }
  SSL_CTX_set_options(native, SSL_OP_NO_TICKET);
  SSL_CTX_set_session_cache_mode(native, SSL_SESS_CACHE_OFF);
  SSL_CTX_set_num_tickets(native, 0);
  context.set_verify_mode(ssl::verify_peer | ssl::verify_fail_if_no_peer_cert);
  context.set_verify_depth(0);
}

std::function<bool(bool, boost::asio::ssl::verify_context &)> peer_verifier(
    SecurityMode mode, std::shared_ptr<std::optional<PeerIdentity>> result) {
  auto policy = std::make_shared<RecordingPolicy>(peer_policy(mode), std::move(result));
  auto callback = RATLSVerifyCallbackBuilder::from_policy(std::move(policy));
  return [callback](bool preverified, boost::asio::ssl::verify_context &context) {
    return callback(preverified, context.native_handle()) == 1;
  };
}

NetworkKey::NetworkKey() {
  std::unique_ptr<EVP_PKEY_CTX, decltype(&EVP_PKEY_CTX_free)> ctx(EVP_PKEY_CTX_new_id(EVP_PKEY_X25519, nullptr),
                                                                  EVP_PKEY_CTX_free);
  EVP_PKEY *raw = nullptr;
  if (!ctx || EVP_PKEY_keygen_init(ctx.get()) != 1 || EVP_PKEY_keygen(ctx.get(), &raw) != 1) {
    throw std::runtime_error("network key generation failed");
  }
  key_.reset(raw);
  unsigned char public_key[32];
  size_t size = sizeof(public_key);
  if (EVP_PKEY_get_raw_public_key(raw, public_key, &size) != 1 || size != sizeof(public_key)) {
    throw std::runtime_error("network public key extraction failed");
  }
  public_key_ = td::hex_encode(td::Slice(public_key, size));
}
}  // namespace cocoon::pipeline
