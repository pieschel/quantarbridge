#!/usr/bin/env bash

set -euo pipefail

DVMHOST_COMMIT="01979084df9fc6a5737fac9efb213430268377c9"
TETRA_CODEC_COMMIT="21d884064478d63306ec654378e666ae41503d00"
SERVICE_USER="quantar"
DEFAULT_ADMIN_USER="qbadmin"
DEFAULT_ADMIN_PASSWORD="quantarbridge"
INSTALL_DIR="/home/${SERVICE_USER}/quantarbridge"
RUNTIME_DIR="/home/${SERVICE_USER}/quantar-runtime"
DVMHOST_DIR="/home/${SERVICE_USER}/src/dvmhost"
TETRA_CODEC_DIR="/home/${SERVICE_USER}/src/tetra-codec"

if [[ "$(uname -m)" != "aarch64" && "$(uname -m)" != "arm64" ]]; then
  echo "The rootfs installer must run natively on ARM64." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  build-essential \
  ca-certificates \
  cmake \
  file \
  git \
  libasio-dev \
  libncurses-dev \
  libssl-dev \
  libyaml-cpp-dev \
  mosquitto-clients \
  openssh-server \
  pkg-config \
  python3 \
  python3-requests \
  python3-websocket \
  python3-yaml \
  rsync \
  sudo

if ! id -u "${DEFAULT_ADMIN_USER}" >/dev/null 2>&1; then
  if [[ "$(getent passwd 1000 | cut -d: -f1)" != "pi" ]]; then
    echo "Raspberry Pi bootstrap user pi was not found at UID 1000." >&2
    exit 1
  fi
  default_admin_hash="$(openssl passwd -6 "${DEFAULT_ADMIN_PASSWORD}")"
  /usr/lib/userconf-pi/userconf "${DEFAULT_ADMIN_USER}" "${default_admin_hash}"
fi
for group in sudo adm dialout plugdev users input render netdev gpio i2c spi video; do
  if getent group "${group}" >/dev/null; then
    usermod -aG "${group}" "${DEFAULT_ADMIN_USER}"
  fi
done

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${SERVICE_USER}"
fi
usermod -aG dialout "${SERVICE_USER}"

install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "/home/${SERVICE_USER}/src"
install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${RUNTIME_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

runuser -u "${SERVICE_USER}" -- git clone https://github.com/DVMProject/dvmhost.git "${DVMHOST_DIR}"
runuser -u "${SERVICE_USER}" -- git -C "${DVMHOST_DIR}" checkout --detach "${DVMHOST_COMMIT}"
for patch in dvmhost.patch dvmhost-quantar-rssi.patch; do
  runuser -u "${SERVICE_USER}" -- git -C "${DVMHOST_DIR}" apply --check "${INSTALL_DIR}/patches/${patch}"
  runuser -u "${SERVICE_USER}" -- git -C "${DVMHOST_DIR}" apply "${INSTALL_DIR}/patches/${patch}"
done

runuser -u "${SERVICE_USER}" -- cmake \
  -S "${DVMHOST_DIR}" \
  -B "${DVMHOST_DIR}/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_TUI_SUPPORT=0 \
  -DENABLE_SETUP_TUI=0
runuser -u "${SERVICE_USER}" -- cmake --build "${DVMHOST_DIR}/build" --parallel "$(nproc)"

runuser -u "${SERVICE_USER}" -- git clone https://github.com/outerplane/tetra-codec.git "${TETRA_CODEC_DIR}"
runuser -u "${SERVICE_USER}" -- git -C "${TETRA_CODEC_DIR}" checkout --detach "${TETRA_CODEC_COMMIT}"
runuser -u "${SERVICE_USER}" -- cmake \
  -S "${TETRA_CODEC_DIR}" \
  -B "${TETRA_CODEC_DIR}/build" \
  -DCMAKE_BUILD_TYPE=Release
runuser -u "${SERVICE_USER}" -- cmake --build "${TETRA_CODEC_DIR}/build" --parallel "$(nproc)"

runuser -u "${SERVICE_USER}" -- cmake \
  -S "${INSTALL_DIR}" \
  -B "${INSTALL_DIR}/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DDVMHOST_SOURCE_DIR="${DVMHOST_DIR}" \
  -DDVMHOST_COMMON_LIBRARY="${DVMHOST_DIR}/build/libcommon.a"
runuser -u "${SERVICE_USER}" -- cmake --build "${INSTALL_DIR}/build" --parallel "$(nproc)"
runuser -u "${SERVICE_USER}" -- ctest --test-dir "${INSTALL_DIR}/build" --output-on-failure

for service_file in "${INSTALL_DIR}"/deploy/*.service; do
  [[ "${service_file}" == *.user.service ]] && continue
  install -m 0644 "${service_file}" /etc/systemd/system/
done
install -m 0644 "${INSTALL_DIR}"/deploy/*.timer /etc/systemd/system/
install -m 0644 "${INSTALL_DIR}"/deploy/*.path /etc/systemd/system/
install -m 0440 "${INSTALL_DIR}/image/quantarbridge-sudoers" \
  /etc/sudoers.d/quantarbridge-dashboard
visudo -cf /etc/sudoers.d/quantarbridge-dashboard
install -m 0755 "${INSTALL_DIR}/image/quantarbridge-setup" /usr/local/sbin/quantarbridge-setup
install -d -m 0755 /etc/motd.d
install -m 0644 "${INSTALL_DIR}/image/quantarbridge-motd" /etc/motd.d/90-quantarbridge
install -d -m 0755 /etc/ssh/sshd_config.d
install -m 0644 "${INSTALL_DIR}/image/60-quantarbridge-default-login.conf" \
  /etc/ssh/sshd_config.d/60-quantarbridge-default-login.conf
install -m 0644 "${INSTALL_DIR}/image/QUANTARBRIDGE-README.txt" \
  /boot/firmware/QUANTARBRIDGE-README.txt

systemctl enable ssh.service
systemctl disable \
  dvmfne.service \
  dvmhost.service \
  dvmbridge-p25-to-dmr.service \
  dvmbridge-dmr-to-p25.service \
  tetrapack-brew-audio.service \
  quantarbridge.service \
  tetrapack-brew-bridge.service \
  quantar-dashboard.service 2>/dev/null || true
touch /boot/firmware/ssh

file "${INSTALL_DIR}/build/quantarbridge" | grep -q 'ARM aarch64'
file "${DVMHOST_DIR}/build/dvmhost" | grep -q 'ARM aarch64'
file "${TETRA_CODEC_DIR}/build/libtetra-codec.so" | grep -q 'ARM aarch64'
id -nG "${DEFAULT_ADMIN_USER}" | tr ' ' '\n' | grep -qx sudo
passwd -S "${DEFAULT_ADMIN_USER}" | grep -Eq "^${DEFAULT_ADMIN_USER} P "
[[ "$(getent passwd 1000 | cut -d: -f1)" == "${DEFAULT_ADMIN_USER}" ]]
[[ "$(getent shadow "${DEFAULT_ADMIN_USER}" | cut -d: -f3)" -gt 0 ]]
[[ ! -e /etc/ssh/sshd_config.d/rename_user.conf ]]
if systemctl --quiet is-enabled userconfig.service; then
  echo "Raspberry Pi first-boot user configuration is still enabled." >&2
  exit 1
fi

strip --strip-unneeded "${INSTALL_DIR}/build/quantarbridge"
strip --strip-unneeded \
  "${DVMHOST_DIR}/build/dvmhost" \
  "${DVMHOST_DIR}/build/dvmfne" \
  "${DVMHOST_DIR}/build/dvmbridge" \
  "${TETRA_CODEC_DIR}/build/libtetra-codec.so"

rm -rf "${DVMHOST_DIR}/.git" "${TETRA_CODEC_DIR}/.git"
rm -rf "${INSTALL_DIR}/build/CMakeFiles" "${DVMHOST_DIR}/build/CMakeFiles" \
  "${TETRA_CODEC_DIR}/build/CMakeFiles"
apt-get clean
rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/*

if [[ -n "${GITHUB_SHA:-}" ]]; then
  printf '%s\n' "${GITHUB_SHA}" > /etc/quantarbridge-image-version
fi

echo "ARM64 QuantarBridge root filesystem prepared successfully."
