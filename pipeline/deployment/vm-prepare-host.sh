#!/bin/bash
# Explicit non-confidential lab preparation; never reboots or changes IOMMU flags.
set -euo pipefail
test "$(id -u)" = 0
test -c /dev/kvm
test -d /sys/kernel/iommu_groups
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
apt-get update
apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils \
    libvirt-daemon-system libvirt-clients ovmf virtiofsd cloud-image-utils \
    ubuntu-cloudimage-keyring iputils-arping psmisc curl ca-certificates
systemctl start libvirtd
# Keep an existing host network configuration. Provision an isolated default
# libvirt NAT network separately if this prerequisite is not met.
virsh -c qemu:///system net-info default | grep -Eq '^Active: *yes$'
test -r /usr/share/OVMF/OVMF_CODE_4M.fd
test -r /usr/share/OVMF/OVMF_VARS_4M.fd

install -d -m 0755 /var/lib/cocoon-pipeline-vm/images
cd /var/lib/cocoon-pipeline-vm/images
base=https://cloud-images.ubuntu.com/noble/20260926
name=noble-server-cloudimg-amd64.img
checksum=6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2
if ! test -f "$name"; then
    curl --fail --location --retry 3 "$base/SHA256SUMS" -o SHA256SUMS
    curl --fail --location --retry 3 "$base/SHA256SUMS.gpg" -o SHA256SUMS.gpg
    gpgv --keyring /usr/share/keyrings/ubuntu-cloudimage-keyring.gpg SHA256SUMS.gpg SHA256SUMS
    grep -Fx "$checksum *$name" SHA256SUMS
    curl --fail --location --retry 3 "$base/$name" -o "$name.partial"
    printf '%s  %s\n' "$checksum" "$name.partial" | sha256sum -c -
    mv "$name.partial" "$name"
fi
printf '%s  %s\n' "$checksum" "$name" | sha256sum -c -
chmod 0444 "$name"
dpkg-query -W > /var/lib/cocoon-pipeline-vm/host-packages.tsv
