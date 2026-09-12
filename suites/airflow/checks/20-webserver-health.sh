#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

PORT="$(dp_param webserver_port 8090)"

BODY="$(curl -fsS "http://localhost:${PORT}/health")" \
  || dp_fail "GET /health on port ${PORT} did not respond"

printf '%s\n' "$BODY" | grep -q '"metadatabase".*"status": *"healthy"' \
  || dp_fail "health endpoint responded but metadatabase is not healthy: ${BODY}"

dp_ok "webserver /health reports the metadatabase healthy"
