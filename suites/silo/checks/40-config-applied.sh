#!/usr/bin/env bash
# The running server, not the env file. A setting can be written to
# /etc/default/silo and never actually picked up (a restart that did not
# happen, a systemd unit reading a different EnvironmentFile) and nothing
# would complain - this catches that by asking the ports the params say it
# should be on, not by re-reading the file this pack itself wrote.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

PORT="$(dp_param port 9000)"
CONSOLE_PORT="$(dp_param console_port 9001)"

dp_port_busy "$PORT" || dp_fail "nothing is listening on the configured port ${PORT}"
dp_port_busy "$CONSOLE_PORT" || dp_fail "nothing is listening on the configured console_port ${CONSOLE_PORT}"

if ! curl -fsS --max-time 15 --noproxy '*' \
     "http://127.0.0.1:${PORT}/minio/health/live" >/dev/null 2>&1; then
  dp_fail "the S3 API health endpoint does not answer on the configured port ${PORT}"
fi

dp_ok "silo is actually serving on port=${PORT} console_port=${CONSOLE_PORT}, not just what /etc/default/silo says"
