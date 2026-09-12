#!/usr/bin/env bash
# Negative check: bad_model has a deliberate duplicate id and a `unique` test
# on it. If dbt test does not fail here, dbt's testing feature is not actually
# catching data problems - a pack whose "it works" claim is `dbt test` exits 0
# would sail through this on a build that ignores results entirely.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
INSTALL_DIR="$(dp_param install_dir /opt/dbt)"
PROJECT_DIR="$(dp_param project_dir /opt/dbt/project)"
PROFILES_DIR="$(dirname "$PROJECT_DIR")/profiles"
DBT="${INSTALL_DIR}/.venv/bin/dbt"

OUTPUT="$("$DBT" run --project-dir "$PROJECT_DIR" --profiles-dir "$PROFILES_DIR" \
  --select "${NS}.bad_model" 2>&1)" \
  || dp_fail "could not even build bad_model (the fixture itself is broken):
${OUTPUT}"

set +e
OUTPUT="$("$DBT" test --project-dir "$PROJECT_DIR" --profiles-dir "$PROFILES_DIR" \
  --select "${NS}.bad_model" 2>&1)"
RC=$?
set -e

[ "$RC" -ne 0 ] || dp_fail "dbt test exited 0 on a model with a duplicate id and a unique test — bad data is not being caught:
${OUTPUT}"

printf '%s\n' "$OUTPUT" | grep -qi "fail" \
  || dp_fail "dbt test failed (rc=${RC}) but did not report the failure as expected:
${OUTPUT}"

dp_ok "dbt test correctly failed on the deliberate uniqueness violation (rc=${RC})"
