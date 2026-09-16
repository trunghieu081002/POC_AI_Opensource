#!/usr/bin/env bash
# A throwaway source schema+table (mimics an upstream database dlt would
# extract from, e.g. Odoo) and a throwaway CSV file - both namespaced with
# DP_TEST_NS so a run never collides with a previous one's leftovers or a
# real pipeline's own tables. Nothing here touches destination schemas;
# dlt itself creates those on first load.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
INSTALL_DIR="$(dp_param install_dir /opt/dlt)"
# db_password is `secret: true`, masked before dpagent's own state ever
# stores it - reading it back via dp_param at suite time would get the mask,
# not the real password (see steps/40-selftest-config.sh's own comment for
# how this was found). host/port/database/db_user are not secret and would
# resolve fine either way, but all five come from this one file so nothing
# can drift between what was actually installed and what the suite tests.
# shellcheck source=/dev/null
source "${INSTALL_DIR}/selftest.env"
HOST="$DLT_SELFTEST_HOST"
PORT="$DLT_SELFTEST_PORT"
DATABASE="$DLT_SELFTEST_DATABASE"
DB_USER="$DLT_SELFTEST_DB_USER"
DB_PASSWORD="$DLT_SELFTEST_DB_PASSWORD"

psql_selftest() {
  PGPASSWORD="$DB_PASSWORD" psql -h "$HOST" -p "$PORT" -U "$DB_USER" -d "$DATABASE" \
    -v ON_ERROR_STOP=1 "$@"
}

SRC_SCHEMA="${NS}_src"
if [ "$DP_DRY_RUN" != "1" ] && psql_selftest -tAc \
    "select 1 from information_schema.schemata where schema_name='${SRC_SCHEMA}'" \
    | grep -q 1; then
  dp_warn "leftover ${SRC_SCHEMA} from a previous run; removing it"
  dp_run bash -c "PGPASSWORD='${DB_PASSWORD}' psql -h '${HOST}' -p '${PORT}' -U '${DB_USER}' -d '${DATABASE}' -v ON_ERROR_STOP=1 -c 'DROP SCHEMA IF EXISTS ${SRC_SCHEMA} CASCADE;'"
fi

dp_run bash -c "PGPASSWORD='${DB_PASSWORD}' psql -h '${HOST}' -p '${PORT}' -U '${DB_USER}' -d '${DATABASE}' -v ON_ERROR_STOP=1 <<SQL
CREATE SCHEMA ${SRC_SCHEMA};
CREATE TABLE ${SRC_SCHEMA}.items (id bigint primary key, name text, amount numeric);
INSERT INTO ${SRC_SCHEMA}.items VALUES (1, 'widget', 10.5), (2, 'gadget', 20.0);
SQL"

CSV_PATH="/tmp/${NS}_dlt_selftest.csv"
{
  echo "id,name,amount"
  echo "1,widget,10.5"
  echo "2,gadget,20.0"
} | dp_write "$CSV_PATH" 0644

dp_ok "throwaway source schema ${SRC_SCHEMA} and CSV fixture at ${CSV_PATH} ready"
