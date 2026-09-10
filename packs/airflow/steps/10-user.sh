#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/af-lib.sh"

dp_require_root
HOME_DIR="$(af_home)"

dp_run mkdir -p "$(af_install_dir)"
dp_ensure_user airflow "$HOME_DIR" /bin/bash
dp_run mkdir -p "$HOME_DIR/dags" "$HOME_DIR/logs" "$HOME_DIR/plugins"
dp_run chown -R airflow:airflow "$(af_install_dir)"

dp_ok "airflow user and AIRFLOW_HOME (${HOME_DIR}) ready"
