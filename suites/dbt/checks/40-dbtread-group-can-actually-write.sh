#!/usr/bin/env bash
# The whole point of the dbtread group (packs/dbt/steps/30-project.sh,
# packs/airflow/steps/10-user.sh) is that airflow can orchestrate dbt - but
# setgid on the project tree only fixes group *ownership* on new entries, not
# their permission bits. Whoever runs dbt first (this suite's own
# 10-connection-works.sh check, running as root) leaves logs/dbt.log and
# everything under target/ at the usual 644 - group read-only. A *different*
# dbtread member (airflow, in real use) then hits a raw PermissionError with
# nothing in the traceback suggesting a permissions problem. `nobody` (uid
# 65534, present on every Linux, already the stand-in used in the shared
# library's own dp_as_user tests) plays the "different group member" role
# here so this check needs no dedicated pack installed to prove it.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

PROJECT_DIR="$(dp_param project_dir /opt/dbt/project)"
TESTFILE="${PROJECT_DIR}/logs/.dpagent-selftest-write-$$"

cleanup() {
  gpasswd -d nobody dbtread >/dev/null 2>&1 || true
  rm -f "$TESTFILE"
}
trap cleanup EXIT

dp_run usermod -aG dbtread nobody

if dp_as_user nobody -- touch "$TESTFILE" 2>/dev/null; then
  dp_ok "a dbtread member other than whoever ran dbt first can still write here"
else
  dp_fail "nobody (added to dbtread for this check only) could not write to \
${PROJECT_DIR}/logs - the group-sharing grant does not survive a second \
user, which is exactly what airflow orchestrating dbt needs"
fi
