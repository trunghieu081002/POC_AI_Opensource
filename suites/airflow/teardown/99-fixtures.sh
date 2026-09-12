#!/usr/bin/env bash
# Runs whatever happened above, including after a crash mid-suite. Every
# removal is guarded so a partial setup tears down cleanly.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/airflow/af-lib.sh"

NS="${DP_TEST_NS:?}"
DAGS_DIR="$(af_home)/dags"

for dag_id in "${NS}_ok" "${NS}_fail"; do
  af_run dags delete "$dag_id" --yes >/dev/null 2>&1 || true
done

dp_run rm -f "${DAGS_DIR}/${NS}_ok.py" "${DAGS_DIR}/${NS}_fail.py"
dp_run rm -rf "${DAGS_DIR}/__pycache__"

dp_ok "throwaway DAGs and their run history removed"
