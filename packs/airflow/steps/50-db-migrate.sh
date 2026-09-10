#!/usr/bin/env bash
# `airflow db migrate` is safe to re-run (alembic no-ops when there is nothing
# to do), but it still opens a DB connection and takes real time, so a marker
# file lets the step guard skip it on an already-migrated install.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/af-lib.sh"

dp_require_root
MARKER="$(af_home)/.dpagent-db-migrated"

af_run db migrate

dp_run touch "$MARKER"
dp_run chown airflow:airflow "$MARKER"

dp_ok "metadata database migrated"
