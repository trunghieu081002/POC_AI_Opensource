#!/usr/bin/env bash
# Liveness only: both units active, the webserver answers HTTP, and the CLI can
# reach the metadata database. Whether a DAG actually runs is the acceptance
# suite's job, not this one's.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/af-lib.sh"

PORT="$(dp_param webserver_port 8090)"
failed=0

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then dp_ok "$label"; else dp_err "$label"; failed=1; fi
}

check "airflow-scheduler is active"  dp_svc_active airflow-scheduler
check "airflow-webserver is active"  dp_svc_active airflow-webserver
check "port ${PORT} is listening"    dp_port_busy "$PORT"

# --noproxy '*': a host behind a real corporate proxy otherwise routes this
# loopback check through it, failing with a proxy/connection error that
# looks like the webserver itself is down.
if curl -fsS --max-time 15 --noproxy '*' -o /dev/null "http://127.0.0.1:${PORT}/health" 2>/dev/null; then
  dp_ok "webserver /health responds"
elif curl -fsS --max-time 15 --noproxy '*' -o /dev/null "http://127.0.0.1:${PORT}/" 2>/dev/null; then
  dp_ok "webserver root responds (no /health endpoint on this version)"
else
  dp_err "webserver does not answer HTTP on ${PORT}"
  failed=1
fi

if af_query db check >/dev/null 2>&1; then
  dp_ok "airflow db check succeeds"
else
  dp_err "airflow db check failed - the CLI cannot reach the metadata database"
  failed=1
fi

[ "$failed" -eq 0 ] || dp_fail "airflow verify failed"
dp_ok "airflow verified"
