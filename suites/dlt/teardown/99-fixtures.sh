#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
INSTALL_DIR="$(dp_param install_dir /opt/dlt)"
# shellcheck source=/dev/null
source "${INSTALL_DIR}/selftest.env"
HOST="$DLT_SELFTEST_HOST"
PORT="$DLT_SELFTEST_PORT"
DATABASE="$DLT_SELFTEST_DATABASE"
DB_USER="$DLT_SELFTEST_DB_USER"
DB_PASSWORD="$DLT_SELFTEST_DB_PASSWORD"

dp_run bash -c "PGPASSWORD='${DB_PASSWORD}' psql -h '${HOST}' -p '${PORT}' -U '${DB_USER}' -d '${DATABASE}' -v ON_ERROR_STOP=1 <<SQL
DROP SCHEMA IF EXISTS ${NS}_src CASCADE;
DROP SCHEMA IF EXISTS ${NS}_landing_pg CASCADE;
DROP SCHEMA IF EXISTS ${NS}_landing_csv CASCADE;
SQL"

dp_run rm -f "/tmp/${NS}_dlt_selftest.csv"
dp_run rm -rf "/tmp/${NS}_dlt_state"

dp_ok "throwaway schemas, CSV fixture, and dlt state removed"
