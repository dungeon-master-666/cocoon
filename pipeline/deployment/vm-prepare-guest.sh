#!/bin/bash
# Run once inside the dev QEMU guest; does not install or change host GPU drivers.
set -euo pipefail
test "$(id -u)" = 0
test "$(systemd-detect-virt)" = kvm
case "$(hostname)" in cocoon-pipeline-vm-*) ;; *) echo 'Not an owned pipeline VM' >&2; exit 1 ;; esac

cloud-init status --wait
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends linux-generic linux-headers-generic \
    nvidia-driver-580-open docker.io python3 iproute2 util-linux iputils-arping \
    qemu-guest-agent curl ca-certificates gnupg pciutils

install -d -m 0755 /usr/share/keyrings
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey |
    gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list |
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update
apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl enable --now docker qemu-guest-agent
systemctl restart docker

install -d /var/lib/cocoon-pipeline-vm
dpkg-query -W > /var/lib/cocoon-pipeline-vm/guest-packages.tsv
printf '%s\n' 'Guest preparation complete. Reboot the guest before using its GPU.'
