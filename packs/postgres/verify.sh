#!/usr/bin/env bash
# Health check. Exits 0 only if PostgreSQL genuinely answers queries — a unit
# that is "active" but rejecting connections is a failure, not a pass.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/pg-lib.sh"

VERSION="$(pg_version)"
SERVICE="$(pg_service)"
PORT="$(dp_param port 5432)"
failed=0

check() {  # check <label> <command...>
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    dp_ok "$label"
  else
    dp_err "$label"
    failed=1
  fi
}

check "service ${SERVICE} is active"        dp_svc_active "$SERVICE"
check "something is listening on ${PORT}"   dp_port_busy "$PORT"
check "pg_isready on port ${PORT}"          pg_is_up

server_version="$(pg_query 'SHOW server_version;' || true)"
if [ -n "$server_version" ]; then
  dp_ok "server answers queries: ${server_version}"
  case "$server_version" in
    "${VERSION}."*|"${VERSION}") ;;
    *) dp_warn "running ${server_version} but the pack asked for ${VERSION}" ;;
  esac
else
  dp_err "server did not answer SELECT"
  failed=1
fi

actual_port="$(pg_query 'SHOW port;' || true)"
if [ -n "$actual_port" ] && [ "$actual_port" != "$PORT" ]; then
  dp_err "configured port is ${actual_port}, expected ${PORT}"
  failed=1
fi

# Every requested database must actually be there.
# Compared as a set rather than one query per name: no SQL is built from a
# parameter, so nothing here depends on getting shell quoting right.
present="$(pg_query 'SELECT datname FROM pg_database;' || true)"
while read -r name; do
  [ -n "$name" ] || continue
  if printf '%s\n' "$present" | grep -qxF "$name"; then
    dp_ok "database ${name} exists"
  else
    dp_err "database ${name} is missing"
    failed=1
  fi
done < <(dp_json_list databases)

[ "$failed" -eq 0 ] || dp_fail "postgres verify failed"
dp_ok "postgres ${VERSION} verified"
