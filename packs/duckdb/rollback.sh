#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/duckdb)"

case "$INSTALL_DIR" in
  /opt/*)
    if [ -d "$INSTALL_DIR" ]; then
      dp_run rm -rf "$INSTALL_DIR"
    fi ;;
  *) dp_warn "refusing to remove unexpected install_dir: ${INSTALL_DIR}" ;;
esac

dp_ok "duckdb rolled back"
