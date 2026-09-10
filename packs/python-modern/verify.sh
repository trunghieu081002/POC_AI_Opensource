#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

FOUND="$(dp_find_python 3 8 3 13 || true)"
[ -n "$FOUND" ] || dp_fail "no python 3.8-3.13 found on PATH"

"$FOUND" -c 'import venv, ssl, sqlite3' 2>/dev/null || \
  dp_fail "${FOUND} exists but is missing a stdlib module dbt/airflow need (venv/ssl/sqlite3)"

dp_ok "python-modern verified: ${FOUND} ($("$FOUND" -c 'import sys; print("%d.%d"%sys.version_info[:2])'))"
