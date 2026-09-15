#!/usr/bin/env bash
# Exit 0 only if the thing genuinely works.
#
# `systemctl is-active` is not enough — a process can be up and broken. Query the
# port, run the client, hit the health endpoint. This script is what `dpagent
# verify` runs, and what promotion from draft to stable depends on.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

PORT="$(dp_param port 8080)"
failed=0

check() {  # check <label> <command...>
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then dp_ok "$label"; else dp_err "$label"; failed=1; fi
}

check "service is active"                dp_svc_active CHANGEME
check "listening on ${PORT}"             dp_port_busy "$PORT"
# --noproxy '*': curl follows http_proxy/https_proxy for every request,
# loopback included, unless told not to - a host behind a corporate proxy
# would otherwise route this local check through it.
check "health endpoint answers"          curl -fsS --max-time 10 --noproxy '*' "http://127.0.0.1:${PORT}/health"

[ "$failed" -eq 0 ] || dp_fail "CHANGEME verify failed"
dp_ok "CHANGEME verified"
