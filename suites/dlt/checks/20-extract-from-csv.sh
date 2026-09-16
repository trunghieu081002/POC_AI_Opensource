#!/usr/bin/env bash
# The csv connector's whole mechanism: a plain stdlib csv.DictReader fed to
# dlt as a resource. Deliberately not dlt's filesystem/read_csv helper - no
# extra extra needed, and it proves the *simplest* correct path works,
# which is what packs/dlt's own runtime wiring (runtime.py's csv connector)
# actually uses.
#
# Every field lands as text (character varying): CSV has no native types, and
# landing is "as received, nothing cast yet" (docs/layer2.md) - a pipeline
# whose source is CSV declares text columns in its schema_contract gate and
# casts in the raw stage, same as any other source.
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
DATASET="${NS}_landing_csv"
CSV_PATH="/tmp/${NS}_dlt_selftest.csv"

[ -f "$CSV_PATH" ] || dp_fail "fixture CSV missing at ${CSV_PATH} - did setup run?"

OUTPUT="$(DP_URL="postgresql://${DB_USER}:${DB_PASSWORD}@${HOST}:${PORT}/${DATABASE}" \
  DP_DATASET="$DATASET" DP_CSV="$CSV_PATH" DP_STATE_DIR="/tmp/${NS}_dlt_state" \
  DP_PIPELINE_NAME="${NS}_csv_selftest" \
  "$PYTHON" - <<'PYEOF' 2>&1
import csv
import os
import dlt


@dlt.resource(name="items", write_disposition="replace")
def read_csv_rows(path):
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            yield row


pipeline = dlt.pipeline(
    pipeline_name=os.environ["DP_PIPELINE_NAME"],
    destination=dlt.destinations.postgres(credentials=os.environ["DP_URL"]),
    dataset_name=os.environ["DP_DATASET"],
    pipelines_dir=os.environ["DP_STATE_DIR"],
)
info = pipeline.run(read_csv_rows(os.environ["DP_CSV"]))
print(info)
PYEOF
)" || dp_fail "dlt extract from csv failed:
${OUTPUT}"

printf '%s\n' "$OUTPUT" | grep -qi "LOADED" || dp_fail "dlt did not report a loaded package:
${OUTPUT}"

ROWS="$(PGPASSWORD="$DB_PASSWORD" psql -h "$HOST" -p "$PORT" -U "$DB_USER" -d "$DATABASE" \
  -tAc "select count(*) from ${DATASET}.items")"
[ "$ROWS" = "2" ] || dp_fail "expected 2 rows landed in ${DATASET}.items, found ${ROWS}"

NAMES="$(PGPASSWORD="$DB_PASSWORD" psql -h "$HOST" -p "$PORT" -U "$DB_USER" -d "$DATABASE" \
  -tAc "select string_agg(name, ',' order by id) from ${DATASET}.items")"
[ "$NAMES" = "widget,gadget" ] || dp_fail "landed row content does not match the CSV: got '${NAMES}'"

dp_ok "dlt extracted 2 rows from the CSV fixture and landed them correctly in ${DATASET}.items"
