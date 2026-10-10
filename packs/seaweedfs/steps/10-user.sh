#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
DATA_DIR="$(dp_param data_dir /var/lib/seaweedfs)"

dp_ensure_user seaweedfs "" /sbin/nologin
dp_run mkdir -p "${INSTALL_DIR}/bin" "$DATA_DIR"
dp_run chown seaweedfs:seaweedfs "$DATA_DIR"
dp_run chmod 0750 "$DATA_DIR"
dp_ok "seaweedfs user and directories ready"
