#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/mn-lib.sh"

dp_require_root
INSTALL_DIR="$(mn_install_dir)"
DATA_DIR="$(mn_data_dir)"

dp_ensure_user minio "$INSTALL_DIR" /sbin/nologin
dp_run mkdir -p "$INSTALL_DIR/bin" "$DATA_DIR"
dp_run chown -R minio:minio "$INSTALL_DIR" "$DATA_DIR"

dp_ok "minio user, ${INSTALL_DIR} and ${DATA_DIR} ready"
