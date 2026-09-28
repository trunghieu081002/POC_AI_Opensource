#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

# S3 bucket names allow lowercase letters, digits and hyphens only - DP_TEST_NS
# (dpagent_selftest_<run_id>) has underscores, so this cannot reuse it as-is
# the way the postgres suite reuses it as a database name directly.
BUCKET="$(echo "${DP_TEST_NS:?}" | tr '_' '-')"

export S3_HOST=127.0.0.1
export S3_PORT="$(dp_param port 9000)"
export S3_ACCESS_KEY="$(dp_param root_user silo_admin)"
export S3_SECRET_KEY="$(dp_param_required root_password)"

# Drop a leftover from an interrupted previous run before creating it, so the
# suite starts from a known state rather than inheriting one.
python3 "${DP_SUITE_ROOT:?}/s3sig.py" DELETE "$BUCKET" >/dev/null 2>&1 || true

if ! python3 "${DP_SUITE_ROOT:?}/s3sig.py" PUT "$BUCKET" >/dev/null; then
  dp_fail "could not create test bucket ${BUCKET}"
fi

dp_ok "test bucket ${BUCKET} created"
