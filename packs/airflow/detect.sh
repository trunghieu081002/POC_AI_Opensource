#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/af-lib.sh"

if [ -x "$(af_bin)" ]; then
  echo "installed=1"
  echo "version=$("$(af_bin)" version 2>/dev/null || true)"
  echo "webserver_active=$(dp_svc_active airflow-webserver && echo 1 || echo 0)"
  echo "scheduler_active=$(dp_svc_active airflow-scheduler && echo 1 || echo 0)"
else
  echo "installed=0"
fi
