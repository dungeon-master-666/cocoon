#include "tee/cocoon/Tee.h"

#if defined(__i386__) || defined(__x86_64__)
#include <cpuid.h>
#endif

#include <openssl/bn.h>
#include <openssl/pem.h>
#include <openssl/rand.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

#include "td/utils/Status.h"
#include "td/utils/filesystem.h"

#include "tee/cocoon/sev/Tee.h"
#include "tee/cocoon/tdx/Tee.h"
#include "tee/cocoon/utils.h"

namespace cocoon {

namespace {

constexpr int MAX_CERT_CHAIN_DEPTH = 1;

/**
 * Generates a self-signed X.509 certificate with the given private key and configuration.
 *
 * @param private_key Ed25519 private key for signing the certificate
 * @param config Certificate configuration (subject, validity, extensions, etc.)
 * @return PEM-encoded certificate or error
 */
td::Result<std::string> generate_self_signed_cert(const tde2e_core::PrivateKey &private_key,
                                                  const TeeCertConfig &config) {
  // Validate configuration parameters
  if (config.country.size() != 2) {
    return td::Status::Error(
        PSLICE() << "Invalid country code: must be exactly 2 characters (ISO 3166-1 alpha-2), got '" << config.country
                 << "' (" << config.country.size() << " chars)");
  }

  if (config.common_name.empty()) {
    return td::Status::Error("Certificate common name cannot be empty");
  }

  if (config.validity_seconds == 0) {
    return td::Status::Error("Certificate validity must be positive");
  }

  constexpr td::uint32 MAX_VALIDITY_SECONDS = (1u << 30);
  if (config.validity_seconds > MAX_VALIDITY_SECONDS) {
    return td::Status::Error(PSLICE() << "Certificate validity too large: " << config.validity_seconds
                                      << " seconds (max: " << MAX_VALIDITY_SECONDS << ")");
  }

  // Convert private key to OpenSSL format
  auto public_key = private_key.to_public_key();
  auto private_key_bytes = private_key.to_secure_string();

  OPENSSL_MAKE_PTR(openssl_pkey,
                   EVP_PKEY_new_raw_private_key(EVP_PKEY_ED25519, nullptr, private_key_bytes.as_slice().ubegin(),
                                                private_key_bytes.size()),
                   EVP_PKEY_free, "Failed to create OpenSSL private key from Ed25519 key");

  // Create X.509 certificate structure
  OPENSSL_MAKE_PTR(certificate, X509_new(), X509_free, "Failed to create X509 certificate structure");

  // Set certificate serial number: 128-bit random
  unsigned char serial_bytes[16];
  OPENSSL_CHECK_OK(RAND_bytes(serial_bytes, sizeof(serial_bytes)), "Failed to generate random serial");
  OPENSSL_MAKE_PTR(serial_bn, BN_bin2bn(serial_bytes, sizeof(serial_bytes), nullptr), BN_free,
                   "Failed to create BIGNUM for serial");
  OPENSSL_CHECK_PTR(BN_to_ASN1_INTEGER(serial_bn.get(), X509_get_serialNumber(certificate.get())),
                    "Failed to set certificate serial number");

  // Set certificate validity period
  if (config.current_time.has_value()) {
    // Use provided time instead of system time
    td::uint32 not_before = config.current_time.value();

    // Check for overflow when adding validity_seconds
    if (not_before > std::numeric_limits<td::uint32>::max() - config.validity_seconds) {
      return td::Status::Error(PSLICE() << "Certificate validity would overflow: notBefore=" << not_before
                                        << " + validity_seconds=" << config.validity_seconds);
    }
    td::uint32 not_after = not_before + config.validity_seconds;

    OPENSSL_MAKE_PTR(asn1_not_before, ASN1_TIME_set(nullptr, not_before), ASN1_TIME_free,
                     "Failed to create ASN1_TIME for notBefore");
    OPENSSL_MAKE_PTR(asn1_not_after, ASN1_TIME_set(nullptr, not_after), ASN1_TIME_free,
                     "Failed to create ASN1_TIME for notAfter");

    OPENSSL_CHECK_OK(X509_set1_notBefore(certificate.get(), asn1_not_before.get()),
                     "Failed to set certificate notBefore time");
    OPENSSL_CHECK_OK(X509_set1_notAfter(certificate.get(), asn1_not_after.get()),
                     "Failed to set certificate notAfter time");
  } else {
    // Use system time (backwards compatible)
    OPENSSL_CHECK_PTR(X509_gmtime_adj(X509_get_notBefore(certificate.get()), 0),
                      "Failed to set certificate notBefore time");
    OPENSSL_CHECK_PTR(X509_gmtime_adj(X509_get_notAfter(certificate.get()), config.validity_seconds),
                      "Failed to set certificate notAfter time");
  }

  for (const auto &[oid_string, extension_value] : config.extra_extensions) {
    // Create OID object (allow numerical OID format)
    OPENSSL_MAKE_PTR(extension_oid, OBJ_txt2obj(oid_string.c_str(), 1), ASN1_OBJECT_free,
                     PSLICE() << "Failed to create OID object for: '" << oid_string << "'");

    // Create extension value as ASN.1 OCTET STRING
    OPENSSL_MAKE_PTR(extension_data, ASN1_OCTET_STRING_new(), ASN1_OCTET_STRING_free,
                     "Failed to create ASN1_OCTET_STRING for extension data");
    OPENSSL_CHECK_OK(ASN1_OCTET_STRING_set(extension_data.get(), td::Slice(extension_value).ubegin(),
                                           td::narrow_cast<int>(extension_value.size())),
                     "Failed to set extension data in ASN1_OCTET_STRING");

    // Create the X.509 extension (critical)
    OPENSSL_MAKE_PTR(x509_extension,
                     X509_EXTENSION_create_by_OBJ(nullptr, extension_oid.get(), 1, extension_data.get()),
                     X509_EXTENSION_free, PSLICE() << "Failed to create X509 extension for OID: " << oid_string);

    // Add extension to certificate
    OPENSSL_CHECK_OK(X509_add_ext(certificate.get(), x509_extension.get(), -1),
                     PSLICE() << "Failed to add extension '" << oid_string << "' to certificate");
  }

  // Set certificate public key
  OPENSSL_CHECK_OK(X509_set_pubkey(certificate.get(), openssl_pkey.get()), "Failed to set certificate public key");

  X509V3_CTX v3;
  memset(&v3, 0, sizeof(v3));
  X509V3_set_ctx_nodb(&v3);
  X509V3_set_ctx(&v3, /*issuer*/ certificate.get(), /*subject*/ certificate.get(), nullptr, nullptr, 0);
  X509V3_CTX *v3_ptr = &v3;

  // Add Basic Constraints (critical, CA:FALSE)
  OPENSSL_MAKE_PTR(basic_constraints_extension,
                   X509V3_EXT_conf_nid(nullptr, v3_ptr, NID_basic_constraints, "critical,CA:FALSE"),
                   X509_EXTENSION_free, "Failed to create Basic Constraints extension");
  OPENSSL_CHECK_OK(X509_add_ext(certificate.get(), basic_constraints_extension.get(), -1),
                   "Failed to add Basic Constraints extension to certificate");

  // Add Key Usage (critical, digitalSignature only for Ed25519)
  OPENSSL_MAKE_PTR(key_usage_extension,
                   X509V3_EXT_conf_nid(nullptr, v3_ptr, NID_key_usage, "critical,digitalSignature"),
                   X509_EXTENSION_free, "Failed to create Key Usage extension");
  OPENSSL_CHECK_OK(X509_add_ext(certificate.get(), key_usage_extension.get(), -1),
                   "Failed to add Key Usage extension to certificate");

  // Add Extended Key Usage (critical)
  OPENSSL_MAKE_PTR(extended_key_usage_extension,
                   X509V3_EXT_conf_nid(nullptr, v3_ptr, NID_ext_key_usage, "critical,serverAuth,clientAuth"),
                   X509_EXTENSION_free, "Failed to create Extended Key Usage extension");
  OPENSSL_CHECK_OK(X509_add_ext(certificate.get(), extended_key_usage_extension.get(), -1),
                   "Failed to add Extended Key Usage extension to certificate");

  // Add Subject Key Identifier (hash)
  OPENSSL_MAKE_PTR(ski_extension, X509V3_EXT_conf_nid(nullptr, v3_ptr, NID_subject_key_identifier, "hash"),
                   X509_EXTENSION_free, "Failed to create Subject Key Identifier extension");
  OPENSSL_CHECK_OK(X509_add_ext(certificate.get(), ski_extension.get(), -1),
                   "Failed to add Subject Key Identifier extension to certificate");

  /*
  // Add Authority Key Identifier (keyid,issuer)
  OPENSSL_MAKE_PTR(aki_extension, X509V3_EXT_conf_nid(nullptr, v3_ptr, NID_authority_key_identifier, "keyid,issuer"),
                   X509_EXTENSION_free, "Failed to create Authority Key Identifier extension");
  OPENSSL_CHECK_OK(X509_add_ext(certificate.get(), aki_extension.get(), -1),
                   "Failed to add Authority Key Identifier extension to certificate");
  */

  // Build Subject Alternative Name extension
  std::string san_value;
  for (size_t i = 0; i < config.san_names.size(); ++i) {
    if (i > 0) {
      san_value += ",";
    }

    const auto &name = config.san_names[i];
    if (name.find(':') != std::string::npos && name != "localhost") {
      // Contains colon - likely IPv6 address
      san_value += "IP:" + name;
    } else if (name.find('.') != std::string::npos || name == "localhost") {
      // Contains dot or is localhost - treat as DNS name
      san_value += "DNS:" + name;
    } else {
      // Assume IPv4 address or other IP format
      san_value += "IP:" + name;
    }
  }

  OPENSSL_MAKE_PTR(san_extension, X509V3_EXT_conf_nid(nullptr, nullptr, NID_subject_alt_name, san_value.c_str()),
                   X509_EXTENSION_free, "Failed to create Subject Alternative Name extension");

  OPENSSL_CHECK_OK(X509_add_ext(certificate.get(), san_extension.get(), -1),
                   "Failed to add Subject Alternative Name extension to certificate");

  // Set certificate subject name (also issuer, since self-signed)
  X509_NAME *subject_name = X509_get_subject_name(certificate.get());
  if (!subject_name) {
    return td::Status::Error("Failed to get certificate subject name structure");
  }

  // Add subject name components
  OPENSSL_CHECK_OK(X509_NAME_add_entry_by_txt(subject_name, "C", MBSTRING_ASC,
                                              (const unsigned char *)config.country.c_str(), -1, -1, 0),
                   PSLICE() << "Failed to add country '" << config.country << "' to certificate subject");

  OPENSSL_CHECK_OK(X509_NAME_add_entry_by_txt(subject_name, "ST", MBSTRING_ASC,
                                              (const unsigned char *)config.state.c_str(), -1, -1, 0),
                   PSLICE() << "Failed to add state '" << config.state << "' to certificate subject");

  if (!config.locality.empty()) {
    OPENSSL_CHECK_OK(X509_NAME_add_entry_by_txt(subject_name, "L", MBSTRING_ASC,
                                                (const unsigned char *)config.locality.c_str(), -1, -1, 0),
                     PSLICE() << "Failed to add locality '" << config.locality << "' to certificate subject");
  }

  OPENSSL_CHECK_OK(X509_NAME_add_entry_by_txt(subject_name, "O", MBSTRING_ASC,
                                              (const unsigned char *)config.organization.c_str(), -1, -1, 0),
                   PSLICE() << "Failed to add organization '" << config.organization << "' to certificate subject");

  OPENSSL_CHECK_OK(
      X509_NAME_add_entry_by_txt(subject_name, "OU", MBSTRING_ASC,
                                 (const unsigned char *)config.organizational_unit.c_str(), -1, -1, 0),
      PSLICE() << "Failed to add organizational unit '" << config.organizational_unit << "' to certificate subject");

  OPENSSL_CHECK_OK(X509_NAME_add_entry_by_txt(subject_name, "CN", MBSTRING_ASC,
                                              (const unsigned char *)config.common_name.c_str(), -1, -1, 0),
                   PSLICE() << "Failed to add common name '" << config.common_name << "' to certificate subject");

  // Set issuer name (same as subject for self-signed certificates)
  OPENSSL_CHECK_OK(X509_set_issuer_name(certificate.get(), subject_name), "Failed to set certificate issuer name");

  // Sign the certificate with the private key
  OPENSSL_CHECK_OK(X509_sign(certificate.get(), openssl_pkey.get(), nullptr),
                   "Failed to sign certificate with private key");

  // Convert certificate to PEM format
  OPENSSL_MAKE_PTR(certificate_bio, BIO_new(BIO_s_mem()), BIO_free,
                   "Failed to create memory BIO for certificate output");
  OPENSSL_CHECK_OK(PEM_write_bio_X509(certificate_bio.get(), certificate.get()),
                   "Failed to write certificate to PEM format");

  // Extract PEM data from BIO
  char *certificate_data = nullptr;
  long certificate_length = BIO_get_mem_data(certificate_bio.get(), &certificate_data);

  if (certificate_length <= 0 || !certificate_data) {
    return td::Status::Error("Failed to extract certificate data from BIO");
  }

  return std::string(certificate_data, certificate_length);
}

}  // namespace

td::Result<TeeType> TeeInterface::this_cpu_tee_type() {
#if defined(__i386__) || defined(__x86_64__)
  unsigned int eax, ebx, ecx, edx;

  if (__get_cpuid(0, &eax, &ebx, &ecx, &edx)) {
    if (ebx == 0x756e6547 && edx == 0x49656e69 && ecx == 0x6c65746e) {
      return TeeType::Tdx;
    }

    if (ebx == 0x68747541 && edx == 0x69746e65 && ecx == 0x444d4163) {
      return TeeType::Sev;
    }
  }
#endif

  return td::Status::Error("Unsupported Tee type");
}

TeeCertAndKey::TeeCertAndKey(std::string cert_pem, std::string key_pem)
    : impl_(std::make_shared<Impl>(Impl{std::move(cert_pem), std::move(key_pem)})) {
}

const std::string &TeeCertAndKey::cert_pem() const {
  return impl_->cert_pem;
}

const std::string &TeeCertAndKey::key_pem() const {
  return impl_->key_pem;
}

td::Result<TeeCertAndKey> generate_cert_and_key(const TeeInterface *tee, TeeCertConfig config) {
  TRY_RESULT(private_key, tde2e_core::PrivateKey::generate());
  TRY_RESULT(private_key_pem, private_key.to_pem());

  if (tee) {
    auto public_key = private_key.to_public_key();
    TRY_STATUS(tee->prepare_cert_config(config, public_key));
  }
  TRY_RESULT(cert, generate_self_signed_cert (private_key, config));

  return TeeCertAndKey{std::move(cert), private_key_pem.as_slice().str()};
}

td::Result<TeeCertAndKey> load_cert_and_key(td::Slice name) {
  TRY_RESULT(cert_pem, td::read_file_str(PSLICE() << name << "_cert.pem"));
  TRY_RESULT(key_pem, td::read_file_str(PSLICE() << name << "_key.pem"));

  return TeeCertAndKey{std::move(cert_pem), std::move(key_pem)};
}

void SslCtxFree::operator()(void *ptr) const {
  SSL_CTX_free(static_cast<SSL_CTX *>(ptr));
}

td::Result<SslCtxHolder> create_ssl_ctx(SslOptions options) {
  td::clear_openssl_errors("create_ssl_ctx");

  const SSL_METHOD *ssl_method = options.mode == SslOptions::Mode::Client ? TLS_client_method() : TLS_server_method();
  OPENSSL_CHECK_PTR(ssl_method, "Failed to obtain TLS method");

  OPENSSL_MAKE_PTR(ctx_ptr, SSL_CTX_new(ssl_method), SSL_CTX_free, "Failed to create SSL_CTX");

  long ctx_options =
      SSL_OP_NO_SSLv2 | SSL_OP_NO_SSLv3 | SSL_OP_NO_TICKET;  // disable TLS session resumption via session tickets
  SSL_CTX_set_options(ctx_ptr.get(), ctx_options);
  SSL_CTX_set_min_proto_version(ctx_ptr.get(), TLS1_3_VERSION);
  SSL_CTX_set_mode(ctx_ptr.get(), SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER | SSL_MODE_ENABLE_PARTIAL_WRITE);

  OPENSSL_MAKE_PTR(cert_bio, BIO_new_mem_buf(options.cert_and_key.cert_pem().c_str(), -1), BIO_free,
                   "Failed to create BIO for certificate");
  OPENSSL_MAKE_PTR(cert, PEM_read_bio_X509(cert_bio.get(), nullptr, nullptr, nullptr), X509_free,
                   "Failed to parse certificate PEM");
  OPENSSL_CHECK_OK(SSL_CTX_use_certificate(ctx_ptr.get(), cert.get()), "Failed to set certificate in SSL_CTX");

  OPENSSL_MAKE_PTR(key_bio, BIO_new_mem_buf(options.cert_and_key.key_pem().c_str(), -1), BIO_free,
                   "Failed to create BIO for private key");
  OPENSSL_MAKE_PTR(pkey, PEM_read_bio_PrivateKey(key_bio.get(), nullptr, nullptr, nullptr), EVP_PKEY_free,
                   "Failed to parse private key PEM");
  OPENSSL_CHECK_OK(SSL_CTX_use_PrivateKey(ctx_ptr.get(), pkey.get()), "Failed to set private key in SSL_CTX");

  OPENSSL_CHECK_OK(SSL_CTX_check_private_key(ctx_ptr.get()), "Private key does not match the certificate");

  int verify_flags = SSL_VERIFY_PEER;
  if (options.mode == SslOptions::Mode::Server) {
    verify_flags |= SSL_VERIFY_FAIL_IF_NO_PEER_CERT;
  }
  SSL_CTX_set_verify(ctx_ptr.get(), verify_flags, RATLS_verify_callback);
  SSL_CTX_set_verify_depth(ctx_ptr.get(), MAX_CERT_CHAIN_DEPTH);
  RATLS_extract_context(ctx_ptr.get(), true)->custom_verify_callback = options.custom_verify;

  // NOTE: We intentionally always send a certificate and perform full RA verification per-connection.
  // Future optimization: cache DCAP verification and policy decisions in router by hash(quote),
  // with TTL bound to collateral freshness and policy version.

  const std::string cipher_suites = "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384";
  OPENSSL_CHECK_OK(SSL_CTX_set_ciphersuites(ctx_ptr.get(), cipher_suites.c_str()),
                   PSLICE() << "Failed to set cipher suites \"" << cipher_suites << "\"");

  SslCtxHolder holder;
  holder.reset(ctx_ptr.release());
  return holder;
}

}  // namespace cocoon
