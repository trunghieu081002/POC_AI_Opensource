#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

PORT="$(dp_param webserver_port 8090)"

# --noproxy '*': curl follows http_proxy/https_proxy for every request,
# loopback included, unless told not to - a host behind a real corporate
# proxy would otherwise route this local health check through it, failing
# with a proxy/connection error that looks like airflow itself is broken.
BODY="$(curl -fsS --noproxy '*' "http://localhost:${PORT}/health")" \
  || dp_fail "GET /health on port ${PORT} did not respond"

printf '%s\n' "$BODY" | grep -q '"metadatabase".*"status": *"healthy"' \
  || dp_fail "health endpoint responded but metadatabase is not healthy: ${BODY}"

dp_ok "webserver /health reports the metadatabase healthy"
