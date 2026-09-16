#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

INSTALL_DIR="$(dp_param install_dir /opt/dlt)"
DLT_BIN="${INSTALL_DIR}/.venv/bin/dlt"
failed=0

if [ -x "$DLT_BIN" ]; then
  dp_ok "dlt binary present at ${DLT_BIN}"
else
  dp_err "dlt binary missing at ${DLT_BIN}"
  failed=1
fi

if [ -x /usr/local/bin/dlt ]; then
  dp_ok "/usr/local/bin/dlt wrapper present"
else
  dp_err "/usr/local/bin/dlt wrapper missing"
  failed=1
fi

if [ "$failed" -eq 0 ]; then
  # Also proves the postgres destination and sql_database source extras
  # actually import - a package can be "installed" (the binary exists) while
  # missing an extra whose absence only surfaces the first time a pipeline
  # tries to use it.
  IMPORT_CHECK="$("${INSTALL_DIR}/.venv/bin/python" -c '
import dlt
import dlt.destinations.impl.postgres
import dlt.sources.sql_database
print("ok")
' 2>&1)"
  if [ "$IMPORT_CHECK" = "ok" ]; then
    dp_ok "postgres destination and sql_database source both import cleanly"
  else
    dp_err "dlt's postgres/sql_database extras do not import: ${IMPORT_CHECK}"
    failed=1
  fi
fi

[ "$failed" -eq 0 ] || dp_fail "dlt verify failed"
dp_ok "dlt verified (liveness only - dpagent's own acceptance suite proves an actual extract+load)"
