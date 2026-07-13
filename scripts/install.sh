#!/usr/bin/env bash

set -euo pipefail

DVMHOST_COMMIT="01979084df9fc6a5737fac9efb213430268377c9"
SERVICE_USER="quantar"
INSTALL_DIR="/home/${SERVICE_USER}/quantarbridge"
RUNTIME_DIR="/home/${SERVICE_USER}/quantar-runtime"
DVMHOST_DIR="/home/${SERVICE_USER}/src/dvmhost"
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BM_ID=""
BM_CALLSIGN=""
BM_MASTER=""
RX_FREQUENCY=""
TX_FREQUENCY=""
SERIAL_PORT="/dev/ttyUSB0"
ARS_SERVER_IP="10.0.0.2"
ARS_PEER_IP="10.0.0.1"
P25_NAC="293"
P25_NETWORK_ID="BB800"
P25_SYSTEM_ID="001"
LATITUDE="0.0"
LONGITUDE="0.0"
HEIGHT="0"
POWER="25"
LOCATION="Quantar Bridge"
DASHBOARD_LISTEN="127.0.0.1"
DASHBOARD_PORT="8088"
FORCE=0
ENABLE_WATCHDOGS=0

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/install.sh [options]

Required:
  --bm-id ID                 Six-digit BrandMeister repeater ID
  --bm-callsign CALLSIGN     Callsign assigned to the repeater
  --bm-master HOSTNAME       BrandMeister master hostname
  --rx-frequency HZ          Repeater receive frequency in Hz
  --tx-frequency HZ          Repeater transmit frequency in Hz

Optional:
  --serial-port PATH         Quantar V.24 interface (default: /dev/ttyUSB0)
  --ars-server-ip ADDRESS    APX ARS/TMS server address (default: 10.0.0.2)
  --ars-peer-ip ADDRESS      Compatibility peer address (default: 10.0.0.1)
  --p25-nac HEX              Three-digit P25 NAC (default: 293)
  --p25-network-id HEX       Five-digit P25 network ID (default: BB800)
  --p25-system-id HEX        Three-digit P25 system ID (default: 001)
  --latitude DECIMAL         Repeater latitude (default: 0.0)
  --longitude DECIMAL        Repeater longitude (default: 0.0)
  --height METRES            Antenna height (default: 0)
  --power WATTS              Transmitter power (default: 25)
  --location TEXT            Public site description (default: Quantar Bridge)
  --dashboard-listen IP      Dashboard bind address (default: 127.0.0.1)
  --dashboard-port PORT      Dashboard TCP port (default: 8088)
  --enable-watchdogs         Enable recovery timers after installation
  --force                    Replace an existing managed installation
  --help                     Show this help
EOF
}

require_value() {
  if [[ $# -lt 2 || -z "$2" ]]; then
    echo "Missing value for $1" >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bm-id) require_value "$@"; BM_ID="$2"; shift 2 ;;
    --bm-callsign) require_value "$@"; BM_CALLSIGN="$2"; shift 2 ;;
    --bm-master) require_value "$@"; BM_MASTER="$2"; shift 2 ;;
    --rx-frequency) require_value "$@"; RX_FREQUENCY="$2"; shift 2 ;;
    --tx-frequency) require_value "$@"; TX_FREQUENCY="$2"; shift 2 ;;
    --serial-port) require_value "$@"; SERIAL_PORT="$2"; shift 2 ;;
    --ars-server-ip) require_value "$@"; ARS_SERVER_IP="$2"; shift 2 ;;
    --ars-peer-ip) require_value "$@"; ARS_PEER_IP="$2"; shift 2 ;;
    --p25-nac) require_value "$@"; P25_NAC="$2"; shift 2 ;;
    --p25-network-id) require_value "$@"; P25_NETWORK_ID="$2"; shift 2 ;;
    --p25-system-id) require_value "$@"; P25_SYSTEM_ID="$2"; shift 2 ;;
    --latitude) require_value "$@"; LATITUDE="$2"; shift 2 ;;
    --longitude) require_value "$@"; LONGITUDE="$2"; shift 2 ;;
    --height) require_value "$@"; HEIGHT="$2"; shift 2 ;;
    --power) require_value "$@"; POWER="$2"; shift 2 ;;
    --location) require_value "$@"; LOCATION="$2"; shift 2 ;;
    --dashboard-listen) require_value "$@"; DASHBOARD_LISTEN="$2"; shift 2 ;;
    --dashboard-port) require_value "$@"; DASHBOARD_PORT="$2"; shift 2 ;;
    --enable-watchdogs) ENABLE_WATCHDOGS=1; shift ;;
    --force) FORCE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root with sudo." >&2
  exit 1
fi

for value in BM_ID BM_CALLSIGN BM_MASTER RX_FREQUENCY TX_FREQUENCY; do
  if [[ -z "${!value}" ]]; then
    echo "Missing required option for ${value}." >&2
    usage >&2
    exit 2
  fi
done

read -r -s -p "BrandMeister device password: " BM_PASSWORD
echo
read -r -s -p "Dashboard password (minimum 12 characters): " DASHBOARD_PASSWORD
echo
read -r -s -p "Repeat dashboard password: " DASHBOARD_PASSWORD_CONFIRM
echo
if [[ "${DASHBOARD_PASSWORD}" != "${DASHBOARD_PASSWORD_CONFIRM}" ]]; then
  echo "Dashboard passwords do not match." >&2
  exit 2
fi
read -r -s -p "BrandMeister API key (optional, press Enter to skip): " BM_API_KEY
echo

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  build-essential \
  ca-certificates \
  cmake \
  git \
  libasio-dev \
  libncurses-dev \
  libssl-dev \
  libyaml-cpp-dev \
  mosquitto-clients \
  pkg-config \
  python3 \
  python3-requests \
  python3-websocket \
  python3-yaml \
  rsync \
  sudo

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${SERVICE_USER}"
fi
usermod -aG dialout "${SERVICE_USER}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "/home/${SERVICE_USER}/src"

if [[ "$(realpath "${REPOSITORY_ROOT}")" != "${INSTALL_DIR}" ]]; then
  if [[ -e "${INSTALL_DIR}" && "${FORCE}" -ne 1 ]]; then
    echo "${INSTALL_DIR} already exists; back it up and rerun with --force." >&2
    exit 1
  fi
  install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${INSTALL_DIR}"
  rsync -a --delete \
    --exclude '.git/' \
    --exclude 'build/' \
    --exclude 'runtime/' \
    "${REPOSITORY_ROOT}/" "${INSTALL_DIR}/"
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

CONFIGURE_ARGS=(
  --runtime-dir "${RUNTIME_DIR}"
  --install-dir "${INSTALL_DIR}"
  --bm-id "${BM_ID}"
  --bm-callsign "${BM_CALLSIGN}"
  --bm-master "${BM_MASTER}"
  --rx-frequency "${RX_FREQUENCY}"
  --tx-frequency "${TX_FREQUENCY}"
  --serial-port "${SERIAL_PORT}"
  --ars-server-ip "${ARS_SERVER_IP}"
  --ars-peer-ip "${ARS_PEER_IP}"
  --p25-nac "${P25_NAC}"
  --p25-network-id "${P25_NETWORK_ID}"
  --p25-system-id "${P25_SYSTEM_ID}"
  --latitude "${LATITUDE}"
  --longitude "${LONGITUDE}"
  --height "${HEIGHT}"
  --power "${POWER}"
  --location "${LOCATION}"
  --dashboard-listen "${DASHBOARD_LISTEN}"
  --dashboard-port "${DASHBOARD_PORT}"
  --bm-password-stdin
)
if [[ "${FORCE}" -eq 1 ]]; then
  CONFIGURE_ARGS+=(--force)
fi
printf '%s\n' "${BM_PASSWORD}" | runuser -u "${SERVICE_USER}" -- \
  python3 "${INSTALL_DIR}/scripts/configure.py" "${CONFIGURE_ARGS[@]}"

if [[ -e "${DVMHOST_DIR}" && "${FORCE}" -ne 1 ]]; then
  echo "${DVMHOST_DIR} already exists; rerun with --force only after backing it up." >&2
  exit 1
fi
if [[ -e "${DVMHOST_DIR}" ]]; then
  rm -rf --one-file-system "${DVMHOST_DIR}"
fi
runuser -u "${SERVICE_USER}" -- git clone https://github.com/DVMProject/dvmhost.git "${DVMHOST_DIR}"
runuser -u "${SERVICE_USER}" -- git -C "${DVMHOST_DIR}" checkout --detach "${DVMHOST_COMMIT}"
runuser -u "${SERVICE_USER}" -- git -C "${DVMHOST_DIR}" apply --check "${INSTALL_DIR}/patches/dvmhost.patch"
runuser -u "${SERVICE_USER}" -- git -C "${DVMHOST_DIR}" apply "${INSTALL_DIR}/patches/dvmhost.patch"

runuser -u "${SERVICE_USER}" -- cmake \
  -S "${DVMHOST_DIR}" \
  -B "${DVMHOST_DIR}/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_TUI_SUPPORT=0 \
  -DENABLE_SETUP_TUI=0
runuser -u "${SERVICE_USER}" -- cmake --build "${DVMHOST_DIR}/build" --parallel "$(nproc)"

runuser -u "${SERVICE_USER}" -- cmake \
  -S "${INSTALL_DIR}" \
  -B "${INSTALL_DIR}/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DDVMHOST_SOURCE_DIR="${DVMHOST_DIR}" \
  -DDVMHOST_COMMON_LIBRARY="${DVMHOST_DIR}/build/libcommon.a"
runuser -u "${SERVICE_USER}" -- cmake --build "${INSTALL_DIR}/build" --parallel "$(nproc)"
runuser -u "${SERVICE_USER}" -- ctest --test-dir "${INSTALL_DIR}/build" --output-on-failure

AUTH_ARGS=(
  --config "${RUNTIME_DIR}/quantar-dashboard.json"
  --init-auth
  --username admin
  --password-stdin
)
if [[ "${FORCE}" -eq 1 ]]; then
  AUTH_ARGS+=(--force)
fi
printf '%s\n' "${DASHBOARD_PASSWORD}" | runuser -u "${SERVICE_USER}" -- \
  python3 "${INSTALL_DIR}/dashboard/app.py" "${AUTH_ARGS[@]}"

if [[ -n "${BM_API_KEY}" ]]; then
  printf '%s\n' "${BM_API_KEY}" > "${RUNTIME_DIR}/bm_api.key"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${RUNTIME_DIR}/bm_api.key"
  chmod 0600 "${RUNTIME_DIR}/bm_api.key"
fi

install -m 0644 "${INSTALL_DIR}"/deploy/*.service /etc/systemd/system/
install -m 0644 "${INSTALL_DIR}"/deploy/*.timer /etc/systemd/system/
install -m 0644 "${INSTALL_DIR}"/deploy/*.path /etc/systemd/system/

cat > /etc/sudoers.d/quantarbridge-dashboard <<'EOF'
quantar ALL=(root) NOPASSWD: /usr/bin/systemctl restart dvmhost.service
quantar ALL=(root) NOPASSWD: /usr/bin/systemctl restart dvmfne.service
quantar ALL=(root) NOPASSWD: /usr/bin/systemctl restart dvmbridge-p25-to-dmr.service
quantar ALL=(root) NOPASSWD: /usr/bin/systemctl restart dvmbridge-dmr-to-p25.service
quantar ALL=(root) NOPASSWD: /usr/bin/systemctl restart quantarbridge.service
quantar ALL=(root) NOPASSWD: /usr/bin/systemctl restart tetrapack-brew-bridge.service
EOF
chmod 0440 /etc/sudoers.d/quantarbridge-dashboard
visudo -cf /etc/sudoers.d/quantarbridge-dashboard

systemctl daemon-reload
CORE_UNITS=(
  dvmfne.service
  dvmhost.service
  dvmbridge-p25-to-dmr.service
  dvmbridge-dmr-to-p25.service
  quantarbridge.service
  tetrapack-brew-bridge.service
  quantar-dashboard.service
)
systemctl enable --now "${CORE_UNITS[@]}"

if [[ -n "${BM_API_KEY}" ]]; then
  systemctl enable --now \
    bm-static-sync.timer \
    bm-static-guard.timer \
    ensure-static-tg.timer \
    quantar-static-recover.path
fi
if [[ "${ENABLE_WATCHDOGS}" -eq 1 ]]; then
  systemctl enable --now \
    dvmhost-recover.timer \
    dmr-to-p25-recover.timer \
    bm-to-p25-recover.timer
fi

unset BM_PASSWORD DASHBOARD_PASSWORD DASHBOARD_PASSWORD_CONFIRM BM_API_KEY
echo "Installation complete. Dashboard: http://${DASHBOARD_LISTEN}:${DASHBOARD_PORT}/"
