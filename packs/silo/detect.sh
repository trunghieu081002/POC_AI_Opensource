#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/silo-lib.sh"

if [ -x "$(sl_bin)" ]; then
  echo "installed=1"
  echo "version=$("$(sl_bin)" --version 2>/dev/null | head -1 || true)"
  echo "active=$(dp_svc_active silo && echo 1 || echo 0)"
else
  echo "installed=0"
fi
