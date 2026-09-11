#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/af-lib.sh"

dp_require_root

PYTHON="$(dp_find_python 3 8 3 12 || true)"
if [ "$DP_DRY_RUN" != "1" ]; then
  [ -n "$PYTHON" ] || dp_fail "no python 3.8-3.12 on PATH (python-modern should have provided one)"
fi

dp_run "$PYTHON" -m venv "$(af_venv)"
dp_run "$(af_venv)/bin/pip" install --quiet --upgrade pip wheel
dp_run chown -R airflow:airflow "$(af_venv)"

dp_ok "venv created at $(af_venv) using ${PYTHON}"
