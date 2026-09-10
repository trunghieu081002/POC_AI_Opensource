#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

INSTALL_DIR="$(dp_param install_dir /opt/dbt)"
if [ -x "${INSTALL_DIR}/.venv/bin/dbt" ]; then
  echo "installed=1"
  echo "version=$("${INSTALL_DIR}/.venv/bin/dbt" --version 2>/dev/null | head -1)"
else
  echo "installed=0"
fi
