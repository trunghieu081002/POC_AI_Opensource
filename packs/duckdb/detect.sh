#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

INSTALL_DIR="$(dp_param install_dir /opt/duckdb)"
BIN="${INSTALL_DIR}/bin/duckdb"

if [ -x "$BIN" ]; then
  echo "installed=1"
  echo "version=$("$BIN" --version 2>/dev/null | head -1 || true)"
else
  echo "installed=0"
fi
