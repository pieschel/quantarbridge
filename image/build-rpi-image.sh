#!/usr/bin/env bash

set -euo pipefail

BASE_IMAGE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-06-19/2026-06-18-raspios-trixie-arm64-lite.img.xz"
BASE_IMAGE_SHA256="acff736ca7945e3b305f07cda4abdb870910e12634991da69783611756e381b3"
IMAGE_GROWTH_GIB=3

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="${RUNNER_TEMP:-/tmp}/quantarbridge-rpi-image"
OUTPUT_DIR="${REPOSITORY_ROOT}/dist"
ROOTFS_DIR="${WORK_DIR}/rootfs"
BASE_ARCHIVE="${WORK_DIR}/raspios-lite-arm64.img.xz"
IMAGE_PATH="${WORK_DIR}/quantarbridge-rpios-arm64.img"
LOOP_DEVICE=""

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this image builder as root." >&2
  exit 1
fi
if [[ "$(uname -m)" != "aarch64" && "$(uname -m)" != "arm64" ]]; then
  echo "This builder requires a native ARM64 Linux host." >&2
  exit 1
fi

unmount_image() {
  for mount_point in \
    "${ROOTFS_DIR}/run" \
    "${ROOTFS_DIR}/sys" \
    "${ROOTFS_DIR}/proc" \
    "${ROOTFS_DIR}/dev/pts" \
    "${ROOTFS_DIR}/dev" \
    "${ROOTFS_DIR}/etc/resolv.conf" \
    "${ROOTFS_DIR}/boot/firmware" \
    "${ROOTFS_DIR}"; do
    if mountpoint -q "${mount_point}"; then
      umount -l "${mount_point}"
    fi
  done
}

cleanup() {
  set +e
  unmount_image
  if [[ -n "${LOOP_DEVICE}" ]]; then
    losetup -d "${LOOP_DEVICE}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  curl \
  e2fsprogs \
  file \
  mount \
  parted \
  rsync \
  util-linux \
  xz-utils \
  zerofree

install -d "${WORK_DIR}" "${OUTPUT_DIR}" "${ROOTFS_DIR}"
curl --fail --location --retry 5 --retry-all-errors \
  --output "${BASE_ARCHIVE}" "${BASE_IMAGE_URL}"
echo "${BASE_IMAGE_SHA256}  ${BASE_ARCHIVE}" | sha256sum --check --strict
xz --decompress --keep "${BASE_ARCHIVE}"
mv "${BASE_ARCHIVE%.xz}" "${IMAGE_PATH}"

truncate --size "+${IMAGE_GROWTH_GIB}G" "${IMAGE_PATH}"
LOOP_DEVICE="$(losetup --find --show --partscan "${IMAGE_PATH}")"
parted --script "${LOOP_DEVICE}" resizepart 2 100%
e2fsck -f -y "${LOOP_DEVICE}p2"
resize2fs "${LOOP_DEVICE}p2"

mount "${LOOP_DEVICE}p2" "${ROOTFS_DIR}"
install -d "${ROOTFS_DIR}/boot/firmware"
mount "${LOOP_DEVICE}p1" "${ROOTFS_DIR}/boot/firmware"

install -d "${ROOTFS_DIR}/home/quantar/quantarbridge"
rsync -a --delete \
  --exclude '.git/' \
  --exclude 'build/' \
  --exclude 'dist/' \
  --exclude 'runtime/' \
  "${REPOSITORY_ROOT}/" "${ROOTFS_DIR}/home/quantar/quantarbridge/"

mount --bind /etc/resolv.conf "${ROOTFS_DIR}/etc/resolv.conf"
mount --bind /dev "${ROOTFS_DIR}/dev"
mount --bind /dev/pts "${ROOTFS_DIR}/dev/pts"
mount --types proc proc "${ROOTFS_DIR}/proc"
mount --types sysfs sys "${ROOTFS_DIR}/sys"
mount --types tmpfs tmpfs "${ROOTFS_DIR}/run"

chroot "${ROOTFS_DIR}" /bin/bash /home/quantar/quantarbridge/image/install-in-rootfs.sh

file "${ROOTFS_DIR}/home/quantar/quantarbridge/build/quantarbridge" | tee \
  "${OUTPUT_DIR}/ARM64-FILE-CHECK.txt"
file "${ROOTFS_DIR}/home/quantar/src/dvmhost/build/dvmhost" | tee -a \
  "${OUTPUT_DIR}/ARM64-FILE-CHECK.txt"
file "${ROOTFS_DIR}/home/quantar/src/tetra-codec/build/libtetra-codec.so" | tee -a \
  "${OUTPUT_DIR}/ARM64-FILE-CHECK.txt"
grep -q 'ARM aarch64' "${OUTPUT_DIR}/ARM64-FILE-CHECK.txt"

unmount_image
e2fsck -f -y "${LOOP_DEVICE}p2"
zerofree "${LOOP_DEVICE}p2"
losetup -d "${LOOP_DEVICE}"
LOOP_DEVICE=""
trap - EXIT

COMMIT="${GITHUB_SHA:-unknown}"
SHORT_COMMIT="${COMMIT:0:12}"
IMAGE_VERSION="${SHORT_COMMIT}"
if [[ "${GITHUB_REF_TYPE:-}" == "tag" && -n "${GITHUB_REF_NAME:-}" ]]; then
  IMAGE_VERSION="${GITHUB_REF_NAME}"
fi
FINAL_IMAGE="${OUTPUT_DIR}/quantarbridge-rpios-trixie-arm64-${IMAGE_VERSION}.img"
mv "${IMAGE_PATH}" "${FINAL_IMAGE}"
xz --threads=0 --compress --keep --force "${FINAL_IMAGE}"
rm -f "${FINAL_IMAGE}"
sha256sum "${FINAL_IMAGE}.xz" > "${FINAL_IMAGE}.xz.sha256"

cat > "${OUTPUT_DIR}/BUILD-INFO.txt" <<EOF
QuantarBridge Raspberry Pi image
Architecture: ARM64 / aarch64
Base: Raspberry Pi OS Lite 64-bit, Debian 13 (trixie), 2026-06-18
Base SHA256: ${BASE_IMAGE_SHA256}
QuantarBridge commit: ${COMMIT}
Release version: ${GITHUB_REF_NAME:-development}
Image: $(basename "${FINAL_IMAGE}.xz")
EOF

ls -lh "${OUTPUT_DIR}"
