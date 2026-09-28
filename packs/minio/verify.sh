#!/usr/bin/env bash
# Liveness only: service active, both ports listening, the real S3 health
# endpoint answers - not just "the process exists".
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/mn-lib.sh"

PORT="$(dp_param port 9000)"
CONSOLE_PORT="$(dp_param console_port 9001)"
failed=0

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then dp_ok "$label"; else dp_err "$label"; failed=1; fi
}

check "minio is active"                dp_svc_active minio
check "S3 API port ${PORT} listening"  dp_port_busy "$PORT"
check "console port ${CONSOLE_PORT} listening" dp_port_busy "$CONSOLE_PORT"

# --noproxy '*': see the airflow/postgres packs' own verify.sh for why -
# a corporate proxy otherwise intercepts this loopback check.
if curl -fsS --max-time 15 --noproxy '*' "http://127.0.0.1:${PORT}/minio/health/live" \
    >/dev/null 2>&1; then
  dp_ok "S3 API health endpoint answers"
else
  dp_err "S3 API health endpoint does not answer on ${PORT}"
  failed=1
fi

[ "$failed" -eq 0 ] || dp_fail "minio verify failed"
dp_ok "minio verified"
