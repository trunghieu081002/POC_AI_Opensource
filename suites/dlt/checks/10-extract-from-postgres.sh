#!/usr/bin/env bash
# The odoo_postgres connector's whole mechanism: dlt's sql_database source
# reading a real table and landing it, as-received, into a real destination
# dataset. Proves the postgres destination and sql_database source actually
# work together, not just that each imports (verify.sh already checked that).
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
PYTHON="${INSTALL_DIR}/.venv/bin/python"
DATASET="${NS}_landing_pg"

[ -x "$PYTHON" ] || dp_fail "dlt venv python missing at ${PYTHON}"

OUTPUT="$(DP_URL="postgresql://${DB_USER}:${DB_PASSWORD}@${HOST}:${PORT}/${DATABASE}" \
  DP_NS="$NS" DP_DATASET="$DATASET" DP_STATE_DIR="/tmp/${NS}_dlt_state" \
  "$PYTHON" - <<'PYEOF' 2>&1
import os
import dlt
from dlt.sources.sql_database import sql_database

url = os.environ["DP_URL"]
ns = os.environ["DP_NS"]

source = sql_database(credentials=url, schema=f"{ns}_src", table_names=["items"])
pipeline = dlt.pipeline(
    pipeline_name=f"{ns}_pg_selftest",
    destination=dlt.destinations.postgres(credentials=url),
    dataset_name=os.environ["DP_DATASET"],
    pipelines_dir=os.environ["DP_STATE_DIR"],
)
info = pipeline.run(source)
print(info)
PYEOF
)" || dp_fail "dlt extract from postgres failed:
${OUTPUT}"

printf '%s\n' "$OUTPUT" | grep -qi "LOADED" || dp_fail "dlt did not report a loaded package:
${OUTPUT}"

ROWS="$(PGPASSWORD="$DB_PASSWORD" psql -h "$HOST" -p "$PORT" -U "$DB_USER" -d "$DATABASE" \
  -tAc "select count(*) from ${DATASET}.items")"
[ "$ROWS" = "2" ] || dp_fail "expected 2 rows landed in ${DATASET}.items, found ${ROWS}"

NAMES="$(PGPASSWORD="$DB_PASSWORD" psql -h "$HOST" -p "$PORT" -U "$DB_USER" -d "$DATABASE" \
  -tAc "select string_agg(name, ',' order by id) from ${DATASET}.items")"
[ "$NAMES" = "widget,gadget" ] || dp_fail "landed row content does not match the source: got '${NAMES}'"

dp_ok "dlt extracted 2 rows from postgres and landed them correctly in ${DATASET}.items"
