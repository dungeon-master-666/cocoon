#include "tee/cocoon/sev/GuestDevice.h"

#if defined(__linux__)

#include <fcntl.h>
#include <linux/sev-guest.h>
#include <sys/ioctl.h>

#include <errno.h>
#include <string.h>
#include <unistd.h>

#include "td/utils/crypto.h"

namespace sev {

namespace {

td::Result<std::vector<sev::GuestDevice::CertTableEntry>> parse_cert_table(const std::byte* buffer, size_t size) {
  std::vector<sev::GuestDevice::CertTableEntry> cert_table;

  struct hdr {
    td::UInt128 guid;
    td::uint32 offset;
    td::uint32 length;
  };
  static_assert(sizeof(hdr) == 24);

  const hdr* h = reinterpret_cast<const hdr*>(buffer);
  for (;;) {
    if (size < static_cast<size_t>(reinterpret_cast<const std::byte*>(h) - buffer)) {
      return td::Status::Error("Cannot parse CertTable: out of bounds");
    }

    if (h->guid.is_zero() && !h->offset && !h->length) {
      return cert_table;
    }

    if (size < h->offset || size < (h->offset + h->length)) {
      return td::Status::Error("Cannot parse CertTable: out of bounds");
    }

    cert_table.emplace_back(h->guid, std::string(reinterpret_cast<const char*>(&buffer[h->offset]), h->length));

    ++h;
  }
}

template <typename Request, typename Response>
td::Status do_snp_guest_ioctl(int fd, int op, const Request& request, Response* response) {
  snp_guest_request_ioctl cmd = {};

  cmd.msg_version = 1;
  cmd.req_data = reinterpret_cast<uintptr_t>(&request);
  cmd.resp_data = reinterpret_cast<uintptr_t>(response);

  if (::ioctl(fd, op, &cmd, sizeof(cmd)) == -1) {
    const auto saved_errno = errno;
    return td::Status::Error(PSTRING() << "Cannot do GuestDevice ioctl: op:" << op << ": "
                                       << strerrordesc_np(saved_errno));
  }

  return td::Status::OK();
}

}  // namespace

td::Result<GuestDevice> GuestDevice::open() {
  const char* kPath = "/dev/sev-guest";

  Fd fd(static_cast<int>(TEMP_FAILURE_RETRY(::open(kPath, O_RDONLY | O_NOCTTY, 0))));
  if (fd.get() == -1) {
    const auto saved_errno = errno;
    return td::Status::Error(PSTRING() << "Cannot open " << kPath << ": " << strerrordesc_np(saved_errno));
  }

  return GuestDevice(std::move(fd));
}

GuestDevice::~GuestDevice() {
  if (fd_.get() != -1) {
    if (::close(fd_.get()) == -1) {
      const auto saved_errno = errno;
      LOG(FATAL) << "cannot close GuestDevice: " << strerrordesc_np(saved_errno);
    }
  }
}

td::Result<AttestationReport> GuestDevice::get_report(const td::UInt512& user_claims_hash) const {
  snp_report_req req = {};
  memcpy(req.user_data, user_claims_hash.raw, sizeof(req.user_data));
  req.vmpl = 0;

  union {
    snp_report_resp resp;
    MsgReportRsp msg_report_rsp;
  } resp;
  TRY_STATUS(do_snp_guest_ioctl(fd_.get(), SNP_GET_REPORT, req, &resp));

  if (resp.msg_report_rsp.status) {
    return td::Status::Error(PSTRING() << "Cannot get report: bad status: " << resp.msg_report_rsp.status);
  }

  if (resp.msg_report_rsp.report_size != sizeof(AttestationReport)) {
    return td::Status::Error(PSTRING() << "Cannot get report: unexpected report size: "
                                       << resp.msg_report_rsp.report_size);
  }

  return resp.msg_report_rsp.report;
}

td::Result<std::pair<AttestationReport, std::vector<GuestDevice::CertTableEntry>>> GuestDevice::get_extended_report(
    const td::UInt512& user_claims_hash) const {
  // Certificate storage must be paged aligned and not exceed SEV_FW_BLOB_MAX_SIZE.
  // See drivers/virt/coco/sev-guest/sev-guest.c for details.
  constexpr size_t kSize = 16 << 10;
  alignas(4096) std::byte storage[kSize];

  snp_ext_report_req req = {};
  memcpy(req.data.user_data, user_claims_hash.raw, sizeof(req.data.user_data));
  req.data.vmpl = 0;
  req.certs_address = reinterpret_cast<uintptr_t>(storage);
  req.certs_len = kSize;

  union {
    snp_report_resp resp;
    MsgReportRsp msg_report_rsp;
  } resp;
  TRY_STATUS(do_snp_guest_ioctl(fd_.get(), SNP_GET_EXT_REPORT, req, &resp));

  if (resp.msg_report_rsp.status) {
    return td::Status::Error(PSTRING() << "Cannot get extended report: bad status: " << resp.msg_report_rsp.status);
  }

  if (resp.msg_report_rsp.report_size != sizeof(AttestationReport)) {
    return td::Status::Error(PSTRING() << "Cannot get extended report: unexpected report size: "
                                       << resp.msg_report_rsp.report_size);
  }

  TRY_RESULT(cert_table, parse_cert_table(storage, kSize));

  return std::make_pair(resp.msg_report_rsp.report, std::move(cert_table));
}

td::Result<td::UInt256> GuestDevice::get_derived_key(td::Slice name) const {
  snp_derived_key_req req = {};

  req.root_key_select = 0;
  GuestFieldSelect gfs = {};
  gfs.image_id = 1;
  gfs.measurement = 1;
  GuestFieldSelectCastDevice cast_device;
  cast_device.as_guest_field_select = gfs;
  req.guest_field_select = cast_device.as_uint64;
  req.vmpl = 1;

  snp_derived_key_resp resp;
  TRY_STATUS(do_snp_guest_ioctl(fd_.get(), SNP_GET_DERIVED_KEY, req, &resp));

  td::UInt256 key;
  td::hmac_sha256(td::Slice(resp.data, sizeof(resp.data)), name, key.as_mutable_slice());

  return key;
}

}  // namespace sev

#else

namespace sev {

td::Result<GuestDevice> GuestDevice::open() {
  return td::Status::Error("SEV guest device is only supported on Linux");
}

GuestDevice::~GuestDevice() = default;

td::Result<AttestationReport> GuestDevice::get_report(const td::UInt512& user_claims_hash) const {
  return td::Status::Error("SEV guest device is only supported on Linux");
}

td::Result<std::pair<AttestationReport, std::vector<GuestDevice::CertTableEntry>>> GuestDevice::get_extended_report(
    const td::UInt512& user_claims_hash) const {
  return td::Status::Error("SEV guest device is only supported on Linux");
}

td::Result<td::UInt256> GuestDevice::get_derived_key(td::Slice name) const {
  return td::Status::Error("SEV guest device is only supported on Linux");
}

}  // namespace sev

#endif
