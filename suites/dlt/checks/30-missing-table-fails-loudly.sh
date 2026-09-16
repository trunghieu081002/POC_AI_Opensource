#!/usr/bin/env bash
# Negative: a pipeline.yaml naming a table that does not exist on the source
# must halt loudly at extract, not silently "succeed" having moved nothing -
# the same discipline docs/layer2.md holds gates to at every later stage.
# Confirmed empirically (not assumed from dlt's docs) that sql_database
# actually raises when asked to reflect a table that is not there.
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

if OUTPUT="$(DP_URL="postgresql://${DB_USER}:${DB_PASSWORD}@${HOST}:${PORT}/${DATABASE}" \
  DP_NS="$NS" DP_STATE_DIR="/tmp/${NS}_dlt_state" \
  "$PYTHON" - <<'PYEOF' 2>&1
import os
from dlt.sources.sql_database import sql_database

ns = os.environ["DP_NS"]
sql_database(credentials=os.environ["DP_URL"], schema=f"{ns}_src",
             table_names=["does_not_exist"])
print("UNEXPECTED SUCCESS")
PYEOF
)"; then
  dp_fail "extracting a nonexistent table did not fail - a typo'd table name \
in a pipeline's source.tables would silently move nothing instead of halting:
${OUTPUT}"
fi

printf '%s\n' "$OUTPUT" | grep -qi "not available\|does not exist\|no such table" \
  || dp_fail "extraction failed, but not for the expected reason:
${OUTPUT}"

dp_ok "a nonexistent source table fails loudly, as it must"
