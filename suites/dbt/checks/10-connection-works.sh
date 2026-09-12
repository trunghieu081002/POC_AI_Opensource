#!/usr/bin/env bash
# The cheapest, most fundamental claim: dbt can actually open a connection to
# the Postgres target named in its own profile. Everything below depends on
# this, so it runs first and is critical.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

INSTALL_DIR="$(dp_param install_dir /opt/dbt)"
PROJECT_DIR="$(dp_param project_dir /opt/dbt/project)"
PROFILES_DIR="$(dirname "$PROJECT_DIR")/profiles"
DBT="${INSTALL_DIR}/.venv/bin/dbt"

[ -x "$DBT" ] || dp_fail "dbt binary missing at ${DBT}"

OUTPUT="$("$DBT" debug --project-dir "$PROJECT_DIR" --profiles-dir "$PROFILES_DIR" 2>&1)" \
  || dp_fail "dbt debug failed:
${OUTPUT}"

printf '%s\n' "$OUTPUT" | grep -q "All checks passed" \
  || dp_fail "dbt debug did not report success:
${OUTPUT}"

dp_ok "dbt debug: connection to the configured target works"
