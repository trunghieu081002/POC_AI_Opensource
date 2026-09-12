#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

for svc in airflow-scheduler airflow-webserver; do
  dp_svc_active "$svc" || dp_fail "$svc is not active"
done

dp_ok "airflow-scheduler and airflow-webserver are active"
