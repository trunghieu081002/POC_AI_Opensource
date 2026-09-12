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

# dbt is not a declared dependency (this pack works standalone against any
# Postgres), so this is best-effort: a no-op if dbt was never installed here.
# Orchestrating dbt is this stack's actual point, and dbt's profile - which
# holds the warehouse password - is 0640 root:dbtread, unreadable by anyone
# else. If dbt gets installed *after* airflow, re-run (or --force) this pack
# once dbt is present - this step is checkpointed like any other and will not
# retroactively join a group that did not exist yet the first time it ran.
dp_join_group airflow dbtread

dp_ok "airflow user and AIRFLOW_HOME (${HOME_DIR}) ready"
