#!/usr/bin/env bash
# Runs whatever happened above, including after a crash mid-suite. Every removal
# is guarded so a partial setup tears down cleanly.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/postgres/pg-lib.sh"

NS="${DP_TEST_NS:?}"
PORT="$(dp_param port 5432)"
ROLE="dpagent_selftest_role"

psql_super() { runuser -u postgres -- psql -tAX -p "$PORT" -d postgres "$@"; }

# The suite may hold a connection from a check that died; without this the drop
# fails and the next run inherits a dirty database.
psql_super -c "
  SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE datname = '${NS}' AND pid <> pg_backend_pid();" >/dev/null 2>&1 || true

if [ "$(psql_super -c "SELECT 1 FROM pg_database WHERE datname = '${NS}';" 2>/dev/null)" = "1" ]; then
  dp_run runuser -u postgres -- psql -q -p "$PORT" -d postgres -c "DROP DATABASE ${NS};"
  dp_ok "dropped ${NS}"
fi

if [ "$(psql_super -c "SELECT 1 FROM pg_roles WHERE rolname = '${ROLE}';" 2>/dev/null)" = "1" ]; then
  dp_run runuser -u postgres -- psql -q -p "$PORT" -d postgres -c "DROP ROLE ${ROLE};"
  dp_ok "dropped role ${ROLE}"
fi

dp_ok "fixtures removed"
