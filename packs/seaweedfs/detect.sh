#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
if [ -x "${INSTALL_DIR}/bin/weed" ]; then
  echo "installed=1"
  echo "version=$("${INSTALL_DIR}/bin/weed" version 2>/dev/null | head -1)"
else
  echo "installed=0"
fi
