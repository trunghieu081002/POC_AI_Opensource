#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/postgres/pg-lib.sh"

NS="${DP_TEST_NS:?}"
PORT="$(dp_param port 5432)"

# Drop a leftover from an interrupted previous run before creating it, so the
# suite starts from a known state rather than inheriting one.
if [ "$(pg_query "SELECT 1 FROM pg_database WHERE datname = '${NS}';")" = "1" ]; then
  dp_warn "leftover ${NS} from a previous run; dropping it"
  dp_run runuser -u postgres -- psql -q -p "$PORT" -d postgres \
    -c "DROP DATABASE ${NS};"
fi

dp_run runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q -p "$PORT" -d postgres \
  -c "CREATE DATABASE ${NS};"

dp_ok "test database ${NS} created"
