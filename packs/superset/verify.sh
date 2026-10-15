#!/usr/bin/env bash
set -euo pipefail
source "${DP_LIB:?}"
dp_svc_active superset
/opt/superset/.venv/bin/pip check
port="$(dp_param webserver_port 8088)"
for attempt in $(seq 1 60); do
 if curl --fail --silent "http://127.0.0.1:${port}/health" >/dev/null; then dp_ok "Superset healthy"; exit 0; fi
 sleep 2
done
dp_fail "Superset health endpoint did not become ready"
