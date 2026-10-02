#include "tee/cocoon/sev/ABI.h"

#include <string.h>

#if defined(__i386__) || defined(__x86_64__)
#include <cpuid.h>
#endif

#include "td/utils/Slice-decl.h"
#include "td/utils/format.h"
#include "td/utils/logging.h"
#include "td/utils/misc.h"

namespace {

sev::TCBVersion tcb_version_for_cpu(int family, int model) {
  if (family == 0x1A && ((0x90 <= model && model <= 0xAF) || (0xC0 <= model && model <= 0xCF))) {
    return sev::TCBVersion::V1;
  }

  if ((family == 0x1A && (0x00 <= model && model <= 0x1F)) || (family == 0x19 && (0x00 <= model && model <= 0x1F))) {
    return sev::TCBVersion::V0;
  }

  LOG(FATAL) << "family:" << family << ", model:" << model;

  return sev::TCBVersion::V0;
}

}  // namespace

namespace sev {

td::StringBuilder& operator<<(td::StringBuilder& sb, SigningAlgorithm algo) {
  switch (algo) {
    case SigningAlgorithm::ECDSA_P384_with_SHA384:
      return sb << "ECDSA P384 with SHA384";
  }

  return sb;
}

td::StringBuilder& operator<<(td::StringBuilder& sb, ECCCurve curve) {
  switch (curve) {
    case ECCCurve::P384:
      return sb << "ECCCurve:P384";
  }

  return sb;
}

td::StringBuilder& operator<<(td::StringBuilder& sb, const ECDSAP384withSHA384Signature& signature) {
  return sb << "ECDSA P-384 with SHA-384 Signature: R:" << td::format::as_hex_dump<0>(signature.R.as_slice())
            << ", S:" << td::format::as_hex_dump<0>(signature.S.as_slice());
}

td::StringBuilder& operator<<(td::StringBuilder& sb, const ECDSAP384PublicKey& public_key) {
  return sb << "ECDSA P-384 Public Key: Curve:" << public_key.curve
            << ", QX:" << td::format::as_hex_dump<0>(public_key.QX.as_slice())
            << ", QY:" << td::format::as_hex_dump<0>(public_key.QY.as_slice());
}

td::StringBuilder& operator<<(td::StringBuilder& sb, GuestPolicy guest_policy) {
  return sb << "GuestPolicy: page_swap_disable:" << guest_policy.page_swap_disable
            << ", ciphertext_hiding_dram:" << guest_policy.ciphertext_hiding_dram
            << ", rapl_dis:" << guest_policy.rapl_dis << ", mem_aes_256_xts:" << guest_policy.mem_aes_256_xts
            << ", cxl_allow:" << guest_policy.cxl_allow << ", single_socket:" << guest_policy.single_socket
            << ", debug:" << guest_policy.debug << ", migrate_ma:" << guest_policy.migrate_ma
            << ", reserved_must_be_one:" << guest_policy.reserved_must_be_one << ", smt:" << guest_policy.smt
            << ", abi_major:" << guest_policy.abi_major << ", abi_minor:" << guest_policy.abi_minor;
}

td::StringBuilder& operator<<(td::StringBuilder& sb, PlatformInfo platform_info) {
  return sb << "PlatformInfo: tio_en:" << platform_info.tio_en
            << ", alias_check_complete:" << platform_info.alias_check_complete
            << ", ciphertext_hiding_dram_en:" << platform_info.ciphertext_hiding_dram_en
            << ", rapl_dis:" << platform_info.rapl_dis << ", ecc_en:" << platform_info.ecc_en
            << ", tsme_en:" << platform_info.tsme_en << ", smt_en:" << platform_info.smt_en;
}

td::CSlice product_name_to_string(ProductName product_name) {
  switch (product_name) {
    case ProductName::Milan:
      return "Milan";
    case ProductName::Genoa:
      return "Genoa";
    case ProductName::Siena:
      return "Siena";
    case ProductName::Turin:
      return "Turin";
  }

  UNREACHABLE();
}

td::Result<ProductName> product_name_from_name(td::Slice n) {
  if (n == "Milan") {
    return ProductName::Milan;
  }

  if (n == "Genoa") {
    return ProductName::Genoa;
  }

  if (n == "Siena") {
    return ProductName::Siena;
  }

  if (n == "Turin") {
    return ProductName::Turin;
  }

  return td::Status::Error(PSTRING() << "Unexpected product name: " << n);
}

td::Result<ProductName> product_name_from_name_and_stepping(td::Slice nas) {
  auto [n, s] = td::split(nas, '-');

  return product_name_from_name(n);
}

td::StringBuilder& operator<<(td::StringBuilder& sb, ProductName product_name) {
  return sb << product_name_to_string(product_name);
}

td::Result<ProductName> product_name_from_cpu(int family, int extended_model) {
  switch (family) {
    case 0x19: {
      switch (extended_model) {
        case 0x0:
          return ProductName::Milan;
        case 0x1:
        case 0x11:
          return ProductName::Genoa;
        case 0xA:
          return ProductName::Siena;
      }

      break;
    }

    case 0x1A: {
      if (extended_model == 0x0 || extended_model == 0x1) {
        return ProductName::Turin;
      }
      break;
    }
  }

  return td::Status::Error(PSTRING() << "Unknown CPU: family: " << family << ", extended_model:" << extended_model);
}

td::Result<ProductName> product_name_from_this_cpu() {
#if defined(__i386__) || defined(__x86_64__)
  unsigned int eax, ebx, ecx, edx;

  if (!__get_cpuid(1, &eax, &ebx, &ecx, &edx)) {
    return td::Status::Error("Cannot determine product name for CPU: cpuid failed");
  }

  const auto extended_family = (eax & 0x0ff00000) >> 20;
  const auto extended_model = (eax & 0x000f0000) >> 16;
  const auto family = (eax & 0x00000f00) >> 8;

  return product_name_from_cpu(extended_family + family, extended_model);
#else
  return td::Status::Error("Cannot determine SEV product name on a non-x86 CPU");
#endif
}

td::uint32 make_cpu_signature(int cpu_fam_id, int cpu_mod_id, int cpu_stepping) {
  td::uint32 cpu_fam_id_lo, cpu_fam_id_hi;

  if (0xf < cpu_fam_id) {
    cpu_fam_id_lo = 0xf;
    cpu_fam_id_hi = (cpu_fam_id - 0x0f) & 0xff;
  } else {
    cpu_fam_id_lo = cpu_fam_id;
    cpu_fam_id_hi = 0;
  }

  const td::uint32 cpu_mod_id_lo = cpu_mod_id & 0xf;
  const td::uint32 cpu_mod_id_hi = (cpu_mod_id >> 4) & 0xf;
  const td::uint32 cpu_stepping_lo = cpu_stepping & 0xf;

  return (cpu_fam_id_hi << 20) | (cpu_mod_id_hi << 16) | (cpu_fam_id_lo << 8) | (cpu_mod_id_lo << 4) | cpu_stepping_lo;
}

int extended_model_from_cpu_mod_id(int cpu_mod_id) {
  // Model is an 8-bit value and is defined as: Model[7:0] = {ExtendedModel[3:0],BaseModel[3:0]}. E.g. If
  // ExtendedModel[3:0]=Eh and BaseModel[3:0]=8h, then Model[7:0] = E8h. If BaseFamily[3:0] is less than Fh
  // then ExtendedModel[3:0] is reserved and Model is equal to BaseModel[3:0]

  return (cpu_mod_id & 0xff) >> 4;
}

td::StringBuilder& operator<<(td::StringBuilder& sb, TCBVersionV1 tcb_version) {
  return sb << "TCBVersion: microcode:" << tcb_version.microcode << ", snp:" << tcb_version.snp
            << ", tee:" << tcb_version.tee << ", bootloader:" << tcb_version.boot_loader << ", fmc:" << tcb_version.fmc;
}

td::StringBuilder& operator<<(td::StringBuilder& sb, TCBVersionV0 tcb_version) {
  return sb << "TCBVersion: microcode:" << tcb_version.microcode << ", snp:" << tcb_version.snp
            << ", tee:" << tcb_version.tee << ", bootloader:" << tcb_version.boot_loader;
}

td::StringBuilder& operator<<(td::StringBuilder& sb, const AttestationReport& report) {
  ProductName product_name = ProductName::Milan;

  sb << "Attestation report: version: " << report.version << ",\n";
  sb << "guest_svn: " << report.guest_svn << ",\n";
  sb << "policy: " << report.policy << ",\n";
  sb << "family_id: " << td::format::as_hex(report.family_id) << ",\n";
  sb << "image_id: " << td::format::as_hex(report.image_id) << ",\n";
  sb << "vmpl: " << report.vmpl << ",\n";
  sb << "signature algorithm: " << report.signature_algo << ",\n";

  auto tcb_version = TCBVersion::V0;
  if (3 <= report.version) {
    tcb_version_for_cpu(report.cpuid_fam_id, report.cpuid_mod_id);
    product_name =
        product_name_from_cpu(report.cpuid_fam_id, extended_model_from_cpu_mod_id(report.cpuid_mod_id)).move_as_ok();
  }

  auto print_tcb_version = [](td::StringBuilder& sb, const char* name, TCBVersion tcb_version, td::uint64 raw) {
    sb << name << ": ";

    TCBVersionCastDevice tcb_version_cast_device{.as_uint64 = raw};
    switch (tcb_version) {
      case TCBVersion::V0:
        sb << tcb_version_cast_device.as_v0;
        break;
      case TCBVersion::V1:
        sb << tcb_version_cast_device.as_v1;
        break;
    }

    sb << ",\n";
  };
  print_tcb_version(sb, "current_tcb", tcb_version, report.current_tcb);

  sb << "signing_key: " << report.signing_key << ",\n";
  sb << "mask_chip_key: " << report.mask_chip_key << ",\n";
  sb << "author_key_en: " << report.author_key_en << ",\n";
  sb << "report_data: " << td::format::as_hex_dump<0>(report.report_data.as_slice()) << ",\n";
  sb << "measurement: " << td::format::as_hex_dump<0>(report.measurement.as_slice()) << ",\n";
  sb << "host_data: " << td::format::as_hex_dump<0>(report.host_data.as_slice()) << ",\n";
  sb << "id_key_digest: " << td::format::as_hex_dump<0>(report.id_key_digest.as_slice()) << ",\n";
  sb << "author_key_digest: " << td::format::as_hex_dump<0>(report.author_key_digest.as_slice()) << ",\n";
  sb << "report_id: " << td::format::as_hex_dump<0>(report.report_id.as_slice()) << ",\n";
  sb << "report_id_ma: " << td::format::as_hex_dump<0>(report.report_id_ma.as_slice()) << ",\n";
  print_tcb_version(sb, "reported_tcb", tcb_version, report.reported_tcb);
  if (3 <= report.version) {
    sb << "cpu_fam_id: " << report.cpuid_fam_id << ",\n";
    sb << "cpu_mod_id: " << report.cpuid_mod_id << ",\n";
    sb << "cpu_step: " << report.cpuid_step << ",\n";
  }
  sb << "chip_id: " << td::format::as_hex_dump<0>(report.chip_id.as_slice()) << ",\n";
  print_tcb_version(sb, "commited_tcb", tcb_version, report.commited_tcb);
  sb << "current_build: " << report.current_build << ",\n";
  sb << "current_minor: " << report.current_minor << ",\n";
  sb << "current_major: " << report.current_major << ",\n";
  sb << "committed_build: " << report.committed_build << ",\n";
  sb << "committed_minor: " << report.committed_minor << ",\n";
  sb << "committed_major: " << report.committed_major << ",\n";
  print_tcb_version(sb, "launch_tcb", tcb_version, report.launch_tcb);
  sb << "launch_mit_vector: " << td::format::as_hex(report.launch_mit_vector) << ",\n";
  sb << "current_mit_vector: " << td::format::as_hex(report.current_mit_vector) << ",\n";

  sb << "ProductName: " << product_name << ",\n";

  sb << report.signature;

  return sb;
}

}  // namespace sev
